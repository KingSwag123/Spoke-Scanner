CREATE TABLE IF NOT EXISTS watchlists (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    max_price DOUBLE PRECISION NOT NULL CHECK (max_price > 0),
    game TEXT,
    set_name TEXT,
    set_code TEXT,
    rarity TEXT,
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
    game TEXT,
    set_name TEXT,
    set_code TEXT,
    rarity TEXT,
    payload TEXT NOT NULL,
    total_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    initial_batch BOOLEAN NOT NULL DEFAULT FALSE,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claim_token TEXT,
    in_flight_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_watch_dms_ready
    ON pending_watch_dms(next_attempt);

-- Publish applies these additive tables/columns to production.  The
-- application never performs PostgreSQL DDL at startup.
CREATE TABLE IF NOT EXISTS watchlist_dm_pacing (
    user_id BIGINT PRIMARY KEY,
    next_digest_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_digest_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The metered Mercari personal lane survives bot process restarts.  This is
-- deliberately distinct from public marketplace scan accounting.
CREATE TABLE IF NOT EXISTS watch_source_daily_budgets (
    source TEXT PRIMARY KEY,
    budget_day DATE NOT NULL,
    used_requests INTEGER NOT NULL CHECK (used_requests >= 0)
);

CREATE TABLE IF NOT EXISTS watch_targeted_search_jobs (
    user_id BIGINT NOT NULL,
    normalized_name TEXT NOT NULL,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claim_token TEXT,
    in_flight_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, normalized_name),
    FOREIGN KEY (user_id, normalized_name)
        REFERENCES watchlists(user_id, normalized_name)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_watch_targeted_search_jobs_due
    ON watch_targeted_search_jobs(next_attempt);
-- The scanner's "already alerted" listing ids (see scanner_state.py). The JSON
-- file that holds them does not survive a republish, so without this table
-- every listing still in the feeds alerts again after each publish.
CREATE TABLE IF NOT EXISTS scanner_seen (
    item_id TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
