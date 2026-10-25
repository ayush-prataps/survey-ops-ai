"""
CLI entrypoint for eval.scorer.
Usage:
    python -m eval.scorer [path/to/report.json] [--ground-truth path/to/fault.json]
"""

import argparse
import sys
from pathlib import Path

from eval.scorer.scorer import (
    find_latest_file,
    print_score_report,
    save_score_record,
    score_incident,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="SurveyOps Post-Run Evaluation Scorer")
    parser.add_argument("report", nargs="?", default=None, help="Path to IncidentReport JSON file")
    parser.add_argument("--ground-truth", dest="ground_truth", default=None, help="Path to ground truth JSON file")
    parser.add_argument("--reports-dir", dest="reports_dir", default="eval/reports", help="Directory containing reports")
    args = parser.parse_args()

    report_path: Path | None = None
    if args.report:
        report_path = Path(args.report)
    else:
        report_path = find_latest_file(Path(args.reports_dir), "*.json")

    if not report_path or not report_path.exists():
        print(f"[ERROR] No report JSON file found in {args.reports_dir} or specified path.", file=sys.stderr)
        sys.exit(1)

    gt_path = Path(args.ground_truth) if args.ground_truth else None
    score = score_incident(report_path, ground_truth_path=gt_path)
    print_score_report(score)
    saved = save_score_record(score)
    print(f"[ok] Evaluation score saved to: {saved}")


if __name__ == "__main__":
    main()
