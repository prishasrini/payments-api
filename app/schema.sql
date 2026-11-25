-- Double-entry wallet payments with idempotency.
-- Money is stored as integer paise (1 INR = 100 paise): never use floats for money.

CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('customer', 'merchant', 'system')),
    balance     BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- the database itself refuses to let a real account go negative,
    -- even if application code has a bug
    CONSTRAINT no_overdraft CHECK (balance >= 0 OR kind = 'system')
);

CREATE TABLE IF NOT EXISTS api_keys (
    key          TEXT PRIMARY KEY,
    merchant_id  TEXT NOT NULL REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id               TEXT PRIMARY KEY,
    merchant_id      TEXT NOT NULL REFERENCES accounts(id),
    customer_id      TEXT NOT NULL REFERENCES accounts(id),
    amount           BIGINT NOT NULL CHECK (amount > 0),
    currency         TEXT NOT NULL DEFAULT 'INR',
    status           TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    failure_reason   TEXT,
    refunded_amount  BIGINT NOT NULL DEFAULT 0,
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT refund_within_amount CHECK (refunded_amount >= 0 AND refunded_amount <= amount)
);

CREATE TABLE IF NOT EXISTS refunds (
    id          TEXT PRIMARY KEY,
    payment_id  TEXT NOT NULL REFERENCES payments(id),
    amount      BIGINT NOT NULL CHECK (amount > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every money movement is two (or more) rows that sum to zero.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id          BIGSERIAL PRIMARY KEY,
    txn_id      TEXT NOT NULL,          -- payment id, refund id or top-up id
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    amount      BIGINT NOT NULL,        -- positive = credit, negative = debit
    kind        TEXT NOT NULL CHECK (kind IN ('charge', 'refund', 'topup')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_account_idx ON ledger_entries (account_id);

-- One row per (merchant, Idempotency-Key). The primary key is what makes two
-- simultaneous requests with the same key impossible to both "win".
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope          TEXT NOT NULL,       -- merchant id: keys are per merchant
    key            TEXT NOT NULL,
    request_hash   TEXT NOT NULL,       -- detects the same key reused for a different request
    status         TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    response_code  INT,
    response_body  JSONB,
    locked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
