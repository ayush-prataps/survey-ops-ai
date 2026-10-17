"""
Traffic Generator (Locust): Simulates steady, realistic traffic mix for the target app.
Mostly POST /orders, some GET reads, and pagination requests.
"""

import os
import random
import uuid
from locust import HttpUser, between, task

ITEM_IDS = ["item-101", "item-102", "item-103", "item-104"]
USER_IDS = ["usr-1", "usr-2", "usr-3", "usr-4", "usr-5"]

MIN_WAIT = float(os.getenv("LOCUST_MIN_WAIT", "1.0"))
MAX_WAIT = float(os.getenv("LOCUST_MAX_WAIT", "3.0"))


class TargetAppUser(HttpUser):
    wait_time = between(MIN_WAIT, MAX_WAIT)

    def on_start(self):
        self.auth_token = f"token-for-{random.choice(USER_IDS)}-sec"
        self.recent_order_ids: list[str] = []

    @task(10)
    def create_order(self):
        """Simulates customer placing an order."""
        item_id = random.choice(ITEM_IDS)
        amount = round(random.uniform(20.0, 350.0), 2)
        payload = {
            "user_id": random.choice(USER_IDS),
            "item_id": item_id,
            "quantity": 1,
            "amount": amount,
            "idempotency_key": f"idemp-{uuid.uuid4().hex[:12]}",
        }
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        with self.client.post("/orders", json=payload, headers=headers, catch_response=True) as resp:
            if resp.status_code == 201:
                data = resp.json()
                if "order_id" in data:
                    self.recent_order_ids.append(data["order_id"])
                    if len(self.recent_order_ids) > 50:
                        self.recent_order_ids.pop(0)
                resp.success()
            elif resp.status_code == 409:
                resp.success()  # Out of stock is expected business flow
            else:
                resp.failure(f"Order creation failed: {resp.status_code}")

    @task(4)
    def list_orders_paginated(self):
        """Simulates user checking order history with pagination."""
        page = random.randint(1, 3)
        self.client.get(f"/orders?page={page}&page_size=10", name="/orders?page=[p]&page_size=10")

    @task(3)
    def view_recent_order(self):
        """Simulates checking order status."""
        if self.recent_order_ids:
            order_id = random.choice(self.recent_order_ids)
            self.client.get(f"/orders/{order_id}", name="/orders/[id]")
        else:
            self.client.get("/orders?page=1&page_size=5")

    @task(1)
    def health_probe(self):
        """Periodic health check verification."""
        self.client.get("/health", name="/health")
