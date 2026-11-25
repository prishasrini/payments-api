import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

KEY = {"X-API-Key": "sk_test_demo"}


def idem():
    return {"Idempotency-Key": str(uuid.uuid4())}


def balance(api, acct):
    return {a["id"]: a["balance"] for a in api.get("/v1/accounts").json()["data"]}[acct]


def assert_books_balance(api):
    check = api.get("/v1/ledger/check").json()
    assert check["balanced"], check


# ---------------------------------------------------------------- basics ---

def test_payment_moves_money(api):
    r = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 12000}, headers=idem())
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "succeeded"
    assert balance(api, "cust_priya") == 38000
    assert balance(api, "merch_chai") == 12000
    assert_books_balance(api)


def test_insufficient_funds_is_recorded_not_charged(api):
    r = api.post("/v1/payments", json={"customer_id": "cust_meera", "amount": 9999}, headers=idem())
    assert r.status_code == 402
    assert r.json()["failure_reason"] == "insufficient_funds"
    assert balance(api, "cust_meera") == 5000
    assert_books_balance(api)


def test_auth_and_key_required(base):
    r = httpx.post(f"{base}/v1/payments", json={"customer_id": "cust_priya", "amount": 100}, headers=idem())
    assert r.status_code == 401
    r = httpx.post(f"{base}/v1/payments", json={"customer_id": "cust_priya", "amount": 100},
                   headers={"X-API-Key": "nope", **idem()})
    assert r.status_code == 401
    r = httpx.post(f"{base}/v1/payments", json={"customer_id": "cust_priya", "amount": 100}, headers=KEY)
    assert r.status_code == 400  # no Idempotency-Key


def test_unknown_customer(api):
    r = api.post("/v1/payments", json={"customer_id": "cust_ghost", "amount": 100}, headers=idem())
    assert r.status_code == 404


# ----------------------------------------------------------- idempotency ---

def test_retry_with_same_key_charges_once(api):
    h = idem()
    first = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 5000}, headers=h)
    second = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 5000}, headers=h)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balance(api, "cust_priya") == 45000
    assert len(api.get("/v1/payments").json()["data"]) == 1


def test_same_key_different_body_is_rejected(api):
    h = idem()
    api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 5000}, headers=h)
    r = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 9000}, headers=h)
    assert r.status_code == 422


def test_failed_payment_replays_the_same_failure(api):
    h = idem()
    a = api.post("/v1/payments", json={"customer_id": "cust_meera", "amount": 9999}, headers=h)
    api.post(f"/v1/accounts/cust_meera/topup", json={"amount": 100000}, headers=idem())
    b = api.post("/v1/payments", json={"customer_id": "cust_meera", "amount": 9999}, headers=h)
    # even though Meera can afford it now, the retry returns the original answer
    assert a.json()["id"] == b.json()["id"] and b.status_code == 402


def test_20_concurrent_retries_charge_exactly_once(base):
    h = {**KEY, **idem()}
    body = {"customer_id": "cust_priya", "amount": 1000}

    def send(_):
        return httpx.post(f"{base}/v1/payments", json=body, headers=h, timeout=30)

    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(send, range(20)))

    codes = [r.status_code for r in results]
    assert set(codes) <= {201, 409}, codes
    ids = {r.json()["id"] for r in results if r.status_code == 201}
    assert len(ids) == 1  # every success is the SAME payment
    with httpx.Client(base_url=base, headers=KEY) as api:
        assert balance(api, "cust_priya") == 49000
        assert len(api.get("/v1/payments").json()["data"]) == 1
        assert_books_balance(api)


# ---------------------------------------------------------- concurrency ---

def test_concurrent_payments_never_overdraw(base):
    # Arjun has 20000. 15 payments of 3000 fired at once: exactly 6 may succeed.
    def send(_):
        return httpx.post(
            f"{base}/v1/payments", json={"customer_id": "cust_arjun", "amount": 3000},
            headers={**KEY, **idem()}, timeout=30,
        )

    with ThreadPoolExecutor(15) as pool:
        results = list(pool.map(send, range(15)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 6 and codes.count(402) == 9, codes
    with httpx.Client(base_url=base, headers=KEY) as api:
        assert balance(api, "cust_arjun") == 2000
        assert_books_balance(api)


# --------------------------------------------------------------- refunds ---

def test_partial_and_full_refund(api):
    pid = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 10000}, headers=idem()).json()["id"]
    r1 = api.post(f"/v1/payments/{pid}/refunds", json={"amount": 4000}, headers=idem())
    assert r1.status_code == 201 and r1.json()["payment"]["refunded_amount"] == 4000
    r2 = api.post(f"/v1/payments/{pid}/refunds", json={}, headers=idem())  # the rest
    assert r2.status_code == 201 and r2.json()["amount"] == 6000
    r3 = api.post(f"/v1/payments/{pid}/refunds", json={"amount": 1}, headers=idem())
    assert r3.status_code == 409
    assert balance(api, "cust_priya") == 50000
    assert len(api.get(f"/v1/payments/{pid}").json()["refunds"]) == 2
    assert_books_balance(api)


def test_concurrent_refunds_cannot_exceed_payment(base):
    with httpx.Client(base_url=base, headers=KEY) as api:
        pid = api.post("/v1/payments", json={"customer_id": "cust_priya", "amount": 10000}, headers=idem()).json()["id"]

    def send(_):
        return httpx.post(f"{base}/v1/payments/{pid}/refunds", json={"amount": 3000},
                          headers={**KEY, **idem()}, timeout=30)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(send, range(8)))
    assert sorted(r.status_code for r in results).count(201) == 3  # 3 x 3000 fits in 10000, a 4th does not
    with httpx.Client(base_url=base, headers=KEY) as api:
        assert api.get(f"/v1/payments/{pid}").json()["refunded_amount"] == 9000
        assert_books_balance(api)


def test_cannot_refund_failed_payment(api):
    pid = api.post("/v1/payments", json={"customer_id": "cust_meera", "amount": 99999}, headers=idem()).json()["id"]
    assert api.post(f"/v1/payments/{pid}/refunds", json={}, headers=idem()).status_code == 409


# ------------------------------------------------------------ the bug demo ---

def test_unsafe_endpoint_really_is_broken(base):
    """Proves the safeguards matter: the naive version corrupts the books."""
    def send(_):
        return httpx.post(f"{base}/v1/demo/unsafe-payment",
                          json={"customer_id": "cust_priya", "amount": 1000}, headers=KEY, timeout=30)

    with ThreadPoolExecutor(5) as pool:
        list(pool.map(send, range(5)))
    with httpx.Client(base_url=base, headers=KEY) as api:
        assert api.get("/v1/ledger/check").json()["balanced"] is False
