"""
Auth Service: Provides authentication and token verification.
"""

from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

# Configure JSON structured logging to stdout
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "auth",
            "level": record.levelname,
            "message": record.getMessage(),
            "endpoint": getattr(record, "endpoint", None),
            "status": getattr(record, "status", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        return json.dumps(log_obj)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("auth")
logger.setLevel(logging.INFO)
logger.handlers = [handler]
logger.propagate = False

app = FastAPI(title="Auth Service", version="1.0.0")

# Instrument Prometheus metrics (/metrics) with custom buckets (p99 > 1s visible)
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
Instrumentator().instrument(app, latency_lowr_buckets=LATENCY_BUCKETS).expose(app)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    
    # Avoid log spamming health and metrics in standard request paths
    if request.url.path not in ("/health", "/metrics"):
        logger.info(
            f"{request.method} {request.url.path} responded {response.status_code} in {duration_ms}ms",
            extra={
                "endpoint": f"{request.method} {request.url.path}",
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    return response


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: str


class VerifyResponse(BaseModel):
    valid: bool
    user_id: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "auth"}


@app.post("/auth/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    # Dummy authentication logic accepting test users
    user_id = f"usr-{abs(hash(req.username)) % 10000}"
    token = f"token-for-{user_id}-sec"
    return LoginResponse(token=token, user_id=user_id)


@app.get("/auth/verify", response_model=VerifyResponse)
def verify(authorization: Optional[str] = Header(None)) -> VerifyResponse:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = authorization

    if not token or token == "invalid":
        raise HTTPException(status_code=401, detail="Invalid token")

    # Extract user_id or return default test user
    user_id = "usr-1"
    if "token-for-" in token:
        try:
            user_id = token.split("token-for-")[1].split("-")[0]
        except Exception:
            user_id = "usr-1"

    return VerifyResponse(valid=True, user_id=user_id)
