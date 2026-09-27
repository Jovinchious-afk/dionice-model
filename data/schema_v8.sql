-- ============================================================
-- Schema v8 — Run this in Supabase SQL Editor
-- 1. model_params: tunable model parameters (confidence floors, hype
--    threshold, concentration cap, starter-position rule, scorer weights).
--    The quarterly learning report PROPOSES values; the investor approves
--    them on the Streamlit "Učenje" page and only ACTIVE rows are used.
-- 2. learning_reports: the quarterly learning report (statistics + text).
-- 3. model_lessons.status: lessons are no longer injected into prompts
--    automatically — they start as PROPOSED and need approval.
-- 4. llm_usage: tokens and cost per run, for the monthly budget guard.
-- 5. decisions.entry_plan / decisions.model: whether a call came with a
--    "buy part now" starter plan, and which model made the final call.
-- Everything is additive: the code keeps working before this file runs.
-- ============================================================

CREATE TABLE IF NOT EXISTS model_params (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN (
        'PROPOSED', 'ACTIVE', 'REJECTED', 'RETIRED'
    )),
    reason TEXT,
    evidence JSONB,
    report_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    decided_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS model_params_key_status_idx ON model_params (key, status);

CREATE TABLE IF NOT EXISTS learning_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    period_label TEXT,
    report_markdown TEXT,
    stats_json JSONB,
    model TEXT
);

ALTER TABLE model_lessons ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE model_lessons ADD COLUMN IF NOT EXISTS report_id UUID;
ALTER TABLE model_lessons ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS llm_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    job TEXT,
    model TEXT,
    batch BOOLEAN DEFAULT FALSE,
    calls INT,
    input_tokens INT,
    output_tokens INT,
    cache_write_tokens INT,
    cache_read_tokens INT,
    cost_usd NUMERIC
);

CREATE INDEX IF NOT EXISTS llm_usage_created_idx ON llm_usage (created_at DESC);

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS entry_plan TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS model TEXT;
