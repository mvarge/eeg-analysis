"""
LabChart EEG text export parser (segment-aware).

Handles both clean recordings (one 6-line header, then data) and pathological
files where the recording was stopped and restarted mid-experiment — LabChart
writes a fresh 6-line header at the join and the sample clock restarts at 0.

Key behaviours mandated by docs/DATA_REFERENCE.md:

  * latin-1 encoding, CRLF line endings, tab-delimited data.
  * A file is a SEQUENCE OF SEGMENTS. Each segment starts with a 6-line
    header. Data rows within a segment carry `time` relative to that
    segment's t=0.
  * Header parsing is case-insensitive and whitespace-tolerant. Channel
    names may appear as `EEG Fz-Pz` in one header and `EEG FZ-PZ` in
    another within the same file — both are matched.
  * A global timeline is reconstructed from each segment's ExcelDateTime
    (Excel serial, days since 1899-12-30). Per-segment monotonicity is
    verified. Across-segment time is derived, never taken from the
    per-segment `time` column.
  * Markers keep their global time (for cross-segment reasoning) and
    per-segment sample index (for slicing the correct segment's array).
  * The `first` marker is overloaded (incongruent onset + block-1
    boundary). Trial pairing uses "onset followed by `key` before the
    next onset marker", NEVER positional heuristics.
  * Blocks are identified by the `second`-marker rule primarily, with
    the >30 s marker-gap rule as fallback for files without any
    `second` marker (block-1-only recordings).

Regression-tested against:
  * S1P002 clean single-segment file → 160 trials (80/80).
  * Simulated block-1-only truncation → 80 trials (40/40), NOT 81.
  * Simulated multi-header file → correct global timeline; per-segment
    monotonicity holds; no block-detection false positive from the
    -327s negative "gap" at the join.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import StringIO
from typing import List, Optional, Tuple

import numpy as np

from logging_setup import get_logger

logger = get_logger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────
BLOCK_GAP_S = 30.0                 # marker gap that separates the two blocks
EXCEL_EPOCH = datetime(1899, 12, 30)  # Excel's day-0 (accounts for 1900 leap-year bug)

# Expected channel names, normalised (lowercase, whitespace collapsed, "EEG " prefix stripped)
EXPECTED_CH_NORMS = ("fz-pz", "c3-c4")

# Real trial onsets and boundary onsets both use these labels
ONSET_LABELS = ("con", "first")
RESPONSE_LABEL = "key"
BLOCK2_LABEL = "second"
END_TOKEN_RE = re.compile(r"^\**\s*END\s*\**$", re.IGNORECASE)


# ── Data classes ───────────────────────────────────────────────────────────
@dataclass
class SegmentHeader:
    """The parsed contents of one 6-line LabChart header."""
    interval_s: float                     # sample interval in seconds
    sampling_rate: float                  # 1/interval_s
    excel_datetime: Optional[float]       # Excel serial (days since 1899-12-30), or None if unparseable
    date_text: str                        # human-readable date string from the header
    time_format: str                      # e.g. "StartOfBlock"
    date_format: str
    channel_names_raw: List[str]          # exactly as they appear in the file
    range_text: List[str]                 # exactly as they appear (info only; data are always µV)
    line_start: int                       # 0-indexed file line number where "Interval=" appears
    line_end: int                         # 0-indexed file line number of last header line (inclusive)


@dataclass
class Segment:
    """One contiguous recording block (bounded by headers or file end)."""
    index: int                            # 0-based segment index within the file
    header: SegmentHeader
    fz: np.ndarray                        # shape (n_samples,), µV
    c3: np.ndarray                        # shape (n_samples,), µV
    local_time: np.ndarray                # shape (n_samples,), seconds relative to segment start
    markers: List["Marker"] = field(default_factory=list)
    time_start_global: float = 0.0        # seconds since first segment's ExcelDateTime start
    monotonic: bool = True                # False if per-segment time is NOT strictly increasing
    uniform_interval: bool = True         # False if any diff(time) deviates by >5% from interval_s

    @property
    def sample_count(self) -> int:
        return self.fz.shape[0]

    @property
    def duration_s(self) -> float:
        return self.local_time[-1] - self.local_time[0] if self.sample_count else 0.0


@dataclass
class Marker:
    """A single event marker from the recording (with both local and global time)."""
    label: str                            # 'con', 'first', 'key', 'second', 'END', ...
    segment_index: int                    # which segment this marker belongs to
    sample_index_local: int               # index into that segment's fz/c3 arrays
    time_local: float                     # seconds within its segment (starts at 0 per segment)
    time_global: float                    # seconds since first segment's ExcelDateTime start
    raw_comment: str                      # unmodified comment field, for diagnostics


@dataclass
class Trial:
    """A stimulus onset paired with the participant's response."""
    trial: int                            # 1-based, across the whole recording
    btrial: int                           # 1-based, within its block
    block: int                            # 1 or 2 (rarely more if aborted/restarted; caller must decide)
    cond: str                             # 'con' or 'first'
    onset: float                          # global time (s) of the onset marker
    key: float                            # global time (s) of the paired `key`
    rt_ms: int                            # (key - onset) * 1000, rounded
    segment_index: int                    # segment the onset lives in
    onset_sample_local: int               # sample index of onset within its segment
    onset_sample_concat: int              # sample index of onset in the CONCATENATED fz/c3 arrays


