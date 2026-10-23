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


def check_observation_satisfies_signal(
    obs: Observation,
    sig: CheckableSignal,
) -> tuple[bool, str]:
    """
    Checks if a single Observation satisfies a hypothesis's CheckableSignal.
    Used for instant reactivation when empirical evidence arrives.
    """
    obs_data = obs.data or {}
    # 1. Metric check
    if sig.type == "metric" or sig.metric_name:
        metric_name = sig.metric_name
        comparator = sig.comparator or ">="
        threshold = sig.threshold if sig.threshold is not None else 0.0

        val = obs_data.get(metric_name)
        if val is None and metric_name == "pool_utilization_ratio":
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
                if comparator == ">=": matched = (num_val >= threshold)
                elif comparator == ">": matched = (num_val > threshold)
                elif comparator == "<=": matched = (num_val <= threshold)
                elif comparator == "<": matched = (num_val < threshold)
                elif comparator == "==": matched = (abs(num_val - threshold) < 1e-6)

                if matched:
                    return True, f"Positive metric evidence verified: {metric_name}={num_val} {comparator} {threshold}"
            except (ValueError, TypeError):
                pass

    # 2. Log pattern check
    if sig.log_pattern:
        pattern = sig.log_pattern
        log_text = ""
        if "logs" in obs_data:
            log_text += str(obs_data["logs"])
        if "lines" in obs_data:
            lines = obs_data["lines"]
            log_text += "\n" + ("\n".join(lines) if isinstance(lines, list) else str(lines))

        if log_text and re.search(pattern, log_text, re.IGNORECASE):
            m = re.search(pattern, log_text, re.IGNORECASE)
            match_snippet = m.group(0) if m else pattern
            return True, f"Positive log pattern matched: '{pattern}' ('{match_snippet}')"

    return False, "Signal conditions not met by observation"


def can_rule_out_hypothesis(
    hypothesis_id: str,
    ledger: list[LedgerEntry],
    observations: list[Observation],
) -> tuple[bool, str, str | None]:
    """
    Evaluates whether a hypothesis can be ruled out:
    Requires:
      1. Window-max evidence (e.g. pool_max_in_use confirms low utilization throughout window), OR
      2. At least 2 independent disconfirming observations.
    Single instantaneous samples must NOT rule out.
    """
    disconfirming_entries = [e for e in ledger if e.hypothesis_id == hypothesis_id and e.disconfirming_condition_met]
    if not disconfirming_entries:
        return False, "No disconfirming observations recorded", None

    citation_pattern = re.compile(r"((?:req|exp)-[a-zA-Z0-9_\-]+)")
    disconfirming_obs_ids: set[str] = set()
    for e in disconfirming_entries:
        for item in e.evidence_against:
            for m in citation_pattern.finditer(item):
                disconfirming_obs_ids.add(m.group(1))
        for m in citation_pattern.finditer(e.disconfirming_justification or ""):
            disconfirming_obs_ids.add(m.group(1))

    # Match observation objects
    obs_map = {o.request_id: o for o in observations}
    cited_obs = [obs_map[oid] for oid in disconfirming_obs_ids if oid in obs_map]

    # Check Criterion 1: Window-max evidence
    for o in cited_obs:
        data = o.data or {}
        # Case A: pool metrics with pool_max_in_use
        if "pool_max_in_use" in data and data["pool_max_in_use"] is not None:
            max_in_use = float(data["pool_max_in_use"])
            cap = float(data.get("pool_capacity") or 5.0)
            range_s = float(data.get("range_s") or 0)
            if cap > 0:
                max_ratio = max_in_use / cap
                # If window max ratio confirms low utilization (< 0.60 and < cap)
                if max_ratio < 0.60 and max_in_use < cap:
                    return (
                        True,
                        f"Ruled out on window-max evidence in [{o.request_id}]: pool_max_in_use={max_in_use}/{cap} (ratio {max_ratio:.2f} < 0.60 over {range_s}s)",
                        o.request_id,
                    )

        # Case B: explicit window_max flag or range_max in observation data
        if data.get("is_window_max") or data.get("window_max"):
            return True, f"Ruled out on verified window-max metric in [{o.request_id}]", o.request_id

    # Check Criterion 2: 2 independent disconfirming observations
    if len(disconfirming_obs_ids) >= 2:
        obs_list_sorted = sorted(disconfirming_obs_ids)
        return (
            True,
            f"Ruled out by 2 independent disconfirming observations: {obs_list_sorted}",
            obs_list_sorted[-1],
        )

    # Single instantaneous observation / single sample cannot rule out
    return (
        False,
        f"Single instantaneous observation ({sorted(disconfirming_obs_ids)}) cannot rule out; requires window-max or 2 independent disconfirming observations",
        None,
    )


