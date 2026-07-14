"""
Demographics CSV loader.

The researcher exports one CSV that lists every participant along with metadata
(age, sex, handedness, block ordering, sleep, caffeine, screen use, gaming
history, etc). The file is a wide sheet (~45 columns) exported from a
Qualtrics-style form and uses a Windows-1252 / Latin-1 encoding.

We keep a scientifically relevant subset of columns for UI display and CSV
export, but also stash the full row so users can add more later.

LabChart filenames follow `S{S}P{PP}` — session number, participant number.
E.g. `S1P002.txt` → session=1, participant=2 → matches CSV row with
`Session Number` == 1 AND `Participant Number` == 2.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional


# Columns worth showing in the UI (in display order).
# Order matters — this is what the frontend will render top-to-bottom / left-to-right.
DISPLAY_FIELDS = [
    ("age",                  "Age (in years)",                                                          "Age"),
    ("sex",                  "Sex Assigned at Birth",                                                   "Sex"),
    ("handedness",           "What is your dominant hand?",                                             "Handedness"),
    ("refresh_ordering",     "Refresh Rate Condition Ordering",                                         "Block order"),
    ("sleep_quality",        "How did you sleep last night?",                                           "Sleep quality"),
    ("sleep_hours",          "Estimated sleep length (hours)",                                          "Sleep (h)"),
    ("sleep_extra_min",      "Additional minutes of sleep",                                             "+ min"),
    ("sleepiness",           "Select the number that best describes your current level of sleepiness.", "Alertness"),
    ("caffeine",             "Have you consumed caffeine in the last 6 hours?",                         "Caffeine"),
    ("screen_hours_day",     "On average, how many hours per day do you spend using screens?",          "Screen h/day"),
    ("games_weekly",         "Do you play video games at least once per week?",                         "Weekly gamer"),
    ("games_hours_week",     "How many hours per week do you spend gaming on average?",                 "Games h/wk"),
    ("games_last_week",      "How many hours have you spent gaming over this past week?",              "Games last wk"),
    ("game_type",            "What type of games do you play?",                                         "Game type"),
    ("hi_refresh_monitor",   "Do you use high-refresh-rate monitors (120 Hz, 144 Hz, or higher)?",     "Hi-Hz monitor"),
    ("hi_refresh_freq",      "If yes, what refresh rate does your primary display use?",               "Primary Hz"),
    ("prior_eeg",            "Have you taken part in EEG research before?",                             "Prior EEG"),
    ("notes",                "Notes",                                                                    "Notes"),
]

# Filename regex — matches S1P002, S12P007, s1p002, etc.
FILENAME_PATTERN = re.compile(r"S(\d+)P(\d+)", re.IGNORECASE)


# ============================================================
#  Gaming-exposure classification (Tier 3 / H2 & H4)
# ============================================================
# Participants are sorted into three mutually exclusive, exhaustive gaming
# categories by an a-priori rule applied to three questionnaire fields. This is
# a DEFINED classification rule, not a sample-relative or median split, and not
# a stored roster — membership is always derived from the data, so adding or
# editing a participant re-derives categories with no code change.
#
# Fields (from DISPLAY_FIELDS):
#   avg_hours  = games_hours_week  ("...hours per week...on average?")
#   week_hours = games_last_week   ("...hours...over this past week?")
#   game_type  = game_type         (fast-paced / both / slower-paced / none)
#
# Rule:
#   HIGH = avg_hours >= HIGH AND week_hours >= HIGH
#          AND game_type in the fast-paced / both set
#   LOW  = avg_hours <= LOW  AND week_hours <= LOW      (game_type ignored)
#   EXCLUDED (from H2/H4) = everything else. Captures: (a) >=HIGH on one hours
#          measure but <=LOW on the other; (b) >=HIGH on both but game_type is
#          slower-paced / none; (c) any hours value strictly between LOW and
#          HIGH (e.g. 4.5), which satisfies neither gate.
#
# Thresholds live here as config (surfaced in the How-it-works tab, never as
# inline UI literals). High side is inclusive at 5, low side inclusive at 4;
# the 4<x<5 gap belongs to EXCLUDED.
GAMING_HOURS_HIGH = 5.0   # avg AND week hours must both be >= this for HIGH
GAMING_HOURS_LOW = 4.0    # avg AND week hours must both be <= this for LOW

# game_type values that qualify HIGH membership (fast-paced action, or an even
# mix). Matched case-insensitively by prefix so encoding/quote variants of the
# verbatim questionnaire strings still match. game_type is NOT considered for
# LOW.
GAMING_TYPE_HIGH_PREFIXES = ("fast-paced", "both")


def _gaming_hours_value(value: Optional[str]) -> Optional[float]:
    """Parse an hours answer to a float, or None when blank/unparseable.

    The questionnaire stores hours as free text ("12", "2.5", "0"). A missing
    or non-numeric hours answer cannot satisfy either the HIGH or LOW gate, so
    the participant falls to EXCLUDED — which is the correct, visible outcome.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Keep digits, dot, minus; tolerate stray unit text like "10 hours".
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _gaming_type_is_high(value: Optional[str]) -> bool:
    """True when game_type qualifies HIGH membership (fast-paced or 'both')."""
    if not value:
        return False
    v = str(value).strip().lower()
    return any(v.startswith(p) for p in GAMING_TYPE_HIGH_PREFIXES)


