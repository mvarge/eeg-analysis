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
    BLINK_UV, EMG_BETA, BURST_Z, BURST_IMPACT, COINC_Z,
    HP_HZ, WIN_S, PAD_S, THETA_BAND, BETA_BAND, TOTAL_BAND,
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
from checks import run_subject_checks, checks_to_payload
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
        "config": {
            "hp_hz": HP_HZ,
            "window_s": WIN_S,
            "pad_s": PAD_S,
            "theta_band": list(THETA_BAND),
            "beta_band": list(BETA_BAND),
            "total_band": list(TOTAL_BAND),
            "blink_uv": BLINK_UV,
            "emg_beta": EMG_BETA,
            "burst_z": BURST_Z,
            "burst_impact": BURST_IMPACT,
            "coinc_z": COINC_Z,
        },
    }


def _channel_payload(s: ChannelSummary) -> dict:
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
            "abs_median_con": _safe(round(b["abs_median_con"], 3)),
            "abs_median_inc": _safe(round(b["abs_median_inc"], 3)),
            "rel_median_con": _safe(round(b["rel_median_con"], 4)),
            "rel_median_inc": _safe(round(b["rel_median_inc"], 4)),
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
        "abs_median_con": _safe(round(s.abs_median_con, 3)),
        "abs_median_inc": _safe(round(s.abs_median_inc, 3)),
        "rel_median_con": _safe(round(s.rel_median_con, 4)),
        "rel_median_inc": _safe(round(s.rel_median_inc, 4)),
        "by_block": by_block,
    }


def _trial_row(t: TrialResult) -> dict:
    """Trial payload for the frontend (rounded for JSON size)."""
    return {
        "trial": t.trial,
        "btrial": t.btrial,
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

        return {
            "status": "success",
            "result_id": result_id,
            "summary": _summary_payload(result),
            "trials": [_trial_row(t) for t in result.trials],
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
    return b_payload + payload


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
                "trials": [_trial_row(t) for t in r.trials],
            }
            for rid, r in _results.items()
        ]
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
]


def _block_cells(summary: ChannelSummary, blk: int) -> list:
    b = (summary.by_block or {}).get(blk)
    if not b:
        return ["", "", "", "", "", "", "", ""]
    return [
        b["n_surv_con"], b["n_surv_inc"], b["n_exc_con"], b["n_exc_inc"],
        round(b["rel_median_con"], 6), round(b["rel_median_inc"], 6),
        round(b["abs_median_con"], 6), round(b["abs_median_inc"], 6),
    ]


def _summary_row(r: PipelineResult, demo_cols=None) -> list:
    if demo_cols is None:
        demo_cols = _demographic_columns()
    ts, bs = r.theta_summary, r.beta_summary
    demo = match_demographics(r.filename, _demographics) if _demographics else None
    row = [
        r.filename, r.recording_date,
        ts.surviving, ts.excluded,
        round(ts.rel_median_con, 6), round(ts.rel_median_inc, 6),
        round(ts.abs_median_con, 6), round(ts.abs_median_inc, 6),
        bs.surviving, bs.excluded,
        round(bs.rel_median_con, 6), round(bs.rel_median_inc, 6),
        round(bs.abs_median_con, 6), round(bs.abs_median_inc, 6),
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


# ============================================================
#  Static frontend
# ============================================================
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
