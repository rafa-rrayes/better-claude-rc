-- M3: accounts + machine registry (plan §data model).
-- Plain SQL, applied in filename order by migrate.py / app.db.apply_migrations.

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         citext UNIQUE NOT NULL,
    plan          text NOT NULL DEFAULT 'free',
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- One table for every hashed credential the platform hands out. Plaintext
-- tokens are never stored: SHA-256 only (DB theft yields no usable secrets).
CREATE TABLE auth_tokens (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose    text NOT NULL CHECK (purpose IN ('magic_link', 'session', 'enroll')),
    token_hash text UNIQUE NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE INDEX auth_tokens_user_idx ON auth_tokens (user_id);

-- PG stores the agent's PUBLIC key only; the seed never leaves the machine.
CREATE TABLE machines (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          text NOT NULL DEFAULT '',
    ed25519_pk    bytea UNIQUE NOT NULL,
    agent_version text,
    proto         int,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz,
    revoked_at    timestamptz
);
CREATE INDEX machines_user_idx ON machines (user_id);

-- Billing lands in M5; the schema exists now so migrations stay linear.
CREATE TABLE subscriptions (
    user_id                uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    stripe_customer_id     text,
    stripe_subscription_id text,
    status                 text,
    price_id               text,
    current_period_end     timestamptz
);

-- Webhook idempotency ledger (INSERT ... ON CONFLICT DO NOTHING).
CREATE TABLE stripe_events (
    id          text PRIMARY KEY,
    type        text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload     jsonb NOT NULL
);

-- Append-only, metadata only (no prompts/outputs/paths ever).
CREATE TABLE audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    user_id    uuid,
    machine_id uuid,
    event      text NOT NULL,
    meta       jsonb NOT NULL DEFAULT '{}'
);

CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_rewrite
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