def gaming_category(
    avg_hours: Optional[str],
    week_hours: Optional[str],
    game_type: Optional[str],
) -> str:
    """Classify one participant's gaming exposure (Tier 3 / H2 & H4).

    Applies the fixed rule above to the three verbatim questionnaire values.
    Returns one of: "high", "low", "excluded".

    "excluded" means excluded from the H2/H4 gaming comparison specifically —
    the participant is still shown in Tier 3 under its own labelled category,
    never silently merged into LOW or dropped.
    """
    avg = _gaming_hours_value(avg_hours)
    week = _gaming_hours_value(week_hours)

    # HIGH: both hours measures at/above the high threshold AND a qualifying
    # game type. Missing hours (None) never satisfies >=.
    if (avg is not None and week is not None
            and avg >= GAMING_HOURS_HIGH and week >= GAMING_HOURS_HIGH
            and _gaming_type_is_high(game_type)):
        return "high"

    # LOW: both hours measures at/below the low threshold (game type ignored).
    if (avg is not None and week is not None
            and avg <= GAMING_HOURS_LOW and week <= GAMING_HOURS_LOW):
        return "low"

    # Everything else — one-sided, high-hours-but-wrong-type, or the 4<x<5 gap.
    return "excluded"


def handedness_category(value: Optional[str]) -> str:
    """Map the verbatim demographics handedness string to a canonical category.

    The demographics CSV stores free-form-ish values ("Right-handed",
    "Left-handed", "Ambidextrous"). We normalise to one of four buckets so the
    group view can filter/colour by handedness without depending on the exact
    surface string. This is a *display/filter* categoriser only — it never
    adjusts or normalises the data (handedness sensitivity is reveal-only).

    Returns one of: "right", "left", "ambidextrous", "unknown".
    """
    if not value:
        return "unknown"
    v = str(value).strip().lower()
    if not v:
        return "unknown"
    if v.startswith("ambi"):
        return "ambidextrous"
    if v.startswith("right") or v in ("r", "rh"):
        return "right"
    if v.startswith("left") or v in ("l", "lh"):
        return "left"
    return "unknown"


@dataclass
class Demographic:
    """One participant's demographic record."""
    session: int
    participant: int
    display: dict = field(default_factory=dict)   # {field_key: value}
    block_order: dict = field(default_factory=dict)  # {1: "60 Hz", 2: "165 Hz"}
    aborted: bool = False
    raw: dict = field(default_factory=dict)      # full CSV row for future use


def parse_demographics(csv_bytes: bytes) -> list[Demographic]:
    """Parse a demographics CSV from raw bytes. Returns a list of Demographic rows."""

    # Try utf-8 first, then latin-1 / cp1252
    text = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = csv_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Could not decode demographics CSV")

    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        return []

    # The header may span multiple physical lines because some column names
    # contain embedded newlines. csv.reader already handles quoted newlines,
    # so `rows[0]` is the logical header (single Python list).
    header = rows[0]

    # Build a name→index lookup, normalising whitespace + Unicode replacement chars.
    def clean(s: str) -> str:
        return s.replace("\ufffd", "").replace("\n", " ").strip()

    idx = {clean(h): i for i, h in enumerate(header)}

    def get(row: list[str], col_name: str) -> str:
        i = idx.get(col_name)
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

    # Also try normalised match if exact key missing (some column names may
    # have trailing chars from encoding hiccups)
    def get_soft(row: list[str], col_name: str) -> str:
        v = get(row, col_name)
        if v:
            return v
        low_target = col_name.lower()
        for k, i in idx.items():
            if k.lower() == low_target and i < len(row):
                return row[i].strip()
        return ""

    out: list[Demographic] = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue

        session_raw = get_soft(row, "Session Number")
        participant_raw = get_soft(row, "Participant Number")
        try:
            session = int(float(session_raw))
        except (ValueError, TypeError):
            continue
        try:
            participant = int(float(participant_raw))
        except (ValueError, TypeError):
            continue

        # Block ordering → {1: "60 Hz", 2: "165 Hz"}
        ordering = get_soft(row, "Refresh Rate Condition Ordering")
        block_order = _parse_block_order(ordering)
        aborted = "abort" in ordering.lower()

        display = {}
        for key, col_name, _label in DISPLAY_FIELDS:
            display[key] = get_soft(row, col_name) or ""

        # Store raw row too so the UI can offer "show more" later
        raw = {clean(h): row[i].strip() if i < len(row) else ""
               for h, i in [(h, i) for h, i in idx.items()]}

        out.append(Demographic(
            session=session,
            participant=participant,
            display=display,
            block_order=block_order,
            aborted=aborted,
            raw=raw,
        ))
    return out


def _parse_block_order(text: str) -> dict:
    """
    Convert 'First 60 Hz, Second 165 Hz' → {1: '60 Hz', 2: '165 Hz'}.
    Returns {} if parsing fails or session was aborted.
    """
    if not text or "abort" in text.lower():
        return {}
    m1 = re.search(r"first[^\d]*(\d+)\s*Hz", text, re.IGNORECASE)
    m2 = re.search(r"second[^\d]*(\d+)\s*Hz", text, re.IGNORECASE)
    order = {}
    if m1: order[1] = f"{m1.group(1)} Hz"
    if m2: order[2] = f"{m2.group(1)} Hz"
    return order


def parse_filename_ids(filename: str) -> Optional[tuple[int, int]]:
    """
    Extract (session, participant) from a filename like 'S1P002.txt'.
    Returns None if the pattern doesn't match.
    """
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0]
    m = FILENAME_PATTERN.search(stem)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def match_demographics(filename: str, demographics: list[Demographic]) -> Optional[Demographic]:
    """Find the demographic row for a given filename, or None if no match."""
    ids = parse_filename_ids(filename)
    if ids is None:
        return None
    session, participant = ids
    for d in demographics:
        if d.session == session and d.participant == participant:
            return d
    return None
