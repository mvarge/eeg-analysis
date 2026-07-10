"""
Validity checks for a single subject (per docs/DATA_VALIDITY_CHECKING.md).

Emits a list of Check records — each has a code, a severity level
(INFO/WARN/HALT), a message, and optional context.

Codes are grouped by family:
  E — file structure (E001 … E009)
  M — markers (M001 … M006)
  T — trials (T000 … T007)
  S — signal, scoped to analysed epochs only (S001 … S004)
  J — join / EEG-behavioural alignment (J000 … J006)  (built from an AlignmentResult)
  B — block-selection ambiguity (B000 … B002)         (populated when segment selection runs)
  D — condition mapping via demographics (D001 … D005)
  C — cohort-level (C001 … C005)                      (see checks_cohort.run_cohort_checks)

Design principles inherited from the doc (§1):
  * Every check has a code and a level. Silence is a legitimate output.
  * Signal checks are scoped to analysed epochs, never the whole file.
  * S002 (NaN inside epoch) is a HALT; a whole-file NaN check is useless
    because every recording in this dataset contains NaN in its rest
    periods.
  * E008 (per-segment monotonicity) is a HALT; global monotonicity is
    expected to fail across a multi-segment file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from parser import (
    ParsedEEG, Marker, Segment,
    ONSET_LABELS, RESPONSE_LABEL, BLOCK2_LABEL, END_TOKEN_RE,
    EXPECTED_CH_NORMS, _normalise_channel_name,
)
from pipeline import PipelineResult, TrialResult, WIN_S, PAD_S
from behavioural import BehaviouralSession, AlignmentResult
from demographics import Demographic
from logging_setup import get_logger

logger = get_logger(__name__)


# Levels
INFO = "INFO"
WARN = "WARN"
HALT = "HALT"

CLIP_THRESHOLD_UV = 199.9
DEAD_ELECTRODE_SD_UV = 0.5
FAST_RT_MS = 150.0
SLOW_RT_MS = 1500.0
REACH_S = 0.179  # per docs — same constant used in the pipeline
EPOCH_TOTAL_S = WIN_S + REACH_S  # 0 to onset + 0.500 + 0.179 = 0.679 s


@dataclass
class Check:
    code: str          # e.g. 'E002', 'M003', 'J002'
    level: str         # 'INFO' | 'WARN' | 'HALT'
    message: str       # human-readable one-liner
    context: dict = field(default_factory=dict)  # extra structured data


# ── E-family: file structure ───────────────────────────────────────────────
def check_file_structure(parsed: ParsedEEG) -> List[Check]:
    checks: List[Check] = []

    if not parsed.segments:
        checks.append(Check("E001", HALT, "No parseable header or data."))
        return checks

    first = parsed.segments[0].header
    fs = first.sampling_rate
    if fs <= 0:
        checks.append(Check("E003", HALT, f"Interval= cannot be parsed (fs={fs})."))
    elif abs(fs - 400.0) > 0.5:
        checks.append(Check("E004", HALT, f"Sampling rate {fs:.2f} Hz ≠ 400 Hz."))

    n_ch = len(first.channel_names_raw)
    if n_ch != 2:
        checks.append(Check("E005", HALT, f"Channel count {n_ch} ≠ 2."))

    ch_norms = [_normalise_channel_name(x) for x in first.channel_names_raw]
    if len(ch_norms) < 2 or ch_norms[0] != EXPECTED_CH_NORMS[0] or ch_norms[1] != EXPECTED_CH_NORMS[1]:
        checks.append(Check(
            "E006", HALT,
            f"Channel names {first.channel_names_raw} do not match Fz-Pz/C3-C4 (case-insensitive).",
        ))

    # E007: capitalisation-only mismatch in any later header (INFO).
    for seg in parsed.segments[1:]:
        for a, b in zip(first.channel_names_raw, seg.header.channel_names_raw):
            if a != b and a.lower() == b.lower():
                checks.append(Check(
                    "E007", INFO,
                    f"Segment {seg.index}: channel name capitalisation differs ({a!r} vs {b!r}).",
                ))
                break

    if len(parsed.segments) > 1:
        checks.append(Check(
            "E002", WARN,
            f"File contains {len(parsed.segments)} header blocks — recording was stopped and restarted.",
            {"n_segments": len(parsed.segments)},
        ))

    # E008 / E009 per segment
    for seg in parsed.segments:
        if not seg.monotonic:
            checks.append(Check(
                "E008", HALT,
                f"Segment {seg.index}: local time vector is not monotonic.",
                {"segment_index": seg.index},
            ))
        if not seg.uniform_interval:
            checks.append(Check(
                "E009", WARN,
                f"Segment {seg.index}: sample interval not uniform (dropped samples?).",
                {"segment_index": seg.index},
            ))

    return checks


# ── M-family: markers ──────────────────────────────────────────────────────
def check_markers(parsed: ParsedEEG) -> List[Check]:
    checks: List[Check] = []
    markers = parsed.markers
    known = set(ONSET_LABELS) | {RESPONSE_LABEL, BLOCK2_LABEL, "end"}

    for m in markers:
        lab = m.label.lower()
        if lab == "inc":
            checks.append(Check("M002", WARN, f"Marker 'inc' present at global t={m.time_global:.3f}s (not expected in this dataset)."))
            continue
        if lab not in known:
            checks.append(Check("M001", WARN, f"Unexpected marker label {m.label!r} at t={m.time_global:.3f}s."))

    n_second = sum(1 for m in markers if m.label.lower() == BLOCK2_LABEL)
    if n_second != 2:
        checks.append(Check(
            "M003", WARN,
            f"`second` marker count = {n_second} (expected 2). Extra/aborted runs may be present.",
            {"n_second": n_second},
        ))

    n_end = sum(1 for m in markers if END_TOKEN_RE.match(m.label) or m.label.strip().lower() == "end")
    if n_end > 1:
        checks.append(Check(
            "M004", WARN,
            f"More than one END marker ({n_end}). Multiple completed runs in one file.",
            {"n_end": n_end},
        ))

    # M005: two adjacent `first` markers with no `con` or `key` between them
    prev_first_idx: Optional[int] = None
    for i, m in enumerate(markers):
        lab = m.label.lower()
        if lab == "first":
            if prev_first_idx is not None:
                between = markers[prev_first_idx + 1: i]
                if not any(x.label.lower() in ("con", "key") for x in between):
                    checks.append(Check(
                        "M005", WARN,
                        f"Two adjacent `first` markers at global t={markers[prev_first_idx].time_global:.3f}s "
                        f"and t={m.time_global:.3f}s with nothing between — phantom block-1 boundary (program reload).",
                        {"between_count": len(between)},
                    ))
            prev_first_idx = i
        elif lab in ("con", "key"):
            prev_first_idx = None  # reset when a real trial appears

    # M006: duplicate timestamps (any two markers with identical global time)
    seen: dict = {}
    for m in markers:
        key = round(m.time_global, 6)
        if key in seen:
            checks.append(Check(
                "M006", WARN,
                f"Duplicate marker timestamps at global t={m.time_global:.3f}s: {seen[key]} and {m.label}.",
                {"time_s": m.time_global},
            ))
        else:
            seen[key] = m.label

    return checks


# ── T-family: trials ───────────────────────────────────────────────────────
def check_trials(parsed: ParsedEEG) -> List[Check]:
    checks: List[Check] = []
    trials = parsed.trials
    n = len(trials)

    n_onsets = sum(1 for m in parsed.markers if m.label.lower() in ONSET_LABELS)
    n_key    = sum(1 for m in parsed.markers if m.label.lower() == RESPONSE_LABEL)

    checks.append(Check(
        "T000", INFO,
        f"{n} paired trials from {n_onsets} onset markers ({n_onsets - n} boundary/unpaired).",
        {"n_trials": n, "n_onsets": n_onsets, "n_key": n_key},
    ))

    if n_key != n:
        checks.append(Check(
            "T001", WARN,
            f"key count {n_key} ≠ paired-trial count {n}. Unpaired responses.",
            {"n_key": n_key, "n_trials": n},
        ))

    for t in trials:
        if t.rt_ms <= 0:
            checks.append(Check(
                "T004", HALT,
                f"Trial {t.trial}: RT ≤ 0 ({t.rt_ms} ms). Marker ordering is broken.",
                {"trial": t.trial, "rt_ms": t.rt_ms},
            ))

    n_fast = sum(1 for t in trials if 0 < t.rt_ms < FAST_RT_MS)
    n_slow = sum(1 for t in trials if t.rt_ms > SLOW_RT_MS)
    if n_fast:
        checks.append(Check("T002", WARN, f"{n_fast} trial(s) with RT < {FAST_RT_MS:.0f} ms.", {"n_fast": n_fast}))
    if n_slow:
        checks.append(Check("T003", WARN, f"{n_slow} trial(s) with RT > {SLOW_RT_MS:.0f} ms.", {"n_slow": n_slow}))

    if trials:
        rts = [t.rt_ms for t in trials]
        checks.append(Check(
            "T005", INFO,
            f"RT median {int(np.median(rts))} ms, range {int(min(rts))}-{int(max(rts))} ms.",
            {"median": float(np.median(rts)), "min": float(min(rts)), "max": float(max(rts))},
        ))

    # Per-block counts
    for blk in sorted({t.block for t in trials}):
        blk_trials = [t for t in trials if t.block == blk]
        n_blk = len(blk_trials)
        n_con = sum(1 for t in blk_trials if t.cond == "con")
        n_inc = sum(1 for t in blk_trials if t.cond == "first")
        if n_blk != 80:
            checks.append(Check(
                "T006", WARN,
                f"Block {blk} trial count {n_blk} ≠ 80.",
                {"block": blk, "n_trials": n_blk},
            ))
        if n_con != 40 or n_inc != 40:
            checks.append(Check(
                "T007", WARN,
                f"Block {blk}: {n_con} congruent / {n_inc} incongruent (expected 40/40).",
                {"block": blk, "n_congruent": n_con, "n_incongruent": n_inc},
            ))

    return checks


# ── S-family: signal (SCOPED TO ANALYSED EPOCHS) ────────────────────────────
def check_signal_scoped(parsed: ParsedEEG, result: PipelineResult) -> List[Check]:
    """Signal checks strictly within analysed epochs (docs §5)."""
    checks: List[Check] = []
    if not parsed.trials:
        return checks

    fs = parsed.sampling_rate
    n_samples_epoch = int(round(EPOCH_TOTAL_S * fs))
    fz = parsed.fz
    c3 = parsed.c3
    total_samples = len(fz)

    n_nan_epochs = 0
    for t in parsed.trials:
        start = t.onset_sample_concat
        end = start + n_samples_epoch
        if end > total_samples:
            checks.append(Check(
                "S001", HALT,
                f"Trial {t.trial} epoch extends past end of recording ({end} > {total_samples}).",
                {"trial": t.trial},
            ))
            continue

        fz_ep = fz[start:end]
        c3_ep = c3[start:end]

        if np.isnan(fz_ep).any() or np.isnan(c3_ep).any():
            n_nan_epochs += 1
            checks.append(Check(
                "S002", HALT,
                f"Trial {t.trial}: NaN inside analysed epoch (dropped from analysis).",
                {"trial": t.trial},
            ))
            continue  # a NaN halts further per-trial signal checks for this trial

        if (np.abs(fz_ep) >= CLIP_THRESHOLD_UV).any() or (np.abs(c3_ep) >= CLIP_THRESHOLD_UV).any():
            checks.append(Check(
                "S003", WARN,
                f"Trial {t.trial}: sample ≥ {CLIP_THRESHOLD_UV} µV in analysed epoch (amplifier rail).",
                {"trial": t.trial},
            ))

        if fz_ep.std() < DEAD_ELECTRODE_SD_UV or c3_ep.std() < DEAD_ELECTRODE_SD_UV:
            checks.append(Check(
                "S004", WARN,
                f"Trial {t.trial}: epoch SD < {DEAD_ELECTRODE_SD_UV} µV — dead electrode?",
                {"trial": t.trial},
            ))

    # INFO-on-pass summary (docs §1.3): report the NaN-drop outcome even when
    # clean, so the reader sees the check ran. Cross-check against what the
    # pipeline actually dropped (parsed.nan_dropped_trials) — they should match.
    n_pipeline_dropped = len(getattr(parsed, "nan_dropped_trials", []))
    checks.append(Check(
        "S000", INFO,
        f"NaN-in-epoch scan: {n_nan_epochs} epoch(s) contain NaN; "
        f"pipeline dropped {n_pipeline_dropped} trial(s) for NaN.",
        {"nan_epochs": n_nan_epochs, "pipeline_dropped": n_pipeline_dropped},
    ))

    return checks


# ── J-family: EEG↔behavioural join ─────────────────────────────────────────
def check_alignment(alignments: Sequence[AlignmentResult]) -> List[Check]:
    checks: List[Check] = []
    for a in alignments:
        matched = len(a.matched_pairs)
        ue = len(a.unmatched_eeg_indices)
        ub = len(a.unmatched_beh_indices)
        if matched < 10:
            checks.append(Check("J001", HALT, f"Block {a.block}: only {matched} matched trials (< 10).", {"block": a.block}))
        if a.rt_correlation < 0.99 or np.isnan(a.rt_correlation):
            checks.append(Check(
                "J002", HALT,
                f"Block {a.block}: RT correlation {a.rt_correlation:.4f} < 0.99.",
                {"block": a.block, "r": float(a.rt_correlation)},
            ))
        if a.congruency_agreement < 1.0 or np.isnan(a.congruency_agreement):
            checks.append(Check(
                "J003", HALT,
                f"Block {a.block}: congruency agreement {a.congruency_agreement*100:.1f}% < 100%.",
                {"block": a.block, "cong_agreement": float(a.congruency_agreement)},
            ))
        if not np.isnan(a.eeg_offset_ms) and (a.eeg_offset_ms < 0 or a.eeg_offset_ms > 100):
            checks.append(Check(
                "J004", WARN,
                f"Block {a.block}: offset {a.eeg_offset_ms:+.1f} ms outside 0–100 ms.",
                {"block": a.block, "offset_ms": float(a.eeg_offset_ms)},
            ))
        if ue or ub:
            checks.append(Check(
                "J006", WARN,
                f"Block {a.block}: EEG has {ue} unmatched, beh has {ub} unmatched.",
                {"block": a.block, "unmatched_eeg": ue, "unmatched_beh": ub},
            ))
        # J000 — INFO summary (emit only if no HALT for this block)
        halted = any(
            c.code in ("J001", "J002", "J003") and c.context.get("block") == a.block
            for c in checks
        )
        if not halted:
            checks.append(Check(
                "J000", INFO,
                f"Block {a.block}: matched {matched}, offset {a.eeg_offset_ms:+.1f} ms, r={a.rt_correlation:.4f}, congruency {a.congruency_agreement*100:.1f}%.",
                {"block": a.block},
            ))
    return checks


# ── D-family: demographics mapping ─────────────────────────────────────────
def check_demographics(demo: Optional[Demographic], subject_parity: Optional[str]) -> List[Check]:
    checks: List[Check] = []
    if demo is None:
        checks.append(Check("D001", HALT, "No demographics row for this participant."))
        return checks
    if demo.aborted:
        checks.append(Check("D003", INFO, "Refresh Rate Condition Ordering = aborted (participant excluded)."))
        return checks
    if not demo.block_order:
        checks.append(Check("D002", HALT, f"Refresh Rate Condition Ordering unparseable: {demo.refresh_rate_ordering!r}."))
        return checks
    b1 = demo.block_order.get(1)
    b2 = demo.block_order.get(2)
    if b1 and b2 and b1 == b2:
        checks.append(Check("D005", HALT, f"Both blocks map to the same refresh rate ({b1})."))

    # D004: if subject_parity is available, check it agrees with block_order
    if subject_parity and b1:
        # In the sample: even parity → First 60 Hz; odd → First 165 Hz
        expected_b1 = "60 Hz" if subject_parity == "even" else "165 Hz"
        if b1 != expected_b1:
            checks.append(Check(
                "D004", WARN,
                f"Demographics ordering (block 1 = {b1}) disagrees with OpenSesame subject_parity={subject_parity!r} "
                f"(would predict block 1 = {expected_b1}).",
                {"parity": subject_parity, "demo_b1": b1, "expected_b1": expected_b1},
            ))
    return checks


# ── Top-level per-subject ──────────────────────────────────────────────────
def run_subject_checks(
    parsed: ParsedEEG,
    result: PipelineResult,
    alignments: Sequence[AlignmentResult] = (),
    beh_session: Optional[BehaviouralSession] = None,
    demographic: Optional[Demographic] = None,
) -> List[Check]:
    """Run every per-subject check family and return the flat list."""
    all_checks: List[Check] = []
    all_checks.extend(check_file_structure(parsed))
    # If E001 fired, everything downstream would crash — bail early.
    if any(c.code == "E001" for c in all_checks):
        return all_checks
    all_checks.extend(check_markers(parsed))
    all_checks.extend(check_trials(parsed))
    all_checks.extend(check_signal_scoped(parsed, result))
    if alignments:
        all_checks.extend(check_alignment(alignments))
    # D-family: run only when demographics are available. Absence of a
    # demographic row can be an expected state (user hasn't uploaded the
    # CSV) — the endpoint decides whether to invoke this.
    if demographic is not None:
        parity = beh_session.trials[0].subject_parity if beh_session and beh_session.trials else None
        all_checks.extend(check_demographics(demographic, parity))
    return all_checks


def checks_to_payload(checks: Iterable[Check]) -> List[dict]:
    """Serialise Check records for the JSON API."""
    return [
        {"code": c.code, "level": c.level, "message": c.message, "context": c.context}
        for c in checks
    ]
