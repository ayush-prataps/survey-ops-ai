#!/usr/bin/env python3
"""
Scores Diagnostician Validation Runs against Ground Truth.
Reads: eval/validation_rerun_results.json
Computes:
- Total runs
- Outcomes per batch (Control A, Pool Fault Batch 1 & 2, Control B)
- Confident-wrong count over ALL runs
- Average tokens per run and wall time
"""

import json
from pathlib import Path
import sys

RESULTS_FILE = Path("eval/validation_rerun_results.json")

def main():
    if not RESULTS_FILE.exists():
        print(f"Error: {RESULTS_FILE} does not exist. Run scripts/validate_diagnostician.py first.")
        sys.exit(1)

    results = json.loads(RESULTS_FILE.read_text())
    print(f"\n=======================================================")
    print(f"         VALIDATION SCORING REPORT ({len(results)} RUNS)       ")
    print(f"=======================================================")

    total_runs = len(results)
    confident_wrong_total = 0
    correct_root_cause = 0
    abstained_total = 0
    budget_exhausted_total = 0

    print(f"\n{'Batch':<18} | {'Run':<3} | {'Outcome':<16} | {'Root Cause':<28} | {'Conf':<5} | {'Tokens':<7} | {'Time (s)':<8}")
    print("-" * 95)

    for r in results:
        batch = r.get("batch", "unknown")
        run_idx = r.get("run", 0)
        outcome = r.get("outcome", "unknown")
        root_cause = r.get("root_cause") or "None"
        conf = f"{r.get('heuristic_score') or 0.0:.2f}"
        tokens = r.get("total_tokens", 0)
        wall_time = r.get("wall_time_s", 0.0)

        # Ground truth evaluation
        is_confident_wrong = False
        is_correct = False

        if "ControlA" in batch:
            # Healthy system: no root cause exists
            if outcome == "root_cause":
                is_confident_wrong = True
            else:
                is_correct = True
        elif "PoolFault" in batch:
            # Real pool fault: ground truth is connection_pool_exhaustion
            if outcome == "root_cause":
                if root_cause == "connection_pool_exhaustion":
                    is_correct = True
                    correct_root_cause += 1
                else:
                    is_confident_wrong = True
        elif "ControlB" in batch:
            # Upstream 5xx fault
            if outcome == "root_cause":
                if root_cause == "upstream_dependency_failure":
                    is_correct = True
                    correct_root_cause += 1
                else:
                    is_confident_wrong = True
            else:
                # Abstain/budget_exhausted on non-pool fault is safe (not confident-wrong)
                pass

        if is_confident_wrong:
            confident_wrong_total += 1
            status_tag = " [CONFIDENT WRONG!]"
        elif outcome == "abstained":
            abstained_total += 1
            status_tag = ""
        elif outcome == "budget_exhausted":
            budget_exhausted_total += 1
            status_tag = ""
        else:
            status_tag = " [CORRECT]"

        print(f"{batch:<18} | {run_idx:<3} | {outcome:<16} | {root_cause:<28} | {conf:<5} | {tokens:<7} | {wall_time:<8}{status_tag}")

    print("-" * 95)
    avg_tokens = sum(r.get("total_tokens", 0) for r in results) / total_runs if total_runs else 0
    avg_time = sum(r.get("wall_time_s", 0) for r in results) / total_runs if total_runs else 0
    max_tokens = max((r.get("total_tokens", 0) for r in results), default=0)

    print(f"\nSUMMARY STATISTICS:")
    print(f"  Total Runs:              {total_runs}")
    print(f"  Confident-Wrong Runs:    {confident_wrong_total} (TARGET: 0)")
    print(f"  Correct Root Cause:      {correct_root_cause}")
    print(f"  Abstained / Ruled Out:   {abstained_total}")
    print(f"  Budget Exhausted:        {budget_exhausted_total}")
    print(f"  Average Tokens / Run:    {avg_tokens:.1f}")
    print(f"  Max Tokens in Any Run:   {max_tokens} (TARGET: <= 60000)")
    print(f"  Average Wall Time / Run: {avg_time:.2f}s")
    print(f"=======================================================\n")

if __name__ == "__main__":
    main()
