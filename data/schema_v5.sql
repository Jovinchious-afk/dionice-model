-- ============================================================
-- Schema v5 — Run this in Supabase SQL Editor
-- Tracks whether/when a recommended buy zone was actually reached,
-- on both decisions (historical log) and watchlist (active items).
-- buy_zone is free text ("< $135.00") written by Claude, so
-- buy_zone_numeric holds the parsed threshold used for comparison.
-- ============================================================

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS buy_zone_numeric NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS buy_zone_reached_at DATE;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS buy_zone_reached_price NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS lowest_price_since_rec NUMERIC;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS lowest_price_date DATE;

ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS buy_zone_numeric NUMERIC;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS buy_zone_reached_at DATE;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS buy_zone_reached_price NUMERIC;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS lowest_price_since_rec NUMERIC;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS lowest_price_date DATE;
