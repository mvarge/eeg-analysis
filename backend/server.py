"""
FastAPI server for the EEG Flanker Analysis tool.

Wraps the parser + pipeline and exposes:
  POST /api/upload                         upload + analyse one LabChart file
  GET  /api/subjects                       list all in-memory results
  DELETE /api/subjects/{id}                drop one from memory
  GET  /api/compare                        aggregate data for group view
  GET  /api/download-csv-trials/{id}       per-trial CSV (results.csv format)
  GET  /api/download-csv-exclusions/{id}   excluded-trials CSV (exclusions.csv)
  GET  /api/download-csv/{id}              per-subject summary (medians)
  GET  /api/download-csv-trials-all        combined per-trial CSV
  GET  /api/download-csv-all               combined summary CSV
  GET  /api/download-csv-batch-summary     batch summary with balance flags
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from parser import parse_labchart, parse_labchart_multi, ParsedEEG
from subject_id import parse_filename as parse_upload_filename
from pipeline import (
    PipelineResult, TrialResult, ChannelSummary,
    BLINK_UV, BLINK_SLOW_HZ, BLINK_K, EMG_BETA, EMG_K, BURST_Z, BURST_IMPACT, COINC_Z,
    HP_HZ, WIN_S, PAD_S, THETA_BAND, BETA_BAND, TOTAL_BAND, FREQ_STEP,
    THETA_CYC, BETA_CYC,
    run_pipeline,
)
from demographics import (
    Demographic, DISPLAY_FIELDS,
    parse_demographics, match_demographics, parse_filename_ids,
)
from behavioural import (
    BehaviouralSession, AlignmentResult,
    parse_behavioural_session, align_block,
)
from merge import select_blocks
from checks import run_subject_checks, checks_to_payload, check_cohort_amplitude
from logging_setup import get_logger, configure_logging

configure_logging()
logger = get_logger(__name__)


app = FastAPI(title="EEG Flanker Analysis")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_results: Dict[str, PipelineResult] = {}
_parsed: Dict[str, "ParsedEEG"] = {}                # keep the ParsedEEG around for validity checks
_behavioural: Dict[str, BehaviouralSession] = {}   # subject_id → parsed behavioural
_alignment: Dict[str, List[AlignmentResult]] = {}  # subject_id → per-block alignment
_demographics: List[Demographic] = []           # loaded from most recent CSV upload
_demographics_source: str | None = None         # filename of the CSV upload, for UI

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


# ============================================================
#  Helpers
# ============================================================
def _safe(x):
    """Replace NaN/Inf with None for JSON."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def _condition_label(cond: str) -> str:
    """Convert internal marker label to display condition."""
    return "congruent" if cond == "con" else "incongruent"


def _demographic_payload(filename: str) -> dict:
    """
    Build the demographics payload for a given filename.

    Always returns a dict with `matched: bool`, `session/participant` parsed
    from the filename (or None), and `fields`, a list of {key, label, value}
    entries in display order. `block_order` maps 1/2 → e.g. '60 Hz'.
    """
    ids = parse_filename_ids(filename)
    payload = {
        "matched": False,
        "session": ids[0] if ids else None,
        "participant": ids[1] if ids else None,
        "fields": [],
        "block_order": {},
        "aborted": False,
        "csv_source": _demographics_source,
    }
    if not _demographics:
        return payload
    demo = match_demographics(filename, _demographics)
    if demo is None:
        return payload
    payload["matched"] = True
    payload["session"] = demo.session
    payload["participant"] = demo.participant
    payload["aborted"] = demo.aborted
    payload["block_order"] = {str(k): v for k, v in demo.block_order.items()}
    payload["fields"] = [
        {"key": key, "label": label, "value": demo.display.get(key, "")}
        for key, _col, label in DISPLAY_FIELDS
    ]
    return payload


def _summary_payload(r: PipelineResult) -> dict:
    """Structured summary for the frontend."""
    return {
        "filename": r.filename,
        "recording_date": r.recording_date,
        "sampling_rate": r.sampling_rate,
        "channel_names": r.channel_names,
        "n_trials": r.n_trials,
        "n_blocks": r.n_blocks,
        "n_congruent": sum(1 for t in r.trials if t.cond == "con"),
        "n_incongruent": sum(1 for t in r.trials if t.cond == "first"),
        "theta": _channel_payload(r.theta_summary),
        "beta":  _channel_payload(r.beta_summary),
        "demographics": _demographic_payload(r.filename),
        # Per-recording adaptive thresholds + contamination metrics (Tasks
        # 4/5/8). These are the values THIS recording used, self-calibrated
        # from its own distributions — distinct from the frozen config below.
        "adaptive": {
            "blink_threshold_uv": _safe(round(getattr(r, "blink_threshold_uv", 0.0), 1)),
            "emg_threshold": _safe(round(getattr(r, "emg_threshold", 0.0), 0)),
            "c3_beta_share": _safe(round(getattr(r, "c3_beta_share", 0.0), 4)),
            "c3_high_share": _safe(round(getattr(r, "c3_high_share", 0.0), 4)),
            "fz_ptp_median": _safe(round(getattr(r, "fz_ptp_median", 0.0), 1)),
        },
        "config": {
            "hp_hz": HP_HZ,
            "window_s": WIN_S,
            "pad_s": PAD_S,
            "theta_band": list(THETA_BAND),
            "beta_band": list(BETA_BAND),
            "total_band": list(TOTAL_BAND),
            "blink_uv": BLINK_UV,
            "blink_slow_hz": BLINK_SLOW_HZ,
            "blink_k": BLINK_K,
            "emg_beta": EMG_BETA,
            "burst_z": BURST_Z,
            "burst_impact": BURST_IMPACT,
            "coinc_z": COINC_Z,
        },
    }


def _channel_payload(s: ChannelSummary) -> dict:
    # When a channel is scope-excluded (Task 7), its power values must not be
    # reported (acceptance: "no beta power reported"). Survival/exclusion
    # bookkeeping is kept so the reader sees why it was dropped.
    excl = s.channel_excluded
    by_block = {}
    for blk, b in (s.by_block or {}).items():
        by_block[str(blk)] = {
            "n_total_con": b["n_total_con"],
            "n_total_inc": b["n_total_inc"],
            "n_surv_con": b["n_surv_con"],
            "n_surv_inc": b["n_surv_inc"],
            "n_exc_con": b["n_exc_con"],
            "n_exc_inc": b["n_exc_inc"],
            "exclusion_pct_con": _safe(round(b["exclusion_pct_con"], 1)),
            "exclusion_pct_inc": _safe(round(b["exclusion_pct_inc"], 1)),
            "abs_median_con": None if excl else _safe(round(b["abs_median_con"], 3)),
            "abs_median_inc": None if excl else _safe(round(b["abs_median_inc"], 3)),
            "rel_median_con": None if excl else _safe(round(b["rel_median_con"], 4)),
            "rel_median_inc": None if excl else _safe(round(b["rel_median_inc"], 4)),
        }
    return {
        "channel": s.channel,
        "band": s.band,
        "surviving": s.surviving,
        "excluded": s.excluded,
        "surviving_by_condition": s.surviving_by_condition,
        "excluded_by_condition": s.excluded_by_condition,
        "exclusion_pct_con": _safe(round(s.exclusion_pct_con, 1)),
        "exclusion_pct_inc": _safe(round(s.exclusion_pct_inc, 1)),
        "balance_flag": s.balance_flag,
        "abs_median_con": None if excl else _safe(round(s.abs_median_con, 3)),
        "abs_median_inc": None if excl else _safe(round(s.abs_median_inc, 3)),
        "rel_median_con": None if excl else _safe(round(s.rel_median_con, 4)),
        "rel_median_inc": None if excl else _safe(round(s.rel_median_inc, 4)),
        "by_block": by_block,
        # Channel-scoped exclusion (Task 7). When True, this derivation/band
        # was invalidated by a contamination finding; power values are nulled
        # so a downstream consumer can't mistake them for valid.
        "channel_excluded": s.channel_excluded,
        "exclusion_code": s.exclusion_code,
        "exclusion_reason": s.exclusion_reason,
    }