@dataclass
class ParsedEEG:
    """Complete parsed result from a LabChart file.

    Backwards-compatible attributes (fz, c3, markers, trials, n_blocks,
    sampling_rate, recording_date, channel_names, filename) are exposed at
    the top level. Multi-segment metadata lives in `segments`.
    """
    filename: str
    recording_date: str
    sampling_rate: float
    channel_names: List[str]              # normalised, from the FIRST segment header
    fz: np.ndarray                        # concatenation of all segments' fz (in file order)
    c3: np.ndarray                        # concatenation of all segments' c3
    markers: List[Marker]                 # all markers, in the order they appear
    trials: List[Trial]                   # paired trials only
    n_blocks: int                         # 1 or 2 (occasionally more before segment selection)
    segments: List[Segment]               # per-segment detail
    # Byte-for-byte diagnostics available on request
    warnings: List[str] = field(default_factory=list)
    # Cluster metadata (one entry per marker cluster detected before
    # committing to blocks). Used by the B-family validity checks to
    # detect ambiguous or split recordings.
    cluster_meta: List[dict] = field(default_factory=list)
    # Trials dropped by the pipeline because their analysed window overlapped
    # a NaN/Inf sample (work-order Task 1). Populated by run_pipeline; read by
    # the S002 validity check. Each entry: {trial, block, cond, onset_sample}.
    nan_dropped_trials: List[dict] = field(default_factory=list)
    # Block-selection B-codes (work-order Task 3). Populated by the server
    # after select_blocks(); each entry is (code, level, message). Read by
    # the checks payload so B000/B001/B002 surface in the validity panel.
    block_codes: List[tuple] = field(default_factory=list)
    # Adaptive-blink slow-band inputs (work-order Task 4). Populated by
    # run_pipeline; read by the S007 check (missed small blinks). One
    # slow-band peak-to-peak value (µV) per analysed trial, plus the frozen
    # median+K·MAD threshold that recording used.
    blink_slow_ptp: List[float] = field(default_factory=list)
    blink_threshold_uv: float = 0.0

    @property
    def n_segments(self) -> int:
        return len(self.segments)


# ── Utility ────────────────────────────────────────────────────────────────
def _normalise_channel_name(raw: str) -> str:
    """Normalise a channel-title cell to a canonical key.

    Strips optional 'EEG ' prefix, collapses whitespace, lowercases.
    'EEG Fz-Pz ' → 'fz-pz'; 'EEG FZ-PZ' → 'fz-pz'.
    """
    s = raw.strip()
    if s.lower().startswith("eeg "):
        s = s[4:]
    return re.sub(r"\s+", "", s).lower()


def _excel_serial_to_datetime(serial: float) -> datetime:
    """Excel serial (days since 1899-12-30) → Python datetime."""
    return EXCEL_EPOCH + timedelta(days=serial)


