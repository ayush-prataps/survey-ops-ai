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
from typing import Optional, Any
from surveyops.schemas.diagnosis import Outcome, LedgerEntry
from surveyops.schemas.propositions import Proposition, CheckableSignal
from surveyops.schemas.experiments import Observation
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


def evaluate_checkable_signal_for_leader(
    leader: HypothesisItem,
    ledger: list[LedgerEntry],
    propositions: list[Proposition],
    observations: list[Observation] | None = None,
) -> tuple[bool, str]:
    """
    Deterministically evaluates leader's cited Observations against the proposition's
    machine-checkable signal (Requirement 2 & 3):
    - Propositions without a CheckableSignal cannot win.
    - Leader must have cited observation(s) providing positive empirical evidence.
    - Evaluates metric thresholds (comparator, threshold) and/or log patterns.
    """
    prop = next((p for p in propositions if p.proposition_id == leader.supporting_proposition_id), None)
    if not prop and not leader.supporting_proposition_id:
        prop = next((p for p in propositions if p.failure_mode == leader.failure_mode), None)

    if not prop or not getattr(prop, "checkable_signal", None):
        return False, "Propositions without a machine-checkable signal cannot win"

    sig: CheckableSignal = prop.checkable_signal

    leader_entries = [e for e in ledger if e.hypothesis_id == leader.hypothesis_id]
    if not leader_entries:
        return False, "Leader has no ledger entries recorded"

    # Extract cited request IDs and fields from evidence_for
    citation_pattern = re.compile(r"((?:req|exp)-[a-zA-Z0-9_\-]+)")
    cited_req_ids = set()
    cited_evidence_items: list[str] = []
    for e in leader_entries:
        for item in e.evidence_for:
            cited_evidence_items.append(item)
            for m in citation_pattern.finditer(item):
                cited_req_ids.add(m.group(1))

    if not cited_req_ids:
        return False, "Leader has no cited request_ids in evidence_for"

    obs_list = list(observations or [])
    cited_obs = [o for o in obs_list if o.request_id in cited_req_ids]

    # Evaluate positive evidence against CheckableSignal
    # 1. Metric check
    if sig.type == "metric" or sig.metric_name:
        metric_name = sig.metric_name
        comparator = sig.comparator or ">="
        threshold = sig.threshold if sig.threshold is not None else 0.0

        for obs in cited_obs:
            obs_data = obs.data or {}
            val = obs_data.get(metric_name)
            if val is None and metric_name == "pool_utilization_ratio":
                # Fallback to ratio or calculate from in_use and capacity
                val = obs_data.get("ratio")
                if val is None and "pool_in_use" in obs_data and "pool_capacity" in obs_data:
                    cap = obs_data.get("pool_capacity")
                    in_use = obs_data.get("pool_max_in_use", obs_data.get("pool_in_use"))
                    if cap and float(cap) > 0:
                        val = float(in_use) / float(cap)

            if val is not None:
                try:
                    num_val = float(val)
                    matched = False
                    if comparator == ">=":
                        matched = (num_val >= threshold)
                    elif comparator == ">":
                        matched = (num_val > threshold)
                    elif comparator == "<=":
                        matched = (num_val <= threshold)
                    elif comparator == "<":
                        matched = (num_val < threshold)
                    elif comparator == "==":
                        matched = (abs(num_val - threshold) < 1e-6)

                    if matched:
                        return True, (
                            f"Positive metric evidence verified in [{obs.request_id}]: "
                            f"{metric_name}={num_val} {comparator} {threshold}"
                        )
                except (ValueError, TypeError):
                    pass

    # 2. Log pattern check (for log_pattern signals or metric fallback patterns)
    if sig.log_pattern:
        pattern = sig.log_pattern
        for obs in cited_obs:
            obs_data = obs.data or {}
            log_text = ""
            if "logs" in obs_data:
                log_text += str(obs_data["logs"])
            if "lines" in obs_data:
                lines = obs_data["lines"]
                log_text += "\n" + ("\n".join(lines) if isinstance(lines, list) else str(lines))

            if log_text and re.search(pattern, log_text, re.IGNORECASE):
                m = re.search(pattern, log_text, re.IGNORECASE)
                match_snippet = m.group(0) if m else pattern
                return True, (
                    f"Positive log evidence verified in [{obs.request_id}]: "
                    f"matched pattern '{pattern}' ('{match_snippet}')"
                )

    # Also check if cited text in evidence_for explicitly provides verified metric or log match
    for item in cited_evidence_items:
        if sig.type == "metric" and sig.metric_name:
            m_metric = re.search(re.escape(sig.metric_name) + r"\s*(?:=|is|:)\s*([0-9.]+)", item, re.IGNORECASE)
            if m_metric:
                try:
                    val = float(m_metric.group(1))
                    comp = sig.comparator or ">="
                    thresh = sig.threshold if sig.threshold is not None else 0.0
                    matched = False
                    if comp == ">=": matched = (val >= thresh)
                    elif comp == ">": matched = (val > thresh)
                    elif comp == "<=": matched = (val <= thresh)
                    elif comp == "<": matched = (val < thresh)
                    elif comp == "==": matched = (abs(val - thresh) < 1e-6)
                    if matched:
                        return True, f"Positive metric evidence cited in ledger: {sig.metric_name}={val} {comp} {thresh}"
                except ValueError:
                    pass
        if sig.log_pattern and re.search(sig.log_pattern, item, re.IGNORECASE):
            # Only count if the cited req_id is an actual observation of source docker_logs or similar
            cited_in_log_obs = any(o.request_id in item and (o.source in ("docker_logs", "logs") or "logs" in o.data or "lines" in o.data) for o in cited_obs)
            if cited_in_log_obs:
                return True, f"Positive log pattern cited from log observation: '{sig.log_pattern}'"

    return False, (
        f"Positive evidence not found: required {sig.type} signal "
        f"({sig.metric_name or ''} {sig.comparator or ''} {sig.threshold or ''} / log: '{sig.log_pattern or ''}') "
        f"not satisfied by cited observations {sorted(cited_req_ids)}"
    )


