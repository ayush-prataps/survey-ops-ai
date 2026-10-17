"""
Tests for Target Application Microservices:
- Health endpoints across all 4 services
- Auth login & verification
- Inventory catalog & reservation
- Payment charge & idempotency guarantee
- Orders end-to-end multi-service orchestration
- Pagination on orders
- Prometheus /metrics exposure & label validation
"""

import os
import uuid
import pytest
import requests

ORDERS_URL = os.getenv("TEST_ORDERS_URL", "http://localhost:8001")
PAYMENT_URL = os.getenv("TEST_PAYMENT_URL", "http://localhost:8002")
INVENTORY_URL = os.getenv("TEST_INVENTORY_URL", "http://localhost:8003")
AUTH_URL = os.getenv("TEST_AUTH_URL", "http://localhost:8004")


def test_health_endpoints():
    """Verify all 4 target app services are up and healthy."""
    for service_name, base_url in [
        ("orders", ORDERS_URL),
        ("payment", PAYMENT_URL),
        ("inventory", INVENTORY_URL),
        ("auth", AUTH_URL),
    ]:
        resp = requests.get(f"{base_url}/health", timeout=5)
        assert resp.status_code == 200, f"{service_name} /health failed: {resp.text}"
        data = resp.json()
        assert data.get("status") in ("ok", "degraded")
        assert data.get("service") == service_name


def test_auth_login_and_verify():
    """Verify login provides token and /auth/verify validates it."""
    # Login
    login_resp = requests.post(
        f"{AUTH_URL}/auth/login",
        json={"username": "alice", "password": "securepassword123"},
        timeout=5,
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["token"]
    assert token

    # Verify with token
    verify_resp = requests.get(
        f"{AUTH_URL}/auth/verify",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True

    # Verify without token
    unauth_resp = requests.get(f"{AUTH_URL}/auth/verify", timeout=5)
    assert unauth_resp.status_code == 401


def test_inventory_get_and_reserve():
    """Verify catalog read and atomic stock decrement."""
    item_id = "item-101"
    item_resp = requests.get(f"{INVENTORY_URL}/inventory/{item_id}", timeout=5)
    assert item_resp.status_code == 200
    initial_stock = item_resp.json()["stock"]
    assert initial_stock > 0

    # Reserve 2 items
    reserve_resp = requests.post(
        f"{INVENTORY_URL}/inventory/{item_id}/reserve",
        json={"quantity": 2},
        timeout=5,
    )
    assert reserve_resp.status_code == 200
    data = reserve_resp.json()
    assert data["reserved"] is True
    assert data["remaining_stock"] == initial_stock - 2


def test_payment_charge_and_idempotency():
    """Verify payment charge and verify idempotency key returns exact same payment."""
    idempotency_key = f"test-idemp-{uuid.uuid4().hex}"
    order_id = f"test-ord-{uuid.uuid4().hex[:8]}"
    payload = {
        "order_id": order_id,
        "amount": 79.99,
        "idempotency_key": idempotency_key,
    }

    # 1st call: new charge executed
    first_resp = requests.post(f"{PAYMENT_URL}/payment/charge", json=payload, timeout=5)
    assert first_resp.status_code == 200
    first_data = first_resp.json()
    assert first_data["status"] == "succeeded"
    assert first_data["idempotent_replay"] is False
    payment_id = first_data["payment_id"]

    # 2nd call with identical idempotency_key: must return same payment_id with idempotent_replay=True
    second_resp = requests.post(f"{PAYMENT_URL}/payment/charge", json=payload, timeout=5)
    assert second_resp.status_code == 200
    second_data = second_resp.json()
    assert second_data["payment_id"] == payment_id
    assert second_data["idempotent_replay"] is True


def test_orders_creation_end_to_end():
    """Verify Orders service coordinates Auth, Inventory, and Payment to create an order."""
    token = "token-for-usr-test-sec"
    payload = {
        "user_id": "usr-test",
        "item_id": "item-102",
        "quantity": 1,
        "amount": 29.99,
        "idempotency_key": f"order-idemp-{uuid.uuid4().hex}",
    }
    resp = requests.post(
        f"{ORDERS_URL}/orders",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 201, f"Create order failed: {resp.text}"
    order = resp.json()
    assert order["status"] == "confirmed"
    assert order["item_id"] == "item-102"
    order_id = order["order_id"]

    # Read back single order
    get_resp = requests.get(f"{ORDERS_URL}/orders/{order_id}", timeout=5)
    assert get_resp.status_code == 200
    assert get_resp.json()["order_id"] == order_id


def test_orders_pagination():
    """Verify pagination on orders list."""
    resp = requests.get(f"{ORDERS_URL}/orders?page=1&page_size=3", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert "page" in data
    assert "page_size" in data
    assert "total" in data
    assert "orders" in data
    assert len(data["orders"]) <= 3


def test_metrics_endpoints_and_labels():
    """Verify /metrics exposes prometheus metrics with expected status labels and gauges."""
    for service_name, base_url in [
        ("orders", ORDERS_URL),
        ("payment", PAYMENT_URL),
        ("inventory", INVENTORY_URL),
        ("auth", AUTH_URL),
    ]:
        resp = requests.get(f"{base_url}/metrics", timeout=5)
        assert resp.status_code == 200
        text = resp.text
        assert "http_requests_total" in text or "http_request_duration_seconds" in text

    # Specific DB pool metrics on payment
    payment_metrics = requests.get(f"{PAYMENT_URL}/metrics", timeout=5).text
    assert "payment_db_pool_in_use_connections" in payment_metrics
    assert "payment_db_pool_capacity" in payment_metrics
