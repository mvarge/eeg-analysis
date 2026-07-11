"""
OpenSesame behavioural CSV parser + EEG↔behavioural alignment.

See docs/DATA_REFERENCE.md §4 and §6.

Handles all three known task-program variants (standard, no-practice,
second-only) with a single reader that selects columns by NAME, never
by position. Only nine columns are required (§4.2); everything else is
ignored.

Behavioural files can be split across two CSVs for one participant
(S3P006). This module reads a *list* of files; rows from each file are
concatenated in the given order, and block labels come from each row's
own `stage` column — so the join is a no-op if the second file is
already the second-only variant.

Alignment (§6):

  * Positional join is unsafe: any EEG trial dropout desyncs everything
    after it.
  * The correct key is `response_time`, offset by a per-block constant
    (median EEG RT − median behavioural RT), matched greedily with a
    tolerance and requiring congruency to agree.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from logging_setup import get_logger

logger = get_logger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────
REQUIRED_COLUMNS = (
    "stage",
    "congruent",
    "correct",
    "response_time",
    "response",
    "correct_response",
    "subject_nr",
    "subject_parity",
)

# Optional columns — useful when present, silently absent otherwise.
# `live_row` is the OpenSesame per-block trial index (0-79, resets each block);
# it gives the canonical flanker "task number" the reviewer navigates by.
OPTIONAL_COLUMNS = ("flankers", "targets", "live_row")

STAGE_BLOCK_MAP = {"first": 1, "second": 2}
TRIALS_PER_BLOCK = 80

# Alignment defaults. Overridable via the align function's parameters.
DEFAULT_RT_TOLERANCE_MS = 20.0


# ── Data classes ───────────────────────────────────────────────────────────
@dataclass
class BehaviouralTrial:
    """One row of the OpenSesame CSV, projected to the columns that matter."""
    row_index: int          # 0-based row index across all input files, in load order
    source_file: str        # filename this row came from
    block: int              # 1 for stage=='first', 2 for stage=='second'
    stage_raw: str          # original stage value (for diagnostics)
    congruent: bool         # True if congruent==1
    correct: bool           # True if correct==1
    response_time_ms: float
    response: str
    correct_response: str
    subject_nr: int
    subject_parity: str
    flankers: Optional[str] = None
    targets: Optional[str] = None
    live_row: Optional[int] = None  # OpenSesame per-block trial index (0-79), if present

    @property
    def task_number(self) -> Optional[int]:
        """Canonical 1-based flanker task number across both blocks (1-160).

        Derived from the OpenSesame per-block `live_row` (0-based, resets each
        block) plus the block offset. Returns None when `live_row` is absent OR
        when it falls outside the expected [0, TRIALS_PER_BLOCK) range or the
        block is not 1/2 — rather than silently emitting a number > 160. A
        restarted/aborted flanker run can push live_row past 79; such rows are
        flagged by `validate_task_numbering` and get no canonical number here.
        """
        if self.live_row is None:
            return None
        if self.block not in (1, 2):
            return None
        if not (0 <= self.live_row < TRIALS_PER_BLOCK):
            return None
        return (self.block - 1) * TRIALS_PER_BLOCK + self.live_row + 1


@dataclass
class BehaviouralFile:
    """Parsed contents of one behavioural CSV."""
    filename: str
    variant: str            # 'standard', 'no-practice', 'second-only', or 'unknown'
    n_rows: int
    trials: List[BehaviouralTrial]
    warnings: List[str] = field(default_factory=list)


@dataclass
class BehaviouralSession:
    """One participant's behavioural data, concatenated from 1+ files."""
    subject_nr: int
    files: List[BehaviouralFile]
    trials: List[BehaviouralTrial]
    warnings: List[str] = field(default_factory=list)


# ── CSV reading ────────────────────────────────────────────────────────────
def _sniff_variant(header: Sequence[str]) -> str:
    """Classify an OpenSesame CSV by column set (§4.4)."""
    cols = set(header)
    if "live_row_exp_2_loop" in cols and "live_row_exp_1_loop" not in cols:
        return "second-only"
    if "live_row_practice_loop" not in cols:
        return "no-practice"
    return "standard"


def _require_columns(header: Sequence[str]) -> dict:
    """Return {name: column_index} for every required column; raise if any missing."""
    idx = {}
    header_list = list(header)
    for name in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
        if name in header_list:
            idx[name] = header_list.index(name)
    missing = [c for c in REQUIRED_COLUMNS if c not in idx]
    if missing:
        raise ValueError(
            f"OpenSesame CSV is missing required columns: {missing}. "
            f"Header has {len(header_list)} columns."
        )
    return idx