def _flanker_context(subject_id: str) -> tuple[dict, List[dict]]:
    """Map EEG trials to canonical flanker task numbers and list missing trials.

    Uses the per-block RT-alignment (``AlignmentResult.matched_pairs``) already
    computed by ``_run_alignment``. Returns:

      * ``flanker_map``: ``{id(TrialResult): task_number}`` for every EEG trial
        that was paired to a behavioural (flanker) row. The task number is the
        canonical 1-160 index derived from the flanker file's ``live_row``.
      * ``missing_trials``: a list of dicts for behavioural (flanker) trials that
        have NO matching EEG trial ("eeg data not found"), in task-number order.

    Empty ``flanker_map`` / ``missing_trials`` when no behavioural data is loaded
    (numbering then falls back to the EEG enumeration on the frontend).
    """
    flanker_map: dict = {}
    missing: List[dict] = []
    if subject_id not in _results or subject_id not in _behavioural:
        return flanker_map, missing

    result = _results[subject_id]
    session = _behavioural[subject_id]
    alignment = _alignment.get(subject_id, [])

    for ar in alignment:
        blk = ar.block
        # Reproduce the exact ordering _run_alignment used so eeg_i lines up.
        eeg_block = [t for t in result.trials if t.block == blk]
        beh_block = [t for t in session.trials if t.block == blk]
        for eeg_i, beh_i in ar.matched_pairs:
            if 0 <= eeg_i < len(eeg_block) and 0 <= beh_i < len(beh_block):
                tn = beh_block[beh_i].task_number
                if tn is not None:
                    flanker_map[id(eeg_block[eeg_i])] = tn
        # Behavioural rows with no EEG match → "missing" (EEG data not found).
        for beh_i in ar.unmatched_beh_indices:
            if 0 <= beh_i < len(beh_block):
                bt = beh_block[beh_i]
                missing.append({
                    "trial": bt.task_number,
                    "btrial": (bt.live_row + 1) if bt.live_row is not None else None,
                    "block": bt.block,
                    "cond": "con" if bt.congruent else "inc",
                    "condition": _condition_label("con" if bt.congruent else "inc"),
                    "rt_ms": round(bt.response_time_ms, 1),
                    "correct": bt.correct,
                    "reason": "eeg data not found",
                })

    missing.sort(key=lambda m: (m["trial"] is None, m["trial"] or 0))
    return flanker_map, missing


def _trial_row(t: TrialResult, flanker_map: Optional[dict] = None) -> dict:
    """Trial payload for the frontend (rounded for JSON size).

    ``flanker_map`` (``{id(TrialResult): task_number}``) supplies the canonical
    flanker task number when the trial was paired to a behavioural row. The EEG
    enumeration (``trial``/``btrial``) is always kept so the analyst can still
    locate the epoch in the raw EEG recording.
    """
    task_number = None
    if flanker_map is not None:
        task_number = flanker_map.get(id(t))
    return {
        "trial": t.trial,
        "btrial": t.btrial,
        "task_number": task_number,
        "block": t.block,
        "cond": t.cond,
        "condition": _condition_label(t.cond),
        "onset": round(t.onset, 4),
        "key": round(t.key, 3),
        "rt_ms": t.rt_ms,
        "fz_ptp": round(t.fz_ptp, 2),
        "theta_fft": round(t.theta_fft, 2),
        "beta_fft": round(t.beta_fft, 2),
        "maxz": round(t.maxz, 3),
        "impact": round(t.impact, 2),
        "coinc": round(t.coinc, 3),
        "blink": t.blink,
        "fz_exclude": t.fz_exclude,
        "c3_exclude": t.c3_exclude,
        "reason": t.reason,
        "theta_abs": round(t.theta_abs, 3),
        "theta_rel": round(t.theta_rel, 4),
        "beta_abs":  round(t.beta_abs, 3),
        "beta_rel":  round(t.beta_rel, 4),
    }


def _spectra_payload(r: PipelineResult) -> dict:
    """Wavelet spectra (averaged across surviving trials) for plotting."""
    # Per-trial spectra rounded to 4 decimals to keep the JSON small.
    def round_matrix(arr):
        return [[round(float(v), 4) for v in row] for row in arr]

    return {
        "freqs": r.spectrum_freqs.tolist(),
        "theta_congruent":   r.theta_spectrum_con.tolist(),
        "theta_incongruent": r.theta_spectrum_inc.tolist(),
        "beta_congruent":    r.beta_spectrum_con.tolist(),
        "beta_incongruent":  r.beta_spectrum_inc.tolist(),
        "theta_excluded":    r.theta_spectrum_excluded.tolist(),
        "beta_excluded":     r.beta_spectrum_excluded.tolist(),
        # Per-trial spectra so the frontend can toggle Avg / All views
        "theta_per_trial":   round_matrix(r.theta_spec_all),
        "beta_per_trial":    round_matrix(r.beta_spec_all),
    }


# ============================================================
#  Refresh-rate comparison (Issues & Changes 4)
# ============================================================
# The reviewer's primary question is whether EEG activity and reaction time
# differ between the 60 Hz and 165 Hz refresh-rate conditions. The three
# measures of interest are:
#   - theta_rel : Fz-Pz theta relative power   (included = fz_exclude == False)
#   - beta_rel  : C3-C4 beta relative power     (included = c3_exclude == False)
#   - rt_ms     : reaction time                 (included = rt_ms > 0)
#
# Per participant we aggregate the included trials in each refresh condition
# with the MEAN (natural for RT and matching the reviewer's "how much does each
# measure change on average" framing), take the (higher − lower) refresh-rate
# difference, then summarise those per-participant differences across the
# sample. Refresh rate is derived from the demographics block_order label, not
# block number, so counterbalanced participants are handled correctly.

_REFRESH_MEASURES = {
    "theta_rel": {
        "label": "Fz–Pz theta relative power",
        "channel": "Fz-Pz",
        "band": "theta (4–8 Hz)",
        "unit": "relative power",
        "exclude_attr": "fz_exclude",
        "value_attr": "theta_rel",
        "decimals": 4,
    },
    "beta_rel": {
        "label": "C3–C4 beta relative power",
        "channel": "C3-C4",
        "band": "beta (13–30 Hz)",
        "unit": "relative power",
        "exclude_attr": "c3_exclude",
        "value_attr": "beta_rel",
        "decimals": 4,
    },
    "rt_ms": {
        "label": "Reaction time",
        "channel": "behavioural",
        "band": None,
        "unit": "ms",
        "exclude_attr": None,          # RT is not tied to a channel
        "value_attr": "rt_ms",
        "decimals": 1,
    },
}


def _hz_sort_key(label: str) -> float:
    """Numeric Hz for ordering refresh-rate labels ('60 Hz' -> 60.0)."""
    m = re.search(r"(\d+(?:\.\d+)?)", label or "")
    return float(m.group(1)) if m else float("inf")


def _block_hz_map(filename: str) -> Dict[int, str]:
    """Block-number -> refresh-rate label from matched demographics.

    Returns {} when demographics are absent/unmatched or block_order is empty,
    in which case the subject cannot be split by refresh rate.
    """
    if not _demographics:
        return {}
    demo = match_demographics(filename, _demographics)
    if demo is None or not demo.block_order:
        return {}
    return dict(demo.block_order)


def _measure_trial_included(t: TrialResult, spec: dict) -> bool:
    if spec["value_attr"] == "rt_ms":
        return t.rt_ms is not None and t.rt_ms > 0
    return not bool(getattr(t, spec["exclude_attr"]))


def _group_stats(diffs: List[float]) -> dict:
    """Mean/median/spread of the per-participant differences (the headline)."""
    arr = np.asarray(diffs, dtype=float)
    n = int(arr.size)
    out = {
        "n": n,
        "mean_diff": None, "median_diff": None,
        "sd_diff": None, "sem_diff": None,
        "ci95_lo": None, "ci95_hi": None,
    }
    if n == 0:
        return out
    mean = float(np.mean(arr))
    out["mean_diff"] = mean
    out["median_diff"] = float(np.median(arr))
    if n >= 2:
        sd = float(np.std(arr, ddof=1))
        sem = sd / math.sqrt(n)
        out["sd_diff"] = sd
        out["sem_diff"] = sem
        try:
            from scipy import stats as _stats
            tcrit = float(_stats.t.ppf(0.975, n - 1))
        except Exception:
            tcrit = 1.96
        out["ci95_lo"] = mean - tcrit * sem
        out["ci95_hi"] = mean + tcrit * sem
    return out


