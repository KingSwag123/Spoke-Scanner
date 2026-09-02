CREATE TABLE IF NOT EXISTS watchlists (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    max_price DOUBLE PRECISION NOT NULL CHECK (max_price > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_watchlists_user
    ON watchlists(user_id);

CREATE TABLE IF NOT EXISTS watchlist_deliveries (
    user_id BIGINT NOT NULL,
    item_id TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS pending_watch_dms (
    user_id BIGINT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    max_price DOUBLE PRECISION NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claim_token TEXT,
    in_flight_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_watch_dms_ready
    ON pending_watch_dms(next_attempt);