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
from surveyops.schemas.diagnosis import Outcome
from .schemas import ExitDecision, HypothesisItem

DEFAULT_CONFIDENCE_THRESHOLD = 0.80
DEFAULT_CONFIDENCE_MARGIN = 0.20
DEFAULT_MAX_LOOP_ITERATIONS = 5


def get_confidence_threshold() -> float:
    return float(os.environ.get("CONFIDENCE_THRESHOLD", str(DEFAULT_CONFIDENCE_THRESHOLD)))


def get_confidence_margin() -> float:
    return float(os.environ.get("CONFIDENCE_MARGIN", str(DEFAULT_CONFIDENCE_MARGIN)))


def get_max_loop_iterations() -> int:
    return int(os.environ.get("MAX_LOOP_ITERATIONS", str(DEFAULT_MAX_LOOP_ITERATIONS)))


def evaluate_exit_conditions(
    hypotheses: list[HypothesisItem],
    tested_disconfirming_hypotheses: set[str],
    iteration: int,
    max_iterations: int | None = None,
    confidence_threshold: float | None = None,
    confidence_margin: float | None = None,
    llm_recommendation: str = "continue",
) -> ExitDecision:
    """
    Evaluates deterministic exit conditions for the current diagnostic iteration.
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

    has_disconfirming_test = active_top.hypothesis_id in tested_disconfirming_hypotheses

    # Check root_cause criteria:
    # 1. Confidence threshold passed
    # 2. Margin over active runner-up passed
    # 3. At least one disconfirming experiment tested against it
    if (
        active_top.confidence >= conf_thresh
        and margin >= conf_margin
        and has_disconfirming_test
    ):
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.root_cause,
            top_hypothesis=active_top,
            confidence=active_top.confidence,
            reason=(
                f"Top hypothesis '{active_top.hypothesis_id}' ({active_top.failure_mode}) passed "
                f"confidence threshold {conf_thresh:.2f} (got {active_top.confidence:.2f}), "
                f"margin threshold {conf_margin:.2f} (got {margin:.2f}), and completed a disconfirming experiment."
            ),
        )

    # Check iteration budget exhaustion
    if iteration >= max_iters:
        # If confidence is very low (< 0.40) or recommendation is abstain, abstain
        if active_top.confidence < 0.40 or llm_recommendation.lower() == "abstain":
            return ExitDecision(
                should_exit=True,
                outcome=Outcome.abstained,
                top_hypothesis=active_top,
                confidence=active_top.confidence,
                reason=(
                    f"Iteration budget exhausted at iteration {iteration}/{max_iters} "
                    f"with insufficient evidence (top confidence {active_top.confidence:.2f} < 0.40)."
                ),
            )
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.budget_exhausted,
            top_hypothesis=active_top,
            confidence=active_top.confidence,
            reason=(
                f"Iteration budget exhausted ({iteration}/{max_iters}) without meeting root-cause criteria "
                f"(confidence={active_top.confidence:.2f}/{conf_thresh:.2f}, margin={margin:.2f}/{conf_margin:.2f}, "
                f"disconfirming_tested={has_disconfirming_test})."
            ),
        )

    # If LLM explicitly recommends abstain and active hypotheses lack confidence
    if llm_recommendation.lower() == "abstain" and active_top.confidence < 0.50:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.abstained,
            top_hypothesis=active_top,
            confidence=active_top.confidence,
            reason="Abstained based on insufficient empirical evidence to support candidate hypotheses.",
        )

    # Otherwise continue to next iteration
    return ExitDecision(
        should_exit=False,
        outcome=Outcome.budget_exhausted,
        top_hypothesis=active_top,
        confidence=active_top.confidence,
        reason=(
            f"Continuing loop (iteration {iteration}/{max_iters}): top hypothesis '{active_top.hypothesis_id}' "
            f"confidence={active_top.confidence:.2f} (need >={conf_thresh:.2f}), margin={margin:.2f} (need >={conf_margin:.2f}), "
            f"disconfirming_tested={has_disconfirming_test}."
        ),
    )
