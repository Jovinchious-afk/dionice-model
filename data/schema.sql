-- ============================================================
-- Dionice Model — Supabase PostgreSQL Schema
-- Run this in Supabase SQL Editor (supabase.com → SQL Editor)
-- ============================================================

-- Portfolio transactions (what you actually bought/sold on Revolut)
CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    company_name TEXT,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    shares NUMERIC NOT NULL CHECK (shares > 0),
    price_per_share NUMERIC NOT NULL CHECK (price_per_share > 0),
    currency TEXT NOT NULL DEFAULT 'EUR',
    trade_date TIMESTAMPTZ NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Agent watchlist suggestions (what the AI recommends to watch)
CREATE TABLE IF NOT EXISTS watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    company_name TEXT,
    category TEXT CHECK (category IN (
        'quality_compounder', 'value_cyclical', 'turnaround',
        'speculative_growth', 'dividend_defensive'
    )),
    suggested_at TIMESTAMPTZ DEFAULT NOW(),
    action TEXT CHECK (action IN (
        'BUY_BELOW', 'ADD_ON_DIP', 'WAIT', 'WATCHLIST', 'SELL', 'NO_TRADE'
    )),
    buy_zone TEXT,
    target_price TEXT,
    confidence INT CHECK (confidence BETWEEN 1 AND 10),
    thesis TEXT,
    catalyst TEXT,
    downside_scenario TEXT,
    position_size TEXT,
    evidence_json JSONB,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
        'ACTIVE', 'BOUGHT', 'EXPIRED', 'DISMISSED'
    ))
);

-- Decision log: agent recommendation vs what user actually did → backtesting
CREATE TABLE IF NOT EXISTS decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommended_at TIMESTAMPTZ DEFAULT NOW(),
    symbol TEXT NOT NULL,
    agent_action TEXT,
    agent_buy_zone TEXT,
    agent_confidence INT,
    agent_thesis TEXT,
    user_action TEXT CHECK (user_action IN (
        'FOLLOWED', 'IGNORED', 'PARTIALLY_FOLLOWED', 'PENDING'
    )) DEFAULT 'PENDING',
    user_action_note TEXT,
    price_at_recommendation NUMERIC,
    price_30d NUMERIC,
    price_90d NUMERIC,
    price_180d NUMERIC,
    outcome_30d TEXT CHECK (outcome_30d IN ('correct', 'wrong', 'neutral', 'pending')) DEFAULT 'pending',
    outcome_90d TEXT CHECK (outcome_90d IN ('correct', 'wrong', 'neutral', 'pending')) DEFAULT 'pending',
    outcome_180d TEXT CHECK (outcome_180d IN ('correct', 'wrong', 'neutral', 'pending')) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Newsletter archive (full content of each sent email)
CREATE TABLE IF NOT EXISTS newsletters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sent_at TIMESTAMPTZ DEFAULT NOW(),
    type TEXT NOT NULL CHECK (type IN ('WEEKLY', 'MONTHLY')),
    subject TEXT,
    content_json JSONB,
    actions_summary TEXT
);

-- ============================================================
-- Seed: initial portfolio position (1,450 VG shares, 2026-05-24)
-- Update price_per_share to your actual average cost in EUR
-- ============================================================
INSERT INTO transactions (symbol, company_name, action, shares, price_per_share, currency, trade_date, notes)
VALUES (
    'VG',
    'Venture Global LNG',
    'BUY',
    1450,
    0.00,  -- UPDATE THIS: enter your actual average cost per share in EUR
    'EUR',
    '2026-05-24 00:00:00+00',
    'Initial position — all-in entry as of 2026-05-24'
)
ON CONFLICT DO NOTHING;
