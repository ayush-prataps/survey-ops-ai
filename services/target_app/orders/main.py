"""
Orders Service: Orchestrates order placement by coordinating with Auth, Inventory, and Payment services.
"""

from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from typing import Any, Optional
import uuid

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
import httpx
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import Column, Integer, Numeric, String, TIMESTAMP, create_engine, desc, func, select
from sqlalchemy.orm import declarative_base, sessionmaker

# Configure JSON structured logging to stdout
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "orders",
            "level": record.levelname,
            "message": record.getMessage(),
            "endpoint": getattr(record, "endpoint", None),
            "status": getattr(record, "status", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        return json.dumps(log_obj)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("orders")
logger.setLevel(logging.INFO)
logger.handlers = [handler]
logger.propagate = False

# -----------------------------------------------------------------------------
# Configuration: Service Base URLs and DB parameters from environment
# -----------------------------------------------------------------------------
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth:8004").rstrip("/")
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory:8003").rstrip("/")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment:8002").rstrip("/")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "target_app")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class OrderRecord(Base):
    __tablename__ = "orders"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    item_id = Column(String(64), nullable=False)
    quantity = Column(Integer, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    status = Column(String(32), nullable=False, default="confirmed")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


app = FastAPI(title="Orders Service", version="1.0.0")

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


# Reusable HTTP client for downstream microservice calls
http_client = httpx.Client(timeout=10.0)


class CreateOrderRequest(BaseModel):
    user_id: str
    item_id: str
    quantity: int = 1
    amount: float
    idempotency_key: Optional[str] = None


class OrderResponse(BaseModel):
    order_id: str
    user_id: str
    item_id: str
    quantity: int
    amount: float
    status: str
    created_at: Optional[str] = None


class PaginatedOrdersResponse(BaseModel):
    page: int
    page_size: int
    total: int
    orders: list[OrderResponse]


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            conn.execute(select(1))
        return {
            "status": "ok",
            "service": "orders",
            "downstream": {
                "auth": AUTH_SERVICE_URL,
                "inventory": INVENTORY_SERVICE_URL,
                "payment": PAYMENT_SERVICE_URL,
            },
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {"status": "degraded", "service": "orders", "error": str(e)}


@app.post("/orders", response_model=OrderResponse, status_code=201)
def create_order(
    req: CreateOrderRequest,
    authorization: Optional[str] = Header(None),
) -> OrderResponse:
    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    idemp_key = req.idempotency_key or f"idemp-{order_id}"

    # 1. Auth check
    auth_header = authorization or "Bearer default-token"
    try:
        auth_resp = http_client.get(
            f"{AUTH_SERVICE_URL}/auth/verify",
            headers={"Authorization": auth_header},
        )
        if auth_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Authentication failed")
    except httpx.RequestError as e:
        logger.error(f"Auth service unreachable: {e}")
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    # 2. Inventory reserve
    try:
        inv_resp = http_client.post(
            f"{INVENTORY_SERVICE_URL}/inventory/{req.item_id}/reserve",
            json={"quantity": req.quantity},
        )
        if inv_resp.status_code == 409:
            raise HTTPException(status_code=409, detail="Insufficient inventory stock")
        if inv_resp.status_code != 200:
            raise HTTPException(status_code=inv_resp.status_code, detail="Inventory reservation failed")
    except httpx.RequestError as e:
        logger.error(f"Inventory service unreachable: {e}")
        raise HTTPException(status_code=503, detail="Inventory service unavailable")

    # 3. Payment charge
    try:
        pay_resp = http_client.post(
            f"{PAYMENT_SERVICE_URL}/payment/charge",
            json={
                "order_id": order_id,
                "amount": req.amount,
                "idempotency_key": idemp_key,
            },
        )
        if pay_resp.status_code != 200:
            raise HTTPException(status_code=pay_resp.status_code, detail="Payment charge failed")
    except httpx.RequestError as e:
        logger.error(f"Payment service unreachable: {e}")
        raise HTTPException(status_code=503, detail="Payment service unavailable")

    # 4. Save confirmed order in Postgres
    with SessionLocal() as db:
        order = OrderRecord(
            id=order_id,
            user_id=req.user_id,
            item_id=req.item_id,
            quantity=req.quantity,
            amount=req.amount,
            status="confirmed",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        return OrderResponse(
            order_id=order.id,
            user_id=order.user_id,
            item_id=order.item_id,
            quantity=order.quantity,
            amount=float(order.amount),
            status=order.status,
            created_at=order.created_at.isoformat() if order.created_at else None,
        )


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(order_id: str) -> OrderResponse:
    with SessionLocal() as db:
        order = db.execute(select(OrderRecord).where(OrderRecord.id == order_id)).scalar_one_or_none()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return OrderResponse(
            order_id=order.id,
            user_id=order.user_id,
            item_id=order.item_id,
            quantity=order.quantity,
            amount=float(order.amount),
            status=order.status,
            created_at=order.created_at.isoformat() if order.created_at else None,
        )


@app.get("/orders", response_model=PaginatedOrdersResponse)
def list_orders(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
) -> PaginatedOrdersResponse:
    offset = (page - 1) * page_size
    with SessionLocal() as db:
        total = db.scalar(select(func.count(OrderRecord.id))) or 0
        stmt = (
            select(OrderRecord)
            .order_by(desc(OrderRecord.created_at))
            .offset(offset)
            .limit(page_size)
        )
        records = db.execute(stmt).scalars().all()

        orders = [
            OrderResponse(
                order_id=r.id,
                user_id=r.user_id,
                item_id=r.item_id,
                quantity=r.quantity,
                amount=float(r.amount),
                status=r.status,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in records
        ]

        return PaginatedOrdersResponse(
            page=page,
            page_size=page_size,
            total=total,
            orders=orders,
        )
