#!/usr/bin/env python3
"""
Master orchestrator for bisecting the pool-fault regression across:
(A) token-usage-fix merge commit (wt-a)
(B) checkable-signal / positive-evidence commit (wt-b)
(C) current token-reduction-v2 with token changes reverted except thinking-off and fixed early-abstain
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from surveyops.agents.remediator.stub_server import app as remediator_app

WORKSPACE_ROOT = "/Users/ayushpratapsingh/dev/Projects/survey-ops-ai"
PYTHON_BIN = os.path.join(WORKSPACE_ROOT, ".venv", "bin", "python")
SINGLE_SCRIPT = os.path.join(WORKSPACE_ROOT, "scripts", "run_bisect_single.py")


def start_stub_remediator_in_thread(port: int = 8006):
    config = uvicorn.Config(app=remediator_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(30):
        if server.started:
            break
        time.sleep(0.1)
    return server


def generate_pool_latency_traffic():
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {
            "order_id": f"bisect-traffic-{i}-{int(time.time())}",
            "amount": 99.99,
            "idempotency_key": f"key-bisect-{i}-{int(time.time())}",
        }
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


def setup_pool_fault():
    subprocess.run([PYTHON_BIN, "eval/faults/clear_faults.py"], cwd=WORKSPACE_ROOT, check=True)
    subprocess.run(["bash", "scripts/reset_trial.sh"], cwd=WORKSPACE_ROOT, check=True)
    subprocess.run(
        [PYTHON_BIN, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        cwd=WORKSPACE_ROOT,
        check=True,
    )
    generate_pool_latency_traffic()
    time.sleep(2.0)


def run_target(target_label: str, target_cwd: str, env_overrides: dict) -> list[dict]:
    print(f"\n=======================================================")
    print(f">>> STARTING TARGET ({target_label}) in {target_cwd} <<<")
    print(f"Overrides: {env_overrides}")
    print(f"=======================================================\n")

    env = os.environ.copy()
    env.update(env_overrides)

    results = []
    for r in range(1, 4):
        setup_pool_fault()
        generate_pool_latency_traffic()
        time.sleep(1.0)

        out_file = os.path.join(WORKSPACE_ROOT, "eval", f"bisect_{target_label}_run{r}.json")
        cmd = [PYTHON_BIN, SINGLE_SCRIPT, f"{target_label}-run{r}", out_file]

        print(f"Running [{target_label} Run {r}]...")
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=target_cwd, env=env, capture_output=True, text=True)
        dur = round(time.perf_counter() - t0, 2)

        if proc.returncode != 0:
            print(f"ERROR in {target_label} Run {r}:")
            print(proc.stderr[:1000])

        if os.path.exists(out_file):
            with open(out_file) as f:
                data = json.load(f)
            data["returncode"] = proc.returncode
            data["run_wall_time_s"] = dur
            results.append(data)
            print(f"[{target_label} Run {r}]: outcome={data['outcome']}, root_cause={data['root_cause']}, iters={data['iterations']}, tokens={data['total_tokens']}, time={dur}s")
        else:
            print(f"[{target_label} Run {r} FAILED]: no output file written")
            results.append({"target": target_label, "run": r, "error": proc.stderr})

        time.sleep(2.0)

    subprocess.run([PYTHON_BIN, "eval/faults/clear_faults.py"], cwd=WORKSPACE_ROOT, check=True)
    return results


def main():
    start_stub_remediator_in_thread(8006)
    os.makedirs(os.path.join(WORKSPACE_ROOT, "eval"), exist_ok=True)

    targets = [
        (
            "A",
            os.path.join(WORKSPACE_ROOT, ".worktrees", "wt-a"),
            {},
        ),
        (
            "B",
            os.path.join(WORKSPACE_ROOT, ".worktrees", "wt-b"),
            {},
        ),
        (
            "C",
            WORKSPACE_ROOT,
            {
                "SURVEYOPS_ENABLE_DIGESTS": "false",
                "SURVEYOPS_ENABLE_SHORT_LEDGER": "false",
                "SURVEYOPS_ENABLE_PRE_PROBES": "false",
                "SURVEYOPS_ENABLE_EARLY_ABSTAIN": "true",
            },
        ),
    ]

    all_data = {}
    for label, cwd, env_overrides in targets:
        all_data[label] = run_target(label, cwd, env_overrides)

    summary_file = os.path.join(WORKSPACE_ROOT, "eval", "bisect_master_summary.json")
    with open(summary_file, "w") as f:
        json.dump(all_data, f, indent=2)

    print("\n=======================================================")
    print(">>> BISECT RUNS COMPLETE <<<")
    print(f"Summary saved to: {summary_file}")
    print("=======================================================")


if __name__ == "__main__":
    main()
