-- Mining pool persistence. This is the canonical pool schema.
-- Applied by scripts/init-pool-postgres.sh

CREATE TABLE IF NOT EXISTS miners (
    address TEXT PRIMARY KEY,
    joined_at TIMESTAMP DEFAULT NOW(),
    last_active TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocks (
    hash TEXT PRIMARY KEY,
    height BIGINT NOT NULL,
    reward NUMERIC(30,0) NOT NULL,
    fees NUMERIC(30,0) NOT NULL,
    status TEXT DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS credits (
    id SERIAL PRIMARY KEY,
    block_hash TEXT REFERENCES blocks(hash),
    miner_address TEXT NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    is_paid BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS credits_block_miner_unique
ON credits (block_hash, miner_address);

CREATE TABLE IF NOT EXISTS block_submissions (
    id SERIAL PRIMARY KEY,
    candidate_hash TEXT NOT NULL,
    node_block_hash TEXT,
    height BIGINT,
    backend TEXT,
    template_seq BIGINT,
    accepted BOOLEAN NOT NULL DEFAULT FALSE,
    outcome TEXT NOT NULL,
    message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS block_submissions_created_at_idx
ON block_submissions (created_at);

CREATE INDEX IF NOT EXISTS block_submissions_outcome_created_idx
ON block_submissions (outcome, created_at);

CREATE TABLE IF NOT EXISTS payouts (
    id SERIAL PRIMARY KEY,
    tx_hash TEXT UNIQUE NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blocks_status_height
ON blocks(status, height);

CREATE INDEX IF NOT EXISTS idx_credits_unpaid_block_hash
ON credits(block_hash)
WHERE is_paid = FALSE;