def compute_heuristic_confidence_score(
    leader: HypothesisItem,
    ledger: list[LedgerEntry],
    margin: float,
    disconfirming_passed: bool,
    has_positive_evidence: bool = False,
) -> float:
    """
    Computes heuristic confidence score purely in code (Requirement 5):
    - Distinct cited supporting observations in evidence_for.
    - Whether disconfirming test passed.
    - Margin over runner-up.
    - Hard requirement: Must have positive evidence (from checkable signal); without it, score cannot exceed 0.35.
    """
    if not has_positive_evidence:
        return 0.35

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
    observations: list[Observation] | None = None,
) -> ExitDecision:
    """
    Evaluates deterministic exit conditions for the current diagnostic iteration (Requirement 2 & 5).
    root_cause requires ALL of:
    (a) at least one experiment run specifically against leader's disconfirming_if
    (b) that experiment's verdict is 'not met' (disconfirming_condition_met == False)
    (c) confidence margin over runner-up >= CONFIDENCE_MARGIN (default 0.20)
    (d) leader's cited evidence satisfies machine-checkable signal (has_positive_evidence == True)
    (e) heuristic confidence score >= CONFIDENCE_THRESHOLD (default 0.80)
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

    # Signal match & Heuristic confidence score check (Requirements 2, 3, 5)
    if ledger is not None:
        has_positive_evidence, signal_reason = evaluate_checkable_signal_for_leader(
            active_top,
            ledger,
            propositions or [],
            observations or [],
        )
        heuristic_score = compute_heuristic_confidence_score(
            active_top,
            ledger,
            margin,
            disconfirming_passed,
            has_positive_evidence=has_positive_evidence,
        )
    else:
        has_positive_evidence, signal_reason = False, "no_ledger_provided"
        heuristic_score = 0.35

    logger.info(
        f"EXIT EVAL: leader={active_top.hypothesis_id} self_conf={active_top.confidence:.2f}, "
        f"heuristic={heuristic_score:.2f} (margin={margin:.2f}, disconfirming_passed={disconfirming_passed}, "
        f"has_positive_evidence={has_positive_evidence}, reason='{signal_reason}')"
    )

    # Check root_cause criteria: ALL of (a), (b), (c), (d), (e)
    root_cause_eligible = (
        has_disconfirming_test
        and disconfirming_passed
        and (not disconfirming_met)
        and margin >= conf_margin
        and heuristic_score >= conf_thresh
        and has_positive_evidence
    )

    if root_cause_eligible:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.root_cause,
            top_hypothesis=active_top,
            confidence=heuristic_score,
            self_reported_confidence=active_top.confidence,
            reason=(
                f"Leader '{active_top.hypothesis_id}' ({active_top.failure_mode}) satisfied all root_cause gates: "
                f"disconfirming test passed (verdict 'not met'), margin {margin:.2f} >= {conf_margin:.2f}, "
                f"heuristic confidence {heuristic_score:.2f} >= {conf_thresh:.2f}, "
                f"and {signal_reason}."
            ),
        )

    # If no hypothesis has positive evidence and loop reaches max iterations -> ABSTAIN
    if iteration >= max_iters:
        if not has_positive_evidence:
            return ExitDecision(
                should_exit=True,
                outcome=Outcome.abstained,
                top_hypothesis=active_top,
                confidence=heuristic_score,
                self_reported_confidence=active_top.confidence,
                reason=(
                    f"Abstained: Loop reached max iterations ({iteration}/{max_iters}) "
                    f"without positive empirical evidence for leader '{active_top.hypothesis_id}' ({signal_reason})."
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
                f"disconfirming_tested={has_disconfirming_test}, disconfirming_passed={disconfirming_passed}, "
                f"positive_evidence={has_positive_evidence})."
            ),
        )

    # If LLM explicitly recommends abstain and leader lacks positive evidence
    if llm_recommendation.lower() in ("abstain", "conclude") and not has_positive_evidence:
        return ExitDecision(
            should_exit=True,
            outcome=Outcome.abstained,
            top_hypothesis=active_top,
            confidence=heuristic_score,
            self_reported_confidence=active_top.confidence,
            reason=f"Abstained: LLM recommended '{llm_recommendation}' and no positive empirical evidence supports leader ({signal_reason}).",
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
            f"disconfirming_tested={has_disconfirming_test}, disconfirming_passed={disconfirming_passed}, "
            f"positive_evidence={has_positive_evidence} ({signal_reason})."
        ),
    )

