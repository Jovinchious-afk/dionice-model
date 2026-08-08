-- ============================================================
-- Schema v6 — Run this in Supabase SQL Editor
-- Ticker health: lets the universe clean itself using evidence from the
-- production environment (GitHub Actions) rather than a local check.
-- A local yfinance sweep wrongly reported live blue chips like BK and MMC
-- as dead, so only production fetch results are trusted here.
-- ============================================================

CREATE TABLE IF NOT EXISTS ticker_health (
    symbol TEXT PRIMARY KEY,
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_error TEXT,
    last_ok_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ DEFAULT NOW(),
    -- Feed the quarterly refresh: a ticker looked at many times that never
    -- produced an actionable call is a candidate to rotate out of the pool.
    times_analyzed INT NOT NULL DEFAULT 0,
    times_actioned INT NOT NULL DEFAULT 0
);

-- Dead-ticker lookups run on every weekly run; keep them cheap.
CREATE INDEX IF NOT EXISTS ticker_health_failures_idx
    ON ticker_health (consecutive_failures);