def _to_float(x: str, field_name: str, row_idx: int) -> float:
    try:
        return float(x)
    except ValueError:
        raise ValueError(
            f"Row {row_idx}: non-numeric {field_name!r}: {x!r}"
        )


def _to_int(x: str, field_name: str, row_idx: int) -> int:
    try:
        return int(float(x))  # tolerate '1.0' if present
    except ValueError:
        raise ValueError(
            f"Row {row_idx}: non-integer {field_name!r}: {x!r}"
        )


def parse_behavioural_csv(
    csv_bytes: bytes,
    filename: str,
    starting_row_index: int = 0,
) -> BehaviouralFile:
    """Parse a single OpenSesame CSV.

    Read latin-1 (§4.1). Selects columns by name and tolerates any of
    the three task-program variants. Rows whose `stage` is not `first`
    or `second` are skipped (practice/warm-up rows should not appear in
    the exported CSV, but be defensive).
    """
    text = csv_bytes.decode("latin-1", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(f"{filename}: file is empty.")

    variant = _sniff_variant(header)
    idx = _require_columns(header)

    trials: List[BehaviouralTrial] = []
    warnings: List[str] = []
    row_index = starting_row_index

    for local_row, row in enumerate(reader, start=2):  # start=2 → matches CSV line #
        if not row or all(not c.strip() for c in row):
            continue
        # Guard against ragged rows
        if len(row) <= max(idx[c] for c in REQUIRED_COLUMNS):
            warnings.append(f"{filename}:L{local_row}: short row ({len(row)} cols); skipped.")
            continue

        stage_raw = row[idx["stage"]].strip().lower()
        if stage_raw not in STAGE_BLOCK_MAP:
            # Not a real trial (practice, break, etc.) — skip silently.
            continue

        try:
            trial = BehaviouralTrial(
                row_index=row_index,
                source_file=filename,
                block=STAGE_BLOCK_MAP[stage_raw],
                stage_raw=stage_raw,
                congruent=_to_int(row[idx["congruent"]], "congruent", local_row) == 1,
                correct=_to_int(row[idx["correct"]], "correct", local_row) == 1,
                response_time_ms=_to_float(row[idx["response_time"]], "response_time", local_row),
                response=row[idx["response"]].strip(),
                correct_response=row[idx["correct_response"]].strip(),
                subject_nr=_to_int(row[idx["subject_nr"]], "subject_nr", local_row),
                subject_parity=row[idx["subject_parity"]].strip().lower(),
                flankers=row[idx["flankers"]] if "flankers" in idx else None,
                targets=row[idx["targets"]] if "targets" in idx else None,
                live_row=(
                    _to_int(row[idx["live_row"]], "live_row", local_row)
                    if "live_row" in idx and row[idx["live_row"]].strip() != ""
                    else None
                ),
            )
        except ValueError as e:
            warnings.append(f"{filename}:L{local_row}: {e}; row skipped.")
            continue

        trials.append(trial)
        row_index += 1

    return BehaviouralFile(
        filename=filename,
        variant=variant,
        n_rows=len(trials),
        trials=trials,
        warnings=warnings,
    )


def parse_behavioural_session(
    files: Iterable[tuple[bytes, str]],
) -> BehaviouralSession:
    """Parse one or more behavioural CSVs for a single participant.

    `files` is an ordered iterable of (bytes, filename) tuples. Rows
    from all files are concatenated in order; block labels come from
    each row's own `stage` column.

    Raises ValueError if the files disagree on `subject_nr`.
    """
    parsed: List[BehaviouralFile] = []
    all_trials: List[BehaviouralTrial] = []
    warnings: List[str] = []

    running_row_idx = 0
    for content, filename in files:
        bf = parse_behavioural_csv(content, filename, starting_row_index=running_row_idx)
        parsed.append(bf)
        all_trials.extend(bf.trials)
        running_row_idx += bf.n_rows
        warnings.extend(bf.warnings)

    if not all_trials:
        raise ValueError("No behavioural trials parsed across any input file.")

    # Verify subject_nr agreement across all rows.
    subject_nrs = {t.subject_nr for t in all_trials}
    if len(subject_nrs) > 1:
        raise ValueError(
            f"Behavioural files disagree on subject_nr: {sorted(subject_nrs)}. "
            "Do not merge files from different participants."
        )
    subject_nr = next(iter(subject_nrs))

    # Advisory if the block distribution looks wrong.
    n_b1 = sum(1 for t in all_trials if t.block == 1)
    n_b2 = sum(1 for t in all_trials if t.block == 2)
    if n_b1 == 0:
        warnings.append("Behavioural session has no block-1 rows (stage=='first').")
    if n_b2 == 0:
        warnings.append("Behavioural session has no block-2 rows (stage=='second').")

    # Validate that per-block row counts / live_row ranges support canonical
    # 1-160 numbering. A restarted or aborted flanker run yields extra/short
    # blocks or out-of-range live_row values that would otherwise misnumber or
    # push Task # past 160 — flag these instead of computing silently.
    warnings.extend(validate_task_numbering(all_trials))

    return BehaviouralSession(
        subject_nr=subject_nr,
        files=parsed,
        trials=all_trials,
        warnings=warnings,
    )


def validate_task_numbering(trials: List["BehaviouralTrial"]) -> List[str]:
    """Check that behavioural trials support canonical 1-160 numbering.

    Returns a list of human-readable warnings (empty when everything is clean).
    Flags, per block: row count != TRIALS_PER_BLOCK, live_row values outside
    [0, TRIALS_PER_BLOCK), duplicate live_row values, and any resulting Task #
    that would exceed the 160 ceiling. Does not raise — numbering degrades to a
    None (amber-* fallback) for the offending rows via `task_number`.
    """
    warnings: List[str] = []
    by_block: dict[int, List["BehaviouralTrial"]] = {}
    for t in trials:
        by_block.setdefault(t.block, []).append(t)

    n_expected_max = 0
    for blk in sorted(by_block):
        block_trials = by_block[blk]
        n = len(block_trials)
        if blk in (1, 2):
            n_expected_max = max(n_expected_max, blk)
        if n != TRIALS_PER_BLOCK:
            warnings.append(
                f"Block {blk} has {n} behavioural rows (expected {TRIALS_PER_BLOCK}); "
                "task numbering for this block may be unreliable — verify the "
                "flanker file was not restarted/aborted."
            )
        live_rows = [t.live_row for t in block_trials if t.live_row is not None]
        oor = [lr for lr in live_rows if not (0 <= lr < TRIALS_PER_BLOCK)]
        if oor:
            warnings.append(
                f"Block {blk} has {len(oor)} row(s) with live_row outside "
                f"0-{TRIALS_PER_BLOCK - 1} (e.g. {sorted(set(oor))[:5]}); these get no "
                "canonical Task # (shown with the amber * fallback)."
            )
        dupes = sorted({lr for lr in live_rows if live_rows.count(lr) > 1})
        if dupes:
            warnings.append(
                f"Block {blk} has duplicate live_row value(s) {dupes[:5]}; "
                "restarted flanker run suspected — task numbers may collide."
            )

    # Extra blocks beyond 1/2 cannot be mapped into the 1-160 scheme.
    extra_blocks = [b for b in by_block if b not in (1, 2)]
    if extra_blocks:
        warnings.append(
            f"Behavioural session has unexpected block label(s) {sorted(extra_blocks)}; "
            "only blocks 1 and 2 map to canonical Task # 1-160."
        )

    # Ceiling guard: no emitted task_number should exceed 2*TRIALS_PER_BLOCK.
    ceiling = 2 * TRIALS_PER_BLOCK
    over = [t.task_number for t in trials
            if t.task_number is not None and t.task_number > ceiling]
    if over:
        warnings.append(
            f"{len(over)} trial(s) computed a Task # above {ceiling}; capped to the "
            "amber * fallback to avoid misnumbering."
        )
    return warnings


# ── Alignment (§6) ─────────────────────────────────────────────────────────
@dataclass
class AlignmentResult:
    """Output of `align_eeg_to_behaviour` for one block."""
    block: int
    eeg_offset_ms: float                    # median(EEG_rt) − median(beh_rt); constant trigger latency
    matched_pairs: List[tuple[int, int]]    # list of (eeg_trial_idx, behavioural_trial_idx)
    unmatched_eeg_indices: List[int]        # EEG trials that had no beh row within tolerance
    unmatched_beh_indices: List[int]        # beh rows that had no EEG counterpart (dropouts)
    rt_correlation: float                   # Pearson r across matched pairs (behavioural RT vs adjusted EEG RT)
    congruency_agreement: float             # 0–1 fraction of matched pairs whose congruency agrees
    rt_residual_ms: float = float("nan")    # median |adjusted EEG RT − beh RT| across matched pairs (§6 residual)

    # ── J-code gate helpers (docs §7) ──────────────────────────────────────
    def passes_gate(
        self,
        min_matched: int = 10,
        min_r: float = 0.99,
        min_congruency: float = 1.0,
    ) -> bool:
        """True iff this block's alignment clears the HALT-level J gates
        (J001 matched<min, J002 r<min, J003 congruency<100%). Offset-range
        (J004) and count-mismatch (J006) are WARN-only and do not gate.
        Used by block SELECTION (Task 3) to decide whether a candidate
        segment is the real block."""
        import math as _math
        if len(self.matched_pairs) < min_matched:
            return False
        if _math.isnan(self.rt_correlation) or self.rt_correlation < min_r:
            return False
        if _math.isnan(self.congruency_agreement) or self.congruency_agreement < min_congruency:
            return False
        return True


def _pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def _median(values: Sequence[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def align_block(
    eeg_rts_ms: Sequence[float],
    eeg_congruent: Sequence[bool],
    beh_trials: Sequence[BehaviouralTrial],
    block: int,
    tolerance_ms: float = DEFAULT_RT_TOLERANCE_MS,
) -> AlignmentResult:
    """Align one block's EEG trials to its behavioural rows (§6.2).

    Algorithm:
      1. Estimate the block-constant offset: median(EEG_rt) − median(beh_rt).
      2. Adjust EEG RTs by subtracting the offset.
      3. Walk through EEG trials in order; for each, look at the next
         un-matched behavioural row within `tolerance_ms` whose congruency
         agrees. If none within tolerance, mark the EEG trial unmatched
         and DO NOT advance the behavioural pointer (a dropout on the
         beh side is impossible; a dropout on the EEG side skips a beh row).
      4. Any behavioural rows not matched at the end are counted as
         "EEG dropouts" (behavioural has that trial, EEG lost it).

    Returns an AlignmentResult with metrics per §6.
    """
    if len(eeg_rts_ms) != len(eeg_congruent):
        raise ValueError("eeg_rts_ms and eeg_congruent must be same length.")

    beh_block = [t for t in beh_trials if t.block == block]

    if not eeg_rts_ms or not beh_block:
        return AlignmentResult(
            block=block,
            eeg_offset_ms=float("nan"),
            matched_pairs=[],
            unmatched_eeg_indices=list(range(len(eeg_rts_ms))),
            unmatched_beh_indices=[i for i, _ in enumerate(beh_block)],
            rt_correlation=float("nan"),
            congruency_agreement=float("nan"),
        )

    offset = _median(list(eeg_rts_ms)) - _median([t.response_time_ms for t in beh_block])

    matched: List[tuple[int, int]] = []
    unmatched_eeg: List[int] = []
    beh_used = [False] * len(beh_block)
    beh_ptr = 0  # first still-unmatched beh row

    for eeg_i, (rt_e, cong_e) in enumerate(zip(eeg_rts_ms, eeg_congruent)):
        rt_e_adj = rt_e - offset
        # Skip past any already-used beh rows
        while beh_ptr < len(beh_block) and beh_used[beh_ptr]:
            beh_ptr += 1
        # Scan forward from beh_ptr for the first unused row that agrees
        # on both RT (within tolerance) and congruency. Any rows skipped
        # over become "EEG dropouts" (beh has them; EEG lost them).
        matched_here: Optional[int] = None
        for probe in range(beh_ptr, len(beh_block)):
            if beh_used[probe]:
                continue
            t = beh_block[probe]
            if t.congruent != cong_e:
                continue
            if abs(rt_e_adj - t.response_time_ms) > tolerance_ms:
                continue
            matched_here = probe
            break
        if matched_here is None:
            unmatched_eeg.append(eeg_i)
        else:
            matched.append((eeg_i, matched_here))
            beh_used[matched_here] = True
            beh_ptr = matched_here + 1

    unmatched_beh = [i for i, used in enumerate(beh_used) if not used]

    if matched:
        eeg_rts = [eeg_rts_ms[e] - offset for e, _ in matched]
        beh_rts = [beh_block[b].response_time_ms for _, b in matched]
        rt_r = _pearson_r(eeg_rts, beh_rts)
        cong_agree = sum(
            1 for e, b in matched if eeg_congruent[e] == beh_block[b].congruent
        ) / len(matched)
        # Median absolute residual after removing the constant offset.
        # On a correct join this is a couple of ms (§6 acceptance: ≤ ~2 ms).
        rt_residual = _median([abs(e_rt - b_rt) for e_rt, b_rt in zip(eeg_rts, beh_rts)])
    else:
        rt_r = float("nan")
        cong_agree = float("nan")
        rt_residual = float("nan")

    # Note: unmatched_beh_indices are indices into the *filtered* beh_block
    # list. Callers who want row indices in the original beh_trials list
    # should map through beh_block[i].row_index.
    return AlignmentResult(
        block=block,
        eeg_offset_ms=offset,
        matched_pairs=matched,
        unmatched_eeg_indices=unmatched_eeg,
        unmatched_beh_indices=unmatched_beh,
        rt_correlation=rt_r,
        congruency_agreement=cong_agree,
        rt_residual_ms=rt_residual,
    )