def _classify_incomplete(entry: dict, r, hz_map: Dict[int, str], measure_key: str) -> None:
    """Explain why a subject that HAS a refresh ordering still lacks two usable
    conditions, distinguishing a benign data gap from a fixable upstream fault.

    Sets ``exclusion_kind`` / ``fixable`` / ``note`` on ``entry``:
      * ``block_unreconciled`` (fixable=True): the ordering names a block the
        recording has NO trials for — an aborted-block / block-selection / merge
        failure the analyst should investigate before dropping the subject.
      * ``condition_all_excluded`` (fixable=False): the block exists but every
        trial in it was rejected (artifact / RT filter) — correctly excluded.
      * ``single_condition_ordering`` (fixable=False): the ordering itself only
        lists one refresh rate, so no 60-vs-165 difference can be formed.
    """
    expected_blocks = set(hz_map)
    expected_labels = set(hz_map.values())
    blocks_present = {t.block for t in r.trials}
    missing_blocks = sorted(b for b in expected_blocks if b not in blocks_present)
    filt = "RT filter" if measure_key == "rt_ms" else "artifact rejection"

    if len(expected_labels) < 2:
        entry["exclusion_kind"] = "single_condition_ordering"
        entry["fixable"] = False
        entry["note"] = (
            "Demographics ordering lists only one refresh condition "
            f"({', '.join(sorted(expected_labels)) or 'none'}) — cannot form a "
            "60-vs-165 difference."
        )
    elif missing_blocks:
        labs = ", ".join(f"block {b} ({hz_map[b]})" for b in missing_blocks)
        entry["exclusion_kind"] = "block_unreconciled"
        entry["fixable"] = True
        entry["note"] = (
            f"Ordering expects {labs} but the recording has no trials there — "
            "blocks not reconciled to the ordering (possible aborted-block / "
            "merge issue). Fixable upstream: investigate before dropping."
        )
    else:
        missing_labels = sorted(set(hz_map.values()) - set(entry["conditions"].keys()))
        entry["exclusion_kind"] = "condition_all_excluded"
        entry["fixable"] = False
        entry["note"] = (
            f"All trials in {', '.join(missing_labels) or 'one condition'} were "
            f"excluded ({filt}) — no data left for that condition."
        )


def _refresh_measure_payload(measure_key: str) -> dict:
    """Build the per-participant + group payload for one measure.

    Per participant we report BOTH the median (primary) and the mean of the
    included trial values in each refresh condition, and the signed high−low
    difference computed each way. The median is primary: RT is right-skewed and,
    for the power measures, a median is robust to a residual sub-threshold
    artifact that would drag a mean. The mean is carried alongside for the export
    and the on-screen inspection toggle. The group headline (see ``group``)
    summarises the per-participant *median* differences with their MEAN Δ — the
    statistic matching the paired t-test — plus median/SD/95% CI as robustness.
    """
    spec = _REFRESH_MEASURES[measure_key]
    dp = spec["decimals"]

    participants = []
    diffs_median = []
    diffs_mean = []
    vals_low = []
    vals_high = []
    rate_labels = set()

    for rid, r in _results.items():
        hz_map = _block_hz_map(r.filename)
        entry = {
            "result_id": rid,
            "filename": r.filename,
            "has_both": False,
            "conditions": {},     # hz_label -> {median, mean, n, trials:[...]}
            "diff": None,         # primary (median-based) high−low
            "diff_median": None,
            "diff_mean": None,
            "note": None,
            "exclusion_kind": None,
            "fixable": False,
        }
        if not hz_map:
            entry["note"] = (
                "No refresh-rate ordering in demographics — subject correctly "
                "excluded from this comparison."
            )
            entry["exclusion_kind"] = "no_ordering"
            participants.append(entry)
            continue

        # Skip a channel-excluded band entirely (its power is invalid).
        if measure_key in ("theta_rel", "beta_rel"):
            chan_sum = r.theta_summary if measure_key == "theta_rel" else r.beta_summary
            if getattr(chan_sum, "channel_excluded", False):
                entry["note"] = (
                    f"{spec['channel']} channel excluded — "
                    f"{chan_sum.exclusion_reason or 'contamination'}. "
                    "Power not reported for this band."
                )
                entry["exclusion_kind"] = "channel_excluded"
                participants.append(entry)
                continue

        # Group included trials by refresh-rate label.
        by_rate: Dict[str, list] = {}
        for t in r.trials:
            label = hz_map.get(t.block)
            if not label:
                continue
            if not _measure_trial_included(t, spec):
                continue
            val = getattr(t, spec["value_attr"])
            by_rate.setdefault(label, []).append((t.trial, t.block, float(val)))

        for label, rows in by_rate.items():
            rate_labels.add(label)
            values = [v for _, _, v in rows]
            entry["conditions"][label] = {
                "median": round(float(np.median(values)), dp),
                "mean": round(float(np.mean(values)), dp),
                "n": len(values),
                "trials": [
                    {"trial": tr, "block": bk, "value": round(v, dp)}
                    for tr, bk, v in rows
                ],
            }

        if len(entry["conditions"]) >= 2:
            entry["has_both"] = True
        else:
            _classify_incomplete(entry, r, hz_map, measure_key)
        participants.append(entry)

    # Resolve the two refresh rates present, ordered low -> high.
    ordered_rates = sorted(rate_labels, key=_hz_sort_key)
    low = ordered_rates[0] if ordered_rates else None
    high = ordered_rates[-1] if len(ordered_rates) >= 2 else None

    # Per-participant differences (high − low), computed both ways. The signed
    # direction (high − low) is identical everywhere — screen, provenance, CSV.
    for entry in participants:
        conds = entry["conditions"]
        if low and high and low in conds and high in conds:
            dmed = round(conds[high]["median"] - conds[low]["median"], dp)
            dmean = round(conds[high]["mean"] - conds[low]["mean"], dp)
            entry["diff_median"] = dmed
            entry["diff_mean"] = dmean
            entry["diff"] = dmed  # primary
            diffs_median.append(dmed)
            diffs_mean.append(dmean)
            vals_low.append(conds[low]["median"])
            vals_high.append(conds[high]["median"])
            entry["has_both"] = True
        else:
            entry["has_both"] = False
            # A subject with the ordering but not both conditions still needs a
            # reason (unless one was already assigned above).
            if entry["exclusion_kind"] is None:
                hz_map = _block_hz_map(entry["filename"])
                if hz_map:
                    _classify_incomplete(entry, _results[entry["result_id"]],
                                         hz_map, measure_key)

    # Primary group summary: over the per-participant MEDIAN differences.
    group = _group_stats(diffs_median)
    if vals_low:
        group["mean_low"] = round(float(np.mean(vals_low)), dp)
        group["mean_high"] = round(float(np.mean(vals_high)), dp)
    for k in ("mean_diff", "median_diff", "sd_diff", "sem_diff", "ci95_lo", "ci95_hi"):
        if group.get(k) is not None:
            group[k] = round(group[k], dp)

    # Robustness: the same summary computed over the per-participant MEAN
    # differences (reported alongside; not the headline).
    group_from_means = _group_stats(diffs_mean)
    for k in ("mean_diff", "median_diff", "sd_diff", "sem_diff", "ci95_lo", "ci95_hi"):
        if group_from_means.get(k) is not None:
            group_from_means[k] = round(group_from_means[k], dp)

    return {
        "key": measure_key,
        "label": spec["label"],
        "channel": spec["channel"],
        "band": spec["band"],
        "unit": spec["unit"],
        "decimals": dp,
        "rate_low": low,
        "rate_high": high,
        "diff_label": (f"{high} − {low}" if low and high else None),
        "participants": participants,
        "group": group,
        "group_from_means": group_from_means,
        "provenance": _measure_provenance(measure_key, low, high),
    }


