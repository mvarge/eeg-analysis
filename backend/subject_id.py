"""
Subject-ID normalisation.

Real filenames arrive in a variety of shapes because participants were
sometimes recorded across multiple files:

  S1P002.txt                          → S1P002
  S8P025(1).txt                       → S8P025      (part 1)
  S8P025(2).txt                       → S8P025      (part 2)
  S3P006 (flanker-partial).csv        → S3P006      (part 1)
  S3P006 (second only flanker).csv    → S3P006      (part 2)

The subject ID is the leading `S<digits>P<digits>` code preceding any
parenthesised suffix, space, or extension. Everything grouped under that
ID belongs to the same participant.

We also expose `part_hint()`, which returns:
  * 1 when the filename says "(1)" or "flanker-partial" or is a standalone stem,
  * 2 when it says "(2)" or "second only",
  * None when the file could be either.

The hint is a *suggestion* only. Definitive ordering must come from
segment content (marker structure + behavioural alignment), never from
the filename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Match the leading session/participant code at the start of a filename
# (case-insensitive, allowing arbitrary extra digits — session and
# participant numbers can be multi-digit).
SUBJECT_ID_RE = re.compile(r"^\s*(S\d+P\d+)\s*", re.IGNORECASE)


def subject_id_from_filename(filename: str) -> Optional[str]:
    """Return the canonical subject ID (upper-case S<n>P<nn>) for a filename.

    Returns None if the filename doesn't start with a recognisable code —
    the caller should then fall back to using the filename stem.
    """
    # Strip any leading path
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    m = SUBJECT_ID_RE.match(name)
    if not m:
        return None
    return m.group(1).upper()


def part_hint(filename: str) -> Optional[int]:
    """Return 1 or 2 if the filename hints at a specific part, else None.

    Never used to decide analysis behaviour — only for UI ordering and
    diagnostics. Real segment ordering comes from marker/behaviour content.
    """
    lower = filename.lower()
    # Parenthesised digit suffix: S8P025(1).txt, S8P025(2).txt
    m = re.search(r"\((\d+)\)", lower)
    if m:
        try:
            n = int(m.group(1))
            if n in (1, 2):
                return n
        except ValueError:
            pass
    # Behavioural CSV naming
    if "flanker-partial" in lower or "partial" in lower:
        return 1
    if "second only" in lower or "second-only" in lower:
        return 2
    return None


@dataclass
class ParsedFilename:
    """Structured view of an uploaded filename."""
    original: str          # exactly as uploaded
    subject_id: str        # canonical S<n>P<nn>, or the stem if no match
    part: Optional[int]    # 1, 2, or None
    ext: str               # lowercase extension including dot, e.g. ".txt"
    stem: str              # filename without extension


def parse_filename(filename: str) -> ParsedFilename:
    """Split an uploaded filename into subject_id + part hint."""
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot > 0:
        stem = name[:dot]
        ext = name[dot:].lower()
    else:
        stem = name
        ext = ""
    sid = subject_id_from_filename(name) or stem
    return ParsedFilename(
        original=name,
        subject_id=sid.upper(),
        part=part_hint(name),
        ext=ext,
        stem=stem,
    )
