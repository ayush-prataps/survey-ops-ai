#!/usr/bin/env python3
"""
Regenerates static openapi.json files from running microservices.
Usage: python scripts/generate_openapi.py [--check]
"""

import sys
import json
import urllib.request
from pathlib import Path

TARGET_SERVICES = [
    ("orders", 8001),
    ("payment", 8002),
    ("inventory", 8003),
    ("auth", 8004),
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def fetch_live_openapi(port: int) -> dict:
    url = f"http://localhost:{port}/openapi.json"
    req = urllib.request.Request(url, headers={"User-Agent": "SurveyOps-OpenAPIChecker"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    check_only = "--check" in sys.argv
    has_drift = False

    for svc_name, port in TARGET_SERVICES:
        dest_file = REPO_ROOT / "services" / "target_app" / svc_name / "openapi.json"
        try:
            live_spec = fetch_live_openapi(port)
        except Exception as e:
            print(f"[{svc_name}] Cannot reach live service on port {port}: {e}", file=sys.stderr)
            continue

        if not dest_file.exists():
            print(f"[{svc_name}] Missing {dest_file}")
            has_drift = True
            if not check_only:
                with open(dest_file, "w", encoding="utf-8") as f:
                    json.dump(live_spec, f, indent=2)
                print(f"[{svc_name}] Generated {dest_file}")
            continue

        with open(dest_file, "r", encoding="utf-8") as f:
            existing_spec = json.load(f)

        live_paths = set(live_spec.get("paths", {}).keys())
        existing_paths = set(existing_spec.get("paths", {}).keys())

        if live_paths != existing_paths:
            print(f"[{svc_name}] DRIFT DETECTED!")
            print(f"  Live paths: {live_paths}")
            print(f"  Saved paths: {existing_paths}")
            has_drift = True
            if not check_only:
                with open(dest_file, "w", encoding="utf-8") as f:
                    json.dump(live_spec, f, indent=2)
                print(f"[{svc_name}] Updated {dest_file}")
        else:
            print(f"[{svc_name}] In sync ({len(live_paths)} paths)")

    if check_only and has_drift:
        sys.exit(1)


if __name__ == "__main__":
    main()
