-- ============================================================
-- Schema v2 — Run this in Supabase SQL Editor
-- Adds: positions_meta (personal thesis per holding)
--       manual_watchlist (stocks to always analyze)
-- ============================================================

-- Personal thesis and notes per portfolio position
CREATE TABLE IF NOT EXISTS positions_meta (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL UNIQUE,
    personal_thesis TEXT,        -- why you bought and why you're holding
    macro_view TEXT,             -- your macro thesis (e.g. geopolitical, sector trend)
    target_price_personal TEXT,  -- your own target (not agent's)
    stop_loss TEXT,              -- at what price you'd reconsider
    do_not_sell_until TEXT,      -- condition that must be met before selling
    added_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Manual watchlist: stocks you always want the agent to analyze
CREATE TABLE IF NOT EXISTS manual_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL UNIQUE,
    company_name TEXT,
    reason TEXT,                 -- why you want to watch this
    added_at TIMESTAMPTZ DEFAULT NOW(),
    active BOOLEAN DEFAULT TRUE
);

-- Seed: VG personal thesis
INSERT INTO positions_meta (symbol, personal_thesis, macro_view, do_not_sell_until)
VALUES (
    'VG',
    'All-in position in Venture Global LNG. Company is in heavy capex phase building Calcasieu Pass and Plaquemines LNG terminals. Thesis is long-term LNG demand growth.',
    'Geopolitical tension in Middle East (Iran), Russia-China-USA energy dynamics driving European and Asian LNG demand. USA becoming dominant LNG exporter. VG positioned as low-cost producer.',
    'Plaquemines fully operational and company generates positive FCF. Debt/Equity below 200x or significant contract announcements.'
)
ON CONFLICT (symbol) DO NOTHING;

-- Seed: suggested manual watchlist stocks to get started
INSERT INTO manual_watchlist (symbol, company_name, reason) VALUES
    ('GOOGL', 'Alphabet', 'Quality compounder, AI exposure, strong FCF'),
    ('META',  'Meta Platforms', 'Quality compounder, dominant social, growing margins'),
    ('OXY',   'Occidental Petroleum', 'Value/cyclical, Buffett backing, energy exposure'),
    ('LMT',   'Lockheed Martin', 'Defensive, defence spending growth, dividend'),
    ('COST',  'Costco', 'Quality compounder, recession-resistant, pricing power'),
    ('ABBV',  'AbbVie', 'Dividend/defensive, pharma pipeline, strong cash flow'),
    ('INTC',  'Intel', 'Turnaround, cheap valuation, new management'),
    ('NUE',   'Nucor', 'Value/cyclical, US steel, strong balance sheet'),
    ('PFE',   'Pfizer', 'Turnaround, deep value, pipeline refresh'),
    ('BRK.B', 'Berkshire Hathaway', 'Quality compounder, diversified, cash pile')
ON CONFLICT (symbol) DO NOTHING;
