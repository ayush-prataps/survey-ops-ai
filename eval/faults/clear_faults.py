#!/usr/bin/env python3
"""
clear_faults.py — Remove all toxics from the payment-to-postgres proxy.

Usage:
    python eval/faults/clear_faults.py

Safe to run when no toxics are present (idempotent).
"""

import json
import os
import sys
import urllib.request
import urllib.error

TOXI_API = os.environ.get("TOXIPROXY_API", "http://localhost:8474")
PROXY_NAME = "payment-postgres"


def _toxi_request(method: str, path: str) -> dict | None:
    url = f"{TOXI_API}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        body_text = exc.read().decode(errors="replace")
        print(f"[ERROR] Toxiproxy {method} {path} -> HTTP {exc.code}: {body_text}", file=sys.stderr)
        raise
    except urllib.error.URLError as exc:
        print(f"[ERROR] Cannot reach Toxiproxy at {TOXI_API}: {exc.reason}", file=sys.stderr)
        print("  Is the stack running? docker compose -f infra/docker-compose.yml up -d", file=sys.stderr)
        raise


def clear() -> None:
    """List all toxics on the payment-postgres proxy and delete each one."""
    toxics = _toxi_request("GET", f"/proxies/{PROXY_NAME}/toxics")
    if toxics is None:
        print(f"[warn] Proxy '{PROXY_NAME}' not found — nothing to clear.")
        return

    if not toxics:
        print(f"[ok] No toxics on '{PROXY_NAME}'. System is clean.")
        return

    for toxic in toxics:
        name = toxic.get("name", "")
        result = _toxi_request("DELETE", f"/proxies/{PROXY_NAME}/toxics/{name}")
        if result is None:
            print(f"[warn] Toxic '{name}' not found (already removed).")
        else:
            print(f"[ok] Removed toxic '{name}' from proxy '{PROXY_NAME}'.")

    print("[ok] All toxics cleared. Payment-to-postgres connection is clean.")


def main() -> None:
    clear()


if __name__ == "__main__":
    main()