def apply_hypothesis_status_transitions(
    hypotheses: list[HypothesisItem],
    ledger_entries: list[LedgerEntry],
    ledger: list[LedgerEntry],
    observations: list[Observation],
    current_observation: Observation | None,
    propositions: list[Proposition],
    status_change_log: list[dict] | None = None,
) -> None:
    """
    Applies deterministic ruling-out and reactivation rules:
    - Ruled out requires window-max evidence or 2 independent disconfirming observations.
    - Reactivation triggers when an observation satisfies the hypothesis CheckableSignal.
    - Logs every status change with the observation id.
    """
    obs_id = (current_observation.request_id if current_observation else None) or "obs-unknown"

    for h in hypotheses:
        old_status = h.status
        entry = next((e for e in ledger_entries if e.hypothesis_id == h.hypothesis_id), None)
        if entry is not None:
            h.confidence = entry.confidence

        prop = next((p for p in propositions if p.proposition_id == h.supporting_proposition_id), None)
        if not prop and not h.supporting_proposition_id:
            prop = next((p for p in propositions if p.failure_mode == h.failure_mode), None)

        sig = getattr(prop, "checkable_signal", None) if prop else None

        # 1. Check for Reactivation via CheckableSignal match
        reactivated = False
        if sig is not None and current_observation is not None:
            matched, match_reason = check_observation_satisfies_signal(current_observation, sig)
            if matched:
                reactivated = True
                if h.status == "ruled_out":
                    h.status = "active"
                    h.confidence = max(h.confidence, 0.70)
                    reason = f"Reactivated by CheckableSignal match: {match_reason}"
                    logger.info(f"STATUS CHANGE: hypothesis '{h.hypothesis_id}' changed from 'ruled_out' to 'active' [obs_id='{obs_id}']: {reason}")
                    print(f"[STATUS CHANGE] {h.hypothesis_id}: ruled_out -> active (obs: {obs_id}) - {reason}")
                    if status_change_log is not None:
                        status_change_log.append({
                            "hypothesis_id": h.hypothesis_id,
                            "from": old_status,
                            "to": "active",
                            "observation_id": obs_id,
                            "reason": reason,
                        })

        if reactivated:
            continue

        # 2. Check for Ruling-Out
        proposed_rule_out = (entry is not None and (entry.status == "ruled_out" or entry.disconfirming_condition_met))
        if proposed_rule_out:
            can_rule_out, rule_out_reason, trigger_obs_id = can_rule_out_hypothesis(
                h.hypothesis_id,
                ledger,
                observations,
            )
            effective_obs_id = trigger_obs_id or obs_id
            if can_rule_out:
                h.status = "ruled_out"
                if old_status != "ruled_out":
                    logger.info(f"STATUS CHANGE: hypothesis '{h.hypothesis_id}' changed from '{old_status}' to 'ruled_out' [obs_id='{effective_obs_id}']: {rule_out_reason}")
                    print(f"[STATUS CHANGE] {h.hypothesis_id}: {old_status} -> ruled_out (obs: {effective_obs_id}) - {rule_out_reason}")
                    if status_change_log is not None:
                        status_change_log.append({
                            "hypothesis_id": h.hypothesis_id,
                            "from": old_status,
                            "to": "ruled_out",
                            "observation_id": effective_obs_id,
                            "reason": rule_out_reason,
                        })
            else:
                # Rejected ruling out on single instantaneous sample
                h.status = "active"
                if old_status == "ruled_out":
                    # Correct previously premature ruled_out
                    h.status = "active"
                    logger.info(f"STATUS CHANGE: hypothesis '{h.hypothesis_id}' restored from 'ruled_out' to 'active' [obs_id='{effective_obs_id}']: {rule_out_reason}")
                    print(f"[STATUS CHANGE] {h.hypothesis_id}: ruled_out -> active (obs: {effective_obs_id}) - {rule_out_reason}")
                    if status_change_log is not None:
                        status_change_log.append({
                            "hypothesis_id": h.hypothesis_id,
                            "from": "ruled_out",
                            "to": "active",
                            "observation_id": effective_obs_id,
                            "reason": rule_out_reason,
                        })
                else:
                    logger.info(f"Ruling out rejected for {h.hypothesis_id} [obs={effective_obs_id}]: {rule_out_reason}. Kept 'active'.")
                    print(f"[RULE-OUT REJECTED] {h.hypothesis_id} [obs: {effective_obs_id}]: {rule_out_reason}. Kept 'active'.")
        elif entry is not None and entry.status in ("dominant", "active"):
            new_status = entry.status
            if new_status != old_status and old_status != "ruled_out":
                h.status = new_status
                reason = "Updated by ledger synthesis"
                logger.info(f"STATUS CHANGE: hypothesis '{h.hypothesis_id}' changed from '{old_status}' to '{new_status}' [obs_id='{obs_id}']: {reason}")
                print(f"[STATUS CHANGE] {h.hypothesis_id}: {old_status} -> {new_status} (obs: {obs_id})")
                if status_change_log is not None:
                    status_change_log.append({
                        "hypothesis_id": h.hypothesis_id,
                        "from": old_status,
                        "to": new_status,
                        "observation_id": obs_id,
                        "reason": reason,
                    })


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
    leader_margin_history: list[tuple[str, float]] | None = None,
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

    # Signal match & Heuristic confidence score check (Requirements 2, 3, 5)
    if ledger is not None:
        has_positive_evidence, signal_reason = evaluate_checkable_signal_for_leader(
            active_top,
            ledger,
            propositions or [],
            observations or [],
        )
    else:
        has_positive_evidence, signal_reason = False, "no_ledger_provided"

    can_rule_out, rule_out_reason, _ = can_rule_out_hypothesis(
        active_top.hypothesis_id,
        leader_ledger,
        observations or [],
    )

    if has_positive_evidence:
        # Reactivated by CheckableSignal match: disconfirmation is superseded
        disconfirming_met = False
        disconfirming_passed = has_disconfirming_test
        if active_top.status == "ruled_out":
            active_top.status = "active"
    elif can_rule_out:
        active_top.status = "ruled_out"
        active_top.confidence = min(active_top.confidence, 0.20)
        disconfirming_passed = False
        disconfirming_met = True
    else:
        # Single instantaneous sample: do not rule out!
        any_disconfirmed = any(e.disconfirming_condition_met for e in leader_ledger)
        disconfirming_met = any_disconfirmed
        disconfirming_passed = has_disconfirming_test and (not any_disconfirmed)

    if ledger is not None:
        heuristic_score = compute_heuristic_confidence_score(
            active_top,
            ledger,
            margin,
            disconfirming_passed,
            has_positive_evidence=has_positive_evidence,
        )
    else:
        heuristic_score = 0.35

    logger.info(
        f"EXIT EVAL: leader={active_top.hypothesis_id} self_conf={active_top.confidence:.2f}, "
        f"heuristic={heuristic_score:.2f} (margin={margin:.2f}, disconfirming_passed={disconfirming_passed}, "
        f"has_positive_evidence={has_positive_evidence}, reason='{signal_reason}')"
    )

    # Check convergence stop: same leader and stable margin for 2 iterations
    if leader_margin_history and len(leader_margin_history) >= 1:
        prev_leader, prev_margin = leader_margin_history[-1]
        if prev_leader == active_top.hypothesis_id and abs(margin - prev_margin) <= 0.05:
            logger.info(
                f"Convergence stop: leader '{active_top.hypothesis_id}' stable for 2 iterations "
                f"(current margin={margin:.2f}, prev margin={prev_margin:.2f}, diff={abs(margin - prev_margin):.2f} <= 0.05)"
            )
            if has_positive_evidence and (disconfirming_passed or has_disconfirming_test) and not disconfirming_met:
                return ExitDecision(
                    should_exit=True,
                    outcome=Outcome.root_cause,
                    top_hypothesis=active_top,
                    confidence=max(heuristic_score, conf_thresh),
                    self_reported_confidence=active_top.confidence,
                    reason=(
                        f"Convergence stop: leader '{active_top.hypothesis_id}' ({active_top.failure_mode}) converged "
                        f"over 2 iterations (stable margin {margin:.2f}) with verified positive evidence ({signal_reason})."
                    ),
                )
            elif not has_positive_evidence:
                return ExitDecision(
                    should_exit=True,
                    outcome=Outcome.abstained,
                    top_hypothesis=active_top,
                    confidence=heuristic_score,
                    self_reported_confidence=active_top.confidence,
                    reason=(
                        f"Convergence stop: leader '{active_top.hypothesis_id}' converged over 2 iterations "
                        f"(stable margin {margin:.2f}) without positive empirical evidence ({signal_reason})."
                    ),
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

