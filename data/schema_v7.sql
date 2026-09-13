-- ============================================================
-- Schema v7 — Run this in Supabase SQL Editor
-- 1. analysis_log: one row per analysed ticker per newsletter run, with a
--    snapshot of the key numbers, so the agent sees its own previous calls
--    and exactly what changed before it flips one (GNRC: ADD_ON_DIP four
--    times on identical numbers, then REDUCE).
-- 2. investor_profile: the investor's own checklist and macro lens, appended
--    to every analysis prompt. The content is uploaded from a gitignored
--    local file by scripts/upload_investor_profile.py — never commit it,
--    the GitHub repo is public.
-- 3. account_settings: cash balances, so position sizing and "vs cash"
--    comparisons know about uninvested money.
-- 4. positions_meta.sell_triggers: what would make the investor sell.
-- 5. decisions: benchmark-aware scoring (return vs S&P 500 at exactly day N,
--    measured from the buy-zone fill for BUY calls).
-- ============================================================

CREATE TABLE IF NOT EXISTS analysis_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyzed_at TIMESTAMPTZ DEFAULT NOW(),
    symbol TEXT NOT NULL,
    action TEXT,
    confidence INT,
    buy_zone TEXT,
    target_price TEXT,
    thesis TEXT,
    model TEXT,
    snapshot JSONB
);

CREATE INDEX IF NOT EXISTS analysis_log_symbol_time_idx
    ON analysis_log (symbol, analyzed_at DESC);

CREATE TABLE IF NOT EXISTS investor_profile (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS account_settings (
    key TEXT PRIMARY KEY,          -- 'cash_usd' | 'cash_eur'
    value NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE positions_meta ADD COLUMN IF NOT EXISTS sell_triggers TEXT;

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS entry_price NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS return_30d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS return_90d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS return_180d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS spy_return_30d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS spy_return_90d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS spy_return_180d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS excess_return_30d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS excess_return_90d NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS excess_return_180d NUMERIC;
-- NULL = scored under v1 ("any price rise = correct"); update_prices.py rescores those.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS scoring_version INT;
