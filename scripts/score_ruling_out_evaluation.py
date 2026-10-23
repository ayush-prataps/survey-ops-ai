#!/usr/bin/env python3
"""
Scores the 6 Pool Runs + 3 Control B runs against Ground Truth.
Reads: eval/ruling_out_evaluation_results.json
Ground Truth:
- PoolFault: connection_pool_exhaustion
- ControlB: no payment internal root cause (abstained is correct, payment causes are ruled out)
Reports:
- Pool correct / 6
- Confident-wrong over all
- Mean tokens
- Per-run summary with status changes
"""

import json
from pathlib import Path
import sys

RESULTS_FILE = Path("eval/ruling_out_evaluation_results.json")

def main():
    if not RESULTS_FILE.exists():
        print(f"Error: {RESULTS_FILE} not found.")
        sys.exit(1)

    runs = json.loads(RESULTS_FILE.read_text())
    print("\n==========================================================================================")
    print("                    EVALUATION SCORING REPORT (9 RUNS)                                  ")
    print("==========================================================================================")

    pool_runs = [r for r in runs if r.get("batch") == "PoolFault"]
    ctrl_b_runs = [r for r in runs if r.get("batch") == "ControlB"]

    pool_correct = 0
    confident_wrong_total = 0

    print(f"\n{'Batch':<12} | {'Run':<3} | {'Outcome':<16} | {'Root Cause':<28} | {'Conf':<5} | {'Iters':<5} | {'Tokens':<7} | {'Status Changes'}")
    print("-" * 115)

    for r in runs:
        batch = r.get("batch")
        run_idx = r.get("run")
        outcome = r.get("outcome")
        root_cause = r.get("root_cause") or "None"
        conf = f"{r.get('confidence') or 0.0:.2f}"
        iters = r.get("iterations", 0)
        tokens = r.get("total_tokens", 0)
        status_changes = r.get("status_changes", [])

        is_cw = False
        is_correct = False

        if batch == "PoolFault":
            if outcome == "root_cause":
                if root_cause == "connection_pool_exhaustion":
                    is_correct = True
                    pool_correct += 1
                else:
                    is_cw = True
        elif batch == "ControlB":
            # Upstream 5xx: payment internal causes must not win
            if outcome == "root_cause":
                if root_cause != "upstream_dependency_failure":
                    is_cw = True
                else:
                    is_correct = True
            else:
                is_correct = True  # Abstained on external fault is safe and correct

        if is_cw:
            confident_wrong_total += 1
            tag = " [CONFIDENT WRONG!]"
        elif is_correct:
            tag = " [CORRECT]"
        else:
            tag = ""

        sc_summary = ", ".join(f"{sc['hypothesis_id']} {sc['from']}->{sc['to']}" for sc in status_changes) or "None"
        print(f"{batch:<12} | {run_idx:<3} | {outcome:<16} | {root_cause:<28} | {conf:<5} | {iters:<5} | {tokens:<7} | {sc_summary}{tag}")

    print("-" * 115)
    mean_tokens_all = sum(r.get("total_tokens", 0) for r in runs) / len(runs) if runs else 0
    mean_tokens_pool = sum(r.get("total_tokens", 0) for r in pool_runs) / len(pool_runs) if pool_runs else 0
    mean_tokens_ctrl_b = sum(r.get("total_tokens", 0) for r in ctrl_b_runs) / len(ctrl_b_runs) if ctrl_b_runs else 0

    print(f"\nMETRICS SUMMARY:")
    print(f"  Pool Fault Correct:       {pool_correct} / {len(pool_runs)} ({pool_correct/len(pool_runs)*100:.1f}%)")
    print(f"  Confident-Wrong Total:    {confident_wrong_total} / {len(runs)} ({confident_wrong_total/len(runs)*100:.1f}%)")
    print(f"  Mean Tokens (All 9 runs): {mean_tokens_all:.1f}")
    print(f"  Mean Tokens (Pool):       {mean_tokens_pool:.1f}")
    print(f"  Mean Tokens (Control B):  {mean_tokens_ctrl_b:.1f}")


if __name__ == "__main__":
    main()
