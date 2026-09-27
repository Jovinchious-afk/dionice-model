# Dionice Model — notes for Claude Code

AI stock newsletter (Tue/Thu) plus a Streamlit portfolio tracker for one small retail
investor (Revolut, 300-400 EUR/month, value style). Python, Supabase, GitHub Actions,
Claude API. The newsletter and all user-facing text are Croatian; code and comments
are English. Talk to the user in Croatian.

## Map

- `scripts/run_weekly.py` — newsletter pipeline: discovery → fundamentals → scorer → AI (one
  Message Batch) → sell guard / second pass → email → Supabase
- `analysis/ai_analyst.py` — prompts, JSON schema (structured outputs), batch runner, and
  `finalize()`: every deterministic rule (hype block, confidence floor, 30% concentration
  cap, starter entry plan)
- `analysis/params.py` — tunable parameters; defaults in code, overrides in `model_params`
  (only ACTIVE rows, approved on the Streamlit "Učenje" page)
- `analysis/scorer.py` — 0-100 fundamental score, P/E vs `data/sector_benchmarks.json`
- `analysis/learning.py` + `scripts/run_quarterly_review.py` — quarterly learning report
- `scripts/update_prices.py` — Wednesday job: 30/90/180-day outcomes vs SPY, buy-zone fills
- `analysis/usage.py` — token cost per run → `llm_usage`, monthly budget guard
- `app/portfolio_app.py` — Streamlit (Portfolio, Log Trade, Watchlist, Decisions,
  Newsletteri, Učenje)
- `data/schema*.sql` — Supabase migrations, applied by the user in the SQL Editor

## Commands

- Tests (no network, no cost): `python -m pytest -q`
- Newsletter dry run (no email, no DB writes; real Claude calls, ~$0.05):
  `python scripts/run_weekly.py --dry-run --limit 1 --sync`
- Learning report, statistics only (free): `python scripts/run_quarterly_review.py --dry-run --no-ai`
- Screenshots: `python scripts/verify_screens.py --html <file>` / `--app`
- Production runs on Python 3.11 (GitHub Actions); check new syntax against 3.11.

## Rules

- The GitHub repo is PUBLIC (free Streamlit hosting). Never commit `private/`, `*.xlsx`,
  `*.docx`, `.env`, `supabase pass.txt` or anything personal. The investor profile
  lives only in Supabase.
- AI budget is ~$4-5/month in total. Haiku 4.5 for routine analysis, Sonnet 5 for the
  sell guard and the BUY second pass, Opus 5 once a quarter. Any change that adds
  calls needs a cost estimate first.
- Propose code changes and wait for the user's confirmation before implementing.
- Schema changes: a new `data/schema_vN.sql` (additive, `IF NOT EXISTS`), and the code
  must keep working until the user has run it.
- Leave alone unless asked: cron times (the delayed sends are known), the random
  per-run sampling in `stock_discovery.py` (intended), the dead Congress APIs.
- Sell only when the value changed, not the price; classify cyclicals first; hype is
  an anti-signal. Parameters change only through the learning report plus approval.
- Before installing any third-party skill, plugin or MCP server, scan it with
  NVIDIA SkillSpector — `.env` holds live API keys.

## Done means verified

Use the `verify` skill (`.claude/skills/verify/SKILL.md`) after every change: tests,
the relevant dry run, screenshots you actually look at, and a short report of
what was and was not checked.
