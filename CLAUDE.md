# Dionice Model — notes for Claude Code

AI stock newsletter (Tue/Thu) plus a Streamlit portfolio tracker for one small retail
investor (Revolut, 300-400 EUR/month, value style). Python, Supabase, GitHub Actions,
Claude API. The newsletter and all user-facing text are Croatian; code and comments
are English. Talk to the user in Croatian.

## Map

- `scripts/run_weekly.py` — newsletter pipeline: discovery → fundamentals → scorer → AI (one
  Message Batch, two models) → sell guard / second pass → email → Supabase; every other
  Thursday also the revisit of the last 60 days' BUY ideas and reached zones, every run a
  small A/B sample
- `analysis/ai_analyst.py` — prompts, JSON schema (structured outputs), batch runner, and
  `finalize()`: every deterministic rule (hype block, confidence floor, 30% concentration
  cap, starter entry plan, zone-drift flag)
- `analysis/revisit.py` — revisit schedule (every `revisit_every_weeks` Thursdays, counted
  from the last revisit that ran), selection, prompt block and email line, zone drift,
  stale-watchlist rules
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
- Revisit on any day, whatever the cycle (batch, ~$0.3-0.5): `python scripts/run_weekly.py --dry-run --limit 2 --revisit`
- Learning report, statistics only (free): `python scripts/run_quarterly_review.py --dry-run --no-ai`
- Screenshots: `python scripts/verify_screens.py --html <file>` / `--app`
- Production runs on Python 3.11 (GitHub Actions); check new syntax against 3.11.

## Rules

- The GitHub repo is PUBLIC (free Streamlit hosting). Never commit `private/`, `*.xlsx`,
  `*.docx`, `.env`, `supabase pass.txt` or anything personal. The investor profile
  lives only in Supabase.
- AI budget: the guard is $8/month (`monthly_budget_usd`; the user accepts up to ~$10,
  2026-10-25), measured spend ~$2.5 in Oct 2026. Claude Haiku 5.5 for routine analysis;
  Claude Sonnet 5.5 for the sell guard, second pass, Thursday revisits, the A/B sample
  and the summary; Claude Opus 5.5 once a quarter. Effort is pinned per model (the cache
  is keyed on it). Any change that adds calls needs a cost estimate first.
- Lessons change how the analyst judges, never the system's rules or what an action
  means: the Oct 2026 lesson "don't write WATCHLIST" turned almost every idea into an
  untracked WAIT. If data calls for a different rule, change a parameter instead.
- Data refresh scripts must not overwrite a good file with a bad one: the 2026-10-01
  refresh wrote an empty gem pool. Keep their guards; the newsletter's health line shows
  gem pool size and benchmark coverage.
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
