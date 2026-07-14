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
#   EXCLUDED (from H2/H4) = data present & recognised, but the rule places the
#          participant in neither group. Captures: (a) >=HIGH on one hours
#          measure but <=LOW on the other; (b) >=HIGH on both but game_type is
#          slower-paced / none; (c) any hours value strictly between LOW and
#          HIGH (e.g. 4.5), which satisfies neither gate.
#
# Two further outcomes are distinct from EXCLUDED — the participant *cannot* be
# classified because their data is missing or unreadable, rather than the rule
# deliberately placing them outside both groups:
#   UNCLASSIFIED_MISSING = avg_hours, week_hours, or game_type is blank/absent.
#   UNCLASSIFIED_TYPE    = game_type is present but not a recognised value.
# These are surfaced under their own labels and, like EXCLUDED, are omitted from
# the HIGH-vs-LOW gaming split ONLY. They still contribute to every non-gaming
# analysis (H1 power, H3 RT), which never uses the gaming factor.
#
# The category constants below are the single source of truth for these labels.
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

# game_type values that are recognised but do NOT qualify HIGH (slower-paced, or
# "I don't play games"). A game_type that matches neither the HIGH nor the
# non-qualifying set is *unrecognised* — the participant is flagged unclassified
# rather than guessed into a bucket. The apostrophe variants cover the mojibake
# ("I don\u2019t" / "I don\ufffdt") seen in the Windows-1252 export.
GAMING_TYPE_LOW_PREFIXES = ("slower-paced", "slower paced", "i don")

# Category constants — single source of truth for the gaming labels. HIGH / LOW
# are the compared groups; the remaining three are each omitted from the
# HIGH-vs-LOW split but still flow into all non-gaming analyses.
GAMING_HIGH = "high"
GAMING_LOW = "low"
GAMING_EXCLUDED = "excluded"                 # data present, rule places outside both groups
GAMING_UNCLASSIFIED_MISSING = "unclassified_missing"   # avg/week/type blank or absent
GAMING_UNCLASSIFIED_TYPE = "unclassified_type"         # game_type present but unrecognised

# The categories that are statistically part of the HIGH-vs-LOW comparison.
# Everything not in this set contributes zero to the split's figures and N.
GAMING_COMPARED = (GAMING_HIGH, GAMING_LOW)

# Ordered list of every category the payload/UI may report.
GAMING_CATEGORIES = (
    GAMING_HIGH, GAMING_LOW, GAMING_EXCLUDED,
    GAMING_UNCLASSIFIED_MISSING, GAMING_UNCLASSIFIED_TYPE,
)


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


def gaming_type_class(value: Optional[str]) -> str:
    """Classify a game_type string as 'high' / 'low' / 'missing' / 'unrecognised'.

    'high'        → fast-paced / both (qualifies HIGH membership).
    'low'         → slower-paced / "I don't play games" (recognised, non-qualifying).
    'missing'     → blank / absent.
    'unrecognised'→ present but matches no known value (flag; never guessed).

    Matching is case-insensitive by prefix and tolerates the Windows-1252
    mojibake in the export (e.g. "I don\ufffdt play games").
    """
    if value is None:
        return "missing"
    v = str(value).strip().lower()
    if not v:
        return "missing"
    if any(v.startswith(p) for p in GAMING_TYPE_HIGH_PREFIXES):
        return "high"
    if any(v.startswith(p) for p in GAMING_TYPE_LOW_PREFIXES):
        return "low"
    return "unrecognised"


def _fmt_threshold(x: float) -> str:
    """Render a threshold without a trailing '.0' (5.0 -> '5', 4.5 -> '4.5')."""
    return str(int(x)) if float(x).is_integer() else str(x)


