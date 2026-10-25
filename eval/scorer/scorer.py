"""
Evaluation Scorer for SurveyOps.
Compares final incident reports against blind ground truth records stored in eval/ground_truth/.
Calculates root-cause accuracy, confidence calibration, iteration efficiency, and remediation status.

Invariants:
- Only eval/scorer/ reads eval/ground_truth/ (after a run). SurveyOps agents never read it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger("eval.scorer")


class EvaluationScore(BaseModel):
    incident_id: str
    fault_id: Optional[str]
    timestamp: str
    ground_truth_failure_mode: Optional[str]
    predicted_root_cause: Optional[str]
    outcome: str
    is_correct: bool
    confidence: Optional[float]
    iterations_used: int
    remediation_pr_opened: bool
    pr_url: Optional[str]
    total_tokens: int
    total_duration_seconds: float


def find_latest_file(directory: Path, pattern: str = "*.json") -> Optional[Path]:
    """Finds the most recently modified JSON file in a directory."""
    if not directory.exists():
        return None
    files = list(directory.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def score_incident(
    report_path: Path,
    ground_truth_path: Optional[Path] = None,
    ground_truth_dir: Path = Path("eval/ground_truth"),
) -> EvaluationScore:
    """Scores an IncidentReport against ground truth."""
    if not report_path.exists():
        raise FileNotFoundError(f"Report file not found: {report_path}")

    report_data = json.loads(report_path.read_text(encoding="utf-8"))
    incident_id = report_data.get("incident_id", report_path.stem)
    diagnosis = report_data.get("diagnosis", {})
    remediation = report_data.get("remediation")
    timings = report_data.get("timings", {})
    usage = report_data.get("usage", {})

    outcome = diagnosis.get("outcome", "unknown")
    pred_rc = diagnosis.get("root_cause")
    conf = diagnosis.get("confidence")
    iters = diagnosis.get("iterations_used", 0)

    pr_url = remediation.get("pr_url") if remediation else None
    rem_opened = bool(pr_url)

    # Locate ground truth
    gt_file = ground_truth_path
    if gt_file is None:
        gt_file = find_latest_file(ground_truth_dir)

    gt_data: dict[str, Any] = {}
    if gt_file and gt_file.exists():
        gt_data = json.loads(gt_file.read_text(encoding="utf-8"))

    gt_failure_mode = gt_data.get("failure_mode")
    fault_id = gt_data.get("fault_id")

    # Accuracy check
    is_correct = (
        outcome == "root_cause"
        and pred_rc is not None
        and gt_failure_mode is not None
        and pred_rc == gt_failure_mode
    )

    tokens = usage.get("total_tokens", 0)
    duration = timings.get("total", 0.0)

    score = EvaluationScore(
        incident_id=incident_id,
        fault_id=fault_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        ground_truth_failure_mode=gt_failure_mode,
        predicted_root_cause=pred_rc,
        outcome=outcome,
        is_correct=is_correct,
        confidence=conf,
        iterations_used=iters,
        remediation_pr_opened=rem_opened,
        pr_url=pr_url,
        total_tokens=tokens,
        total_duration_seconds=duration,
    )

    return score


def print_score_report(score: EvaluationScore) -> None:
    """Prints a structured summary of the evaluation score to stdout."""
    res_str = "CORRECT (1.0)" if score.is_correct else "INCORRECT / MISSED (0.0)"
    conf_str = f"{score.confidence:.2f}" if score.confidence is not None else "N/A"
    pr_str = score.pr_url if score.pr_url else "None"

    print("\n" + "=" * 65)
    print("         SURVEYOPS POST-RUN EVALUATION SCORER")
    print("=" * 65)
    print(f"Incident ID:              {score.incident_id}")
    print(f"Ground Truth Fault ID:    {score.fault_id or 'Unknown'}")
    print(f"Ground Truth Cause:       {score.ground_truth_failure_mode or 'Unknown'}")
    print(f"Diagnostician Conclusion: {score.predicted_root_cause or 'None'} (outcome: {score.outcome})")
    print(f"Score Verdict:            {res_str}")
    print(f"Heuristic Confidence:     {conf_str}")
    print(f"Diagnostic Iterations:    {score.iterations_used}")
    print(f"Remediation PR:           {pr_str}")
    print(f"Total Tokens Used:        {score.total_tokens:,}")
    print(f"Total Wall Duration:      {score.total_duration_seconds:.2f}s")
    print("=" * 65 + "\n")


def save_score_record(score: EvaluationScore, output_dir: Path = Path("eval/scores")) -> Path:
    """Saves score JSON record to eval/scores/."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{score.incident_id}_score.json"
    out_file.write_text(score.model_dump_json(indent=2), encoding="utf-8")
    return out_file
