"""
Block selection by behavioural alignment (work-order Task 3, docs §6).

The parser produces CANDIDATE marker clusters (see parser._pair_trials).
On a clean recording there is exactly one candidate per block and this
module is a near no-op. On an aborted/restarted recording (e.g. S3P006)
a block can have several candidates — one valid run plus one or more
aborted attempts. This module chooses the correct candidate for each
block.

Selection rule (hard rule 2): prefer behavioural alignment. For each
block, align every candidate against that block's behavioural rows and
keep the candidate that passes the J-gates (AlignmentResult.passes_gate).

  * exactly one candidate passes  -> B000 INFO, selected
  * no candidate passes           -> B001 HALT (block cannot be trusted)
  * more than one passes          -> B002 HALT (ambiguous)

When behavioural data is ABSENT, fall back to "most trials; ties broken
by latest in time". The tie-break direction (latest) is deliberate: a
repeated run is usually the real attempt after an aborted start, so the
later, equally-sized candidate is the more likely valid one. This
fallback is explicitly weaker than alignment and is only used when
alignment is impossible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from parser import ParsedEEG, Trial
from behavioural import BehaviouralSession, AlignmentResult, align_block
from logging_setup import get_logger

logger = get_logger(__name__)


# Levels mirrored from checks.py to avoid a circular import.
INFO = "INFO"
WARN = "WARN"
HALT = "HALT"


@dataclass
class BlockCandidate:
    """One candidate run for a block, derived from a parser cluster."""
    cluster_index: int
    block: int
    trials: List[Trial]
    t_start_global: float
    t_end_global: float
    has_second: bool
    has_end: bool
    # populated during selection when behavioural data is present
    alignment: Optional[AlignmentResult] = None
    passed_gate: Optional[bool] = None


@dataclass
class SelectionResult:
    """Outcome of block selection for a whole recording."""
    selected_trials: List[Trial]                 # renumbered, in block order
    selected_by_block: Dict[int, BlockCandidate]  # block -> chosen candidate
    candidates_by_block: Dict[int, List[BlockCandidate]]
    codes: List[Tuple[str, str, str]] = field(default_factory=list)  # (code, level, message)
    used_behavioural: bool = False


def _candidates_from_parsed(parsed: ParsedEEG) -> List[BlockCandidate]:
    """Rebuild per-cluster candidates from parsed.cluster_meta + trials.

    Trials carry their block label but not their originating cluster, so we
    reconstruct membership from cluster time bounds. A trial belongs to the
    cluster whose [t_start, t_end] contains its onset. This is exact because
    clusters are non-overlapping in global time.
    """
    cands: List[BlockCandidate] = []
    for meta in parsed.cluster_meta:
        lo = meta["t_start_global"]
        hi = meta["t_end_global"]
        cl_trials = [
            t for t in parsed.trials
            if lo <= t.onset <= hi
        ]
        cands.append(BlockCandidate(
            cluster_index=meta["index"],
            block=meta["assigned_block"],
            trials=cl_trials,
            t_start_global=lo,
            t_end_global=hi,
            has_second=meta.get("has_second", False),
            has_end=meta.get("has_end", False),
        ))
    return cands


def _renumber(selected_by_block: Dict[int, BlockCandidate]) -> List[Trial]:
    """Flatten selected candidates into a single trial list, renumbering
    `trial` (global) and `btrial` (within block) so downstream code sees a
    clean 1..N sequence."""
    out: List[Trial] = []
    trial_num = 0
    for blk in sorted(selected_by_block):
        cand = selected_by_block[blk]
        for btrial, t in enumerate(cand.trials, start=1):
            trial_num += 1
            out.append(Trial(
                trial=trial_num,
                btrial=btrial,
                block=blk,
                cond=t.cond,
                onset=t.onset,
                key=t.key,
                rt_ms=t.rt_ms,
                segment_index=t.segment_index,
                onset_sample_local=t.onset_sample_local,
                onset_sample_concat=t.onset_sample_concat,
            ))
    return out


def select_blocks(
    parsed: ParsedEEG,
    beh_session: Optional[BehaviouralSession] = None,
) -> SelectionResult:
    """Select the correct candidate run for each block.

    If `beh_session` is provided, selection is by alignment (B000/B001/B002).
    Otherwise the trial-count fallback is used (no B-codes emitted; the
    ambiguity is reported by checks.py at WARN instead).
    """
    candidates = _candidates_from_parsed(parsed)
    by_block: Dict[int, List[BlockCandidate]] = {}
    for c in candidates:
        by_block.setdefault(c.block, []).append(c)

    selected: Dict[int, BlockCandidate] = {}
    codes: List[Tuple[str, str, str]] = []
    used_beh = False

    for blk in sorted(by_block):
        cands = by_block[blk]

        # Fast path: a single candidate for this block — nothing to choose.
        if len(cands) == 1:
            selected[blk] = cands[0]
            continue

        if beh_session is not None:
            used_beh = True
            passing: List[BlockCandidate] = []
            for c in cands:
                c.alignment = align_block(
                    eeg_rts_ms=[t.rt_ms for t in c.trials],
                    eeg_congruent=[t.cond == "con" for t in c.trials],
                    beh_trials=beh_session.trials,
                    block=blk,
                )
                c.passed_gate = c.alignment.passes_gate()
                if c.passed_gate:
                    passing.append(c)
                logger.info(
                    "block %d candidate cluster %d: %d trials, "
                    "matched=%d r=%.4f cong=%.2f -> %s",
                    blk, c.cluster_index, len(c.trials),
                    len(c.alignment.matched_pairs),
                    c.alignment.rt_correlation,
                    c.alignment.congruency_agreement,
                    "PASS" if c.passed_gate else "fail",
                )

            if len(passing) == 1:
                selected[blk] = passing[0]
                codes.append((
                    "B000", INFO,
                    f"Block {blk}: 1 of {len(cands)} candidate runs passed "
                    f"behavioural alignment (cluster {passing[0].cluster_index}, "
                    f"{len(passing[0].trials)} trials).",
                ))
            elif len(passing) == 0:
                # HALT — no candidate can be trusted for this block.
                codes.append((
                    "B001", HALT,
                    f"Block {blk}: none of {len(cands)} candidate runs passed "
                    f"behavioural alignment; block cannot be selected.",
                ))
            else:
                # HALT — more than one candidate passes; ambiguous.
                codes.append((
                    "B002", HALT,
                    f"Block {blk}: {len(passing)} candidate runs passed "
                    f"behavioural alignment; selection is ambiguous.",
                ))
                # Do not select a block we cannot disambiguate.
        else:
            # Fallback: most trials; ties -> latest in time.
            best = max(cands, key=lambda c: (len(c.trials), c.t_end_global))
            selected[blk] = best
            logger.warning(
                "block %d: no behavioural data; fell back to trial-count "
                "selection (chose cluster %d with %d trials, latest on ties)",
                blk, best.cluster_index, len(best.trials),
            )

    return SelectionResult(
        selected_trials=_renumber(selected),
        selected_by_block=selected,
        candidates_by_block=by_block,
        codes=codes,
        used_behavioural=used_beh,
    )
