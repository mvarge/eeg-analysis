"""
LabChart EEG text export parser.

Reads a LabChart 8 .txt export file and extracts:
- Header metadata (sampling rate, channel names, recording date)
- Two channels of EEG data as numpy arrays
- Comment markers with sample indices and condition labels

Handles the known marker mislabelling: 'first' = incongruent (should be 'inc').
"""

import re
import numpy as np
from dataclasses import dataclass, field


@dataclass
class Marker:
    """A single event marker from the EEG recording."""
    sample_index: int
    time_seconds: float
    raw_text: str
    condition: str  # 'congruent', 'incongruent', 'block_start', 'block_end', 'key', 'end'


@dataclass
class ParsedEEG:
    """Complete parsed result from a LabChart file."""
    filename: str
    recording_date: str
    sampling_rate: float
    channel_names: list
    channel1: np.ndarray  # Fz-Pz
    channel2: np.ndarray  # C3-C4
    markers: list  # List[Marker]
    trial_markers: list  # Only con/inc trial markers (160 expected)


def parse_labchart(filepath: str) -> ParsedEEG:
    """
    Parse a LabChart 8 text export file.

    Args:
        filepath: Path to the .txt file

    Returns:
        ParsedEEG with all extracted data
    """
    filename = filepath.rsplit("/", 1)[-1] if "/" in filepath else filepath
    filename = filename.rsplit("\\", 1)[-1] if "\\" in filename else filename

    # Read header and data
    sampling_rate = None
    recording_date = None
    channel_names = []
    header_lines = 0

    with open(filepath, encoding="latin-1") as f:
        for line in f:
            header_lines += 1
            stripped = line.strip()

            if stripped.startswith("Interval="):
                # e.g. "Interval=\t0.0025 s"
                parts = stripped.split("\t")
                interval_str = parts[1].strip().replace(" s", "")
                sampling_rate = 1.0 / float(interval_str)

            elif stripped.startswith("ExcelDateTime="):
                # e.g. "ExcelDateTime=\t4.611...\t01/04/2026 20:46:58.34267"
                parts = stripped.split("\t")
                if len(parts) >= 3:
                    recording_date = parts[2].strip()

            elif stripped.startswith("ChannelTitle="):
                # e.g. "ChannelTitle=\tEEG Fz-Pz \tEEG C3-C4"
                parts = stripped.split("\t")
                channel_names = [p.strip() for p in parts[1:] if p.strip()]

            elif stripped.startswith("Range="):
                # Last header line before data
                break

    # Now read all data lines
    times = []
    ch1_data = []
    ch2_data = []
    raw_markers = []  # (line_index, time, raw_comment_text)

    with open(filepath, encoding="latin-1") as f:
        # Skip header
        for _ in range(header_lines):
            next(f)

        for line_idx, line in enumerate(f):
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue

            try:
                t = float(parts[0])
                c1 = float(parts[1])
                c2 = float(parts[2])
            except ValueError:
                continue

            times.append(t)
            ch1_data.append(c1)
            ch2_data.append(c2)

            # Check for comment marker (4th column onwards)
            if len(parts) > 3:
                comment = "\t".join(parts[3:]).strip()
                if comment:
                    raw_markers.append((len(ch1_data) - 1, t, comment))

    channel1 = np.array(ch1_data, dtype=np.float64)
    channel2 = np.array(ch2_data, dtype=np.float64)

    # Parse markers - extract condition from comment text
    # Format: "#1 <marker> #2 <marker>" - we only need #1
    all_markers = []
    stimulus_markers = []  # con/first/second only (no key/END)

    for sample_idx, time_s, comment in raw_markers:
        # Extract the #1 marker text
        match = re.search(r"#1\s+(\S+)", comment)
        if not match:
            continue

        marker_text = match.group(1)

        if marker_text == "key":
            all_markers.append(Marker(sample_idx, time_s, comment, "key"))
        elif "END" in marker_text or "****" in comment:
            all_markers.append(Marker(sample_idx, time_s, comment, "end"))
        elif marker_text in ("con", "first", "second"):
            m = Marker(sample_idx, time_s, comment, marker_text)
            all_markers.append(m)
            stimulus_markers.append(m)

    # Now identify trial markers vs block-start markers
    # Block structure verified by cross-reference:
    #   pos 0: 'first' = Block 1 start
    #   pos 81: 'first' = Block 2 start (right before 'second')
    #   pos 82, 163: 'second' = block boundary markers
    # Strategy: find 'second' positions, skip pos 0 and the 'first' right before each 'second'

    second_positions = [i for i, m in enumerate(stimulus_markers) if m.condition == "second"]

    skip_indices = set()
    skip_indices.add(0)  # Block 1 start
    for sp in second_positions:
        skip_indices.add(sp)      # The 'second' marker itself
    # Only the FIRST 'second' has a block-start marker before it
    # The last 'second' is an END marker — the marker before it is a real trial
    if second_positions:
        skip_indices.add(second_positions[0] - 1)  # Block 2 start (before first 'second')

    trial_markers = []
    for i, m in enumerate(stimulus_markers):
        if i in skip_indices:
            # Label skipped markers appropriately
            if m.condition == "second":
                m.condition = "block_end"
            else:
                m.condition = "block_start"
            continue

        # Relabel: con -> congruent, first -> incongruent
        if m.condition == "con":
            m.condition = "congruent"
        elif m.condition == "first":
            m.condition = "incongruent"

        trial_markers.append(m)

    return ParsedEEG(
        filename=filename,
        recording_date=recording_date or "Unknown",
        sampling_rate=sampling_rate or 400.0,
        channel_names=channel_names,
        channel1=channel1,
        channel2=channel2,
        markers=all_markers,
        trial_markers=trial_markers,
    )
