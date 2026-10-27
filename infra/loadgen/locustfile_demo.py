"""
Locust traffic generator for Demo Mode:
Simulates sustained, steady traffic targeted exclusively at POST /payment/charge.
Maintains enough concurrent load (6 users against pool size 5) to keep pool saturation
and p99 elevation stable throughout the investigation without overwhelming the system.
"""

import os
import uuid
from locust import HttpUser, between, task

MIN_WAIT = float(os.getenv("LOCUST_MIN_WAIT", "0.5"))
MAX_WAIT = float(os.getenv("LOCUST_MAX_WAIT", "1.5"))


class DemoPaymentUser(HttpUser):
    wait_time = between(MIN_WAIT, MAX_WAIT)

    @task
    def charge_payment(self):
        """Simulates incoming payment charge requests."""
        payload = {
            "order_id": f"ord-demo-{uuid.uuid4().hex[:8]}",
            "amount": 49.99,
            "idempotency_key": f"idemp-{uuid.uuid4().hex[:12]}",
        }
        with self.client.post("/payment/charge", json=payload, catch_response=True) as resp:
            if resp.status_code in (200, 201):
                resp.success()
            else:
                resp.failure(f"Charge failed: HTTP {resp.status_code}")
