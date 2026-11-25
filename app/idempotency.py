"""Idempotency keys: the same request sent twice must only charge once.

Flow for a request carrying `Idempotency-Key: abc`:

1. begin(): try to INSERT (merchant, abc) as 'in_progress' in its own short
   transaction. The table's primary key means only ONE concurrent request can
   insert it; everyone else sees the existing row.
     - inserted       -> we own the key, go do the work
     - completed row  -> replay the saved response, do nothing
     - in_progress    -> another request is working on it right now: 409
     - different body -> client bug (same key, different request): 422
2. The work and complete() run in the SAME transaction, so the payment and the
   saved response are committed together or not at all.
3. If the work crashes unexpectedly, release() deletes the key so the client
   can safely retry. A key stuck 'in_progress' (server died mid-request) can be
   taken over after STALE_AFTER seconds.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .db import pool

STALE_AFTER_SECONDS = 60


def request_hash(method: str, path: str, payload: dict) -> str:
    raw = json.dumps({"m": method, "p": path, "b": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def begin(scope: str, key: str, req_hash: str) -> dict | None:
    """Returns None if this request now owns the key, or the saved row to replay."""
    if not key or len(key) > 255:
        raise HTTPException(400, "Idempotency-Key header is required (max 255 chars)")

    with pool.connection() as conn:
        inserted = conn.execute(
            "INSERT INTO idempotency_keys (scope, key, request_hash, status) "
            "VALUES (%s, %s, %s, 'in_progress') ON CONFLICT DO NOTHING RETURNING key",
            (scope, key, req_hash),
        ).fetchone()
        if inserted:
            return None

        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE scope = %s AND key = %s", (scope, key)
        ).fetchone()
        if row is None:  # released between our insert and select; caller can just retry
            raise HTTPException(409, "Request with this Idempotency-Key is being retried, try again")
        if row["request_hash"] != req_hash:
            raise HTTPException(
                422, "This Idempotency-Key was already used for a different request"
            )
        if row["status"] == "completed":
            return row

        taken_over = conn.execute(
            "UPDATE idempotency_keys SET locked_at = now() "
            "WHERE scope = %s AND key = %s AND status = 'in_progress' "
            "AND locked_at < now() - make_interval(secs => %s) RETURNING key",
            (scope, key, STALE_AFTER_SECONDS),
        ).fetchone()
        if taken_over:
            return None
        raise HTTPException(409, "A request with this Idempotency-Key is already in progress")


def complete(conn, scope: str, key: str, code: int, body: dict) -> None:
    conn.execute(
        "UPDATE idempotency_keys SET status = 'completed', response_code = %s, response_body = %s "
        "WHERE scope = %s AND key = %s",
        (code, Jsonb(body), scope, key),
    )


def release(scope: str, key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE scope = %s AND key = %s AND status = 'in_progress'",
            (scope, key),
        )
