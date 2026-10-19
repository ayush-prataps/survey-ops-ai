"""
Deterministic Exit Logic for Diagnostician Loop.
Evaluated purely by code (not LLM) as mandated by Requirement 4:
- root_cause ONLY IF:
  1. Top hypothesis passes confidence threshold (CONFIDENCE_THRESHOLD, default 0.80)
  2. At least one disconfirming experiment was run against it
  3. Margin over runner-up is big enough (CONFIDENCE_MARGIN, default 0.20)
  4. Top hypothesis is active / dominant (not ruled_out)
- Otherwise continue until MAX_LOOP_ITERATIONS (default 5).
- If budget exhausted: outcome=budget_exhausted (or abstained if evidence insufficient).
- If all hypotheses ruled out or evidence deemed insufficient: outcome=abstained.
"""

import os
import logging
import os
import re
from typing import Optional
from surveyops.schemas.diagnosis import Outcome, LedgerEntry
from surveyops.schemas.propositions import Proposition
from .schemas import ExitDecision, HypothesisItem

logger = logging.getLogger("surveyops.diagnostician.exit_logic")

DEFAULT_CONFIDENCE_THRESHOLD = 0.80
DEFAULT_CONFIDENCE_MARGIN = 0.20
DEFAULT_MAX_LOOP_ITERATIONS = 5


def get_confidence_threshold() -> float:
    return float(os.environ.get("CONFIDENCE_THRESHOLD", str(DEFAULT_CONFIDENCE_THRESHOLD)))


def get_confidence_margin() -> float:
    return float(os.environ.get("CONFIDENCE_MARGIN", str(DEFAULT_CONFIDENCE_MARGIN)))


def get_max_loop_iterations() -> int:
    return int(os.environ.get("MAX_LOOP_ITERATIONS", str(DEFAULT_MAX_LOOP_ITERATIONS)))


def compute_heuristic_confidence_score(
    leader: HypothesisItem,
    ledger: list[LedgerEntry],
    margin: float,
    disconfirming_passed: bool,
) -> float:
    """
    Computes heuristic confidence score purely in code (Requirement 5):
    - Distinct cited supporting observations in evidence_for.
    - Whether disconfirming test passed.
    - Margin over runner-up.
    """
    leader_entries = [e for e in ledger if e.hypothesis_id == leader.hypothesis_id]
    cited_req_ids = set()
    for e in leader_entries:
        for item in e.evidence_for:
            m = re.search(r"\[([a-zA-Z0-9_\-]+)[:\s.]", item) or re.search(r"((?:req|exp)-[a-zA-Z0-9_\-]+)", item)
            if m:
                cited_req_ids.add(m.group(1))

    n_obs = len(cited_req_ids)
    obs_component = 0.50 if n_obs >= 2 else (0.35 if n_obs == 1 else 0.10)
    disconfirm_component = 0.30 if disconfirming_passed else 0.0
    margin_component = min(0.20, max(0.0, margin * 0.4))

    score = obs_component + disconfirm_component + margin_component
    return round(min(1.0, max(0.0, score)), 2)


def check_predicted_signal_match(
    leader: HypothesisItem,
    ledger: list[LedgerEntry],
    propositions: list[Proposition],
) -> tuple[bool, str]:
    """
    Checks if leader's cited evidence matches the proposition's predicted_signal (Requirement 2d).
    """
    prop = next((p for p in propositions if p.proposition_id == leader.supporting_proposition_id), None)
    if not prop or not prop.predicted_signal:
        leader_entries = [e for e in ledger if e.hypothesis_id == leader.hypothesis_id]
        has_evidence = any(len(e.evidence_for) > 0 for e in leader_entries)
        return has_evidence, "novel_hypothesis_fallback" if not prop else "no_predicted_signal_defined"

    stopwords = {
        "that", "this", "from", "when", "into", "over", "under", "each", "both",
        "some", "more", "most", "also", "were", "been", "have", "will", "with",
    }
    raw_tokens = re.findall(r"\b[a-zA-Z]{4,}\b", prop.predicted_signal.lower())
    key_signal_tokens = {t for t in raw_tokens if t not in stopwords}

    leader_entries = [e for e in ledger if e.hypothesis_id == leader.hypothesis_id]
    all_evidence_text = " ".join(" ".join(e.evidence_for) for e in leader_entries).lower()

    matched = {t for t in key_signal_tokens if t in all_evidence_text}
    if matched:
        return True, f"Matched predicted_signal keywords: {sorted(matched)[:4]}"
    return False, f"No keywords from predicted_signal '{prop.predicted_signal[:60]}' found in cited evidence"


