# Dionice Model — AI Stock Newsletter & Portfolio Tracker

AI-powered stock analysis system for a small retail investor using Revolut Basic.

**What it does:**
- Sends a stock newsletter every **Tuesday and Thursday** (Croatian)
- Sends a monthly portfolio review on the **1st of each month**
- Sends a **quarterly learning report**: how past calls did against the S&P 500, and
  proposed lessons and parameter changes that you approve in the app
- Tracks your portfolio via a **Streamlit web app**
- Logs agent recommendations vs your decisions and scores them against the S&P 500

**Cost: ~$3-4/month** (Claude Haiku 4.5 via the Message Batches API for routine analysis,
Sonnet 5 for second opinions on buy and sell calls, Opus 5 once a quarter). Spend is
logged per run and shown on the app's "Učenje" page; above the monthly budget the
second opinions are skipped.

---

## Setup Guide (Step by Step)

### Step 1 — Prerequisites (create these accounts)

| Account | Where | What you need |
|---------|-------|---------------|
| GitHub | github.com | Already have ✅ |
| Supabase | supabase.com | Create free project |
| Anthropic | console.anthropic.com | API key (~$5 credit to start) |
| Gmail App Password | myaccount.google.com → Security → App Passwords | **Requires 2FA enabled first** |
| Streamlit Cloud | share.streamlit.io | Sign in with GitHub |

---

### Step 3 — Supabase Setup