def _extract_marker_label(comment: str) -> str:
    """Extract a marker's short label from a LabChart comment field.

    Comments look like:
      "#1 con"
      "#1 key #2 con"
      "#1 ******** END ******** #2 ******** END ********"
      "con"

    Strategy: strip all "#N" prefix tokens, take the first non-empty run.
    Special-case the END sentinel so callers see it as a single logical marker.
    """
    # Remove all "#<digits>" tokens.
    cleaned = re.sub(r"#\d+", " ", comment).strip()
    if not cleaned:
        return ""
    # Check for END sentinel first (may be wrapped in asterisks/spaces)
    if END_TOKEN_RE.match(cleaned) or "END" in cleaned.upper() and set(cleaned) <= set("* END\t "):
        return "END"
    # Take the first whitespace-delimited token
    return cleaned.split()[0]


# ── Header parsing ─────────────────────────────────────────────────────────
_HEADER_KEY_RE = re.compile(r"^([A-Za-z]+)=", )


def _parse_one_header(lines: List[str], start: int) -> Tuple[Optional[SegmentHeader], int]:
    """Parse a 6-line LabChart header starting at `lines[start]`.

    Returns (header, next_line_index). Returns (None, start) if the block at
    `start` doesn't look like a header.

    LabChart headers always have this shape:
        Interval=       <value>
        ExcelDateTime=  <serial> <human_date>
        TimeFormat=     <text>
        DateFormat=     <text or empty>
        ChannelTitle=   <name1> <name2>
        Range=          <val1> <val2>
    """
    if start + 6 > len(lines):
        return None, start
    # Must start with "Interval="
    if not lines[start].lstrip().lower().startswith("interval="):
        return None, start

    header_lines = lines[start : start + 6]
    kv = {}
    for ln in header_lines:
        m = _HEADER_KEY_RE.match(ln.lstrip())
        if not m:
            continue
        key = m.group(1).lower()
        parts = ln.rstrip("\r\n").split("\t")
        # value tokens are everything after the "Key=" part
        rest = parts[1:] if len(parts) > 1 else []
        kv[key] = [p.strip() for p in rest]

    # Interval
    interval_s = 0.0025  # default 400 Hz
    if "interval" in kv and kv["interval"]:
        try:
            # value may be "0.0025 s" or "0.0025"
            raw = kv["interval"][0].split()[0]
            interval_s = float(raw)
        except (ValueError, IndexError):
            pass
    sampling_rate = 1.0 / interval_s if interval_s > 0 else 400.0

    # ExcelDateTime
    excel_dt = None
    date_text = "Unknown"
    if "exceldatetime" in kv and kv["exceldatetime"]:
        try:
            excel_dt = float(kv["exceldatetime"][0])
        except ValueError:
            excel_dt = None
        if len(kv["exceldatetime"]) >= 2:
            date_text = kv["exceldatetime"][1]

    time_format = kv.get("timeformat", [""])[0] if kv.get("timeformat") else ""
    date_format = kv.get("dateformat", [""])[0] if kv.get("dateformat") else ""

    channel_names_raw = kv.get("channeltitle", [])
    range_text = kv.get("range", [])

    return (
        SegmentHeader(
            interval_s=interval_s,
            sampling_rate=sampling_rate,
            excel_datetime=excel_dt,
            date_text=date_text,
            time_format=time_format,
            date_format=date_format,
            channel_names_raw=channel_names_raw,
            range_text=range_text,
            line_start=start,
            line_end=start + 5,
        ),
        start + 6,
    )


