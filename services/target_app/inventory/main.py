"""
Inventory Service: Manages item catalog and stock reservation.
"""

from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import Column, Integer, Numeric, String, TIMESTAMP, create_engine, select, update
from sqlalchemy.orm import declarative_base, sessionmaker

# Configure JSON structured logging to stdout
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "inventory",
            "level": record.levelname,
            "message": record.getMessage(),
            "endpoint": getattr(record, "endpoint", None),
            "status": getattr(record, "status", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        return json.dumps(log_obj)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("inventory")
logger.setLevel(logging.INFO)
logger.handlers = [handler]
logger.propagate = False

# Database configuration
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "target_app")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class InventoryItem(Base):
    __tablename__ = "inventory"

    item_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    stock = Column(Integer, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow)


app = FastAPI(title="Inventory Service", version="1.0.0")

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


class ReserveRequest(BaseModel):
    quantity: int = 1


class ItemResponse(BaseModel):
    item_id: str
    name: str
    stock: int
    price: float


class ReserveResponse(BaseModel):
    reserved: bool
    item_id: str
    quantity: int
    remaining_stock: int


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with engine.connect() as conn:
            conn.execute(select(1))
        return {"status": "ok", "service": "inventory", "db": "connected"}
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        return {"status": "degraded", "service": "inventory", "db": "error"}


@app.get("/inventory/{item_id}", response_model=ItemResponse)
def get_item(item_id: str) -> ItemResponse:
    with SessionLocal() as db:
        item = db.execute(select(InventoryItem).where(InventoryItem.item_id == item_id)).scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        return ItemResponse(
            item_id=item.item_id,
            name=item.name,
            stock=item.stock,
            price=float(item.price),
        )


@app.post("/inventory/{item_id}/reserve", response_model=ReserveResponse)
def reserve_stock(item_id: str, req: ReserveRequest) -> ReserveResponse:
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero")

    with SessionLocal() as db:
        # Atomic reservation check
        item = db.execute(
            select(InventoryItem).where(InventoryItem.item_id == item_id).with_for_update()
        ).scalar_one_or_none()

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        if item.stock < req.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock. Available: {item.stock}, requested: {req.quantity}",
            )

        item.stock -= req.quantity
        db.commit()
        db.refresh(item)

        return ReserveResponse(
            reserved=True,
            item_id=item.item_id,
            quantity=req.quantity,
            remaining_stock=item.stock,
        )
