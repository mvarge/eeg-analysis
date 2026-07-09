"""
LabChart EEG text export parser.

Matches the reference pipeline (pipeline_stages_1to6.py from EEG_analysis V.zip):
  - latin-1 encoding
  - skip 6 header lines (the LabChart preamble)
  - two channels: Fz-Pz (theta) and C3-C4 (beta)
  - marker table with block-gap detection (>30s = new block)
  - trial pairing: each `con` / `first` onset is paired with the next `key`;
    orphan `first` markers at block boundaries are dropped.

Header parsing is best-effort — the reference script ignores the header entirely
and always uses FS=400 Hz, but we surface `sampling_rate`, `recording_date`,
and channel names from the header where present so the UI can show them.
"""

import re
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple


BLOCK_GAP_S = 30.0   # marker gap that separates the two blocks
HEADER_LINES = 6     # LabChart 8 export preamble


@dataclass
class Marker:
    """A single event marker from the recording."""
    time_seconds: float
    sample_index: int
    label: str        # raw label: 'con', 'first', 'key', 'second', etc.


@dataclass
class Trial:
    """A single trial: stimulus onset paired with response key."""
    trial: int         # 1..N across the whole recording
    btrial: int        # 1..N within its block
    block: int         # 1 or 2
    cond: str          # 'con' (congruent) or 'first' (incongruent)
    onset: float       # stimulus onset time (s)
    key: float         # response key time (s)
    rt_ms: int         # reaction time in ms
    onset_sample: int  # sample index of onset


@dataclass
class ParsedEEG:
    """Complete parsed result from a LabChart file."""
    filename: str
    recording_date: str
    sampling_rate: float
    channel_names: List[str]
    fz: np.ndarray            # Fz-Pz (µV)
    c3: np.ndarray            # C3-C4 (µV)
    markers: List[Marker]     # all markers
    trials: List[Trial]       # paired trials only
    n_blocks: int             # detected from marker gaps


def _parse_header(filepath: str) -> Tuple[float, str, List[str]]:
    """Best-effort extraction of sampling rate, date, channel names from header."""
    sampling_rate = 400.0
    recording_date = "Unknown"
    channel_names = ["Fz-Pz", "C3-C4"]

    with open(filepath, "r", encoding="latin-1") as fh:
        for i, line in enumerate(fh):
            if i >= HEADER_LINES:
                break
            stripped = line.strip()

            if stripped.startswith("Interval="):
                parts = stripped.split("\t")
                if len(parts) >= 2:
                    try:
                        interval_str = parts[1].strip().replace(" s", "")
                        sampling_rate = 1.0 / float(interval_str)
                    except (ValueError, ZeroDivisionError):
                        pass

            elif stripped.startswith("ExcelDateTime="):
                parts = stripped.split("\t")
                if len(parts) >= 3:
                    recording_date = parts[2].strip()

            elif stripped.startswith("ChannelTitle="):
                parts = stripped.split("\t")
                names = [p.strip() for p in parts[1:] if p.strip()]
                if names:
                    channel_names = names[:2] + ["Fz-Pz", "C3-C4"][len(names):]

    return sampling_rate, recording_date, channel_names


def _extract_marker_label(comment: str) -> str:
    """Extract the marker's short label from a LabChart comment field.

    LabChart comments look like: "#1 con"  or  "#1 key #2 con"  or just "con".
    We take the label right after '#1' if present, otherwise the first token,
    stripped of any trailing '#N' suffixes.
    """
    text = comment.replace("#1", "").split("#")[0].strip().split()
    if not text:
        return ""
    return text[0]


def parse_labchart(filepath: str) -> ParsedEEG:
    """Parse a LabChart 8 text export file.

    Returns ParsedEEG with the two channels, marker table, and trial pairings.
    """
    # Extract filename (cross-platform)
    filename = filepath.rsplit("/", 1)[-1]
    filename = filename.rsplit("\\", 1)[-1]

    sampling_rate, recording_date, channel_names = _parse_header(filepath)

    # ---- STAGE 1: load samples + markers -----------------------------------
    fz: List[float] = []
    c3: List[float] = []
    markers: List[Marker] = []

    with open(filepath, "r", encoding="latin-1") as fh:
        for i, line in enumerate(fh):
            if i < HEADER_LINES:
                continue
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                t = float(parts[0])
                a = float(parts[1])
                b = float(parts[2])
            except ValueError:
                continue

            fz.append(a)
            c3.append(b)

            # 4th+ column may hold a comment (marker)
            if len(parts) >= 4 and parts[3].strip():
                label = _extract_marker_label(parts[3])
                if label:
                    markers.append(Marker(
                        time_seconds=t,
                        sample_index=len(fz) - 1,
                        label=label,
                    ))

    fz_arr = np.asarray(fz, dtype=np.float64)
    c3_arr = np.asarray(c3, dtype=np.float64)

    # ---- STAGE 2: block detection + trial pairing --------------------------
    # Blocks are separated by marker gaps larger than BLOCK_GAP_S.
    trials = _pair_trials(markers)

    # Count distinct blocks
    n_blocks = len({t.block for t in trials}) if trials else 0

    return ParsedEEG(
        filename=filename,
        recording_date=recording_date,
        sampling_rate=sampling_rate,
        channel_names=channel_names,
        fz=fz_arr,
        c3=c3_arr,
        markers=markers,
        trials=trials,
        n_blocks=n_blocks,
    )


def _pair_trials(markers: List[Marker]) -> List[Trial]:
    """Detect blocks and pair each con/first onset with the next key.

    Follows the reference (pipeline_stages_1to6.py:63-88):
      - split on marker gaps > BLOCK_GAP_S
      - for each stimulus marker (con/first), find the next `key`
      - if another stimulus marker comes before the key, it's an orphan
        (block-boundary `first`) and is dropped.
    """
    if not markers:
        return []

    times = np.array([m.time_seconds for m in markers])
    gaps = np.where(np.diff(times) > BLOCK_GAP_S)[0]

    # block start/end times (with a small buffer)
    starts = [times[0]] + [times[i + 1] for i in gaps]
    ends = [times[i] for i in gaps] + [times[-1]]

    trials: List[Trial] = []
    trial_num = 0

    for block_idx, (t0, t1) in enumerate(zip(starts, ends), start=1):
        seq = [m for m in markers if t0 - 0.5 <= m.time_seconds <= t1 + 0.5]
        block_trial = 0

        i = 0
        while i < len(seq):
            m = seq[i]
            if m.label in ("con", "first"):
                # find next key
                next_key = None
                next_stim = None
                for j in range(i + 1, len(seq)):
                    lab = seq[j].label
                    if lab == "key" and next_key is None:
                        next_key = seq[j]
                    if lab in ("con", "first") and next_stim is None:
                        next_stim = seq[j]
                    if next_key is not None and next_stim is not None:
                        break

                # Only counts as a trial if a key comes before the next stimulus
                if next_key is not None and (next_stim is None or next_key.time_seconds < next_stim.time_seconds):
                    trial_num += 1
                    block_trial += 1
                    trials.append(Trial(
                        trial=trial_num,
                        btrial=block_trial,
                        block=block_idx,
                        cond=m.label,
                        onset=m.time_seconds,
                        key=next_key.time_seconds,
                        rt_ms=int(round((next_key.time_seconds - m.time_seconds) * 1000)),
                        onset_sample=m.sample_index,
                    ))
            i += 1

    return trials
