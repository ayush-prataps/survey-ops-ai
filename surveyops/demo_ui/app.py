"""
SurveyOps Demo UI Service.
FastAPI application serving:
- GET  /                     -> Single-page HTML application (vanilla JS, no build step)
- GET  /events/stream        -> SSE event stream from PostgreSQL events table / memory
- GET  /incident/{id}        -> Incident summary, events, and reports (NO ground truth exposed)
- POST /demo/inject          -> Proxies to controller service with shared secret
- POST /demo/reset           -> Proxies to controller service with shared secret
- POST /demo/reveal          -> Runs scorer in separate step and returns verdict only then
- GET  /demo/replays         -> Lists available replay files in eval/replays/
- POST /demo/replays/save    -> Saves incident events to eval/replays/<name>.json
- GET  /demo/replays/{name}/stream -> SSE stream of historical replay events with relative timing
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from surveyops.events import (
    fetch_events_since,
    get_all_events_for_incident,
    export_replay_file,
    record_event,
)
from eval.scorer.scorer import score_incident, find_latest_file

logger = logging.getLogger("surveyops.demo_ui")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SurveyOps Demo UI", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8088")
# Local fallback if controller is on localhost:8088
SHARED_SECRET = os.environ.get("CONTROLLER_SHARED_SECRET", os.environ.get("INVESTIGATE_SHARED_SECRET", "dev-secret"))

REPLAYS_DIR = REPO_ROOT / "eval" / "replays"
REPLAYS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR = REPO_ROOT / "eval" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
GROUND_TRUTH_DIR = REPO_ROOT / "eval" / "ground_truth"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "demo-ui"}


@app.get("/static/architecture_config.json")
@app.get("/architecture_config.json")
def get_architecture_config() -> dict[str, Any]:
    """Serves the architecture configuration JSON."""
    cfg_file = STATIC_DIR / "architecture_config.json"
    if not cfg_file.exists():
        raise HTTPException(status_code=404, detail="architecture_config.json not found")
    try:
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Invalid config JSON: {exc}")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serves the static single-page application."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return index_file.read_text(encoding="utf-8")


@app.get("/events/stream")
async def events_stream(
    request: Request,
    incident_id: Optional[str] = Query(default=None),
    after_id: int = Query(default=0),
    once: bool = Query(default=False),
) -> StreamingResponse:
    """Server-Sent Events (SSE) streaming live observability events."""

    async def event_generator() -> AsyncGenerator[str, None]:
        last_seen_id = after_id
        # Send initial connection comment
        yield ": connected to surveyops event stream\n\n"

        while True:
            if await request.is_disconnected():
                break

            events = fetch_events_since(incident_id=incident_id, after_id=last_seen_id, limit=50)
            if events:
                for ev in events:
                    last_seen_id = max(last_seen_id, ev["id"])
                    data_str = json.dumps(ev)
                    event_type = ev.get("event_type", "message")
                    yield f"id: {ev['id']}\nevent: {event_type}\ndata: {data_str}\n\n"

            if once:
                break

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/incident/{incident_id}")
def get_incident(incident_id: str) -> dict[str, Any]:
    """Returns incident events and rendered report. NEVER includes ground truth!"""
    events = get_all_events_for_incident(incident_id)

    report_data = None
    report_md = None

    # Check disk for report
    json_path = REPORTS_DIR / f"{incident_id}.json"
    md_path = REPORTS_DIR / f"{incident_id}.md"

    if json_path.exists():
        try:
            report_data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if md_path.exists():
        try:
            report_md = md_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # Database fallback from incident_reports table
    if report_data is None:
        try:
            from surveyops.events import get_surveyops_postgres_conn
            conn = get_surveyops_postgres_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT report_json, report_markdown FROM incident_reports WHERE incident_id = %s",
                    (incident_id,),
                )
                row = cur.fetchone()
                if row:
                    report_data = row[0]
                    report_md = row[1]
            conn.close()
        except Exception:
            pass

    # Check ground truth existence ONLY as boolean, never expose content
    has_gt = bool(list(GROUND_TRUTH_DIR.glob("*.json")))

    return {
        "incident_id": incident_id,
        "event_count": len(events),
        "events": events,
        "report": report_data,
        "report_markdown": report_md,
        "has_ground_truth": has_gt,
    }


class DemoInjectRequest(BaseModel):
    latency_ms: int = 2000
    jitter_ms: int = 100
    trigger_traffic: bool = True


@app.post("/demo/inject")
async def demo_inject(req: DemoInjectRequest) -> dict[str, Any]:
    """Proxies inject request to controller service."""
    target_urls = [CONTROLLER_URL, "http://localhost:8088", "http://127.0.0.1:8088"]
    last_err = None

    for base_url in target_urls:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base_url}/inject",
                    json=req.model_dump(),
                    headers={"X-Shared-Secret": SHARED_SECRET},
                )
                if resp.status_code == 200:
                    try:
                        record_event(
                            "operator",
                            "controller",
                            "operator action: fault injected",
                            {"message": "Fault injected by operator"},
                        )
                    except Exception as ev_err:
                        logger.warning(f"Failed to record inject event: {ev_err}")
                    return resp.json()
                elif resp.status_code == 403:
                    raise HTTPException(status_code=403, detail="Controller rejected shared secret")
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except httpx.RequestError as exc:
            last_err = str(exc)
            continue

    raise HTTPException(status_code=502, detail=f"Could not reach controller: {last_err}")


@app.post("/demo/reset")
async def demo_reset() -> dict[str, Any]:
    """Proxies reset request to controller service."""
    target_urls = [CONTROLLER_URL, "http://localhost:8088", "http://127.0.0.1:8088"]
    last_err = None

    for base_url in target_urls:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base_url}/reset",
                    headers={"X-Shared-Secret": SHARED_SECRET},
                )
                if resp.status_code == 200:
                    try:
                        record_event(
                            "operator",
                            "controller",
                            "operator action: system reset",
                            {"message": "System reset by operator"},
                        )
                    except Exception as ev_err:
                        logger.warning(f"Failed to record reset event: {ev_err}")
                    return resp.json()
                elif resp.status_code == 403:
                    raise HTTPException(status_code=403, detail="Controller rejected shared secret")
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except httpx.RequestError as exc:
            last_err = str(exc)
            continue

    raise HTTPException(status_code=502, detail=f"Could not reach controller: {last_err}")


class RevealRequest(BaseModel):
    incident_id: Optional[str] = None


@app.post("/demo/reveal")
def demo_reveal(req: Optional[RevealRequest] = None) -> dict[str, Any]:
    """
    Runs the existing evaluation scorer against blind ground truth in a separate step.
    Shows the ground truth verdict ONLY when explicitly called.
    """
    inc_id = req.incident_id if req else None

    # Find target report
    target_report_file: Optional[Path] = None
    if inc_id:
        cand = REPORTS_DIR / f"{inc_id}.json"
        if cand.exists():
            target_report_file = cand

    if not target_report_file:
        target_report_file = find_latest_file(REPORTS_DIR, pattern="*.json")

    if not target_report_file or not target_report_file.exists():
        raise HTTPException(
            status_code=404,
            detail="No incident report found to score. Run an investigation first.",
        )

    # Score report against ground truth
    try:
        score = score_incident(
            report_path=target_report_file,
            ground_truth_dir=GROUND_TRUTH_DIR,
        )
        return score.model_dump()
    except Exception as exc:
        logger.error(f"Scorer execution failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Scorer error: {exc}")


@app.get("/demo/replays")
def list_replays() -> list[dict[str, Any]]:
    """Lists saved replay files."""
    replays = []
    for f in sorted(REPLAYS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, list) else 0
            replays.append({
                "name": f.stem,
                "file": f.name,
                "event_count": count,
                "size_bytes": f.stat().st_size,
            })
        except Exception:
            continue
    return replays


class SaveReplayRequest(BaseModel):
    incident_id: str
    name: str


@app.post("/demo/replays/save")
def save_replay(req: SaveReplayRequest) -> dict[str, Any]:
    """Exports events of an incident to a replay JSON file."""
    safe_name = "".join(c for c in req.name if c.isalnum() or c in ("-", "_")).strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid replay name")

    out_file = REPLAYS_DIR / f"{safe_name}.json"
    count = export_replay_file(req.incident_id, out_file)
    if count == 0:
        raise HTTPException(status_code=404, detail=f"No events found for incident {req.incident_id}")

    return {
        "status": "saved",
        "name": safe_name,
        "file": str(out_file),
        "events_count": count,
    }


@app.get("/demo/replays/{name}/stream")
async def stream_replay(name: str, request: Request, fast: bool = Query(default=False)) -> StreamingResponse:
    """Streams replay events with their original relative timing labeled REPLAY."""
    replay_file = REPLAYS_DIR / f"{name}.json"
    if not replay_file.exists():
        raise HTTPException(status_code=404, detail="Replay file not found")

    try:
        events = json.loads(replay_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse replay: {exc}")

    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="Replay contains no events")

    async def replay_generator() -> AsyncGenerator[str, None]:
        yield ": connected to surveyops REPLAY stream\n\n"

        prev_dt: Optional[datetime] = None

        for idx, ev in enumerate(events):
            if await request.is_disconnected():
                break

            if fast:
                await asyncio.sleep(0.001)
            else:
                # Calculate delay from previous timestamp
                ts_str = ev.get("ts")
                curr_dt: Optional[datetime] = None
                if ts_str:
                    try:
                        curr_dt = datetime.fromisoformat(ts_str)
                    except Exception:
                        pass

                if prev_dt and curr_dt:
                    diff_sec = (curr_dt - prev_dt).total_seconds()
                    # Cap delay to 2.5s max so demo doesn't wait indefinitely
                    delay = max(0.05, min(2.5, diff_sec))
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0.05)

                prev_dt = curr_dt

            # Tag event as replay
            replay_ev = dict(ev)
            replay_ev["is_replay"] = True
            replay_ev["replay_label"] = "REPLAY"

            data_str = json.dumps(replay_ev)
            event_type = replay_ev.get("event_type", "message")
            yield f"id: {replay_ev.get('id', idx + 1)}\nevent: {event_type}\ndata: {data_str}\n\n"

        yield "event: replay_completed\ndata: {\"status\": \"completed\"}\n\n"

    return StreamingResponse(
        replay_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/demo/metrics")
async def get_demo_metrics(request: Request) -> dict[str, Any]:
    """
    Proxies summary metrics from controller.
    Rejects any arbitrary PromQL parameters.
    """
    disallowed = {"query", "promql", "expr", "match[]", "select"}
    for k in request.query_params.keys():
        if k.lower() in disallowed:
            raise HTTPException(
                status_code=400,
                detail="Arbitrary PromQL queries are rejected. Only named summary metrics are supported.",
            )

    headers = {"X-Shared-Secret": SHARED_SECRET}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{CONTROLLER_URL}/metrics/summary", headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()
    except httpx.RequestError as exc:
        return {
            "payment_p99_ms": 0.0,
            "pool_in_use": 0,
            "pool_capacity": 5,
            "alert_state": "normal",
            "error": f"controller unavailable: {exc}",
        }

