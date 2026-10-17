#!/usr/bin/env python3
"""
inject_latency.py — Add latency toxic on the payment-to-postgres Toxiproxy proxy.

Usage:
    python eval/faults/inject_latency.py [--latency-ms 2000] [--jitter-ms 100]

Writes a ground-truth record to eval/ground_truth/ (gitignored).
Nothing under surveyops/ reads this folder — scorer reads it only after a run.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

TOXI_API = os.environ.get("TOXIPROXY_API", "http://localhost:8474")
PROXY_NAME = "payment-postgres"
TOXIC_NAME = "latency-payment-postgres"

GROUND_TRUTH_DIR = Path(__file__).parent.parent / "ground_truth"


def _toxi_request(method: str, path: str, body: dict | None = None, ignore_404: bool = False) -> dict:
    url = f"{TOXI_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ignore_404:
            raise
        body_text = exc.read().decode(errors="replace")
        print(f"[ERROR] Toxiproxy {method} {path} -> HTTP {exc.code}: {body_text}", file=sys.stderr)
        raise
    except urllib.error.URLError as exc:
        print(f"[ERROR] Cannot reach Toxiproxy at {TOXI_API}: {exc.reason}", file=sys.stderr)
        print("  Is the stack running? docker compose -f infra/docker-compose.yml up -d", file=sys.stderr)
        raise


def inject(latency_ms: int, jitter_ms: int) -> str:
    """Add (or replace) the latency toxic. Returns the fault_id."""
    # Remove existing toxic if present (ignore 404)
    try:
        _toxi_request("DELETE", f"/proxies/{PROXY_NAME}/toxics/{TOXIC_NAME}", ignore_404=True)
        print(f"[info] Removed existing toxic '{TOXIC_NAME}'")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    # Add latency toxic
    toxic_body = {
        "name": TOXIC_NAME,
        "type": "latency",
        "stream": "upstream",
        "toxicity": 1.0,
        "attributes": {
            "latency": latency_ms,
            "jitter": jitter_ms,
        },
    }
    result = _toxi_request("POST", f"/proxies/{PROXY_NAME}/toxics", toxic_body)
    print(f"[ok] Injected latency toxic: {latency_ms}ms +/-{jitter_ms}ms on proxy '{PROXY_NAME}'")
    print(f"     Toxiproxy response: {result}")

    fault_id = f"fault-{uuid.uuid4().hex[:8]}"

    # Write ground-truth record
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "fault_id": fault_id,
        "type": "latency",
        "target_service": "payment",
        "target_proxy": PROXY_NAME,
        "params": {
            "latency_ms": latency_ms,
            "jitter_ms": jitter_ms,
            "stream": "upstream",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    record_path = GROUND_TRUTH_DIR / f"{fault_id}.json"
    record_path.write_text(json.dumps(record, indent=2))
    print(f"[ok] Ground-truth record written: {record_path}")

    return fault_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject latency on payment->postgres proxy")
    parser.add_argument("--latency-ms", type=int, default=2000,
                        help="Latency to add in milliseconds (default: 2000)")
    parser.add_argument("--jitter-ms", type=int, default=100,
                        help="Jitter in milliseconds (default: 100)")
    args = parser.parse_args()

    fault_id = inject(args.latency_ms, args.jitter_ms)
    print(f"\nFault ID: {fault_id}")
    print("Run clear_faults.py to remove all toxics.")


if __name__ == "__main__":
    main()
