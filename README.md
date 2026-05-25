# Dionice Model — AI Stock Newsletter & Portfolio Tracker

AI-powered stock analysis system for a small retail investor using Revolut Basic.

**What it does:**
- Sends a stock newsletter every **Tuesday and Thursday at 15:00 CET**
- Sends a monthly deep portfolio report on the **first Saturday of each month**
- Tracks your portfolio via a minimal **Streamlit web app**
- Logs agent recommendations vs your decisions for backtesting

**Cost: ~$1/month** (Claude API Haiku only)

---

## Setup Guide (Step by Step)

### Step 1 — Prerequisites (create these accounts)

| Account | Where | What you need |
|---------|-------|---------------|
| GitHub | github.com | Already have ✅ |
| Reddit | reddit.com | Already have ✅ |
| Supabase | supabase.com | Create free project |
| Anthropic | console.anthropic.com | API key (~$5 credit to start) |
| Gmail App Password | myaccount.google.com → Security → App Passwords | **Requires 2FA enabled first** |
| Streamlit Cloud | share.streamlit.io | Sign in with GitHub |

---

### Step 2 — Reddit API Setup

1. Go to [reddit.com/prefs/apps](https://reddit.com/prefs/apps)
2. Click **"Create App"** at the bottom
3. Name: `dionice-model`
4. Type: **script**
5. Redirect URI: `http://localhost:8080`
6. Note down: **client ID** (under app name) and **secret**

---

### Step 3 — Supabase Setup

1. Go to [supabase.com](https://supabase.com) → New Project
2. Remember your project password
3. Go to **SQL Editor** → paste the contents of `data/schema.sql` → Run
4. Update the VG seed row: change `price_per_share` to your actual average cost in EUR
5. Go to **Settings → API** → copy **Project URL** and **anon public key**

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
| `REDDIT_CLIENT_ID` | from Step 2 |
| `REDDIT_CLIENT_SECRET` | from Step 2 |
| `REDDIT_USER_AGENT` | `dionice-model/1.0 by u/YOUR_REDDIT_USERNAME` |
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
├── .github/workflows/       # GitHub Actions (scheduling)
│   ├── weekly_newsletter.yml
│   ├── monthly_report.yml
│   └── update_decision_prices.yml
├── app/
│   └── portfolio_app.py     # Streamlit web app
├── analysis/
│   ├── fundamentals.py      # yfinance data fetcher + cache
│   ├── scorer.py            # 5-category scoring system
│   ├── congress_tracker.py  # Senate/House Stock Watcher
│   ├── reddit_tracker.py    # Reddit PRAW + anti-hype filter
│   ├── ai_analyst.py        # Claude API synthesis
│   └── email_sender.py      # Gmail SMTP + HTML builder
├── scripts/
│   ├── run_weekly.py        # Orchestrates full weekly run
│   ├── run_monthly.py       # Monthly deep report
│   └── update_prices.py    # Fills 30/90/180d prices
├── data/
│   └── schema.sql           # Supabase database schema
├── requirements.txt
├── .env.example             # Template for local .env
└── .gitignore
```

---

## How the AI Analyzes Stocks

1. **Discovers tickers** from Reddit (anti-hype filtered), Congress trades, your portfolio, and manual watchlist
2. **Fetches fundamentals** (P/E, PEG, FCF, margins, debt, etc.) via yfinance with 24h cache
3. **Scores each stock** 0-100 using category-specific weights (quality compounder, value/cyclical, turnaround, speculative growth, dividend/defensive)
4. **Claude analyzes** all signals and produces: action, buy zone, target, thesis, catalyst, downside scenario, evidence table
5. **Email is sent** with max 4-7 actions; "NO TRADE" is a valid primary output

**Reddit rule:** hype score ≥7/10 → automatically blocked from BUY → maximum WATCHLIST  
**Congress rule:** weak signal only — idea source, never a buy trigger  
**No trade rule:** every recommendation is compared to "hold cash or add to best existing position"

---

## Email Schedule

- **Tuesday 15:00 CET** — Weekly newsletter
- **Thursday 15:00 CET** — Weekly newsletter  
- **First Saturday of month, 09:00 UTC** — Monthly deep report
- **Monday 10:00 UTC** — Background price update (no email)

Note: Schedules use UTC. Summer = CEST (UTC+2), so 13:00 UTC = 15:00 CEST.

---

*Not financial advice. AI-generated analysis for educational purposes only.*