# ── Data-row parsing ───────────────────────────────────────────────────────
def _parse_data_rows(
    lines: List[str], start: int, end: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[Tuple[int, float, str]]]:
    """Parse a contiguous block of data rows (lines[start:end]).

    Returns (time_arr, fz_arr, c3_arr, raw_markers) where raw_markers is a list of
    (sample_index_local, time_local, raw_comment) tuples for rows with a comment.

    Values that fail to parse as float (including the LabChart-emitted string "NaN")
    are stored as np.nan. This mirrors what LabChart actually writes.
    """
    times: List[float] = []
    fz_vals: List[float] = []
    c3_vals: List[float] = []
    raw_markers: List[Tuple[int, float, str]] = []

    def _to_float(s: str) -> float:
        s = s.strip()
        if not s:
            return np.nan
        try:
            return float(s)
        except ValueError:
            # LabChart writes literal "NaN" during amplifier saturation.
            if s.lower() == "nan":
                return np.nan
            return np.nan

    for ln in lines[start:end]:
        # A line beginning with "Interval=" would be a new header — stop.
        if ln.lstrip().lower().startswith("interval="):
            break
        parts = ln.rstrip("\r\n").split("\t")
        if len(parts) < 3:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            # Not a data row at all; skip silently.
            continue
        a = _to_float(parts[1])
        b = _to_float(parts[2])
        idx = len(times)
        times.append(t)
        fz_vals.append(a)
        c3_vals.append(b)
        # 4th+ column is optional comment
        if len(parts) >= 4:
            comment = "\t".join(parts[3:]).strip()
            if comment:
                raw_markers.append((idx, t, comment))

    return (
        np.asarray(times, dtype=np.float64),
        np.asarray(fz_vals, dtype=np.float64),
        np.asarray(c3_vals, dtype=np.float64),
        raw_markers,
    )


# ── Top-level ──────────────────────────────────────────────────────────────
def parse_labchart(filepath: str) -> ParsedEEG:
    """Parse a single LabChart 8 text export.

    Handles single-segment and multi-segment files. See module docstring.
    """
    return parse_labchart_multi([filepath])