def gaming_classification(
    avg_hours: Optional[str],
    week_hours: Optional[str],
    game_type: Optional[str],
) -> dict:
    """Classify one participant's gaming exposure with a transparency reason.

    Applies the fixed a-priori rule to the three verbatim questionnaire values
    and returns a dict:
        {
          "category": one of GAMING_CATEGORIES,
          "reason":   human-readable clause that fired (§2 transparency),
          "avg_hours": parsed float or None,
          "week_hours": parsed float or None,
          "game_type": the raw game_type string (or None),
        }

    The rule is sample-independent — it depends only on this participant's own
    values, so it yields the same answer regardless of who else is loaded.

    Completeness is checked first: a blank/absent hours or game_type field means
    the participant cannot be classified (UNCLASSIFIED_MISSING), never silently
    defaulted. A present-but-unrecognised game_type is flagged
    (UNCLASSIFIED_TYPE) rather than guessed into a bucket. EXCLUDED is reserved
    for participants whose data is complete and recognised but whom the rule
    deliberately places in neither HIGH nor LOW.
    """
    avg = _gaming_hours_value(avg_hours)
    week = _gaming_hours_value(week_hours)
    tclass = gaming_type_class(game_type)
    hi = _fmt_threshold(GAMING_HOURS_HIGH)
    lo = _fmt_threshold(GAMING_HOURS_LOW)
    gt_disp = (str(game_type).strip() if game_type else "")

    def result(category: str, reason: str) -> dict:
        return {
            "category": category,
            "reason": reason,
            "avg_hours": avg,
            "week_hours": week,
            "game_type": (str(game_type) if game_type is not None else None),
        }

    # ── Completeness first — cannot classify on incomplete data (§5) ──
    missing = []
    if avg is None:
        missing.append("avg hours")
    if week is None:
        missing.append("past-week hours")
    if tclass == "missing":
        missing.append("game type")
    if missing:
        return result(
            GAMING_UNCLASSIFIED_MISSING,
            "unclassified — missing gaming data (" + ", ".join(missing) + ")",
        )

    # ── Unrecognised game type — flag, never guess (§5) ──
    if tclass == "unrecognised":
        return result(
            GAMING_UNCLASSIFIED_TYPE,
            f"unclassified — unrecognised game type ({gt_disp})",
        )

    # ── HIGH: both hours >= high threshold AND a qualifying game type ──
    if avg >= GAMING_HOURS_HIGH and week >= GAMING_HOURS_HIGH and tclass == "high":
        return result(
            GAMING_HIGH,
            f"HIGH — hours \u2265{hi} on both; game type qualifies ({gt_disp})",
        )

    # ── LOW: both hours <= low threshold (game type not considered) ──
    if avg <= GAMING_HOURS_LOW and week <= GAMING_HOURS_LOW:
        return result(GAMING_LOW, f"LOW — hours \u2264{lo} on both")

    # ── EXCLUDED — complete, recognised, but the rule places outside both ──
    hi_avg = avg >= GAMING_HOURS_HIGH
    hi_week = week >= GAMING_HOURS_HIGH
    if hi_avg and hi_week and tclass == "low":
        reason = f"EXCLUDED — hours qualify but game type is non-qualifying ({gt_disp})"
    elif hi_avg and not hi_week:
        reason = (f"EXCLUDED — avg \u2265{hi} but past-week \u2264{lo} "
                  "(inconsistent hours)")
    elif hi_week and not hi_avg:
        reason = (f"EXCLUDED — past-week \u2265{hi} but avg \u2264{lo} "
                  "(inconsistent hours)")
    else:
        reason = (f"EXCLUDED — hours between {lo} and {hi} on a measure "
                  "(neither gate)")
    return result(GAMING_EXCLUDED, reason)


def gaming_category(
    avg_hours: Optional[str],
    week_hours: Optional[str],
    game_type: Optional[str],
) -> str:
    """Category string only — convenience wrapper over gaming_classification().

    Returns one of GAMING_CATEGORIES. Prefer gaming_classification() when the
    per-participant transparency reason is also needed.
    """
    return gaming_classification(avg_hours, week_hours, game_type)["category"]


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