def evaluate_exit_conditions(
    hypotheses: list[HypothesisItem],
    tested_disconfirming_hypotheses: set[str],
    iteration: int,
    max_iterations: int | None = None,
    confidence_threshold: float | None = None,
    confidence_margin: float | None = None,
    llm_recommendation: str = "continue",
    ledger: list[LedgerEntry] | None = None,
    propositions: list[Proposition] | None = None,
) -> ExitDecision:
    """
    Evaluates deterministic exit conditions for the current diagnostic iteration (Requirement 2 & 5).
    root_cause requires ALL of:
    (a) at least one experiment run specifically against leader's disconfirming_if
    (b) that experiment's verdict is 'not met' (disconfirming_condition_met == False)
    (c) confidence margin over runner-up
    (d) leader's cited evidence includes at least one observation matching predicted_signal
    """
    conf_thresh = confidence_threshold if confidence_threshold is not None else get_confidence_threshold()
    conf_margin = confidence_margin if confidence_margin is not None else get_confidence_margin()
    max_iters = max_iterations if max_iterations is not None else get_max_loop_iterations()

    if not hypotheses:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.abstained,
            top_hypothesis=None,
            confidence=0.0,
            self_reported_confidence=0.0,
            reason="No hypotheses available in state.",
        )

    # Sort hypotheses by confidence descending
    sorted_hypotheses = sorted(hypotheses, key=lambda h: h.confidence, reverse=True)
    top_h = sorted_hypotheses[0]

    # Check if all hypotheses are ruled out
    active_hypotheses = [h for h in sorted_hypotheses if h.status != "ruled_out"]
    if not active_hypotheses:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.abstained,
            top_hypothesis=top_h,
            confidence=top_h.confidence,
            self_reported_confidence=top_h.confidence,
            reason="All candidate hypotheses were ruled out by disconfirming observations.",
        )

    # Calculate margin over highest active rival
    active_top = active_hypotheses[0]
    active_rivals = active_hypotheses[1:]
    if active_rivals:
        runner_up = active_rivals[0]
        margin = active_top.confidence - runner_up.confidence
    else:
        margin = 1.0  # Dominates completely; all rivals ruled out

    # Disconfirming test checks
    has_disconfirming_test = active_top.hypothesis_id in tested_disconfirming_hypotheses
    leader_ledger = [e for e in (ledger or []) if e.hypothesis_id == active_top.hypothesis_id]
    disconfirming_met = any(e.disconfirming_condition_met for e in leader_ledger)

    if disconfirming_met:
        # A disconfirming experiment that came back "met" must lower the leader or rule it out
        active_top.status = "ruled_out"
        active_top.confidence = min(active_top.confidence, 0.20)
        disconfirming_passed = False
    else:
        # Survived falsification test if at least one experiment was run against it
        disconfirming_passed = has_disconfirming_test

    # Signal match & Heuristic confidence score check (Requirements 2d & 5)
    if ledger is not None:
        signal_matched, signal_reason = check_predicted_signal_match(active_top, ledger, propositions or [])
        heuristic_score = compute_heuristic_confidence_score(active_top, ledger, margin, disconfirming_passed)
    else:
        signal_matched, signal_reason = True, "no_ledger_provided"
        heuristic_score = active_top.confidence

    logger.info(
        f"HEURISTIC SCORE: {active_top.hypothesis_id} self_reported={active_top.confidence:.2f}, "
        f"heuristic={heuristic_score:.2f} (margin={margin:.2f}, disconfirming_passed={disconfirming_passed}, "
        f"signal_matched={signal_matched})"
    )

    # Check root_cause criteria: ALL of (a), (b), (c), (d)
    root_cause_eligible = (
        has_disconfirming_test
        and disconfirming_passed
        and (not disconfirming_met)
        and margin >= conf_margin
        and (heuristic_score >= conf_thresh if ledger is not None else active_top.confidence >= conf_thresh)
        and signal_matched
    )

    if root_cause_eligible:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.root_cause,
            top_hypothesis=active_top,
            confidence=heuristic_score if ledger is not None else active_top.confidence,
            self_reported_confidence=active_top.confidence,
            reason=(
                f"Leader '{active_top.hypothesis_id}' ({active_top.failure_mode}) satisfied all root_cause gates: "
                f"disconfirming test passed (verdict 'not met'), margin {margin:.2f} >= {conf_margin:.2f}, "
                f"confidence (heuristic_score={heuristic_score:.2f}, self={active_top.confidence:.2f}) >= {conf_thresh:.2f}, "
                f"disconfirming_tested={has_disconfirming_test}, and {signal_reason}."
            ),
        )

    # Check iteration budget exhaustion
    if iteration >= max_iters:
        if active_top.confidence < 0.40 or heuristic_score < 0.40 or llm_recommendation.lower() == "abstain":
            return ExitDecision(
                should_exit=True,
                outcome=Outcome.abstained,
                top_hypothesis=active_top,
                confidence=heuristic_score,
                self_reported_confidence=active_top.confidence,
                reason=(
                    f"Iteration budget exhausted at iteration {iteration}/{max_iters} "
                    f"with insufficient evidence (confidence {heuristic_score:.2f} < 0.40)."
                ),
            )
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.budget_exhausted,
            top_hypothesis=active_top,
            confidence=heuristic_score,
            self_reported_confidence=active_top.confidence,
            reason=(
                f"Iteration budget exhausted ({iteration}/{max_iters}) without meeting root-cause criteria "
                f"(confidence={heuristic_score:.2f}/{conf_thresh:.2f}, margin={margin:.2f}/{conf_margin:.2f}, "
                f"disconfirming_tested={has_disconfirming_test}, disconfirming_passed={disconfirming_passed}, signal_matched={signal_matched})."
            ),
        )

    # If LLM explicitly recommends abstain and active hypotheses lack confidence
    if llm_recommendation.lower() == "abstain" and active_top.confidence < 0.50:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.abstained,
            top_hypothesis=active_top,
            confidence=heuristic_score,
            self_reported_confidence=active_top.confidence,
            reason="Abstained based on insufficient empirical evidence to support candidate hypotheses.",
        )

    # Otherwise continue to next iteration
    return ExitDecision(
        should_exit=False,
        outcome=Outcome.budget_exhausted,
        top_hypothesis=active_top,
        confidence=heuristic_score,
        self_reported_confidence=active_top.confidence,
        reason=(
            f"Continuing loop (iteration {iteration}/{max_iters}): leader '{active_top.hypothesis_id}' "
            f"confidence={heuristic_score:.2f}/{conf_thresh:.2f}, margin={margin:.2f}/{conf_margin:.2f}, "
            f"disconfirming_tested={has_disconfirming_test}, disconfirming_passed={disconfirming_passed}, signal_matched={signal_matched}."
        ),
    )