def parse_labchart_multi(filepaths: List[str]) -> ParsedEEG:
    """Parse one or more LabChart .txt exports and merge them into a single
    ParsedEEG as if they were one recording.

    Use this when a participant's recording is split across multiple files
    (e.g. S8P025(1).txt + S8P025(2).txt — one participant, two files).

    Each file may itself contain multiple segments (mid-file restart).
    Segments from all files are concatenated in the order the files are
    given. Global time uses the FIRST file's first segment as t=0; every
    subsequent segment (across all files) is placed on that timeline via
    its own ExcelDateTime.

    The ParsedEEG's `filename` becomes the first file's filename; use
    subject_id resolution upstream if you need a canonical ID.
    """
    if not filepaths:
        raise ValueError("parse_labchart_multi requires at least one file path.")
    logger.debug("parse_labchart_multi: %d input file(s)", len(filepaths))

    filename = filepaths[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    warnings: List[str] = []
    segments: List[Segment] = []
    first_excel_dt: Optional[float] = None

    seg_index = 0

    for fp_idx, filepath in enumerate(filepaths):
        with open(filepath, "r", encoding="latin-1", newline="") as fh:
            lines = fh.read().splitlines()

        i = 0
        segments_this_file = 0

        while i < len(lines):
            # Skip any blank/whitespace-only lines between segments
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines):
                break

            # Try to parse a header at this position
            header, after_header = _parse_one_header(lines, i)
            if header is None:
                # Not a header — if this is the very first attempt across all
                # files, that's a hard failure; otherwise stray trailing text.
                if not segments:
                    raise ValueError(
                        f"No parseable LabChart header at line {i} in "
                        f"{filepath!r}. File is not a LabChart export, or "
                        "the encoding is wrong."
                    )
                break

            # Find where the next header starts (or end of file)
            next_header_line = len(lines)
            for j in range(after_header, len(lines)):
                if lines[j].lstrip().lower().startswith("interval="):
                    next_header_line = j
                    break

            # Parse the data rows for this segment
            time_arr, fz_arr, c3_arr, raw_markers = _parse_data_rows(
                lines, after_header, next_header_line
            )

            # Track first segment's ExcelDateTime for global-time reconstruction
            if first_excel_dt is None and header.excel_datetime is not None:
                first_excel_dt = header.excel_datetime

            # Compute this segment's start in the global timeline
            if first_excel_dt is not None and header.excel_datetime is not None:
                time_start_global = (header.excel_datetime - first_excel_dt) * 86400.0
            else:
                time_start_global = (
                    segments[-1].time_start_global + segments[-1].duration_s
                    if segments else 0.0
                )

            monotonic = True
            uniform_interval = True
            if time_arr.size > 1:
                diffs = np.diff(time_arr)
                monotonic = bool(np.all(diffs > 0))
                expected = header.interval_s
                if expected > 0:
                    uniform_interval = bool(np.all(np.abs(diffs - expected) < 0.05 * expected))

            if not monotonic:
                warnings.append(
                    f"Segment {seg_index} ({filepath.rsplit('/', 1)[-1]}): "
                    "local time vector is NOT monotonic. "
                    "This will corrupt any bisect/searchsorted logic. "
                    "Check for corrupted/re-ordered rows."
                )
            if not uniform_interval:
                warnings.append(
                    f"Segment {seg_index} ({filepath.rsplit('/', 1)[-1]}): "
                    f"sample interval is not uniform "
                    f"(expected {header.interval_s:.4f} s). Possible dropped samples."
                )

            segment_markers: List[Marker] = []
            for sample_idx_local, t_local, comment in raw_markers:
                label = _extract_marker_label(comment)
                if not label:
                    continue
                segment_markers.append(
                    Marker(
                        label=label,
                        segment_index=seg_index,
                        sample_index_local=sample_idx_local,
                        time_local=t_local,
                        time_global=time_start_global + t_local,
                        raw_comment=comment,
                    )
                )

            segments.append(
                Segment(
                    index=seg_index,
                    header=header,
                    fz=fz_arr,
                    c3=c3_arr,
                    local_time=time_arr,
                    markers=segment_markers,
                    time_start_global=time_start_global,
                    monotonic=monotonic,
                    uniform_interval=uniform_interval,
                )
            )

            seg_index += 1
            segments_this_file += 1
            i = next_header_line

        if segments_this_file == 0:
            raise ValueError(f"No parseable segments in {filepath!r}.")

    if not segments:
        raise ValueError("No parseable data segments across any input file.")

    # ── Assemble top-level (concatenated) arrays and marker list ──────────
    fz_all = np.concatenate([s.fz for s in segments]) if segments else np.zeros(0)
    c3_all = np.concatenate([s.c3 for s in segments]) if segments else np.zeros(0)

    # Sample-index offset of each segment within the concatenated array.
    segment_concat_offsets: List[int] = []
    running = 0
    for s in segments:
        segment_concat_offsets.append(running)
        running += s.sample_count

    all_markers: List[Marker] = []
    for s in segments:
        all_markers.extend(s.markers)
    all_markers.sort(key=lambda m: (m.segment_index, m.time_local))

    # Channel-name check (case-insensitive)
    first_header = segments[0].header
    ch_norms = [_normalise_channel_name(x) for x in first_header.channel_names_raw]
    channel_names = list(first_header.channel_names_raw[:2]) or ["Fz-Pz", "C3-C4"]
    if len(ch_norms) < 2 or ch_norms[0] != EXPECTED_CH_NORMS[0] or ch_norms[1] != EXPECTED_CH_NORMS[1]:
        warnings.append(
            f"Unexpected channel names (normalised): {ch_norms}. "
            f"Expected {list(EXPECTED_CH_NORMS)}."
        )

    # Multi-header advisory
    if len(segments) > 1:
        warnings.append(
            f"File contains {len(segments)} segments (recording was stopped and "
            "restarted). Global timeline reconstructed from ExcelDateTime; "
            "per-segment time vectors verified monotonic."
        )

    # ── Trial pairing ────────────────────────────────────────────────────
    trials, cluster_meta = _pair_trials(all_markers, segment_concat_offsets)
    n_blocks = len({t.block for t in trials}) if trials else 0

    return ParsedEEG(
        filename=filename,
        recording_date=first_header.date_text,
        sampling_rate=first_header.sampling_rate,
        channel_names=channel_names,
        fz=fz_all,
        c3=c3_all,
        markers=all_markers,
        trials=trials,
        n_blocks=n_blocks,
        segments=segments,
        warnings=warnings,
        cluster_meta=cluster_meta,
    )


