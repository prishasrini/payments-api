"""Non-toy payments API: idempotent charges and refunds over a double-entry ledger.

Guarantees, each enforced at the database level and covered by tests:
- A retried request (same Idempotency-Key) never charges twice.
- Concurrent payments from one wallet can never overdraw it (row locks + CHECK).
- Refunds can never exceed the original payment, even when sent concurrently.
- The ledger always balances: every movement is entries summing to zero.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import idempotency as idem
from .db import init_schema, pool, seed_demo

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
# Artificial delay inside the locked section. 0 in production; tests and the
# demo raise it so race conditions are easy to trigger and observe.
PROCESSING_DELAY = float(os.getenv("PROCESSING_DELAY_MS", "0")) / 1000


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.open(wait=True, timeout=30)
    init_schema()
    if os.getenv("SEED_DEMO", "true").lower() == "true":
        seed_demo()
    yield
    pool.close()


app = FastAPI(
    title="Idempotent Payments API",
    description="Wallet payments with idempotency keys, row locking and a double-entry ledger.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Idempotent-Replayed"],
)


# ------------------------------------------------------------------- auth ---


def merchant_from_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    key = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
    if not key:
        raise HTTPException(401, "Missing API key (X-API-Key header)")
    with pool.connection() as conn:
        row = conn.execute("SELECT merchant_id FROM api_keys WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid API key")
    return row["merchant_id"]


# ---------------------------------------------------------------- helpers ---


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def payment_json(p: dict) -> dict:
    return {
        "id": p["id"],
        "object": "payment",
        "amount": p["amount"],
        "currency": p["currency"],
        "status": p["status"],
        "failure_reason": p["failure_reason"],
        "customer_id": p["customer_id"],
        "merchant_id": p["merchant_id"],
        "refunded_amount": p["refunded_amount"],
        "description": p["description"],
        "created_at": p["created_at"].isoformat(),
    }


def lock_accounts(conn, *account_ids: str) -> dict[str, dict]:
    """Row-lock accounts in a fixed (sorted) order so two transactions can never deadlock."""
    rows = conn.execute(
        "SELECT id, name, kind, balance FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        (sorted(set(account_ids)),),
    ).fetchall()
    return {r["id"]: r for r in rows}


def move_money(conn, txn_id: str, kind: str, debit: str, credit: str, amount: int) -> None:
    conn.execute(
        "INSERT INTO ledger_entries (txn_id, account_id, amount, kind) VALUES "
        "(%s, %s, %s, %s), (%s, %s, %s, %s)",
        (txn_id, debit, -amount, kind, txn_id, credit, amount, kind),
    )
    conn.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amount, debit))
    conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amount, credit))


def run_idempotent(
    merchant_id: str,
    key: str | None,
    request: Request,
    payload: dict,
    work: Callable[[object], tuple[int, dict]],
) -> JSONResponse:
    req_hash = idem.request_hash(request.method, request.url.path, payload)
    saved = idem.begin(merchant_id, key or "", req_hash)
    if saved is not None:
        return JSONResponse(
            saved["response_body"], status_code=saved["response_code"],
            headers={"Idempotent-Replayed": "true"},
        )
    try:
        with pool.connection() as conn, conn.transaction():
            code, body = work(conn)
            idem.complete(conn, merchant_id, key, code, body)
    except Exception:
        idem.release(merchant_id, key)
        raise
    return JSONResponse(body, status_code=code, headers={"Idempotent-Replayed": "false"})


def error(code: int, err_type: str, message: str, **extra) -> tuple[int, dict]:
    return code, {"error": {"type": err_type, "message": message, **extra}}


# --------------------------------------------------------------- payments ---


class PaymentIn(BaseModel):
    customer_id: str
    amount: int = Field(gt=0, le=10_000_000, description="Amount in paise (₹1 = 100)")
    description: str | None = Field(default=None, max_length=200)


@app.post("/v1/payments", status_code=201, tags=["payments"])
def create_payment(
    body: PaymentIn,
    request: Request,
    merchant_id: str = Depends(merchant_from_key),
    idempotency_key: str | None = Header(default=None),
):
    def work(conn):
        accts = lock_accounts(conn, body.customer_id, merchant_id)
        customer = accts.get(body.customer_id)
        if customer is None or customer["kind"] != "customer":
            return error(404, "invalid_request", f"No customer {body.customer_id}")

        if PROCESSING_DELAY:
            time.sleep(PROCESSING_DELAY)  # e.g. a fraud check, while we hold the lock

        pid = new_id("pay")
        if customer["balance"] < body.amount:
            p = conn.execute(
                "INSERT INTO payments (id, merchant_id, customer_id, amount, status, failure_reason, description)"
                " VALUES (%s, %s, %s, %s, 'failed', 'insufficient_funds', %s) RETURNING *",
                (pid, merchant_id, body.customer_id, body.amount, body.description),
            ).fetchone()
            return 402, payment_json(p)

        move_money(conn, pid, "charge", body.customer_id, merchant_id, body.amount)
        p = conn.execute(
            "INSERT INTO payments (id, merchant_id, customer_id, amount, status, description)"
            " VALUES (%s, %s, %s, %s, 'succeeded', %s) RETURNING *",
            (pid, merchant_id, body.customer_id, body.amount, body.description),
        ).fetchone()
        return 201, payment_json(p)

    return run_idempotent(merchant_id, idempotency_key, request, body.model_dump(), work)


@app.get("/v1/payments", tags=["payments"])
def list_payments(limit: int = 50, merchant_id: str = Depends(merchant_from_key)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE merchant_id = %s ORDER BY created_at DESC LIMIT %s",
            (merchant_id, min(limit, 200)),
        ).fetchall()
    return {"data": [payment_json(r) for r in rows]}


@app.get("/v1/payments/{payment_id}", tags=["payments"])
def get_payment(payment_id: str, merchant_id: str = Depends(merchant_from_key)):
    with pool.connection() as conn:
        p = conn.execute(
            "SELECT * FROM payments WHERE id = %s AND merchant_id = %s", (payment_id, merchant_id)
        ).fetchone()
        if p is None:
            raise HTTPException(404, "Payment not found")
        refunds = conn.execute(
            "SELECT id, amount, created_at FROM refunds WHERE payment_id = %s ORDER BY created_at",
            (payment_id,),
        ).fetchall()
    out = payment_json(p)
    out["refunds"] = [{**r, "created_at": r["created_at"].isoformat()} for r in refunds]
    return out


# ---------------------------------------------------------------- refunds ---


class RefundIn(BaseModel):
    amount: int | None = Field(default=None, gt=0, description="Paise; omit to refund the rest")


@app.post("/v1/payments/{payment_id}/refunds", status_code=201, tags=["payments"])
def create_refund(
    payment_id: str,
    body: RefundIn,
    request: Request,
    merchant_id: str = Depends(merchant_from_key),
    idempotency_key: str | None = Header(default=None),
):
    def work(conn):
        # lock the payment row: two refunds for one payment are processed one at a time
        p = conn.execute(
            "SELECT * FROM payments WHERE id = %s AND merchant_id = %s FOR UPDATE",
            (payment_id, merchant_id),
        ).fetchone()
        if p is None:
            return error(404, "invalid_request", "Payment not found")
        if p["status"] != "succeeded":
            return error(409, "invalid_state", "Only succeeded payments can be refunded")

        remaining = p["amount"] - p["refunded_amount"]
        amount = body.amount or remaining
        if amount > remaining:
            return error(
                409, "refund_exceeds_payment",
                f"Only {remaining} paise left to refund", refundable=remaining,
            )
        if PROCESSING_DELAY:
            time.sleep(PROCESSING_DELAY)

        accts = lock_accounts(conn, merchant_id, p["customer_id"])
        if accts[merchant_id]["balance"] < amount:
            return error(409, "insufficient_merchant_balance", "Merchant balance too low to refund")

        rid = new_id("re")
        move_money(conn, rid, "refund", merchant_id, p["customer_id"], amount)
        conn.execute(
            "INSERT INTO refunds (id, payment_id, amount) VALUES (%s, %s, %s)", (rid, payment_id, amount)
        )
        p = conn.execute(
            "UPDATE payments SET refunded_amount = refunded_amount + %s WHERE id = %s RETURNING *",
            (amount, payment_id),
        ).fetchone()
        return 201, {"id": rid, "object": "refund", "amount": amount, "payment": payment_json(p)}

    return run_idempotent(merchant_id, idempotency_key, request, {"id": payment_id, **body.model_dump()}, work)


# --------------------------------------------------------------- accounts ---


class TopUpIn(BaseModel):
    amount: int = Field(gt=0, le=10_000_000)


@app.get("/v1/accounts", tags=["accounts"])
def list_accounts(_merchant_id: str = Depends(merchant_from_key)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, kind, balance FROM accounts WHERE kind <> 'system' ORDER BY kind, id"
        ).fetchall()
    return {"data": rows}


@app.post("/v1/accounts/{account_id}/topup", status_code=201, tags=["accounts"])
def top_up(
    account_id: str,
    body: TopUpIn,
    request: Request,
    merchant_id: str = Depends(merchant_from_key),
    idempotency_key: str | None = Header(default=None),
):
    def work(conn):
        accts = lock_accounts(conn, account_id, "acct_external")
        if account_id not in accts or accts[account_id]["kind"] != "customer":
            return error(404, "invalid_request", f"No customer {account_id}")
        tid = new_id("top")
        move_money(conn, tid, "topup", "acct_external", account_id, body.amount)
        bal = conn.execute("SELECT balance FROM accounts WHERE id = %s", (account_id,)).fetchone()
        return 201, {"id": tid, "object": "topup", "account_id": account_id, "amount": body.amount, "balance": bal["balance"]}

    return run_idempotent(merchant_id, idempotency_key, request, {"id": account_id, **body.model_dump()}, work)


@app.get("/v1/ledger/check", tags=["ledger"])
def ledger_check(_merchant_id: str = Depends(merchant_from_key)):
    """Audit: do the books balance? Every account's balance must equal the sum of its entries."""
    with pool.connection() as conn:
        total = conn.execute("SELECT COALESCE(SUM(amount), 0) AS t FROM ledger_entries").fetchone()["t"]
        mismatched = conn.execute(
            "SELECT a.id, a.balance, COALESCE(SUM(l.amount), 0) AS ledger_sum "
            "FROM accounts a LEFT JOIN ledger_entries l ON l.account_id = a.id "
            "GROUP BY a.id, a.balance HAVING a.balance <> COALESCE(SUM(l.amount), 0)"
        ).fetchall()
        counts = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded, "
            "COUNT(*) FILTER (WHERE status = 'failed') AS failed FROM payments"
        ).fetchone()
    return {
        "balanced": total == 0 and not mismatched,
        "ledger_total": int(total),
        "mismatched_accounts": mismatched,
        "payments": counts,
    }


