-- ============================================================
-- Schema v3 — Run this in Supabase SQL Editor
-- Adds AI-written retrospective reasoning per outcome checkpoint,
-- so the Decisions page can show *why* a call was correct/wrong
-- instead of just the correct/wrong label.
-- ============================================================

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS outcome_reasoning_30d TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS outcome_reasoning_90d TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS outcome_reasoning_180d TEXT;
