---
name: verify
description: Verify a change to the Dionice Model before calling it done — run the tests, dry-run the affected job, screenshot the newsletter, learning report or Streamlit pages and inspect the images. Use after any code change in this repository, and whenever the user asks to check, test or verify something.
---

# Verify a change

Claude cannot see the email or the app unless it renders them, so a change is done
only when the evidence below has been produced and looked at.

## 1. Tests (always, free)

```
python -m pytest -q
```

All must pass. A new rule or calculation gets its own test in `tests/`; tests never
touch the network (use the `book_factory` fixture for prices, fake clients for Supabase).

If syntax could differ between versions (f-strings, typing), also compile on Python
3.11, which GitHub Actions uses:

```
python -m uv python find 3.11   # then: <that python> -m py_compile analysis/*.py scripts/*.py app/*.py
```

## 2. Dry run of what changed

Dry runs send no email and write nothing to Supabase, but Claude calls cost money —
say what a run will cost before starting it.

| Changed | Command | Cost |
|---|---|---|
| newsletter, prompts, scorer, guards | `python scripts/run_weekly.py --dry-run --limit 1 --sync` | ~$0.05 |
| batch path | `python scripts/run_weekly.py --dry-run --limit 0` | ~$0.03, minutes to an hour |
| learning report statistics | `python scripts/run_quarterly_review.py --dry-run --no-ai` | free |
| learning report text | `python scripts/run_quarterly_review.py --dry-run` | ~$0.30 |
| scoring job | `python scripts/update_prices.py --dry-run` | free |

Read the log: every analysis parsed (no "Failed to parse"), the `[usage]` line shows
cache reads, and guards fired where expected.

## 3. Look at it

```
python scripts/verify_screens.py --html <path printed by the dry run>
python scripts/verify_screens.py --app --pages "Portfolio,Decisions,Učenje"
```

Open every PNG with the Read tool and check: numbers match the log, Croatian text
has no foreign script, nothing is cut off, new notes (guards, entry plan, weights)
appear where they should. The app screenshots only navigate — never click a form.

## 4. Report

Tell the user in Croatian what was verified (tests, runs, screenshots), what it cost,
and what was not verified and why. Do not claim a check that was not run.
