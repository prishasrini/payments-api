"""Database pool, schema setup and demo data."""

from __future__ import annotations

import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/payments")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=int(os.getenv("DB_POOL_SIZE", "20")),
    kwargs={"row_factory": dict_row},
    open=False,
)

DEMO_API_KEY = "sk_test_demo"

# (id, name, kind, starting balance in paise)
DEMO_ACCOUNTS = [
    ("acct_external", "Outside world (top-ups)", "system", 0),
    ("merch_chai", "Chennai Chai Co.", "merchant", 0),
    ("cust_priya", "Priya", "customer", 50_000),    # ₹500
    ("cust_arjun", "Arjun", "customer", 20_000),    # ₹200
    ("cust_meera", "Meera", "customer", 5_000),     # ₹50
]


def init_schema() -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with pool.connection() as conn:
        conn.execute(sql)


def seed_demo(reset: bool = False) -> None:
    """Create demo accounts. With reset=True, wipe everything first."""
    with pool.connection() as conn, conn.transaction():
        if reset:
            conn.execute(
                "TRUNCATE ledger_entries, refunds, payments, idempotency_keys, api_keys, accounts"
            )
        exists = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
        if exists:
            return
        for acct_id, name, kind, _ in DEMO_ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts (id, name, kind) VALUES (%s, %s, %s)", (acct_id, name, kind)
            )
        conn.execute(
            "INSERT INTO api_keys (key, merchant_id) VALUES (%s, 'merch_chai')", (DEMO_API_KEY,)
        )
        # starting balances go through the ledger too, so the books always balance
        for acct_id, _, _, balance in DEMO_ACCOUNTS:
            if balance > 0:
                txn = f"topup_seed_{acct_id}"
                conn.execute(
                    "INSERT INTO ledger_entries (txn_id, account_id, amount, kind) VALUES "
                    "(%s, 'acct_external', %s, 'topup'), (%s, %s, %s, 'topup')",
                    (txn, -balance, txn, acct_id, balance),
                )
                conn.execute("UPDATE accounts SET balance = balance - %s WHERE id = 'acct_external'", (balance,))
                conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (balance, acct_id))
