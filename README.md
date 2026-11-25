# Idempotent Payments API

A wallet payments backend that **never double-charges**, even when the same request arrives 25 times at once, the network drops a response, or two refunds race each other.

Built with FastAPI and PostgreSQL. The correctness guarantees live in the database (constraints, row locks, transactions), not just in application code, and every one has a test.

| Problem in real payment systems | How this API handles it |
|---|---|
| User double-taps "Pay", or the app retries after a timeout | **Idempotency keys**: the same key returns the original result and never charges again |
| Many payments from one wallet at the same instant | **Row-level locks** (`SELECT … FOR UPDATE`) plus a `CHECK (balance >= 0)` constraint |
| Two refunds for one payment at once | The payment row is locked; a `CHECK` means refunds can never exceed the amount |
| Lost money or "where did ₹50 go?" bugs | **Double-entry ledger**: every movement is entries that sum to zero, and an audit endpoint proves it |
| Deadlocks under load | Accounts are always locked in sorted order |
| Server crashes mid-request | Payment and saved response commit in one transaction; stuck keys expire after 60s |

There's also a deliberately broken endpoint (`/v1/demo/unsafe-payment`) that shows what goes wrong **without** these safeguards. Fire it concurrently and the ledger stops balancing.

## Run it locally

With Docker Desktop running:
```
docker compose up --build
```
Open **http://localhost:8000/docs** for interactive API docs. The demo API key is `sk_test_demo`.

Then prove it can't double-charge:
```
pip install httpx
python scripts/double_charge_test.py
```

## API at a glance

All requests need `X-API-Key: sk_test_demo`. Everything that moves money also needs an `Idempotency-Key` header (any unique string, such as a UUID). Amounts are in **paise** (₹1 = 100), because money is never stored as a float.

| Method | Path | What it does |
|---|---|---|
| POST | `/v1/payments` | Charge a customer `{customer_id, amount, description?}` |
| GET | `/v1/payments` | List payments |
| GET | `/v1/payments/{id}` | One payment with its refunds |
| POST | `/v1/payments/{id}/refunds` | Refund `{amount?}` (omit amount to refund the rest) |
| GET | `/v1/accounts` | Wallet balances |
| POST | `/v1/accounts/{id}/topup` | Add money to a customer wallet |
| GET | `/v1/ledger/check` | Audit: do the books balance? |
| POST | `/v1/demo/reset` | Reset demo data |
| POST | `/v1/demo/unsafe-payment` | The broken version, for comparison |

Example:
```
curl -X POST http://localhost:8000/v1/payments \
  -H "X-API-Key: sk_test_demo" -H "Idempotency-Key: order-1001" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "cust_priya", "amount": 2500, "description": "Masala chai x2"}'
```
Run it twice and you get the same payment back, with `Idempotent-Replayed: true`.

## Tests

13 tests run against a real Postgres, including concurrency tests (20 simultaneous retries, 15 simultaneous drains of one wallet, 8 racing refunds) and one that proves the unsafe endpoint really does corrupt the ledger.

```
docker compose up -d db
pip install -r requirements-dev.txt
set DATABASE_URL=postgresql://postgres:postgres@localhost:5432/payments
pytest -v
```
(Use `export` instead of `set` on Mac/Linux.) GitHub Actions runs the same suite on every push, via `.github/workflows/ci.yml`.

## Deploy it free (Render + Neon)

1. Push this folder to a GitHub repo.
2. **Database:** create a free Postgres at neon.tech and copy its connection string.
3. **API:** on render.com, go to New → Web Service, pick your repo, and choose **Docker** as the runtime. Add these environment variables:
   - `DATABASE_URL` = your Neon connection string
   - `DEMO_MODE` = `true` (so the demo site's reset and unsafe buttons work)
   - `PROCESSING_DELAY_MS` = `150` (makes the race demo visible; use `0` for a real deployment)
   - `CORS_ORIGINS` = your demo site's URL (or `*`)
4. Deploy, then check `https://<your-app>.onrender.com/health`.
5. Paste that URL into the demo website's settings to connect it.

Render's free tier sleeps after inactivity, so the first request can take about 30 seconds.

## How it's built (read in this order)

1. `app/schema.sql`: tables and the constraints that make bad states impossible.
2. `app/idempotency.py`: the idempotency-key protocol, explained at the top of the file.
3. `app/main.py`: `run_idempotent()`, then `create_payment`, `create_refund`, `lock_accounts`, `move_money`.
4. `tests/test_payments.py`: each test is a guarantee written as code.
5. `unsafe_payment` in `main.py`: the bug, and why it happens (a lost update).

## Interview talking points

- **Why not just check "has this order been paid?"** Two requests can both check before either writes. Only a unique constraint (the idempotency key's primary key) or a lock serializes them.
- **Why is the payment and the saved response in one transaction?** If they were separate, a crash between them could leave a charge with no saved response, and a retry would charge again.
- **Why `FOR UPDATE` and sorted locking order?** It prevents lost updates; consistent ordering prevents deadlocks.
- **Why paise as integers?** `0.1 + 0.2 != 0.3` in floating point.
- **Why double-entry?** Money is never created or destroyed by accident, and an audit query can prove it.

## Resume bullets

- Built a payments API (FastAPI, PostgreSQL) with idempotency keys, row-level locking and a double-entry ledger, guaranteeing exactly-once charges under concurrent retries.
- Wrote concurrency tests (20 parallel retries, wallet-drain and refund races) running in GitHub Actions CI against real Postgres; added a deliberately unsafe endpoint demonstrating the lost-update bug the design prevents.