def _measure_provenance(measure_key: str, low: str, high: str) -> dict:
    """The ordered processing chain from raw signal to the plotted number.

    Real, retained pipeline constants and stage descriptions for this measure.
    Per-participant intermediate values are attached to each participant entry
    (their included trials + condition means + difference); this block gives the
    stage sequence and the config values shared by every participant.
    """
    is_power = measure_key in ("theta_rel", "beta_rel")
    band = "theta" if measure_key == "theta_rel" else "beta"
    num_band = list(THETA_BAND) if band == "theta" else list(BETA_BAND)
    cyc = THETA_CYC if band == "theta" else BETA_CYC
    chan = "Fz-Pz" if measure_key == "theta_rel" else "C3-C4"
    excl_flag = "fz_exclude" if measure_key == "theta_rel" else "c3_exclude"

    stages = []
    stages.append({
        "stage": "1. Acquisition",
        "detail": "LabChart export; bipolar derivation. A 50 Hz acquisition "
                  "low-pass rolls off before the analysis ceiling.",
        "values": {},
        "retained": False,
    })
    if is_power:
        stages.append({
            "stage": "2. High-pass filter",
            "detail": f"{HP_HZ} Hz FIR high-pass on the continuous {chan} signal "
                      "(drift/DC removal). Filtered continuous signal not retained.",
            "values": {"hp_hz": HP_HZ},
            "retained": False,
        })
        stages.append({
            "stage": "3. Epoching",
            "detail": f"Epoch each trial: 0–{int(WIN_S*1000)} ms analysis window "
                      f"+ reach/buffer, with ±{int(PAD_S*1000)} ms wavelet padding. "
                      "Epoch time series not retained.",
            "values": {"window_ms": int(WIN_S * 1000), "pad_ms": int(PAD_S * 1000)},
            "retained": False,
        })
        stages.append({
            "stage": "4. Artifact rejection",
            "detail": ("Adaptive per-recording thresholds. Theta (Fz-Pz) excludes "
                       "blinks (1–7 Hz slow-band p-t-p > median + K·MAD) and "
                       "coincident spikes." if band == "theta" else
                       "Adaptive per-recording thresholds. Beta (C3-C4) excludes "
                       "EMG (beta power > median + K·1.4826·MAD), bursts and "
                       "coincident spikes.") +
                      f" Per-trial flag: {excl_flag}. Included trials keep {excl_flag} == false.",
            "values": {
                "blink_k": BLINK_K, "blink_slow_hz": BLINK_SLOW_HZ,
            } if band == "theta" else {
                "emg_k": EMG_K, "burst_z": BURST_Z, "burst_impact": BURST_IMPACT,
            },
            "retained": True,
        })
        stages.append({
            "stage": "5. Wavelet decomposition",
            "detail": f"Complex Morlet CWT ({int(cyc)} cycles) over "
                      f"{TOTAL_BAND[0]}–{TOTAL_BAND[1]} Hz at {FREQ_STEP} Hz steps, "
                      "power averaged over the analysis window per frequency. "
                      "Full time–frequency matrix not retained; per-trial per-"
                      "frequency spectrum is retained.",
            "values": {
                "freq_range_hz": list(TOTAL_BAND),
                "freq_step_hz": FREQ_STEP,
                "morlet_cycles": cyc,
            },
            "retained": True,
        })
        stages.append({
            "stage": "6. Relative power (per trial)",
            "detail": f"{band}_rel = Σ power in {num_band[0]}–{num_band[1]} Hz ÷ "
                      f"Σ power in {TOTAL_BAND[0]}–{TOTAL_BAND[1]} Hz (per trial). "
                      "The denominator is recomputed from the retained spectrum; "
                      "only the ratio is stored.",
            "values": {
                "numerator_band_hz": num_band,
                "denominator_band_hz": list(TOTAL_BAND),
            },
            "retained": True,
        })
    else:
        stages.append({
            "stage": "2. Reaction time (per trial)",
            "detail": "rt_ms = (keypress time − stimulus onset) × 1000, from the "
                      "LabChart marker timeline. Included when rt_ms > 0.",
            "values": {},
            "retained": True,
        })

    stages.append({
        "stage": f"{'7' if is_power else '3'}. Assign refresh rate",
        "detail": "Each trial's block number is mapped to its refresh-rate label "
                  "via the demographics 'Refresh Rate Condition Ordering' "
                  "(not block number), so counterbalancing is respected.",
        "values": {},
        "retained": True,
    })
    stages.append({
        "stage": f"{'8' if is_power else '4'}. Aggregate per condition",
        "detail": f"Median of the included trial values within each refresh "
                  f"condition ({low} and {high}) for this participant "
                  "(median is primary — robust to skew and residual sub-threshold "
                  "artifact; the mean is also computed and carried in the export "
                  "and the on-screen inspection toggle).",
        "values": {},
        "retained": True,
    })
    stages.append({
        "stage": f"{'9' if is_power else '5'}. Difference",
        "detail": f"Δ = {high} − {low} per participant (same signed direction "
                  "everywhere: screen, trace and CSV). The per-participant medians "
                  "are then summarised across the sample with their MEAN Δ (the "
                  "statistic matching the paired t-test), plus median Δ, SD and "
                  "95% CI as robustness checks.",
        "values": {},
        "retained": True,
    })
    return {"stages": stages}


