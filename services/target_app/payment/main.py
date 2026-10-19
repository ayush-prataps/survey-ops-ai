"""
Payment Service: Processes charges with explicit DB connection pooling and idempotency checking.
"""

from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from typing import Any, Optional
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import Column, Numeric, String, TIMESTAMP, create_engine, event, select
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

# Configure JSON structured logging to stdout
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "payment",
            "level": record.levelname,
            "message": record.getMessage(),
            "endpoint": getattr(record, "endpoint", None),
            "status": getattr(record, "status", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        return json.dumps(log_obj)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("payment")
logger.setLevel(logging.INFO)
logger.handlers = [handler]
logger.propagate = False

# -----------------------------------------------------------------------------
# Configuration: Single DB host env var to allow Toxiproxy insertion later
# -----------------------------------------------------------------------------
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "target_app")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

# Configurable connection pool parameters (faults will target POOL_SIZE & timeout)
POOL_SIZE = int(os.getenv("POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("MAX_OVERFLOW", "0"))
POOL_TIMEOUT = float(os.getenv("POOL_TIMEOUT", "5.0"))

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# -----------------------------------------------------------------------------
# Database Models & Explicit Connection Pool (Separated functions for future fault injection)
# -----------------------------------------------------------------------------
Base = declarative_base()


class PaymentRecord(Base):
    __tablename__ = "payments"

    id = Column(String(64), primary_key=True)
    order_id = Column(String(64), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    idempotency_key = Column(String(128), nullable=False, unique=True)
    status = Column(String(32), nullable=False, default="succeeded")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


# Prometheus Gauges for connection pool monitoring
DB_POOL_IN_USE = Gauge(
    "payment_db_pool_in_use_connections",
    "Number of active connections currently checked out from the payment DB pool",
)
DB_POOL_CAPACITY = Gauge(
    "payment_db_pool_capacity",
    "Configured capacity of the payment DB pool",
)
DB_POOL_CAPACITY.set(POOL_SIZE)


def create_db_engine():
    """Initializes the SQLAlchemy engine with an explicit QueuePool."""
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "checkout")
    def on_pool_checkout(dbapi_con, con_record, con_proxy):
        DB_POOL_IN_USE.inc()

    @event.listens_for(engine, "checkin")
    def on_pool_checkin(dbapi_con, con_record):
        DB_POOL_IN_USE.dec()

    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def check_idempotency(db: Session, idempotency_key: str) -> Optional[PaymentRecord]:
    """
    Checks if a payment with the given idempotency key has already been executed.
    Separated function to allow fault injection (e.g. race condition/missing check).
    """
    stmt = select(PaymentRecord).where(PaymentRecord.idempotency_key == idempotency_key)
    return db.execute(stmt).scalar_one_or_none()


def execute_charge(
    db: Session,
    order_id: str,
    amount: float,
    idempotency_key: str,
) -> PaymentRecord:
    """
    Persists a new payment charge record to the database.
    Separated function to isolate database write execution.
    """
    payment_id = f"pay-{uuid.uuid4().hex[:12]}"
    record = PaymentRecord(
        id=payment_id,
        order_id=order_id,
        amount=amount,
        idempotency_key=idempotency_key,
        status="succeeded",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# -----------------------------------------------------------------------------
# FastAPI App & Endpoints
# -----------------------------------------------------------------------------
app = FastAPI(title="Payment Service", version="1.0.0")

# Instrument Prometheus metrics (/metrics) with custom buckets (p99 > 1s visible)
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
Instrumentator().instrument(app, latency_lowr_buckets=LATENCY_BUCKETS).expose(app)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

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


class ChargeRequest(BaseModel):
    order_id: str
    amount: float
    idempotency_key: str


class ChargeResponse(BaseModel):
    payment_id: str
    order_id: str
    amount: float
    status: str
    idempotent_replay: bool


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            conn.execute(select(1))
        return {
            "status": "ok",
            "service": "payment",
            "db_host": POSTGRES_HOST,
            "pool_size": POOL_SIZE,
        }
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        return {"status": "degraded", "service": "payment", "error": str(e)}


@app.post("/payment/charge", response_model=ChargeResponse)
def charge_payment(req: ChargeRequest) -> ChargeResponse:
    if os.getenv("FAULT_INJECT_5XX", "false").lower() == "true" or os.path.exists("/tmp/fault_inject_5xx"):
        logger.error("Fault injected: Upstream payment gateway unavailable (502 Bad Gateway)")
        raise HTTPException(status_code=502, detail="Upstream payment gateway unavailable")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be positive")

    with SessionLocal() as db:
        # 1. Idempotency check
        existing = check_idempotency(db, req.idempotency_key)
        if existing:
            logger.info(
                f"Idempotent replay for key {req.idempotency_key}, existing payment {existing.id}"
            )
            return ChargeResponse(
                payment_id=existing.id,
                order_id=existing.order_id,
                amount=float(existing.amount),
                status=existing.status,
                idempotent_replay=True,
            )

        # 2. Execute charge
        try:
            record = execute_charge(
                db=db,
                order_id=req.order_id,
                amount=req.amount,
                idempotency_key=req.idempotency_key,
            )
            return ChargeResponse(
                payment_id=record.id,
                order_id=record.order_id,
                amount=float(record.amount),
                status=record.status,
                idempotent_replay=False,
            )
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to charge payment: {e}")
            raise HTTPException(status_code=500, detail="Payment charge execution failed")
