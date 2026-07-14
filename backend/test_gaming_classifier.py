#!/usr/bin/env python3
"""Self-tests for the a-priori gaming-exposure classifier (Tier 3 / H2 & H4).

Stdlib-only — no pytest, no third-party deps. Run directly:

    python backend/test_gaming_classifier.py       # or ../.venv/bin/python

Exit code 0 = all pass, 1 = at least one failure. The classification RULE is
fixed and a-priori; these tests pin it against invented participants with known
answers (§3 of the spec) plus the committed real-dataset totals (§7) and the
missing/unrecognised edge cases (§5). None of these numbers may leak into the
classifier itself — they are external checks only.

Note on game_type strings: the spec's §7 table abbreviates the questionnaire
values ("Both", "Don't play"). The CLASSIFIER matches the *canonical*
questionnaire wording (see GAMING_TYPE_* prefixes), so these fixtures use the
canonical strings. "Both" -> "Both played a similar amount", etc.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demographics import (  # noqa: E402
    gaming_category,
    gaming_classification,
    GAMING_HIGH,
    GAMING_LOW,
    GAMING_EXCLUDED,
    GAMING_UNCLASSIFIED_MISSING,
    GAMING_UNCLASSIFIED_TYPE,
)

# Canonical questionnaire game_type values (§1).
FAST = "Fast-paced/action (shooters, racing, competitive games)"
BOTH = "Both played a similar amount"
SLOW = "Slower-paced (strategy, RPGs, casual/mobile)"
NONE = "I don't play games"
# Mojibake apostrophe variant seen in the Windows-1252 export.
NONE_MOJIBAKE = "I don\ufffdt play games"
NONE_CURLY = "I don\u2019t play games"


# ── §3 synthetic self-tests (13 cases, verbatim from the spec) ──
# (avg_hours, week_hours, game_type, expected_category, branch_covered)
SELF_TESTS = [
    (5,   5,   BOTH, GAMING_HIGH,     "boundary >=5 both; qualifying type (both)"),
    (5,   5,   FAST, GAMING_HIGH,     "qualifying type (fast)"),
    (6,   6,   BOTH, GAMING_HIGH,     "interior HIGH"),
    (5,   5,   SLOW, GAMING_EXCLUDED, "hours qualify, type does NOT (slower)"),
    (5,   5,   NONE, GAMING_EXCLUDED, "hours qualify, type does NOT (none)"),
    (15,  4,   BOTH, GAMING_EXCLUDED, "mixed: high avg, low week"),
    (4,   15,  BOTH, GAMING_EXCLUDED, "mixed: low avg, high week"),
    (4.5, 4.5, BOTH, GAMING_EXCLUDED, "4-5 gap on both"),
    (4.5, 6,   FAST, GAMING_EXCLUDED, "4-5 gap on one"),
    (4,   4,   BOTH, GAMING_LOW,      "boundary <=4 both; type ignored"),
    (4,   4,   FAST, GAMING_LOW,      "LOW hours-only (fast type, still LOW)"),
    (0,   0,   NONE, GAMING_LOW,      "interior LOW"),
    (2,   0,   SLOW, GAMING_LOW,      "low hours, some play"),
]


# ── §7 expected classification — current dataset (canonical game_type) ──
# Rows in supplied order; verify per participant AND totals (HIGH 11/LOW 18/EXCL 4).
DATASET = [
    (25,  33,  BOTH, GAMING_HIGH),
    (20,  30,  BOTH, GAMING_HIGH),
    (20,  15,  BOTH, GAMING_HIGH),
    (10,  15,  FAST, GAMING_HIGH),
    (14,  14,  BOTH, GAMING_HIGH),
    (15,  12,  FAST, GAMING_HIGH),
    (16,  12,  FAST, GAMING_HIGH),
    (6,   10,  BOTH, GAMING_HIGH),
    (16,  8,   BOTH, GAMING_HIGH),
    (8,   6,   BOTH, GAMING_HIGH),
    (12,  6,   FAST, GAMING_HIGH),
    (2,   4,   SLOW, GAMING_LOW),
    (15,  4,   BOTH, GAMING_EXCLUDED),   # acid test: high avg, low past-week
    (8,   3,   BOTH, GAMING_EXCLUDED),   # acid test
    (20,  2,   BOTH, GAMING_EXCLUDED),   # acid test
    (2.5, 1.5, SLOW, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (1,   0,   SLOW, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (2,   0,   SLOW, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (12,  0,   BOTH, GAMING_EXCLUDED),   # acid test
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0,   0,   SLOW, GAMING_LOW),
    (0,   0,   NONE, GAMING_LOW),
    (0.5, 0,   SLOW, GAMING_LOW),
]
EXPECTED_TOTALS = {GAMING_HIGH: 11, GAMING_LOW: 18, GAMING_EXCLUDED: 4}


# ── §5 edge cases — missing / unrecognised data are distinct from EXCLUDED ──
# (avg, week, type, expected_category)
EDGE_TESTS = [
    (None, 5,    BOTH,           GAMING_UNCLASSIFIED_MISSING),  # missing avg
    (5,    None, BOTH,           GAMING_UNCLASSIFIED_MISSING),  # missing week
    ("",   "",   BOTH,           GAMING_UNCLASSIFIED_MISSING),  # blank hours
    (6,    6,    None,           GAMING_UNCLASSIFIED_MISSING),  # missing type
    (6,    6,    "",             GAMING_UNCLASSIFIED_MISSING),  # blank type
    (6,    6,    "MMORPG grand strategy", GAMING_UNCLASSIFIED_TYPE),  # unrecognised, HIGH hours
    (1,    1,    "space sim",    GAMING_UNCLASSIFIED_TYPE),     # unrecognised, LOW hours
    (0,    0,    NONE_MOJIBAKE,  GAMING_LOW),                   # mojibake apostrophe -> LOW
    (0,    0,    NONE_CURLY,     GAMING_LOW),                   # curly apostrophe -> LOW
    (6,    6,    "  Fast-paced/action  ", GAMING_HIGH),         # whitespace tolerated
    (6,    6,    "FAST-PACED/action",     GAMING_HIGH),         # case tolerated
]


def _fails(msg, fails):
    fails.append(msg)
    print("  FAIL: " + msg)


def run():
    fails = []

    print("== §3 synthetic self-tests ==")
    for avg, week, gt, exp, branch in SELF_TESTS:
        got = gaming_category(str(avg), str(week), gt)
        if got != exp:
            _fails(f"avg={avg} week={week} type={gt!r} -> {got} (expected {exp}) [{branch}]", fails)
    print(f"  {len(SELF_TESTS) - len([f for f in fails])}/{len(SELF_TESTS)} checked")

    print("== §7 real-dataset per-row + totals ==")
    totals = {}
    row_fails_before = len(fails)
    for i, (avg, week, gt, exp) in enumerate(DATASET, 1):
        got = gaming_category(str(avg), str(week), gt)
        totals[got] = totals.get(got, 0) + 1
        if got != exp:
            _fails(f"row {i}: avg={avg} week={week} type={gt!r} -> {got} (expected {exp})", fails)
    for cat, want in EXPECTED_TOTALS.items():
        have = totals.get(cat, 0)
        if have != want:
            _fails(f"total {cat}: {have} (expected {want})", fails)
    if len(fails) == row_fails_before:
        print(f"  all {len(DATASET)} rows match; totals HIGH=11 LOW=18 EXCLUDED=4")

    print("== §5 missing / unrecognised edge cases ==")
    for avg, week, gt, exp in EDGE_TESTS:
        a = None if avg is None else str(avg)
        w = None if week is None else str(week)
        got = gaming_category(a, w, gt)
        if got != exp:
            _fails(f"avg={avg!r} week={week!r} type={gt!r} -> {got} (expected {exp})", fails)
    print("  edge cases checked")

    print("== §2 firing-clause reasons present ==")
    for avg, week, gt, _exp, _branch in SELF_TESTS:
        r = gaming_classification(str(avg), str(week), gt)
        if not r.get("reason"):
            _fails(f"no reason for avg={avg} week={week} type={gt!r}", fails)
    print("  every classification carries a reason")

    print()
    if fails:
        print(f"RESULT: {len(fails)} failure(s).")
        return 1
    print("RESULT: all self-tests pass.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
