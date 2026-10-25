"""
Unit tests for eval.scorer.
Tests scoring of IncidentReport JSON against ground truth files.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.scorer import score_incident, save_score_record


def test_scorer_matches_ground_truth(tmp_path: Path):
    # Create fake ground truth
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    gt_file = gt_dir / "fault-123.json"
    gt_file.write_text(
        json.dumps({
            "fault_id": "fault-123",
            "failure_mode": "connection_pool_exhaustion",
            "target_service": "payment",
        })
    )

    # Create fake report
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir()
    rep_file = rep_dir / "inc-123.json"
    rep_file.write_text(
        json.dumps({
            "incident_id": "inc-123",
            "diagnosis": {
                "outcome": "root_cause",
                "root_cause": "connection_pool_exhaustion",
                "confidence": 0.88,
                "iterations_used": 5,
            },
            "remediation": {
                "pr_url": "https://github.com/ayush-prataps/surveyops-target-app/pull/1",
                "branch": "fix/connection_pool_exhaustion_123",
            },
            "timings": {"total": 85.5},
            "usage": {"total_tokens": 42000},
        })
    )

    score = score_incident(rep_file, ground_truth_path=gt_file)
    assert score.is_correct is True
    assert score.ground_truth_failure_mode == "connection_pool_exhaustion"
    assert score.predicted_root_cause == "connection_pool_exhaustion"
    assert score.remediation_pr_opened is True
    assert score.confidence == 0.88
    assert score.iterations_used == 5

    out_file = save_score_record(score, output_dir=tmp_path / "scores")
    assert out_file.exists()


def test_scorer_detects_mismatch(tmp_path: Path):
    gt_file = tmp_path / "fault.json"
    gt_file.write_text(
        json.dumps({
            "fault_id": "fault-999",
            "failure_mode": "retry_storm_without_backoff",
        })
    )

    rep_file = tmp_path / "report.json"
    rep_file.write_text(
        json.dumps({
            "incident_id": "inc-999",
            "diagnosis": {
                "outcome": "root_cause",
                "root_cause": "connection_pool_exhaustion",
                "confidence": 0.90,
                "iterations_used": 3,
            },
        })
    )

    score = score_incident(rep_file, ground_truth_path=gt_file)
    assert score.is_correct is False
    assert score.remediation_pr_opened is False
