"""Fires many identical requests at once and reports whether anyone was charged twice.

Usage:
    python scripts/double_charge_test.py                 # against http://localhost:8000
    python scripts/double_charge_test.py https://your-app.onrender.com
"""

import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
KEY = {"X-API-Key": "sk_test_demo"}
N = 25


def main() -> None:
    httpx.post(f"{BASE}/v1/demo/reset", timeout=30)
    key = str(uuid.uuid4())
    body = {"customer_id": "cust_priya", "amount": 1000, "description": "double-tap test"}

    def send(_):
        return httpx.post(f"{BASE}/v1/payments", json=body,
                          headers={**KEY, "Idempotency-Key": key}, timeout=30)

    print(f"Sending {N} identical payment requests at the same time...")
    with ThreadPoolExecutor(N) as pool:
        results = list(pool.map(send, range(N)))

    print("Status codes:", dict(Counter(r.status_code for r in results)))
    payment_ids = {r.json()["id"] for r in results if r.status_code == 201}
    with httpx.Client(base_url=BASE, headers=KEY, timeout=30) as api:
        payments = api.get("/v1/payments").json()["data"]
        check = api.get("/v1/ledger/check").json()
    print(f"Distinct payments created: {len(payments)} (ids seen by clients: {len(payment_ids)})")
    print(f"Ledger balanced: {check['balanced']}")
    print("PASS: charged exactly once" if len(payments) == 1 and check["balanced"] else "FAIL: double charge!")


if __name__ == "__main__":
    main()