1. Go to [supabase.com](https://supabase.com) → New Project
2. Remember your project password
3. Go to **SQL Editor** → run `data/schema.sql`, then each `data/schema_v2.sql` … `schema_v8.sql` in order
4. Update the VG seed row: change `price_per_share` to your actual average cost in EUR
5. Go to **Settings → API** → copy **Project URL** and **anon public key**

Every new `schema_vN.sql` is additive (`IF NOT EXISTS`) and the code keeps working
until it has been run; features that need it (e.g. the "Učenje" page) say so.

---

### Step 4 — Anthropic API Setup

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Add billing (you need at least $5 credit)
3. Create an API key → copy it

---

### Step 5 — Gmail App Password

1. Go to your Google Account → Security
2. Enable **2-Step Verification** (if not already)
3. Search for **"App Passwords"** → Create one for "Mail"
4. Copy the 16-character password (format: `xxxx xxxx xxxx xxxx`)

---

### Step 6 — Local Setup & Test

```powershell
# In the project directory
pip install -r requirements.txt

# Create your .env file from the example
copy .env.example .env
# Then edit .env with your actual keys

# Test the weekly script locally first
python scripts/run_weekly.py
```

You should receive an email at `lukajovic.172@gmail.com`.

---

### Step 7 — GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Create a **public** repository named `dionice-model`
3. Initialize from this folder:

```powershell
git init
git add .
git commit -m "Initial setup: Dionice AI stock newsletter"
git remote add origin https://github.com/YOUR_USERNAME/dionice-model.git
git push -u origin main
```

---

### Step 8 — GitHub Actions Secrets

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these secrets (one by one):

| Secret name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | from Step 4 |
| `SUPABASE_URL` | from Step 3 |
| `SUPABASE_KEY` | from Step 3 |
| `GMAIL_USER` | `lukajovic.172@gmail.com` |
| `GMAIL_APP_PASSWORD` | from Step 5 |
| `RECIPIENT_EMAIL` | `lukajovic.172@gmail.com` |

**Test GitHub Actions:**
Go to Actions tab → "Weekly Newsletter" → "Run workflow" → check if email arrives.

---

### Step 9 — Streamlit Cloud Deploy

1. Go to [share.streamlit.io](https://share.streamlit.io)
2. Sign in with GitHub
3. New app → select your `dionice-model` repo
4. Main file path: `app/portfolio_app.py`
5. Go to **Advanced settings → Secrets** and add:
```toml
SUPABASE_URL = "https://xxxxxxxxxxxx.supabase.co"
SUPABASE_KEY = "eyJhbGc..."
```
6. Click Deploy

Your portfolio tracker will be live at a URL like:
`https://YOUR_USERNAME-dionice-model-app-portfolio-app-xxxx.streamlit.app`

---

## Usage

### Logging a Trade (Streamlit App)
- Go to your Streamlit URL
- Click **"Log Trade"** in sidebar
- Enter: symbol, company name, BUY/SELL, shares, price, date
- Hit "Save Trade"

### Updating Decision Log
- Go to **"Decisions"** page
- After receiving a newsletter recommendation, mark whether you followed it and why

### Adding to Watchlist Manually
- Go to **"Watchlist"** page
- Use the expander to add any ticker you want the AI to evaluate next week

---

## File Structure

```
dionice-model/
├── .github/workflows/          # GitHub Actions (scheduling)
├── .claude/skills/verify/      # how Claude Code verifies a change (tests, dry runs, screenshots)
├── CLAUDE.md                   # conventions for Claude Code sessions
├── app/
│   └── portfolio_app.py        # Streamlit web app (incl. "Učenje": report, approvals, AI spend)
├── analysis/
│   ├── ai_analyst.py           # prompts, JSON schema, batch runner, deterministic rules
│   ├── scorer.py               # 5-category score, P/E vs industry median
│   ├── fundamentals.py         # yfinance data fetcher + cache
│   ├── checklist.py            # the investor's own checklist + sell-guard signals
│   ├── sentiment_tracker.py    # StockTwits hype (message velocity vs company size)
│   ├── insider_tracker.py      # SEC Form 4 open-market insider trades
│   ├── analysis_history.py     # per-ticker memory + features for the learning report
│   ├── learning.py             # statistics of the quarterly learning report
│   ├── params.py               # tunable parameters (defaults + approved overrides)
│   ├── usage.py                # token cost per run, monthly budget guard
│   ├── prices.py, portfolio.py, macro_context.py, sector_context.py, …
│   └── email_sender.py         # Gmail SMTP + HTML builders
├── scripts/
│   ├── run_weekly.py           # newsletter
│   ├── run_monthly.py          # monthly portfolio review
│   ├── run_quarterly_review.py # quarterly learning report
│   ├── update_prices.py        # 30/90/180-day outcomes vs S&P 500, buy-zone fills
│   ├── refresh_universe.py, refresh_gems.py, refresh_sector_benchmarks.py  # quarterly
│   └── verify_screens.py       # screenshots for verification (dev only)
├── data/                       # universe, gems, sector benchmarks, schema_v*.sql
├── tests/                      # pytest, no network
├── requirements.txt            # production (GitHub Actions, Streamlit Cloud)
└── requirements-dev.txt        # + pytest, playwright
```

## Development

```powershell
pip install -r requirements-dev.txt
python -m playwright install chromium   # once, for screenshots
python -m pytest -q                     # tests, no network, no cost
python scripts/run_weekly.py --dry-run --limit 1 --sync   # ~$0.05, no email, no DB writes
python scripts/run_quarterly_review.py --dry-run --no-ai  # learning statistics, free
```

---

## How the AI Analyzes Stocks

1. **Discovers tickers** from a random sector-rotating universe sample, your portfolio and hidden gems
2. **Fetches fundamentals** (P/E, PEG, FCF, margins, debt, insiders, inventories, seasonality) via yfinance
3. **Scores each stock** 0-100 using category-specific weights; P/E is compared with the industry median
4. **Claude analyzes** all signals (one Message Batch, cached shared prompt, JSON schema) and produces:
   action, buy zone, target, thesis, catalyst, downside scenario; the evidence table comes from the data
5. **Deterministic rules** run on every answer (hype block, confidence floor, sell guard,
   30% concentration cap, "part now, rest at the zone" for high-confidence buys)
6. **Buy candidates get a second opinion** from a stronger model while the monthly budget allows
7. **Email is sent** with max 4-7 actions; "NO TRADE" is a valid primary output

**Hype rule:** StockTwits message velocity relative to company size ≥7/10 → no BUY, maximum WATCHLIST  
**Concentration rule:** no ADD_ON_DIP for a position above 30% of total capital (positions + cash)  
**Sell rule:** SELL/REDUCE on a held stock only when the business deteriorated, not the price  
**Congress rule:** weak signal only — idea source, never a buy trigger  
**No trade rule:** every recommendation is compared to "hold cash or add to best existing position"

## How the Model Learns

Every analysed stock — including the ones the filters drop before the AI — is logged with
its features. Once a quarter `run_quarterly_review.py` measures the calls against the S&P 500
(by action, confidence, category, score, hype; buy zone vs buying at once; which scorer
criteria predicted returns; your real trades vs the same money in SPY). Claude turns the
tables into a report, lessons and bounded parameter proposals. Nothing changes by itself:
you approve lessons and parameters on the "Učenje" page, and the next report measures
results before and after each change.

---

## Email Schedule

- **Tuesday and Thursday, 13:00 UTC** — Weekly newsletter (GitHub starts scheduled jobs late,
  and the batch adds up to an hour, so it usually arrives in the evening)
- **1st of each month, 09:00 UTC** — Monthly portfolio review
- **Wednesday 10:00 UTC** — Background decision scoring vs S&P 500 + buy-zone check (no email)
- **1st of Jan/Apr/Jul/Oct** — universe + sector benchmark refresh (06:00 UTC), then the
  quarterly learning report (09:00 UTC)

Note: Schedules use UTC. Summer = CEST (UTC+2), so 13:00 UTC = 15:00 CEST.

---

*Not financial advice. AI-generated analysis for educational purposes only.*