# ------------------------------------------------------------------- demo ---


@app.post("/v1/demo/reset", tags=["demo"])
def demo_reset():
    if not DEMO_MODE:
        raise HTTPException(404, "Not found")
    seed_demo(reset=True)
    return {"ok": True}


@app.post("/v1/demo/unsafe-payment", tags=["demo"])
def unsafe_payment(body: PaymentIn, merchant_id: str = Depends(merchant_from_key)):
    """THE BUG, ON PURPOSE: no idempotency key, no row lock, read-then-write.

    Fire this a few times at once and watch /v1/ledger/check stop balancing:
    every request reads the same old balance, so customers get charged once
    while the merchant gets credited many times (a "lost update").
    """
    if not DEMO_MODE:
        raise HTTPException(404, "Not found")
    with pool.connection() as conn, conn.transaction():
        cust = conn.execute("SELECT balance FROM accounts WHERE id = %s", (body.customer_id,)).fetchone()
        merch = conn.execute("SELECT balance FROM accounts WHERE id = %s", (merchant_id,)).fetchone()
        if cust is None:
            raise HTTPException(404, "No such customer")
        if cust["balance"] < body.amount:
            raise HTTPException(402, "insufficient_funds")
        time.sleep(max(PROCESSING_DELAY, 0.2))  # widen the race window
        pid = new_id("pay")
        conn.execute("UPDATE accounts SET balance = %s WHERE id = %s", (cust["balance"] - body.amount, body.customer_id))
        conn.execute("UPDATE accounts SET balance = %s WHERE id = %s", (merch["balance"] + body.amount, merchant_id))
        conn.execute(
            "INSERT INTO ledger_entries (txn_id, account_id, amount, kind) VALUES (%s, %s, %s, 'charge'), (%s, %s, %s, 'charge')",
            (pid, body.customer_id, -body.amount, pid, merchant_id, body.amount),
        )
        p = conn.execute(
            "INSERT INTO payments (id, merchant_id, customer_id, amount, status, description) "
            "VALUES (%s, %s, %s, %s, 'succeeded', %s) RETURNING *",
            (pid, merchant_id, body.customer_id, body.amount, (body.description or "") + " [UNSAFE]"),
        ).fetchone()
    return payment_json(p)


@app.get("/health", tags=["ops"])
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return {"service": "payments-api", "docs": "/docs", "health": "/health"}
