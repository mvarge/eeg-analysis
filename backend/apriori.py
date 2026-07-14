"""
A-priori trial-count inclusion rule (APRIORI_TRIAL_COUNT_RULE.md).

Committed *before* the analysis is run; its defensibility rests on being fixed
in advance, justified by design, and reported — not on outcome. This module is
the single authoritative implementation of that rule. It is consumed by the
group view (Tier 1 headline N, Tier 5 exclusion picture) and the individual
view (greyed cells with a note). No thresholds are hard-coded at the call
sites — they live here as config and are surfaced in the How-it-works tab.

Applies identically to all three measures: Fz-Pz theta relative power, C3-C4
beta relative power, and reaction time.

Cell structure
--------------
Each measure, per participant, is divided into four cells by refresh × congruency:
  incongruent·low, incongruent·high, congruent·low, congruent·high
(low/high = the two refresh rates, e.g. 60 / 165 Hz). The retained count per
cell is what survives incorrect-response removal (correct-only) and artifact
rejection — i.e. the trials that actually enter the measure.

The two gates (both committed a-priori)
--------------------------------------
* Floor (per cell): a cell must retain >= FLOOR_MIN_TRIALS clean trials to
  contribute a value. (~40% of the ~40 obtainable trials per cell.)
* Balance (per difference): for any low-vs-high difference, the smaller cell
  must be >= BALANCE_MIN_RATIO of the larger (no worse than 2:1).

A single floor and a single balance value are used across all cells and
comparisons; the *consequence* of failing differs by comparison, not the
threshold.

Comparisons, tiers and consequences
-----------------------------------
* Incongruent low-vs-high  — PRIMARY (the hypothesis). On failure the offending
  incongruent cell is withheld from the primary group test (and greyed with a
  note in the individual view). Does not, on its own, exclude the channel.
* Congruent low-vs-high    — SECONDARY. Flagged/greyed; not in the primary test.
* Combined (con+incon)     — SECONDARY. Same as congruent.
* Congruency effect        — VALIDITY (manipulation check). Reported/flagged
  when viewed; never withheld, never excludes anything.

Channel exclusion — the only trigger
-------------------------------------
An EEG channel (Fz-Pz or C3-C4) is excluded for a participant ONLY when it
retains no trustworthy refresh comparison at all — i.e. when BOTH the
incongruent AND the congruent low-vs-high comparisons fail. A thin incongruent
comparison alone does not exclude the channel. RT is not channel-scoped, but
the same floor/balance/consequences apply to its cells.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


# ============================================================
#  Committed thresholds (config — surfaced in How-it-works)
# ============================================================
FLOOR_MIN_TRIALS = 15        # per cell; >= this many clean trials to contribute
BALANCE_MIN_RATIO = 0.5      # smaller cell >= 50% of larger (i.e. no worse than 2:1)

# Congruency vocabulary used throughout the pipeline: 'con' = congruent,
# 'first' = incongruent (see parser.ONSET_LABELS).
_CON = "con"
_INC = "first"


def _cell_ok(n: int) -> bool:
    """True when a single cell meets the floor."""
    return n >= FLOOR_MIN_TRIALS


def _balanced(n_a: int, n_b: int) -> bool:
    """True when the smaller cell is >= BALANCE_MIN_RATIO of the larger.

    A pair with a zero cell is never balanced (and would already fail the floor).
    """
    hi = max(n_a, n_b)
    lo = min(n_a, n_b)
    if hi == 0:
        return False
    return lo >= BALANCE_MIN_RATIO * hi


def evaluate_comparison(n_low: int, n_high: int) -> dict:
    """Evaluate one low-vs-high comparison from its two cell counts.

    Returns the per-gate outcomes and the overall pass. A comparison PASSES
    only when both cells meet the floor AND the pair is balanced — a difference
    score is only interpretable when both contributing cells are individually
    reliable and comparably sized.
    """
    floor_low = _cell_ok(n_low)
    floor_high = _cell_ok(n_high)
    balance = _balanced(n_low, n_high)
    passes = floor_low and floor_high and balance

    reasons: List[str] = []
    if not floor_low:
        reasons.append(f"low cell {n_low} < floor {FLOOR_MIN_TRIALS}")
    if not floor_high:
        reasons.append(f"high cell {n_high} < floor {FLOOR_MIN_TRIALS}")
    if floor_low and floor_high and not balance:
        reasons.append(
            f"imbalanced ({min(n_low, n_high)} vs {max(n_low, n_high)} "
            f"> 2:1)")

    return {
        "n_low": int(n_low),
        "n_high": int(n_high),
        "floor_low_ok": floor_low,
        "floor_high_ok": floor_high,
        "balance_ok": balance,
        "passes": passes,
        "reason": "; ".join(reasons) if reasons else "",
    }


def evaluate_participant(
    cells: Dict[Tuple[str, str], int],
    rate_low: Optional[str],
    rate_high: Optional[str],
    channel_scoped: bool,
) -> dict:
    """Apply the full rule to one participant × measure.

    ``cells`` maps ``(rate_label, cond)`` -> surviving clean trial count, where
    ``cond`` is 'con' (congruent) or 'first' (incongruent). ``rate_low`` /
    ``rate_high`` are the two refresh-rate labels ordered low -> high (as the
    refresh payload resolves them). ``channel_scoped`` is True for the EEG power
    measures (theta/beta) and False for RT, which is never channel-excluded.

    Returns a dict with each comparison's evaluation, the per-cell floor flags,
    the primary-test inclusion decision, and — for channel-scoped measures — the
    channel-exclusion decision (both incongruent AND congruent must fail).
    """
    def _n(rate: Optional[str], cond: str) -> int:
        if rate is None:
            return 0
        return int(cells.get((rate, cond), 0))

    inc_low, inc_high = _n(rate_low, _INC), _n(rate_high, _INC)
    con_low, con_high = _n(rate_low, _CON), _n(rate_high, _CON)

    incongruent = evaluate_comparison(inc_low, inc_high)
    congruent = evaluate_comparison(con_low, con_high)
    combined = evaluate_comparison(inc_low + con_low, inc_high + con_high)

    # Channel exclusion: only when BOTH incongruent AND congruent fail (and only
    # for channel-scoped measures). RT is never channel-excluded.
    channel_excluded = bool(
        channel_scoped and (not incongruent["passes"]) and (not congruent["passes"])
    )

    # Primary-test inclusion = the incongruent comparison passes. If the channel
    # is excluded the participant is out regardless, but that's implied by both
    # comparisons failing.
    in_primary = incongruent["passes"] and not channel_excluded

    # Per-cell floor flags for greying in the individual view.
    cell_floor = {
        "inc_low": _cell_ok(inc_low),
        "inc_high": _cell_ok(inc_high),
        "con_low": _cell_ok(con_low),
        "con_high": _cell_ok(con_high),
    }
    cell_n = {
        "inc_low": inc_low, "inc_high": inc_high,
        "con_low": con_low, "con_high": con_high,
    }

    return {
        "incongruent": incongruent,   # PRIMARY
        "congruent": congruent,       # secondary
        "combined": combined,         # secondary
        "channel_excluded": channel_excluded,
        "in_primary": in_primary,
        "cell_floor": cell_floor,
        "cell_n": cell_n,
    }


def config() -> dict:
    """The committed thresholds, for the How-it-works tab and provenance."""
    return {
        "floor_min_trials": FLOOR_MIN_TRIALS,
        "balance_min_ratio": BALANCE_MIN_RATIO,
        "balance_max_imbalance": "2:1",
    }