# ============================================================
#  Endpoints
# ============================================================
@app.post("/api/upload")
async def upload_eeg(files: List[UploadFile] = File(...)):
    """Upload and analyse one or more LabChart .txt exports.

    All files sent in a single request MUST belong to the same subject
    (same canonical `S<n>P<nn>` code). They are treated as one recording
    split across multiple files (e.g. S8P025(1).txt + S8P025(2).txt).

    Files are ordered by their filename `part_hint` (1 before 2 before
    unknown), then alphabetically as a tiebreak. This is a UI-ordering
    convenience only — the parser uses each file's ExcelDateTime to
    build the true global timeline.

    Backward compatible: single-file uploads work unchanged, and the
    response shape is identical to the previous version.
    """
    if not files:
        logger.warning("upload_eeg called with no files")
        raise HTTPException(400, "No files uploaded.")
    logger.info(
        "upload_eeg received %d file(s): %s",
        len(files),
        ", ".join(f.filename for f in files),
    )
    for f in files:
        if not f.filename.lower().endswith(".txt"):
            logger.warning("upload_eeg rejected non-.txt file: %s", f.filename)
            raise HTTPException(400, f"{f.filename}: only .txt LabChart exports are accepted")

    # Resolve subject IDs and require all files in one request to agree.
    parsed_names = [parse_upload_filename(f.filename) for f in files]
    sids = {p.subject_id for p in parsed_names}
    if len(sids) > 1:
        logger.warning("upload_eeg mixed subject IDs: %s", sorted(sids))
        raise HTTPException(
            400,
            f"Files in one upload must share a subject ID; got {sorted(sids)}. "
            "Upload each subject's files as a separate request.",
        )
    subject_id = parsed_names[0].subject_id
    logger.info("upload_eeg subject_id resolved to %s", subject_id)

    # Order: part 1 → 2 → unknown; alphabetical tiebreak.
    ordered = sorted(
        zip(files, parsed_names),
        key=lambda x: (x[1].part is None, x[1].part or 0, x[1].original.lower()),
    )
    files_ordered = [f for f, _ in ordered]

    tmp_paths: List[str] = []
    try:
        for f in files_ordered:
            content = await f.read()
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="wb") as tmp:
                tmp.write(content)
                tmp_paths.append(tmp.name)
        logger.debug("upload_eeg wrote %d temp file(s): %s", len(tmp_paths), tmp_paths)

        try:
            parsed = parse_labchart_multi(tmp_paths)
        except Exception:
            logger.exception("parse_labchart_multi failed for %s", subject_id)
            raise HTTPException(422, f"{subject_id}: failed to parse LabChart file(s). See server log for details.")
        parsed.filename = subject_id
        logger.info(
            "%s parsed: %d segments, %d markers, %d trials, %d cluster(s)",
            subject_id,
            len(parsed.segments),
            len(parsed.markers),
            len(parsed.trials),
            len(parsed.cluster_meta),
        )
        if parsed.warnings:
            for w in parsed.warnings:
                logger.warning("%s parser warning: %s", subject_id, w)

        result_id = subject_id

        # Reconcile any placeholder-keyed behavioural session BEFORE block
        # selection, so an already-uploaded CSV can drive candidate choice.
        eeg_ids = parse_filename_ids(result_id)  # (session, participant) or None
        if eeg_ids is not None:
            placeholder = f"?P{eeg_ids[1]:03d}"
            if placeholder in _behavioural and placeholder != result_id:
                logger.info(
                    "reconciling placeholder behavioural %s -> %s",
                    placeholder, result_id,
                )
                _behavioural[result_id] = _behavioural.pop(placeholder)
                _alignment.pop(placeholder, None)

        # ── Block selection (work-order Task 3) ────────────────────────────
        # If any block has >1 candidate run, choose the correct one (by
        # behavioural alignment when available, else the trial-count
        # fallback). On clean recordings this is a no-op. The resulting
        # B-codes are stashed on parsed for the checks payload.
        beh_for_selection = _behavioural.get(result_id)
        try:
            selection = select_blocks(parsed, beh_for_selection)
        except Exception:
            logger.exception("select_blocks failed for %s", result_id)
            raise HTTPException(500, f"{result_id}: block selection failed. See server log for details.")
        parsed.block_codes = selection.codes
        n_before = len(parsed.trials)
        if len(selection.selected_trials) != n_before or len(selection.candidates_by_block) != len(selection.selected_by_block):
            logger.info(
                "%s block selection: %d trials -> %d after selecting %d/%d block candidate(s)%s",
                result_id, n_before, len(selection.selected_trials),
                len(selection.selected_by_block),
                sum(len(v) for v in selection.candidates_by_block.values()),
                " [behavioural]" if selection.used_behavioural else " [trial-count fallback]",
            )
            parsed.trials = selection.selected_trials
            parsed.n_blocks = len(selection.selected_by_block)

        try:
            result = run_pipeline(parsed)
        except Exception:
            logger.exception("run_pipeline failed for %s", subject_id)
            raise HTTPException(500, f"{subject_id}: analysis pipeline failed. See server log for details.")
        _results[result_id] = result
        _parsed[result_id] = parsed
        logger.info(
            "%s pipeline OK: theta_surv=%d beta_surv=%d",
            result_id,
            result.theta_summary.surviving,
            result.beta_summary.surviving,
        )

        # Behavioural was already reconciled before block selection (above).
        # If behavioural data was pre-loaded for this subject, run alignment.
        alignment = _run_alignment(result_id)
        flanker_map, missing_trials = _flanker_context(result_id)

        return {
            "status": "success",
            "result_id": result_id,
            "summary": _summary_payload(result),
            "trials": [_trial_row(t, flanker_map) for t in result.trials],
            "missing_trials": missing_trials,
            "spectra": _spectra_payload(result),
            "source_files": [p.original for _, p in ordered],
            "warnings": parsed.warnings,
            "alignment": [_alignment_payload(a) for a in alignment],
            "accuracy": _accuracy_payload(result_id),
            "checks": _checks_payload_for(result_id),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("upload_eeg unexpected failure for %s", subject_id if 'subject_id' in dir() else '<unknown>')
        raise HTTPException(500, f"Processing error: {e}")
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


@app.get("/api/subjects")
async def list_subjects():
    """List all uploaded subjects."""
    return {
        "subjects": [
            {
                "result_id": rid,
                "filename": r.filename,
                "recording_date": r.recording_date,
                "n_trials": r.n_trials,
                "theta_surviving": r.theta_summary.surviving,
                "beta_surviving": r.beta_summary.surviving,
            }
            for rid, r in _results.items()
        ]
    }


@app.get("/api/subjects/{result_id}/results")
async def subject_results(result_id: str):
    """Full results payload for one already-uploaded subject.

    Mirrors the shape returned by /api/upload so the frontend can re-display
    an individual subject (e.g. when drilling into one from the group view)
    without re-uploading. 404 if the subject is not in memory.
    """
    if result_id not in _results:
        raise HTTPException(404, f"Subject '{result_id}' not found. Upload it first.")

    result = _results[result_id]
    parsed = _parsed.get(result_id)
    alignment = _alignment.get(result_id, [])
    flanker_map, missing_trials = _flanker_context(result_id)

    return {
        "status": "success",
        "result_id": result_id,
        "summary": _summary_payload(result),
        "trials": [_trial_row(t, flanker_map) for t in result.trials],
        "missing_trials": missing_trials,
        "spectra": _spectra_payload(result),
        "source_files": list(getattr(parsed, "source_files", [])) if parsed else [],
        "warnings": list(getattr(parsed, "warnings", [])) if parsed else [],
        "alignment": [_alignment_payload(a) for a in alignment],
        "accuracy": _accuracy_payload(result_id),
        "checks": _checks_payload_for(result_id),
    }


@app.delete("/api/subjects/{result_id}")
async def remove_subject(result_id: str):
    """Drop one subject from memory."""
    _results.pop(result_id, None)
    _parsed.pop(result_id, None)
    _alignment.pop(result_id, None)
    return {"status": "ok"}


# ============================================================
#  Demographics
# ============================================================
@app.post("/api/demographics/upload")
async def upload_demographics(file: UploadFile = File(...)):
    """
    Upload the demographics CSV. Replaces any previously loaded demographics
    in memory. Returns how many rows parsed + which uploaded LabChart files
    now have a matching demographic record.
    """
    global _demographics, _demographics_source
    logger.info("upload_demographics received: %s", file.filename)
    raw = await file.read()
    try:
        demos = parse_demographics(raw)
    except Exception as exc:
        logger.exception("parse_demographics failed for %s", file.filename)
        raise HTTPException(400, f"Could not parse demographics CSV: {exc}")

    _demographics = demos
    _demographics_source = file.filename or "demographics.csv"
    logger.info("demographics: loaded %d participant row(s)", len(demos))

    # Report matching against currently uploaded EEG files
    matches = []
    unmatched = []
    for rid, r in _results.items():
        ids = parse_filename_ids(r.filename)
        m = match_demographics(r.filename, demos) if ids else None
        (matches if m else unmatched).append({"result_id": rid, "filename": r.filename})
    logger.info("demographics: matched %d / unmatched %d subject(s)", len(matches), len(unmatched))

    return {
        "status": "success",
        "csv_source": _demographics_source,
        "n_participants": len(demos),
        "matches": matches,
        "unmatched": unmatched,
    }


@app.get("/api/demographics")
async def get_demographics():
    """List all loaded demographic rows (compact summary)."""
    return {
        "csv_source": _demographics_source,
        "n_participants": len(_demographics),
        "participants": [
            {
                "session": d.session,
                "participant": d.participant,
                "filename_stem": f"S{d.session}P{d.participant:03d}",
                "age": d.display.get("age"),
                "sex": d.display.get("sex"),
                "block_order": {str(k): v for k, v in d.block_order.items()},
                "aborted": d.aborted,
            }
            for d in _demographics
        ],
        "fields": [
            {"key": k, "label": lbl}
            for k, _col, lbl in DISPLAY_FIELDS
        ],
    }


@app.delete("/api/demographics")
async def clear_demographics():
    """Remove the currently loaded demographics."""
    global _demographics, _demographics_source
    _demographics = []
    _demographics_source = None
    return {"status": "ok"}


# ============================================================
#  Behavioural (OpenSesame CSV)
# ============================================================
def _alignment_payload(res: AlignmentResult) -> dict:
    return {
        "block": res.block,
        "eeg_offset_ms": _safe(res.eeg_offset_ms),
        "matched": len(res.matched_pairs),
        "unmatched_eeg": len(res.unmatched_eeg_indices),
        "unmatched_beh": len(res.unmatched_beh_indices),
        "unmatched_beh_row_indices": res.unmatched_beh_indices,
        "rt_correlation": _safe(res.rt_correlation),
        "congruency_agreement": _safe(res.congruency_agreement),
        "rt_residual_ms": _safe(res.rt_residual_ms),
    }


def _checks_payload_for(subject_id: str) -> List[dict]:
    """Run every validity check for one subject and serialise the result."""
    if subject_id not in _parsed or subject_id not in _results:
        return []
    parsed = _parsed[subject_id]
    result = _results[subject_id]
    alignments = _alignment.get(subject_id, [])
    session = _behavioural.get(subject_id)
    demo = match_demographics(subject_id, _demographics) if _demographics else None
    try:
        checks = run_subject_checks(
            parsed, result,
            alignments=alignments,
            beh_session=session,
            demographic=demo,
        )
    except Exception:
        logger.exception("run_subject_checks failed for %s", subject_id)
        return [{
            "code": "X001",
            "level": "HALT",
            "message": "Validity checks crashed — see server log.",
            "context": {},
        }]
    counts = {"HALT": 0, "WARN": 0, "INFO": 0}
    for c in checks:
        counts[c.level] = counts.get(c.level, 0) + 1
    logger.info(
        "checks for %s: %d HALT, %d WARN, %d INFO",
        subject_id, counts.get("HALT", 0), counts.get("WARN", 0), counts.get("INFO", 0),
    )
    payload = checks_to_payload(checks)
    # Prepend block-selection B-codes (Task 3), which are produced outside
    # run_subject_checks (they need the candidate list from select_blocks).
    block_codes = getattr(parsed, "block_codes", []) or []
    b_payload = [
        {"code": code, "level": level, "message": msg, "context": {}}
        for code, level, msg in block_codes
    ]
    # Append the cohort-level C006 amplitude-outlier check (Task 8). It needs
    # every currently-loaded recording's median Fz-Pz ptp; the result for THIS
    # subject is filtered out of the returned list (only its own row is shown).
    fz_by_subject = {
        sid: getattr(r, "fz_ptp_median", 0.0) for sid, r in _results.items()
    }
    cohort_checks = check_cohort_amplitude(fz_by_subject)
    c006_payload = [
        {"code": c.code, "level": c.level, "message": c.message, "context": c.context}
        for c in cohort_checks
        # keep C006 rows relevant to this subject, plus the cohort INFO summary
        if c.context.get("subject") in (None, subject_id) or "cohort_median" in c.context
    ]
    return b_payload + payload + c006_payload


def _accuracy_payload(subject_id: str) -> List[dict]:
    """Per-block accuracy + error-trial counts, if behavioural alignment ran.

    Returns [] when no alignment exists. Otherwise one entry per block:
      - block, n_beh_trials, n_errors, accuracy
      - matched_correct_con / matched_correct_inc / matched_error_con /
        matched_error_inc among the aligned pairs
      - eeg_surviving_after_error_exclusion:
          survival counts on the ANALYSED EEG trials, after also dropping
          any that matched a behavioural error trial (docs §4.3 says to
          exclude these normally).
    The primary EEG-only surviving counts are unaffected — this is an
    auxiliary view.
    """
    if subject_id not in _results or subject_id not in _behavioural:
        return []
    if subject_id not in _alignment:
        return []

    result = _results[subject_id]
    session = _behavioural[subject_id]
    per_block: List[dict] = []

    for align in _alignment[subject_id]:
        blk = align.block
        beh_block = [t for t in session.trials if t.block == blk]
        eeg_block = [t for t in result.trials if t.block == blk]

        # Overall behavioural accuracy for this block (all rows, matched or not).
        n_beh = len(beh_block)
        n_errors = sum(1 for t in beh_block if not t.correct)

        # Restrict to the matched pairs and count correct/error per congruency.
        matched_c_con = matched_c_inc = matched_e_con = matched_e_inc = 0
        error_eeg_indices: List[int] = []  # positions in eeg_block that matched an error
        for eeg_i, beh_i in align.matched_pairs:
            bt = beh_block[beh_i]
            if bt.correct:
                if bt.congruent:
                    matched_c_con += 1
                else:
                    matched_c_inc += 1
            else:
                if bt.congruent:
                    matched_e_con += 1
                else:
                    matched_e_inc += 1
                error_eeg_indices.append(eeg_i)

        # Recompute surviving counts on Fz-Pz and C3-C4 excluding error trials.
        # Use result.trials filtered to this block.
        eeg_surv_after = {"theta": 0, "beta": 0}
        eeg_excl_error = {"theta": 0, "beta": 0}
        for i, t in enumerate(eeg_block):
            is_error = i in error_eeg_indices
            if not t.fz_exclude:
                if is_error:
                    eeg_excl_error["theta"] += 1
                else:
                    eeg_surv_after["theta"] += 1
            if not t.c3_exclude:
                if is_error:
                    eeg_excl_error["beta"] += 1
                else:
                    eeg_surv_after["beta"] += 1

        per_block.append({
            "block": blk,
            "n_beh_trials": n_beh,
            "n_errors": n_errors,
            "accuracy": (n_beh - n_errors) / n_beh if n_beh else float("nan"),
            "matched_correct_con": matched_c_con,
            "matched_correct_inc": matched_c_inc,
            "matched_error_con": matched_e_con,
            "matched_error_inc": matched_e_inc,
            "eeg_surviving_after_error_exclusion": eeg_surv_after,
            "eeg_error_trials_dropped": eeg_excl_error,
        })

    return per_block


def _run_alignment(subject_id: str) -> List[AlignmentResult]:
    """Align the subject's EEG trials against its behavioural session.

    Called after either an EEG or behavioural upload for the same subject.
    No-op (returns []) if either side is missing. Stored in _alignment.
    """
    if subject_id not in _results or subject_id not in _behavioural:
        _alignment.pop(subject_id, None)
        return []
    result = _results[subject_id]
    session = _behavioural[subject_id]
    per_block: List[AlignmentResult] = []
    blocks = sorted({t.block for t in result.trials})
    for blk in blocks:
        eeg_block = [t for t in result.trials if t.block == blk]
        try:
            ar = align_block(
                eeg_rts_ms=[t.rt_ms for t in eeg_block],
                eeg_congruent=[t.cond == "con" for t in eeg_block],
                beh_trials=session.trials,
                block=blk,
            )
        except Exception:
            logger.exception(
                "align_block failed for subject=%s block=%d (eeg=%d, beh=%d)",
                subject_id, blk, len(eeg_block),
                sum(1 for t in session.trials if t.block == blk),
            )
            continue
        per_block.append(ar)
        logger.info(
            "aligned %s blk%d: matched=%d r=%.4f cong=%.2f offset=%.1fms",
            subject_id, blk,
            len(ar.matched_pairs),
            ar.rt_correlation,
            ar.congruency_agreement,
            ar.eeg_offset_ms,
        )
    _alignment[subject_id] = per_block
    return per_block


@app.post("/api/behavioural/upload")
async def upload_behavioural(files: List[UploadFile] = File(...)):
    """Upload one or more OpenSesame behavioural CSVs for a single subject.

    All files in one request must share a subject ID (matched to the
    canonical `S<n>P<nn>` code either via filename or, failing that, the
    `subject_nr` column). Files are concatenated in filename-part order;
    block labels come from each row's `stage` column, not from the
    filename.

    If the EEG for this subject is already loaded, per-block alignment
    is computed and returned. Otherwise the behavioural data is stored
    and alignment is deferred until the EEG arrives.
    """
    if not files:
        logger.warning("upload_behavioural called with no files")
        raise HTTPException(400, "No files uploaded.")
    logger.info(
        "upload_behavioural received %d file(s): %s",
        len(files),
        ", ".join(f.filename for f in files),
    )
    for f in files:
        if not f.filename.lower().endswith(".csv"):
            logger.warning("upload_behavioural rejected non-.csv file: %s", f.filename)
            raise HTTPException(400, f"{f.filename}: only .csv behavioural files are accepted")

    parsed_names = [parse_upload_filename(f.filename) for f in files]
    # A filename yields a subject_id only if it starts with S<n>P<nn>;
    # otherwise its `subject_id` is the extension-less stem, which we
    # ignore for matching purposes and resolve via `subject_nr` below.
    sids_from_filename = {
        p.subject_id for p in parsed_names
        if re.match(r"^S\d+P\d+$", p.subject_id)
    }
    if len(sids_from_filename) > 1:
        logger.warning(
            "upload_behavioural mixed subject IDs: %s",
            sorted(sids_from_filename),
        )
        raise HTTPException(
            400,
            f"Behavioural files in one upload must share a subject ID; "
            f"got {sorted(sids_from_filename)}."
        )
    filename_subject_id = next(iter(sids_from_filename)) if sids_from_filename else None

    ordered = sorted(
        zip(files, parsed_names),
        key=lambda x: (x[1].part is None, x[1].part or 0, x[1].original.lower()),
    )

    file_bytes: List[tuple[bytes, str]] = []
    for f, p in ordered:
        file_bytes.append((await f.read(), p.original))

    try:
        session = parse_behavioural_session(file_bytes)
    except ValueError as e:
        logger.warning("parse_behavioural_session rejected upload: %s", e)
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("parse_behavioural_session unexpected failure")
        raise HTTPException(500, "Failed to parse behavioural CSV — see server log.")

    logger.info(
        "behavioural parsed: subject_nr=%s trials=%d variants=%s",
        session.subject_nr,
        len(session.trials),
        [f.variant for f in session.files],
    )
    if session.warnings:
        for w in session.warnings:
            logger.warning("behavioural warning: %s", w)

    # Resolve the final subject_id. Priority order:
    #   1. S<n>P<nn> code found in any of the uploaded filenames.
    #   2. Match `subject_nr` against an already-loaded EEG subject.
    #   3. Fabricate a placeholder from subject_nr alone (`?PNNN` with
    #      unknown session — will not match EEG until it's loaded).
    subject_id = filename_subject_id
    if subject_id is None:
        # Try to find an EEG subject whose parsed filename gives the same participant.
        for rid in _results:
            ids = parse_filename_ids(rid)
            if ids and ids[1] == session.subject_nr:
                subject_id = rid
                break
    if subject_id is None:
        subject_id = f"?P{session.subject_nr:03d}"
        logger.info("behavioural: no matching EEG loaded, using placeholder %s", subject_id)
    else:
        logger.info("behavioural: resolved subject_id=%s", subject_id)

    _behavioural[subject_id] = session
    alignment = _run_alignment(subject_id)
    _, missing_trials = _flanker_context(subject_id)

    return {
        "status": "success",
        "subject_id": subject_id,
        "n_trials": len(session.trials),
        "variants": [f.variant for f in session.files],
        "source_files": [f.filename for f in session.files],
        "warnings": session.warnings,
        "eeg_loaded": subject_id in _results,
        "alignment": [_alignment_payload(a) for a in alignment],
        "accuracy": _accuracy_payload(subject_id),
        "missing_trials": missing_trials,
        "checks": _checks_payload_for(subject_id),
    }


@app.get("/api/behavioural/{subject_id}")
async def get_behavioural(subject_id: str):
    if subject_id not in _behavioural:
        raise HTTPException(404, f"No behavioural data loaded for {subject_id}.")
    session = _behavioural[subject_id]
    alignment = _alignment.get(subject_id, [])
    return {
        "subject_id": subject_id,
        "n_trials": len(session.trials),
        "variants": [f.variant for f in session.files],
        "source_files": [f.filename for f in session.files],
        "warnings": session.warnings,
        "eeg_loaded": subject_id in _results,
        "alignment": [_alignment_payload(a) for a in alignment],
        "accuracy": _accuracy_payload(subject_id),
        "checks": _checks_payload_for(subject_id),
    }


@app.delete("/api/behavioural/{subject_id}")
async def remove_behavioural(subject_id: str):
    _behavioural.pop(subject_id, None)
    _alignment.pop(subject_id, None)
    return {"status": "ok"}


@app.get("/api/compare")
async def compare_subjects():
    """Aggregate payload for the group view."""
    if len(_results) < 2:
        raise HTTPException(400, "Need at least 2 subjects to compare. Upload more files.")

    return {
        "subjects": [
            {
                "result_id": rid,
                "summary": _summary_payload(r),
                "trials": [_trial_row(t, _flanker_context(rid)[0]) for t in r.trials],
            }
            for rid, r in _results.items()
        ]
    }


@app.get("/api/refresh-comparison")
async def refresh_comparison():
    """60 Hz vs 165 Hz comparison for theta rel, beta rel and reaction time.

    Per participant: the mean value in each refresh condition and the
    (higher − lower) refresh-rate difference. Across the sample: the mean,
    median, SD, SEM and 95% CI of those per-participant differences, with N.
    Refresh rate is derived from demographics block_order (not block number).
    Each measure also carries a provenance chain describing the processing
    stages and the retained config values used to produce the plotted number.
    """
    if len(_results) < 1:
        raise HTTPException(400, "No subjects loaded. Upload files first.")

    measures = [_refresh_measure_payload(k) for k in _REFRESH_MEASURES]
    # A subject is fully usable here only if its demographics carry a block
    # order; surface how many do so the UI can warn.
    n_with_order = sum(1 for r in _results.values() if _block_hz_map(r.filename))
    return {
        "measures": measures,
        "n_subjects": len(_results),
        "n_with_refresh_order": n_with_order,
    }


# ============================================================
#  CSV downloads
# ============================================================
def _csv_response(rows: List[list], header: list, filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


TRIAL_CSV_HEADER = [
    "recording", "trial", "block", "btrial", "block_hz", "cond", "onset", "key", "rt_ms",
    "fz_ptp", "theta_fft", "beta_fft", "maxz", "impact", "coinc",
    "blink", "fz_exclude", "c3_exclude", "reason",
    "theta_abs", "theta_rel", "beta_abs", "beta_rel",
]


def _demographic_columns():
    """Return list of (header, key) tuples for demographic columns in CSV order."""
    if not _demographics:
        return []
    return [(f"demo_{key}", key) for key, _col, _label in DISPLAY_FIELDS] + [("demo_matched", "__matched")]


def _demographic_values(filename: str, key_list: list) -> list:
    """Return CSV values for a filename's demographics matching key_list order."""
    if not _demographics:
        return []
    demo = match_demographics(filename, _demographics)
    out = []
    for _hdr, key in key_list:
        if key == "__matched":
            out.append(bool(demo))
        else:
            out.append(demo.display.get(key, "") if demo else "")
    return out


def _trial_csv_row(rec: str, t: TrialResult, demo_cols=None) -> list:
    if demo_cols is None:
        demo_cols = _demographic_columns()
    demo = match_demographics(rec, _demographics) if _demographics else None
    block_hz = demo.block_order.get(t.block, "") if demo else ""
    row = [
        rec, t.trial, t.block, t.btrial, block_hz, t.cond,
        round(t.onset, 4), round(t.key, 4), t.rt_ms,
        round(t.fz_ptp, 4), round(t.theta_fft, 4), round(t.beta_fft, 4),
        round(t.maxz, 6), round(t.impact, 6), round(t.coinc, 6),
        t.blink, t.fz_exclude, t.c3_exclude, t.reason,
        round(t.theta_abs, 6), round(t.theta_rel, 6),
        round(t.beta_abs, 6),  round(t.beta_rel, 6),
    ]
    row.extend(_demographic_values(rec, demo_cols))
    return row


@app.get("/api/download-csv-trials/{result_id}")
async def download_trials_csv(result_id: str):
    """Per-trial CSV for one subject (results.csv format)."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    demo_cols = _demographic_columns()
    header = TRIAL_CSV_HEADER + [h for h, _ in demo_cols]
    rows = [_trial_csv_row(r.filename, t, demo_cols) for t in r.trials]
    return _csv_response(rows, header, f"{result_id}_results.csv")


@app.get("/api/download-csv-trials-all")
async def download_trials_csv_all():
    """Combined per-trial CSV across all uploaded subjects."""
    if not _results:
        raise HTTPException(404, "No results.")
    demo_cols = _demographic_columns()
    header = TRIAL_CSV_HEADER + [h for h, _ in demo_cols]
    rows = []
    for r in _results.values():
        rows.extend(_trial_csv_row(r.filename, t, demo_cols) for t in r.trials)
    return _csv_response(rows, header, "eeg_group_results.csv")


EXCL_HEADER = ["recording", "trial", "block", "btrial", "cond", "rt_ms",
               "fz_exclude", "c3_exclude", "reason"]


@app.get("/api/download-csv-exclusions/{result_id}")
async def download_exclusions_csv(result_id: str):
    """Exclusion log for one subject."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    rows = [
        [r.filename, t.trial, t.block, t.btrial, t.cond, t.rt_ms,
         t.fz_exclude, t.c3_exclude, t.reason]
        for t in r.trials if t.fz_exclude or t.c3_exclude
    ]
    return _csv_response(rows, EXCL_HEADER, f"{result_id}_exclusions.csv")


SUMMARY_HEADER = [
    "recording", "recording_date",
    "theta_surviving", "theta_excluded",
    "theta_rel_median_con", "theta_rel_median_inc",
    "theta_abs_median_con", "theta_abs_median_inc",
    "beta_surviving", "beta_excluded",
    "beta_rel_median_con", "beta_rel_median_inc",
    "beta_abs_median_con", "beta_abs_median_inc",
    "theta_exclusion_pct_con", "theta_exclusion_pct_inc", "theta_balance_flag",
    "beta_exclusion_pct_con",  "beta_exclusion_pct_inc",  "beta_balance_flag",
    "block1_hz", "block2_hz",
    # Per-block breakdowns (theta then beta, blocks 1..2, rel then abs, con then inc)
    "theta_b1_surv_con", "theta_b1_surv_inc", "theta_b1_exc_con", "theta_b1_exc_inc",
    "theta_b1_rel_median_con", "theta_b1_rel_median_inc",
    "theta_b1_abs_median_con", "theta_b1_abs_median_inc",
    "theta_b2_surv_con", "theta_b2_surv_inc", "theta_b2_exc_con", "theta_b2_exc_inc",
    "theta_b2_rel_median_con", "theta_b2_rel_median_inc",
    "theta_b2_abs_median_con", "theta_b2_abs_median_inc",
    "beta_b1_surv_con", "beta_b1_surv_inc", "beta_b1_exc_con", "beta_b1_exc_inc",
    "beta_b1_rel_median_con", "beta_b1_rel_median_inc",
    "beta_b1_abs_median_con", "beta_b1_abs_median_inc",
    "beta_b2_surv_con", "beta_b2_surv_inc", "beta_b2_exc_con", "beta_b2_exc_inc",
    "beta_b2_rel_median_con", "beta_b2_rel_median_inc",
    "beta_b2_abs_median_con", "beta_b2_abs_median_inc",
    # Adaptive thresholds + contamination metrics (work-order Tasks 4/5/7/8)
    "blink_threshold_uv", "emg_threshold", "fz_ptp_median",
    "c3_beta_share", "c3_high_share",
    "theta_channel_excluded", "theta_exclusion_code",
    "beta_channel_excluded", "beta_exclusion_code",
]


def _r6(v):
    """round() that passes None through (channel-excluded power is None)."""
    return "" if v is None else round(v, 6)


def _block_cells(summary: ChannelSummary, blk: int) -> list:
    b = (summary.by_block or {}).get(blk)
    if not b:
        return ["", "", "", "", "", "", "", ""]
    # When the channel is scope-excluded, power medians must not be reported.
    excl = summary.channel_excluded
    return [
        b["n_surv_con"], b["n_surv_inc"], b["n_exc_con"], b["n_exc_inc"],
        "" if excl else _r6(b["rel_median_con"]), "" if excl else _r6(b["rel_median_inc"]),
        "" if excl else _r6(b["abs_median_con"]), "" if excl else _r6(b["abs_median_inc"]),
    ]


def _summary_row(r: PipelineResult, demo_cols=None) -> list:
    if demo_cols is None:
        demo_cols = _demographic_columns()
    ts, bs = r.theta_summary, r.beta_summary
    te, be = ts.channel_excluded, bs.channel_excluded
    demo = match_demographics(r.filename, _demographics) if _demographics else None
    row = [
        r.filename, r.recording_date,
        ts.surviving, ts.excluded,
        "" if te else _r6(ts.rel_median_con), "" if te else _r6(ts.rel_median_inc),
        "" if te else _r6(ts.abs_median_con), "" if te else _r6(ts.abs_median_inc),
        bs.surviving, bs.excluded,
        "" if be else _r6(bs.rel_median_con), "" if be else _r6(bs.rel_median_inc),
        "" if be else _r6(bs.abs_median_con), "" if be else _r6(bs.abs_median_inc),
        round(ts.exclusion_pct_con, 2), round(ts.exclusion_pct_inc, 2), ts.balance_flag,
        round(bs.exclusion_pct_con, 2), round(bs.exclusion_pct_inc, 2), bs.balance_flag,
        demo.block_order.get(1, "") if demo else "",
        demo.block_order.get(2, "") if demo else "",
    ]
    # Per-block breakdowns
    row.extend(_block_cells(ts, 1))
    row.extend(_block_cells(ts, 2))
    row.extend(_block_cells(bs, 1))
    row.extend(_block_cells(bs, 2))
    # Adaptive + contamination + channel-exclusion columns
    row.extend([
        round(getattr(r, "blink_threshold_uv", 0.0), 1),
        round(getattr(r, "emg_threshold", 0.0), 0),
        round(getattr(r, "fz_ptp_median", 0.0), 1),
        round(getattr(r, "c3_beta_share", 0.0), 4),
        round(getattr(r, "c3_high_share", 0.0), 4),
        ts.channel_excluded, ts.exclusion_code,
        bs.channel_excluded, bs.exclusion_code,
    ])
    row.extend(_demographic_values(r.filename, demo_cols))
    return row


@app.get("/api/download-csv/{result_id}")
async def download_summary_csv(result_id: str):
    """Single-subject summary CSV (medians + balance flags)."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    demo_cols = _demographic_columns()
    header = SUMMARY_HEADER + [h for h, _ in demo_cols]
    return _csv_response([_summary_row(r, demo_cols)], header, f"{result_id}_summary.csv")


@app.get("/api/download-csv-all")
async def download_summary_csv_all():
    """Group summary CSV (one row per subject)."""
    if not _results:
        raise HTTPException(404, "No results.")
    demo_cols = _demographic_columns()
    header = SUMMARY_HEADER + [h for h, _ in demo_cols]
    rows = [_summary_row(r, demo_cols) for r in _results.values()]
    return _csv_response(rows, header, "eeg_group_summary.csv")


@app.get("/api/download-csv-refresh")
async def download_refresh_csv():
    """60 Hz vs 165 Hz comparison as CSV: one row per participant × measure,
    plus a group-summary row per measure.

    Both aggregations are carried per condition (``_median`` — the documented
    primary — and ``_mean``) with the signed high−low difference computed each
    way. These are the fixed computed values; they do NOT follow the on-screen
    inspection toggle, so the export is identical regardless of the UI state.
    The high−low sign is identical everywhere (screen, trace, CSV, SPSS).
    """
    if not _results:
        raise HTTPException(404, "No results.")
    header = [
        "measure", "channel", "unit", "result_id", "filename",
        "rate_low", "rate_low_median", "rate_low_mean", "rate_low_n",
        "rate_high", "rate_high_median", "rate_high_mean", "rate_high_n",
        "diff_high_minus_low_median", "diff_high_minus_low_mean",
        "exclusion_kind", "fixable", "note",
    ]
    rows = []
    for m in (_refresh_measure_payload(k) for k in _REFRESH_MEASURES):
        for p in m["participants"]:
            lo = p["conditions"].get(m["rate_low"]) if m["rate_low"] else None
            hi = p["conditions"].get(m["rate_high"]) if m["rate_high"] else None
            rows.append([
                m["label"], m["channel"], m["unit"], p["result_id"], p["filename"],
                m["rate_low"] or "",
                lo["median"] if lo else "", lo["mean"] if lo else "", lo["n"] if lo else "",
                m["rate_high"] or "",
                hi["median"] if hi else "", hi["mean"] if hi else "", hi["n"] if hi else "",
                p["diff_median"] if p["diff_median"] is not None else "",
                p["diff_mean"] if p["diff_mean"] is not None else "",
                p.get("exclusion_kind") or "",
                ("yes" if p.get("fixable") else "no") if p.get("exclusion_kind") else "",
                p["note"] or "",
            ])
        g = m["group"]           # over per-participant MEDIAN diffs (primary)
        gm = m["group_from_means"]  # over per-participant MEAN diffs (robustness)
        rows.append([
            f"{m['label']} — GROUP SUMMARY", m["channel"], m["unit"], "", "",
            f"median-agg: mean_diff={g['mean_diff']}", f"median_diff={g['median_diff']}",
            f"sd={g['sd_diff']}", f"n={g['n']}",
            f"mean-agg: mean_diff={gm['mean_diff']}", f"median_diff={gm['median_diff']}",
            f"sd={gm['sd_diff']}", f"n={gm['n']}",
            g["mean_diff"] if g["mean_diff"] is not None else "",
            gm["mean_diff"] if gm["mean_diff"] is not None else "",
            "", "",
            f"primary=median aggregation; 95% CI(median-agg)="
            f"[{g['ci95_lo']}, {g['ci95_hi']}]",
        ])
    return _csv_response(rows, header, "eeg_refresh_60_vs_165.csv")


# ============================================================
#  Static frontend
# ============================================================
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