# ── Trial pairing + block detection ────────────────────────────────────────
def _pair_trials(markers: List[Marker], segment_concat_offsets: Optional[List[int]] = None) -> Tuple[List[Trial], List[dict]]:
    """Pair each stimulus onset (con/first) with the next `key` before the
    next onset marker. Assign a block using the `second`-marker rule.

    Returns `(trials, cluster_meta)`. `cluster_meta` is a list of dicts
    describing each detected marker cluster:

        {
            "index": i,
            "assigned_block": 1 or 2,
            "n_trials": int,
            "n_con": int,
            "n_inc": int,       # incongruent (label == 'first' onset)
            "t_start_global": float,
            "t_end_global": float,
            "has_second": bool,
            "has_end": bool,
        }

    This metadata is what the B-family checks use to detect ambiguous
    segment structures (docs §6). The current implementation still
    commits to one trial list — segment SELECTION (choosing between
    duplicate candidates) is deferred to a future phase where every
    candidate is re-aligned against the behavioural data. Until then,
    ambiguous files produce a HALT via B002 in checks.py so wrong
    numbers cannot leak through silently.

    `segment_concat_offsets[i]` is the sample-index offset of segment `i`
    within the concatenated fz/c3 arrays. Trials record both the local sample
    index (into their own segment) and the concatenated sample index
    (into the flat parsed.fz/c3 arrays used downstream).

    Rules (from docs/DATA_REFERENCE.md §3.5, §3.6):

      * A real trial is: onset → key, with no other onset in between.
        Boundary `first` markers fail this and are not counted.
      * Block assignment:
        - Split markers into clusters wherever consecutive marker times
          differ by more than BLOCK_GAP_S (30 s) in GLOBAL time.
        - If exactly two clusters exist, they are blocks 1 and 2.
        - Otherwise: any cluster containing at least one `second` marker
          is treated as block 2; the rest as block 1. If more than one
          candidate remains for either block, the caller (segment
          selection, later phase) must disambiguate.
    """
    if not markers:
        return [], []

    def _is_end(m: Marker) -> bool:
        return bool(END_TOKEN_RE.match(m.label)) or m.label.strip().lower() == "end"

    # A `second` marker only *opens* a block if a real onset follows it
    # SHORTLY (within BLOCK_GAP_S) before the next `second`/END. The marker
    # convention is inconsistent between recordings:
    #   * S1P002: `second` opens block 2 (a `con` onset follows ~0.7 s later),
    #     plus a trailing `second` right before END that closes the block.
    #   * S1P003/4/5: `second` CLOSES each block; the next onset is ~80 s away
    #     across the inter-block gap, and block 2 opens with a plain onset.
    # Requiring the following onset to be within BLOCK_GAP_S distinguishes an
    # opening `second` (S1P002) from a closing one (S1P003) and from the
    # trailing pre-END `second`, so neither produces a spurious candidate.
    def _second_opens_block(idx: int) -> bool:
        if markers[idx].label != BLOCK2_LABEL:
            return False
        t0 = markers[idx].time_global
        for j in range(idx + 1, len(markers)):
            nm = markers[j]
            if nm.time_global - t0 > BLOCK_GAP_S:
                return False
            if nm.label in ONSET_LABELS:
                return True
            if nm.label == BLOCK2_LABEL or _is_end(nm):
                return False
        return False

    # ── Clustering into CANDIDATE blocks (work-order Task 3) ──────────────
    # A candidate block boundary is created by ANY of:
    #   (1) a global-time gap > BLOCK_GAP_S (the original rule), OR
    #   (2) a block-OPENING `second` marker — it explicitly starts a new
    #       block, so it begins a new candidate even when < 30 s from the
    #       previous run (the S3P006 case: an aborted block sits only ~3 s
    #       after the valid first block and the gap rule alone merged them), OR
    #   (3) the marker immediately AFTER an `END` — END closes an experiment
    #       run; anything after it is a new (restart) candidate.
    # Splitting on second/END rather than gaps alone is what lets an
    # aborted/repeated run become its own candidate instead of being fused
    # into a neighbour and inflating the trial count.
    clusters: List[List[Marker]] = [[]]
    prev: Optional[Marker] = None
    for idx, m in enumerate(markers):
        boundary = False
        if clusters[-1]:
            if (m.time_global - clusters[-1][-1].time_global) > BLOCK_GAP_S:
                boundary = True          # (1) time gap
            elif _second_opens_block(idx):
                boundary = True          # (2) block-opening `second`
            elif prev is not None and _is_end(prev):
                boundary = True          # (3) first marker after END
        if boundary:
            clusters.append([])
        clusters[-1].append(m)
        prev = m
    # Drop empty leading cluster if present
    clusters = [c for c in clusters if c]

    # Assign a candidate block number to each cluster.
    #
    # We cannot use "contains a `second`" as the block-2 signal, because in
    # some recordings (S1P003/4/5) `second` CLOSES every block, so both
    # clusters contain one. The reliable signal is a block-OPENING `second`
    # (the S1P002 convention). Mark each cluster by whether it opens with one.
    opener_idxs = {i for i in range(len(markers)) if _second_opens_block(i)}
    _marker_id_to_idx = {id(m): i for i, m in enumerate(markers)}

    def _cluster_opens_with_second(cl: List[Marker]) -> bool:
        # True if this cluster's first onset is preceded by an opening
        # `second` at the cluster head.
        for m in cl:
            if _marker_id_to_idx.get(id(m)) in opener_idxs:
                return True
            if m.label in ONSET_LABELS:
                return False   # reached first onset without seeing an opener
        return False

    n_clusters = len(clusters)
    if n_clusters == 2:
        # The clean, unambiguous case: exactly two runs → blocks 1 and 2 in
        # time order (works for both marker conventions).
        cluster_block = {id(clusters[0]): 1, id(clusters[1]): 2}
    else:
        # Aborted/restarted files: a cluster is block 2 if it OPENS with a
        # `second`, else block 1. This can yield MULTIPLE candidates for a
        # block; selection (by behavioural alignment, or the trial-count
        # fallback) runs downstream in select_blocks().
        cluster_block = {}
        for cl in clusters:
            cluster_block[id(cl)] = 2 if _cluster_opens_with_second(cl) else 1

    trials: List[Trial] = []
    trial_num = 0
    cluster_meta: List[dict] = []

    for c_idx, cl in enumerate(clusters):
        block = cluster_block[id(cl)]
        block_trial = 0
        n_con_in_cluster = 0
        n_inc_in_cluster = 0
        for i, m in enumerate(cl):
            if m.label not in ONSET_LABELS:
                continue
            # Find next key and next onset within this cluster
            next_key = None
            next_stim = None
            for n in cl[i + 1 :]:
                if n.label == RESPONSE_LABEL and next_key is None:
                    next_key = n
                if n.label in ONSET_LABELS and next_stim is None:
                    next_stim = n
                if next_key is not None and next_stim is not None:
                    break
            if next_key is None:
                continue
            if next_stim is not None and next_stim.time_global < next_key.time_global:
                # Onset without a valid response → boundary marker or unresponded trial
                continue

            trial_num += 1
            block_trial += 1
            if m.label == "con":
                n_con_in_cluster += 1
            elif m.label == "first":
                n_inc_in_cluster += 1
            concat_offset = (
                segment_concat_offsets[m.segment_index]
                if segment_concat_offsets is not None and m.segment_index < len(segment_concat_offsets)
                else 0
            )
            trials.append(
                Trial(
                    trial=trial_num,
                    btrial=block_trial,
                    block=block,
                    cond=m.label,
                    onset=m.time_global,
                    key=next_key.time_global,
                    rt_ms=int(round((next_key.time_global - m.time_global) * 1000)),
                    segment_index=m.segment_index,
                    onset_sample_local=m.sample_index_local,
                    onset_sample_concat=concat_offset + m.sample_index_local,
                )
            )

        cluster_meta.append({
            "index": c_idx,
            "assigned_block": block,
            "n_trials": block_trial,
            "n_con": n_con_in_cluster,
            "n_inc": n_inc_in_cluster,
            "t_start_global": cl[0].time_global if cl else 0.0,
            "t_end_global": cl[-1].time_global if cl else 0.0,
            "has_second": any(m.label == BLOCK2_LABEL for m in cl),
            "opens_with_second": _cluster_opens_with_second(cl),
            "has_end": any(_is_end(m) for m in cl),
        })

    return trials, cluster_meta
