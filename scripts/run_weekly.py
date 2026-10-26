"""
Weekly newsletter entry point — runs every Tuesday and Thursday.
Orchestrates: discovery → fundamentals → scoring → checklist and cycle context →
AI analysis (with memory of its own previous calls) → sell guard and second pass
→ email.

Candidate pipeline:
  Main universe: random sector-rotating sample (changes every run)
  Hidden gems:   random sample from the small-cap pool
  Portfolio:     always analyzed regardless of score

The routine analyses go out as one Message Batch (half price; the email arrives
somewhat later). BUY candidates and zones lowered while the price sat at the old
zone are then re-checked by REVIEW_MODEL while the month's AI spend stays under the
budget in model_params.

Every other Thursday (revisit_every_weeks; the day after the Wednesday job marks
reached buy zones) the run also revisits the newsletter's own ideas: every
BUY_BELOW/ADD_ON_DIP call of the last 60 days and every watchlist idea whose zone the
price reached, analysed again by REVIEW_MODEL in the same batch and shown in their
own section from revisit_min_confidence up. The cycle counts from the last revisit
that actually ran, so a failed Thursday is made up the following week.
A few random stocks per run are analysed by REVIEW_MODEL as well (the A/B sample,
never published), so the learning report can compare the two models on the same
stocks.

Usage: python scripts/run_weekly.py [--dry-run] [--limit N] [--sync] [--revisit | --no-revisit]
  --dry-run     no email and no database writes; the HTML goes to a temp file
  --limit N     analyse only the portfolio plus N universe candidates and one gem
  --sync        plain API calls instead of a batch (faster, full price, no A/B sample)
  --revisit     run the revisit today whatever the day or cycle; --no-revisit skips it
"""

import argparse
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from analysis.congress_tracker import get_congress_signals, get_tickers_from_congress
from analysis.insider_tracker import get_insider_signals_batch
from analysis.fundamentals import fetch_multiple
from analysis.scorer import benchmark_coverage, score_stock, classify_category, hard_exclude, resolve_weights
from analysis.ai_analyst import (
    BEARISH_ACTIONS,
    BULLISH_ACTIONS,
    MODEL,
    PROMPT_VERSION,
    REVIEW_EFFORT,
    REVIEW_MODEL,
    analyze_stock,
    build_run_context,
    build_stock_prompt,
    build_system,
    finalize,
    generate_weekly_summary,
    parse_message,
    prewarm_cache,
    request_params,
    run_batch,
)
from analysis.analysis_history import build_features, build_previous_calls_block, load_recent_history, record_analyses
from analysis.checklist import build_checklist, checklist_summary, deterioration_signals, format_checklist
from analysis.email_sender import build_html_email, send_email
from analysis.investor_profile import load_investor_profile
from analysis.params import load_params
from analysis.portfolio import compute_holdings, usd_to_eur_now
from analysis.prices import BENCHMARK, PriceBook, latest_closes
from analysis.revisit import (
    REVISIT_WEEKDAY,
    last_revisit_at,
    next_revisit_on,
    origin_line,
    revisit_due,
    revisit_prompt_block,
    select_revisits,
    stale_watchlist_rows,
)
from analysis.sector_context import format_context
from analysis.sentiment_tracker import apply_market_cap, get_sentiment_batch
from analysis.stock_discovery import gem_pool_size, select_candidates
from analysis.macro_context import fetch_macro_context, format_macro_for_prompt
from analysis.supabase_client import get_supabase
from analysis.ticker_health import get_dead_tickers, load_health, record_run
from analysis.usage import UsageTracker, month_spend

GEM_PRICE_CAP = 12.0  # hidden gems must be under this price to pass through
DECISION_REPEAT_DAYS = 30
DECISION_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP", "WATCHLIST", "HOLD", "SELL", "REDUCE"}
# The watchlist tracks buy zones; HOLD/SELL/REDUCE concern positions already owned
WATCHLIST_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP", "WATCHLIST"}
SELL_GUARD_EFFORT = "high"  # a sell call on a held position is the rarest and costliest decision
AB_SUFFIX = "__ab"          # batch key of the A/B request for a ticker
YAHOO_SECTORS = 11          # sectors a complete P/E benchmark file covers
MIN_GEM_POOL = 40           # below this the health line warns (the 2026-10-01 refresh left 0)
GMAIL_CLIP_BYTES = 100_000  # Gmail hides everything past ~102 KB behind "[Message clipped]"


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def get_latest_lessons(client) -> str | None:
    """Most recent lessons the investor approved on the Streamlit "Učenje" page."""
    try:
        rows = (client.table("model_lessons").select("*").eq("status", "ACTIVE")
                .order("generated_at", desc=True).limit(1).execute().data or [])
    except Exception:
        # Before schema_v8.sql there is no status column: fall back to the latest row
        try:
            rows = client.table("model_lessons").select("*").order("generated_at", desc=True).limit(1).execute().data or []
        except Exception as exc:
            print(f"[run_weekly] Could not load model_lessons: {exc}")
            return None
    return rows[0]["lessons_text"] if rows else None


def schema_v8_applied(client) -> bool:
    if not client:
        return False
    try:
        client.table("decisions").select("entry_plan").limit(1).execute()
        return True
    except Exception:
        return False


def get_active_watchlist(client) -> dict[str, dict]:
    """Active watchlist entries keyed by symbol; ordered so the newest row per symbol wins."""
    try:
        result = client.table("watchlist").select("*").eq("status", "ACTIVE").order("suggested_at").execute()
        return {row["symbol"]: row for row in (result.data or [])}
    except Exception as exc:
        print(f"[run_weekly] Could not load watchlist: {exc}")
        return {}


def get_positions_meta(client) -> dict[str, dict]:
    """Fetches personal thesis per position from positions_meta table."""
    try:
        result = client.table("positions_meta").select("*").execute()
        return {row["symbol"]: row for row in (result.data or [])}
    except Exception as exc:
        print(f"[run_weekly] Could not load positions_meta: {exc}")
        return {}


def get_account_cash(client) -> dict[str, float]:
    """Cash balances from account_settings (entered on the Streamlit Portfolio page)."""
    cash = {"cash_usd": 0.0, "cash_eur": 0.0}
    if not client:
        return cash
    try:
        for row in client.table("account_settings").select("*").execute().data or []:
            if row.get("key") in cash and row.get("value") is not None:
                cash[row["key"]] = float(row["value"])
    except Exception as exc:
        print(f"[run_weekly] Could not load account_settings (run data/schema_v7.sql?): {exc}")
    return cash


def get_portfolio_positions(client) -> list[dict]:
    """Open positions from the transactions log (cost method: analysis/portfolio.py)."""
    if not client:
        return []
    try:
        rows = client.table("transactions").select("*").order("trade_date").execute().data or []
    except Exception as exc:
        print(f"[run_weekly] Supabase error: {exc}")
        return []

    positions = []
    for h in compute_holdings(rows).values():
        if h["shares"] <= 0:
            continue
        positions.append({
            "symbol": h["symbol"],
            "company_name": h["company_name"],
            "shares": round(h["shares"], 4),
            "avg_cost_usd": h["avg_cost_usd"],
            "avg_cost": f"${h['avg_cost_usd']:.2f}",
            "current_price": "N/A",
            "pnl_pct": "N/A",
            "weight": None,
        })
    return positions


def build_portfolio_context(positions: list[dict], cash: dict[str, float], portfolio_value_eur: float | None) -> str:
    parts = []
    for p in positions:
        weight = f", {p['weight'] * 100:.0f}% kapitala" if p.get("weight") is not None else ""
        parts.append(f"{p['symbol']}: {p['shares']:g} dionica @ prosječno {p['avg_cost']} "
                     f"(sada {p['current_price']}, P&L {p['pnl_pct']}{weight})")
    context = ("Pozicije: " + "; ".join(parts)) if parts else "Nema otvorenih pozicija."
    if cash["cash_usd"] or cash["cash_eur"]:
        context += f" | Gotovina spremna za ulaganje: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}"
    if portfolio_value_eur:
        context += f" | Ukupni kapital (pozicije + gotovina) ≈ €{portfolio_value_eur:,.0f}"
    return context


def apply_sell_guard(rec: dict, claude, system, prompt_kwargs: dict, ctx: dict, usage) -> dict:
    """
    A SELL/REDUCE on a held position must rest on a deterioration in the business,
    not on price — the investor's own rule, and the gap behind the GNRC REDUCE of
    2026-09-10. With no deterioration in the data the call becomes HOLD; with some,
    REVIEW_MODEL re-runs the analysis and its verdict stands.
    """
    original = rec.get("action")
    ticker = rec.get("ticker")
    signals = deterioration_signals(ctx["fundamentals"])

    if not signals:
        rec["action"] = "HOLD"
        rec["sell_guard_note"] = (
            f"{original} blokiran: podaci ne pokazuju pogoršanje poslovanja (prihod, marže, dobit, dug, "
            "zalihe, dividenda) — samo cijenu ili sentiment. Pravilo ulagača: ne prodaji zbog pada cijene, "
            "nego ako se promijenila vrijednost."
        )
        print(f"[run_weekly] Sell guard: {ticker} {original} → HOLD (no deterioration signals)")
        return rec

    signals_txt = "; ".join(signals)
    note = (
        f"Prvi model predložio je {original}. Pogoršanja u podacima: {signals_txt}. Procijeni jesu li "
        "privremena (sezona, jednokratni trošak, računovodstveni efekt) ili trajna promjena vrijednosti. "
        "SELL/REDUCE samo ako je vrijednost trajno narušena; inače HOLD."
    )
    print(f"[run_weekly] Sell guard: {ticker} {original} with signals [{signals_txt}] → review by {REVIEW_MODEL}")
    try:
        review = analyze_stock(claude, system, prompt_kwargs, ctx, model=REVIEW_MODEL, review_note=note,
                               effort=SELL_GUARD_EFFORT, usage=usage)
    except Exception as exc:
        rec["sell_guard_note"] = f"{original} uz pogoršanja ({signals_txt}); provjera modelom {REVIEW_MODEL} nije uspjela: {exc}"
        return rec
    if review.get("error"):
        rec["sell_guard_note"] = f"{original} uz pogoršanja ({signals_txt}); odgovor modela {REVIEW_MODEL} nije bilo moguće pročitati."
        return rec

    review["sell_guard_note"] = (
        f"Prvi model: {original}. Pogoršanja u podacima: {signals_txt}. "
        f"Konačnu odluku donio {REVIEW_MODEL}: {review.get('action')}."
    )
    review["first_pass"] = {"model": rec.get("model"), "action": original, "confidence": rec.get("confidence")}
    return review


def second_pass(rec: dict, claude, system, prompt_kwargs: dict, ctx: dict, usage) -> dict:
    """
    BUY candidates are where the newsletter's value is (BUY_BELOW calls beat the
    S&P 500 by ~10pp over 30 days, WATCHLIST calls by nothing), so REVIEW_MODEL
    re-checks them before they reach the email, together with zones the routine
    model lowered while the price sat at the old one. Its verdict stands.
    """
    drift = f"\nUPOZORENJE SUSTAVA: {rec['zone_drift_note']}" if rec.get("zone_drift_note") else ""
    note = (
        f"Prvi model ({rec.get('model')}) predložio je {rec.get('action')} {rec.get('buy_zone') or ''} "
        f"s confidence {rec.get('confidence')}. Njegova teza: {(rec.get('investment_thesis') or '')[:400]}{drift}\n"
        "Neovisno provjeri podatke, valuaciju u odnosu na industriju, zonu kupnje i usporedbu s držanjem gotovine. "
        "Ako se ne slažeš, promijeni akciju ili zonu. Tvoja odluka je konačna."
    )
    try:
        review = analyze_stock(claude, system, prompt_kwargs, ctx, model=REVIEW_MODEL, review_note=note,
                               effort=REVIEW_EFFORT, usage=usage)
    except Exception as exc:
        print(f"[run_weekly] Second pass failed for {rec.get('ticker')}: {exc}")
        return rec
    if review.get("error"):
        print(f"[run_weekly] Second pass unreadable for {rec.get('ticker')} — keeping the first call")
        return rec
    review["first_pass"] = {
        "model": rec.get("model"), "action": rec.get("action"),
        "confidence": rec.get("confidence"), "buy_zone": rec.get("buy_zone"),
    }
    agreed = review.get("action") == rec.get("action")
    review["second_pass_note"] = (
        f"Provjerio jači model ({REVIEW_MODEL}): "
        + ("potvrđeno." if agreed else f"{rec.get('action')} → {review.get('action')}.")
    )
    # The second pass runs after the sell guard, so a held stock it turns into
    # SELL/REDUCE must pass the same test: without deterioration in the business
    # the call stays HOLD (the reviewer here is already the stronger model).
    if ctx.get("in_portfolio") and review.get("action") in BEARISH_ACTIONS \
            and not deterioration_signals(ctx["fundamentals"]):
        review["sell_guard_note"] = (
            f"{review['action']} blokiran: podaci ne pokazuju pogoršanje poslovanja — samo cijenu ili sentiment. "
            "Pravilo ulagača: ne prodaji zbog pada cijene, nego ako se promijenila vrijednost."
        )
        review["action"] = "HOLD"
    return review


def newsletter_worthy(rec: dict, min_confidence: int) -> bool:
    """
    Only calls the investor can act on reach the email: the confidence floor keeps the
    newsletter short enough that Gmail stops clipping it. Two exceptions — hidden gems are
    speculative by definition, and a position he already owns has to be shown whatever
    the confidence is.
    """
    if rec.get("is_hidden_gem") or rec.get("in_portfolio"):
        return True
    confidence = _num(rec.get("confidence"))
    return confidence is not None and confidence >= min_confidence


def load_revisit_sources(client, days: int) -> tuple[list[dict], list[dict]]:
    """Decisions of the revisit window and every active agent watchlist row."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        decisions = client.table("decisions").select("*").gte("recommended_at", cutoff).execute().data or []
    except Exception as exc:
        print(f"[run_weekly] Could not load decisions for the revisit: {exc}")
        decisions = []
    try:
        watchlist = (client.table("watchlist").select("*").eq("status", "ACTIVE")
                     .not_null("confidence").execute().data or [])
    except Exception as exc:
        print(f"[run_weekly] Could not load the watchlist for the revisit: {exc}")
        watchlist = []
    return decisions, watchlist


def spy_change_since(book: PriceBook | None, since_iso: str, now: datetime) -> float | None:
    """S&P 500 change in % from a past call to now, for the revisit line."""
    if book is None:
        return None
    try:
        start = datetime.fromisoformat(str(since_iso))
    except ValueError:
        return None
    p0, p1 = book.close_on_or_before(BENCHMARK, start), book.close_on_or_before(BENCHMARK, now)
    return (p1 / p0 - 1) * 100 if p0 and p1 else None


def choose_ab_sample(jobs: dict[str, dict], revisits: dict, n: int) -> list[str]:
    """Random stocks the review model analyses as well; positions, gems and revisits are left out."""
    eligible = [t for t, job in jobs.items()
                if not job["ctx"]["in_portfolio"] and not job["ctx"]["is_hidden_gem"] and t not in revisits]
    return random.sample(eligible, min(max(n, 0), len(eligible)))


def build_health_line(gem_pool: int, coverage: dict, month_cost: float | None, budget: float,
                      failed: int, dead: int, next_revisit=None) -> str:
    """
    One line at the bottom of the email, so a silent failure (like the empty gem pool)
    is seen at once; in a newsletter without the revisit it also says when the next one runs.
    """
    bench = f"P/E benchmark {coverage['sectors']}/{YAHOO_SECTORS} sektora"
    if coverage.get("carried_over"):
        bench += f" ({coverage['carried_over']} grupa iz prošlog kvartala)"
    parts = [
        f"gemova u bazenu {gem_pool}" + (" ⚠️" if gem_pool < MIN_GEM_POOL else ""),
        bench + (" ⚠️" if coverage["sectors"] < YAHOO_SECTORS else ""),
        f"AI ovaj mjesec ${month_cost:.2f} od ${budget:.2f}" if month_cost is not None else None,
        f"analiza bez odgovora {failed}" + (" ⚠️" if failed else ""),
        f"isključenih mrtvih tickera {dead}" if dead else None,
        f"iduća ponovna provjera: četvrtak {next_revisit.day}. {next_revisit.month}." if next_revisit else None,
    ]
    return "Zdravlje sustava: " + " · ".join(p for p in parts if p)


def expire_stale_watchlist(client, recommendations: list[dict], published: list[dict], history: dict,
                           now: datetime, max_age_days: int) -> None:
    """
    The watchlist mirrors the agent's current view: an idea the latest analysis no
    longer backs, or one nobody has re-analysed for months, goes to EXPIRED (it had
    168 active rows going back to June, ADD_ON_DIP rows for stocks long sold among them).
    """
    analysed = {r.get("ticker"): r for r in recommendations if r.get("ticker") and not r.get("error")}
    stale = stale_watchlist_rows(_load_active_ai_watchlist(client), now, analysed,
                                 {r.get("ticker") for r in published}, WATCHLIST_ACTIONS, set(history), max_age_days)
    if stale:
        _expire_watchlist_rows(client, stale)
        print(f"[run_weekly] Expired {len(stale)} watchlist rows the latest analyses no longer back: "
              f"{sorted({row['symbol'] for row in stale})}")


def _load_active_ai_watchlist(client) -> dict[str, list[dict]]:
    """ACTIVE agent-written watchlist rows per symbol, newest first. Manual rows (no confidence) are left alone."""
    try:
        rows = (client.table("watchlist").select("id,symbol,action,buy_zone,suggested_at")
                .eq("status", "ACTIVE").not_null("confidence")
                .order("suggested_at", desc=True).execute().data or [])
    except Exception as exc:
        print(f"[run_weekly] Could not load watchlist for dedupe: {exc}")
        return {}
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    return by_symbol


def _expire_watchlist_rows(client, rows: list[dict]) -> None:
    for row in rows:
        try:
            client.table("watchlist").update({"status": "EXPIRED"}).eq("id", row["id"]).execute()
        except Exception as exc:
            print(f"[run_weekly] Could not expire watchlist row {row.get('id')}: {exc}")


def save_recommendations_to_supabase(client, recommendations: list[dict], fundamentals_map: dict,
                                     has_v8: bool = False) -> None:
    """
    Writes watchlist and decision rows without the duplicates every run used to add
    (GNRC had six decision rows and four ACTIVE watchlist rows in four weeks).
    A watchlist row is replaced only when the action or buy zone changed; a decision
    is logged only when the action differs from every call on that symbol in the
    last DECISION_REPEAT_DAYS.
    """
    if not client:
        return
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cutoff = (now_dt - timedelta(days=DECISION_REPEAT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    recent_actions: dict[str, set] = {}
    try:
        for row in client.table("decisions").select("symbol,agent_action").gte("recommended_at", cutoff).execute().data or []:
            recent_actions.setdefault(row["symbol"], set()).add(row.get("agent_action"))
    except Exception as exc:
        print(f"[run_weekly] Could not load recent decisions for dedupe: {exc}")

    active = _load_active_ai_watchlist(client)
    superseded = [row for rows in active.values() for row in rows[1:]]
    if superseded:
        _expire_watchlist_rows(client, superseded)
        print(f"[run_weekly] Expired {len(superseded)} superseded watchlist rows")

    for rec in recommendations:
        action = rec.get("action", "NO_ACTION")
        ticker = rec.get("ticker", "")
        if action not in DECISION_ACTIONS or not ticker:
            continue

        if action in WATCHLIST_ACTIONS:
            current = (active.get(ticker) or [None])[0]
            unchanged = (
                current is not None
                and current.get("action") == action
                and (current.get("buy_zone") or "") == (rec.get("buy_zone") or "")
            )
            if not unchanged:
                if current:
                    _expire_watchlist_rows(client, [current])
                try:
                    client.table("watchlist").insert({
                        "symbol": ticker,
                        "company_name": rec.get("company_name", ""),
                        "category": rec.get("category"),
                        "suggested_at": now,
                        "action": action,
                        "buy_zone": rec.get("buy_zone"),
                        "target_price": rec.get("target_price"),
                        "confidence": rec.get("confidence"),
                        "thesis": rec.get("investment_thesis", ""),
                        "catalyst": rec.get("catalyst", ""),
                        "downside_scenario": rec.get("downside_scenario", ""),
                        "position_size": rec.get("position_size"),
                        "evidence_json": rec.get("evidence_table", {}),
                        "status": "ACTIVE",
                    }).execute()
                except Exception as exc:
                    print(f"[run_weekly] Watchlist insert failed for {ticker}: {exc}")

        if action in recent_actions.get(ticker, set()):
            print(f"[run_weekly] {ticker} {action} already logged in the last {DECISION_REPEAT_DAYS} days — no new decision row")
            continue

        row = {
            "recommended_at": now,
            "symbol": ticker,
            "agent_action": action,
            "agent_buy_zone": rec.get("buy_zone"),
            "agent_confidence": rec.get("confidence"),
            "agent_thesis": rec.get("investment_thesis", ""),
            "user_action": "PENDING",
            # From the data, never from the model's text (AFL was once stored as $12,151)
            "price_at_recommendation": _num((fundamentals_map.get(ticker) or {}).get("current_price")),
            "outcome_30d": "pending",
            "outcome_90d": "pending",
            "outcome_180d": "pending",
        }
        if has_v8:
            row["entry_plan"] = rec.get("entry_plan")
            row["model"] = rec.get("model")
        try:
            client.table("decisions").insert(row).execute()
            recent_actions.setdefault(ticker, set()).add(action)
        except Exception as exc:
            print(f"[run_weekly] Decisions insert failed for {ticker}: {exc}")


def save_newsletter_to_supabase(client, subject: str, content: dict, email_type: str = "WEEKLY") -> None:
    if not client:
        return
    try:
        client.table("newsletters").insert({
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "type": email_type,
            "subject": subject,
            "content_json": content,
            "actions_summary": content.get("email_subject_suffix", ""),
        }).execute()
    except Exception as exc:
        print(f"[run_weekly] Error saving newsletter: {exc}")


def main(dry_run: bool = False, limit: int | None = None, sync: bool = False, revisit: bool | None = None):
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    print(f"[run_weekly] Starting analysis for {date_str} ({day_name})"
          f"{' — DRY RUN' if dry_run else ''}{' — sync calls' if sync else ' — batch'}")

    db_client = get_supabase()
    write_client = None if dry_run else db_client
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    usage = UsageTracker("weekly")
    params = load_params(db_client)
    weights = resolve_weights(params.get("scorer_weights"))
    has_v8 = schema_v8_applied(db_client)

    # 1. Portfolio, cash and the investor's own context
    positions = get_portfolio_positions(db_client)
    positions_by_symbol = {p["symbol"]: p for p in positions}
    portfolio_tickers = list(positions_by_symbol)
    cash = get_account_cash(db_client)
    print(f"[run_weekly] Portfolio: {portfolio_tickers} | cash ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}")

    positions_meta = get_positions_meta(db_client) if db_client else {}
    active_watchlist = get_active_watchlist(db_client) if db_client else {}
    print(f"[run_weekly] Active watchlist: {list(active_watchlist.keys())}")

    lessons_text = get_latest_lessons(db_client) if db_client else None
    if lessons_text:
        print("[run_weekly] Loaded approved lessons from the learning report")

    investor_profile = load_investor_profile(db_client)
    print(f"[run_weekly] Investor profile: {'loaded' if investor_profile else 'missing — run scripts/upload_investor_profile.py'}")
    history = load_recent_history(db_client)

    # Tickers production has repeatedly failed to fetch — kept out of this run's sample
    health = {}
    dead_tickers: set[str] = set()
    if db_client:
        health = load_health(db_client)
        dead_tickers = get_dead_tickers(db_client, health)
        if dead_tickers:
            print(f"[run_weekly] {len(dead_tickers)} tickers retired as dead, excluded from sampling")

    # 1a. Thursday revisit, every revisit_every_weeks Thursdays counted from the last one
    # that ran (--revisit / --no-revisit override): the newsletter's own BUY ideas and
    # the reached zones of the last weeks
    last_revisit = last_revisit_at(history)
    if revisit is None:
        revisit_day = (today.weekday() == REVISIT_WEEKDAY
                       and revisit_due(last_revisit, today, params["revisit_every_weeks"]))
        if today.weekday() == REVISIT_WEEKDAY and not revisit_day:
            print(f"[run_weekly] Thursday revisit not due: last one ran {last_revisit:%Y-%m-%d}, "
                  f"every {params['revisit_every_weeks']} weeks")
    else:
        revisit_day = revisit
    next_revisit = None if revisit_day else next_revisit_on(last_revisit, today, params["revisit_every_weeks"])
    revisit_origins: dict[str, dict] = {}
    if revisit_day and db_client:
        decisions_rows, watchlist_rows = load_revisit_sources(db_client, params["revisit_days"])
        fresh_prices = latest_closes([w["symbol"] for w in watchlist_rows if w.get("buy_zone")]
                                     + [d["symbol"] for d in decisions_rows if d.get("agent_buy_zone")])
        revisit_origins = select_revisits(
            decisions_rows, watchlist_rows, today, held=set(portfolio_tickers), dead=dead_tickers,
            days=params["revisit_days"], max_n=int(params["revisit_max"]), prices=fresh_prices,
        )
        print(f"[run_weekly] Thursday revisit: {len(revisit_origins)} ideas — "
              + ", ".join(f"{s} ({o['trigger']})" for s, o in revisit_origins.items()))
    spy_book = PriceBook() if revisit_origins else None
    if spy_book:
        # one S&P 500 download from the oldest revisited call covers every revisit line
        spy_book.closes(BENCHMARK, min(datetime.fromisoformat(o["date"]) for o in revisit_origins.values()))

    # 1b. Macro context (yfinance, free)
    print("[run_weekly] Fetching macro context...")
    macro_data = {}
    try:
        macro_data = fetch_macro_context()
        # CPI carries yoy_pct rather than current, so a blanket .get("current") logged it as 0.0
        parts = []
        for k, v in macro_data.items():
            val = v.get("current", v.get("yoy_pct"))
            parts.append(f"{k}={val:.1f}" if isinstance(val, (int, float)) else f"{k}=n/a")
        print(f"[run_weekly] Macro: {', '.join(parts)}")
    except Exception as exc:
        print(f"[run_weekly] Macro fetch failed (non-critical): {exc}")
    macro_text = format_macro_for_prompt(macro_data)

    # 2. Congress signals (bonus idea source, not primary discovery)
    print("[run_weekly] Fetching Congress trades...")
    congress_tickers = get_tickers_from_congress(days_back=14, min_members=1)
    print(f"[run_weekly] Congress tickers: {congress_tickers[:10]}")

    # 3. Autonomous discovery — builds main + gem candidate lists
    print("[run_weekly] Running autonomous stock discovery...")
    main_candidates, gem_candidates = select_candidates(
        portfolio_tickers=portfolio_tickers,
        congress_tickers=congress_tickers,
        dt=today,
        max_main=35,
        max_gems=8,
        exclude=dead_tickers,
    )
    if limit is not None:
        # Portfolio tickers come first in main_candidates, so they always survive the cut
        main_candidates = main_candidates[: len(portfolio_tickers) + limit]
        gem_candidates = gem_candidates[:1]
    # A revisited idea is analysed as a revisit even when the sample drew it too
    gem_candidates = [g for g in gem_candidates if g not in revisit_origins]
    all_candidates = list(dict.fromkeys(main_candidates + list(revisit_origins) + gem_candidates))
    print(f"[run_weekly] Total candidates to fetch: {all_candidates}")

    # 4. StockTwits sentiment for all candidates — sole source of the hype signal
    print("[run_weekly] Fetching StockTwits sentiment...")
    sentiment_signals = {}
    try:
        sentiment_signals = get_sentiment_batch(all_candidates)
    except Exception as exc:
        print(f"[run_weekly] StockTwits fetch failed: {exc}")

    # 5. Congress signals
    print("[run_weekly] Fetching Congress signals...")
    congress_signals = {}
    try:
        congress_signals = get_congress_signals(days_back=14)
    except Exception as exc:
        print(f"[run_weekly] Congress fetch failed: {exc}")

    # 5b. Insider (SEC Form 4) signals — fresher signal than Congress, 2-day lag
    print("[run_weekly] Fetching insider trading signals...")
    insider_signals = {}
    try:
        insider_signals = get_insider_signals_batch(all_candidates, days_back=14)
    except Exception as exc:
        print(f"[run_weekly] Insider fetch failed (non-critical): {exc}")

    # 6. Fetch fundamentals for all candidates
    print("[run_weekly] Fetching fundamentals (this may take several minutes)...")
    fundamentals_map = fetch_multiple(all_candidates, delay_seconds=1.5)
    # Keep the pre-filter map: fetch_error entries are dropped below, but they are
    # exactly what ticker_health needs in order to retire dead tickers.
    raw_fundamentals = dict(fundamentals_map)
    gem_set = set(gem_candidates)

    # Hype needs the market cap, which only arrives with the fundamentals
    for ticker, signal in sentiment_signals.items():
        apply_market_cap(signal, (fundamentals_map.get(ticker) or {}).get("market_cap"))

    # 6b. Hard exclude structurally broken stocks (portfolio positions bypass).
    # Excluded and low-score stocks are still logged — without AI cost — so the
    # learning report can check whether the filters throw away good stocks.
    print("[run_weekly] Applying hard exclusion filter...")
    shadow_rows: list[dict] = []
    revisit_excluded: dict[str, str] = {}
    filtered_fundamentals = {}
    for ticker, fund in fundamentals_map.items():
        if fund.get("fetch_error"):
            print(f"[run_weekly] Skipping {ticker} — fetch error: {fund['fetch_error']}")
            if ticker in revisit_origins:
                revisit_excluded[ticker] = "nema podataka s Yahooa"
            continue
        if ticker in portfolio_tickers:
            filtered_fundamentals[ticker] = fund  # always keep portfolio positions
            continue
        cat = "speculative_growth" if ticker in gem_set else classify_category(fund)
        should_ex, reason = hard_exclude(fund, cat)
        if should_ex:
            print(f"[run_weekly] Hard exclude {ticker}: {reason}")
            if ticker in revisit_origins:
                revisit_excluded[ticker] = reason  # an earlier idea now fails the hard filter: worth saying
            shadow_rows.append({
                "symbol": ticker, "action": "EXCLUDED", "reason": reason, "fundamentals": fund,
                "features": build_features(fund, score_stock(fund, cat, weights), cat, sentiment_signals.get(ticker),
                                           is_gem=ticker in gem_set, filter_reason=reason),
            })
        else:
            filtered_fundamentals[ticker] = fund
    fundamentals_map = filtered_fundamentals
    print(f"[run_weekly] {len(fundamentals_map)} stocks passed hard exclusion filter")

    # 6c. Live prices, P&L, weights and total capital (positions + cash)
    usd_to_eur = usd_to_eur_now()
    positions_value_usd = 0.0
    for p in positions:
        price = _num(fundamentals_map.get(p["symbol"], {}).get("current_price"))
        if not price:
            continue
        p["current_price"] = f"${price:.2f}"
        p["value_usd"] = p["shares"] * price
        positions_value_usd += p["value_usd"]
        if p["avg_cost_usd"] > 0:
            p["pnl_pct"] = f"{(price / p['avg_cost_usd'] - 1) * 100:+.1f}%"
    capital_usd = positions_value_usd + cash["cash_usd"] + (cash["cash_eur"] / usd_to_eur if usd_to_eur else 0)
    for p in positions:
        if p.get("value_usd") and capital_usd > 0:
            p["weight"] = p["value_usd"] / capital_usd
    capital_eur = capital_usd * usd_to_eur
    portfolio_value_eur = round(capital_eur) if capital_eur > 0 else None
    if portfolio_value_eur:
        weights_txt = ", ".join(f"{p['symbol']} {p['weight'] * 100:.0f}%" for p in positions if p.get("weight") is not None)
        print(f"[run_weekly] Capital: positions ${positions_value_usd:,.0f} + cash → ~€{portfolio_value_eur:,} "
              f"(USD→EUR {usd_to_eur:.4f}) | weights: {weights_txt}")
    portfolio_context = build_portfolio_context(positions, cash, portfolio_value_eur)

    # 6f. Build portfolio sector map for concentration check
    portfolio_sectors: dict[str, int] = {}
    for t in portfolio_tickers:
        sector = fundamentals_map.get(t, {}).get("sector", "")
        if sector and sector not in ("Unknown", ""):
            portfolio_sectors[sector] = portfolio_sectors.get(sector, 0) + 1
    print(f"[run_weekly] Portfolio sectors: {portfolio_sectors}")

    # 7. Score each stock. Gems use the speculative_growth category; main uses auto-classify
    print("[run_weekly] Scoring stocks...")
    scored = {}
    for ticker, fund in fundamentals_map.items():
        if ticker in gem_set:
            price = _num(fund.get("current_price"))
            if price is not None and price >= GEM_PRICE_CAP:
                print(f"[run_weekly] Gem {ticker} price ${price:.2f} ≥ ${GEM_PRICE_CAP} — skipping")
                continue
        category = "speculative_growth" if ticker in gem_set else classify_category(fund)
        scored[ticker] = {
            "fundamentals": fund,
            "score": score_stock(fund, category, weights),
            "category": category,
            "is_gem": ticker in gem_set,
        }

    # 8. Prepare the AI analyses. Main stocks below the score floor (or with too little
    # data to score) are skipped unless held; gems always go to the AI.
    run_context = build_run_context(date_str, macro_text, lessons_text, portfolio_context, portfolio_value_eur, params)
    use_batch = not sync
    # 5-minute TTL for the batch too: see prewarm_cache for the measured hit rates
    system = build_system(investor_profile, run_context, cache_ttl="5m")

    jobs: dict[str, dict] = {}
    for ticker, data in scored.items():
        total_score = data["score"].get("total_score", 0)
        is_gem = data["is_gem"]
        in_portfolio = ticker in positions_by_symbol
        fund = data["fundamentals"]
        low_data = data["score"].get("verdict") == "INSUFFICIENT_DATA"
        origin = revisit_origins.get(ticker)

        # Revisits skip the score floor like positions do: the idea already passed it once
        if not is_gem and not in_portfolio and not origin and (total_score < params["min_ai_score"] or low_data):
            reason = (f"premalo podataka ({data['score'].get('coverage', 0) * 100:.0f}% pokrivenost)" if low_data
                      else f"score {total_score} < {params['min_ai_score']}")
            print(f"[run_weekly] Skipping {ticker} ({reason}, not in portfolio)")
            shadow_rows.append({
                "symbol": ticker, "action": "SKIPPED_SCORE", "reason": reason, "fundamentals": fund,
                "features": build_features(fund, data["score"], data["category"], sentiment_signals.get(ticker),
                                           is_gem=False, filter_reason=reason),
            })
            continue

        meta = positions_meta.get(ticker, {})
        watchlist_context = None
        wl = active_watchlist.get(ticker)
        if wl:
            watchlist_context = (
                f"Dionica je bila preporučena {(wl.get('suggested_at') or '')[:10]} "
                f"| Akcija: {wl.get('action', 'N/A')} "
                f"| Buy zone: {wl.get('buy_zone', 'N/A')} "
                f"| Confidence: {wl.get('confidence', 'N/A')}/10"
            )

        # Sector concentration note (only for main universe, not gems)
        sector_note = None
        if not is_gem:
            fund_sector = fund.get("sector", "")
            count = portfolio_sectors.get(fund_sector, 0)
            if count >= 2:
                sector_note = f"Portfelj već ima {count} pozicije u {fund_sector} sektoru — preporuči manji position size ili WAIT ako nema iznimnog razloga."
            elif count == 1 and not in_portfolio:
                sector_note = f"Portfelj već ima 1 poziciju u {fund_sector} sektoru — napomeni diversifikacijski rizik."

        position_line = None
        position = positions_by_symbol.get(ticker)
        if position:
            weight_txt = (f" | udio u ukupnom kapitalu {position['weight'] * 100:.0f}% "
                          f"(limit {params['concentration_cap'] * 100:.0f}%)" if position.get("weight") is not None else "")
            position_line = (
                f"Pozicija: {position['shares']:g} dionica @ prosječno {position['avg_cost']} | "
                f"sada {position['current_price']} | P&L {position['pnl_pct']}{weight_txt}"
            )

        checklist_items = build_checklist(fund, data["category"])
        prompt_kwargs = dict(
            fundamentals=fund,
            score_result=data["score"],
            congress_signal=congress_signals.get(ticker),
            insider_signal=insider_signals.get(ticker),
            personal_thesis=meta.get("personal_thesis"),
            macro_view=meta.get("macro_view"),
            do_not_sell_until=meta.get("do_not_sell_until"),
            sell_triggers=meta.get("sell_triggers"),
            is_hidden_gem=is_gem,
            sentiment_signal=sentiment_signals.get(ticker),
            watchlist_context=watchlist_context,
            sector_note=sector_note,
            in_portfolio=in_portfolio,
            position_line=position_line,
            previous_calls=build_previous_calls_block(history.get(ticker), fund),
            checklist_text=format_checklist(checklist_items),
            cycle_context=format_context(fund),
            revisit_context=revisit_prompt_block(origin, _num(fund.get("current_price"))) if origin else None,
        )
        last_call = (history.get(ticker) or [None])[0]
        ctx = {
            "fundamentals": fund,
            "score_result": data["score"],
            "in_portfolio": in_portfolio,
            "is_hidden_gem": is_gem,
            "sentiment_signal": sentiment_signals.get(ticker),
            "insider_signal": insider_signals.get(ticker),
            "congress_signal": congress_signals.get(ticker),
            "position_weight": position.get("weight") if position else None,
            "params": params,
            # The zone a lowered zone is compared with (finalize flags drift against it)
            "reference_zone": origin["buy_zone"] if origin else (last_call or {}).get("buy_zone"),
        }
        jobs[ticker] = {"prompt_kwargs": prompt_kwargs, "ctx": ctx, "checklist": checklist_items, "data": data,
                        "model": REVIEW_MODEL if origin else MODEL, "origin": origin}

    # 9. AI analysis: one batch at half price, then synchronous retries for anything missing
    revisit_jobs = [t for t, job in jobs.items() if job["origin"]]
    print(f"[run_weekly] Running AI analysis for {len(jobs)} stocks"
          + (f" ({len(revisit_jobs)} revisits on {REVIEW_MODEL})" if revisit_jobs else "") + "...")
    batch_messages = {}
    ab_tickers: list[str] = []
    if use_batch and jobs:
        requests = {
            ticker: request_params(system, build_stock_prompt(**job["prompt_kwargs"]), model=job["model"])
            for ticker, job in jobs.items()
        }
        # A/B sample: the review model on a few random stocks, for the learning report only
        ab_n = int(params["ab_sample_per_run"]) if limit is None else min(2, int(params["ab_sample_per_run"]))
        spent_before = month_spend(db_client)
        if spent_before is not None and spent_before >= params["monthly_budget_usd"]:
            print(f"[run_weekly] A/B sample skipped: month spend ${spent_before:.2f} ≥ budget")
            ab_n = 0
        ab_tickers = choose_ab_sample(jobs, revisit_origins, ab_n)
        for ticker in ab_tickers:
            requests[f"{ticker}{AB_SUFFIX}"] = request_params(
                system, build_stock_prompt(**jobs[ticker]["prompt_kwargs"]), model=REVIEW_MODEL)
        if ab_tickers:
            print(f"[run_weekly] A/B sample on {REVIEW_MODEL}: {ab_tickers}")
        for model in sorted({r["model"] for r in requests.values()}):
            prewarm_cache(claude, system, usage, model=model)
        batch_messages = run_batch(claude, requests, usage=usage)

    recommendations = []
    for ticker, job in jobs.items():
        model = job["model"]
        try:
            if ticker in batch_messages:
                result, raw = parse_message(batch_messages[ticker])
                rec = finalize(result, job["ctx"], model, raw)
            else:
                rec = analyze_stock(claude, system, job["prompt_kwargs"], job["ctx"], model=model, usage=usage)

            if rec.get("error") and model != REVIEW_MODEL:
                # Haiku 5.5 has no server-side refusal fallback: a declined, cut-off or
                # unreadable reply gets one retry on the review model
                print(f"[run_weekly] {ticker}: {rec['error'][:120]} — retrying on {REVIEW_MODEL}")
                retry = analyze_stock(claude, system, job["prompt_kwargs"], job["ctx"], model=REVIEW_MODEL, usage=usage)
                if not retry.get("error"):
                    rec = retry

            if job["ctx"]["in_portfolio"] and rec.get("action") in BEARISH_ACTIONS:
                rec = apply_sell_guard(rec, claude, system, job["prompt_kwargs"], job["ctx"], usage)

            if job["origin"]:
                rec["revisit"] = job["origin"]
                rec["revisit_line"] = origin_line(
                    job["origin"], _num(job["data"]["fundamentals"].get("current_price")),
                    spy_change_since(spy_book, job["origin"]["date"], today))

            rec["checklist_fails"] = [text for status, text in job["checklist"] if status == "✗"]
            rec["checklist_summary"] = checklist_summary(job["checklist"])
            rec.setdefault("evidence_table", {})["checklist"] = rec["checklist_summary"]
            recommendations.append(rec)

            if rec.get("error"):
                print(f"[run_weekly] {ticker}: {rec['error']}")
        except Exception as exc:
            print(f"[run_weekly] AI analysis failed for {ticker}: {exc}")

    ab_recs = []
    for ticker in ab_tickers:
        message = batch_messages.get(f"{ticker}{AB_SUFFIX}")
        if message is None:
            continue
        result, raw = parse_message(message)
        ab_rec = finalize(result, jobs[ticker]["ctx"], REVIEW_MODEL, raw)
        if not ab_rec.get("error"):
            ab_recs.append(ab_rec)

    # 9b. Second pass on BUY candidates, then on zones lowered while the price sat at
    # the old one, while the month's AI spend is under budget. Calls the review model
    # already made (revisits, retries) are not sent to it again.
    routine = [r for r in recommendations if r.get("model") != REVIEW_MODEL and not r.get("error")]
    buy_candidates = sorted(
        (r for r in routine if r.get("action") in BULLISH_ACTIONS
         and (r.get("confidence") or 0) >= params["second_pass_min_confidence"]),
        key=lambda r: -(r.get("confidence") or 0),
    )
    drift_candidates = [r for r in routine if r.get("zone_drift") and r not in buy_candidates]
    candidates = (buy_candidates + drift_candidates)[: int(params["second_pass_max_per_run"])]
    if candidates:
        spent = month_spend(db_client)
        projected = (spent or 0.0) + usage.cost
        if spent is not None and projected >= params["monthly_budget_usd"]:
            print(f"[run_weekly] Second pass skipped: month spend ${projected:.2f} ≥ budget ${params['monthly_budget_usd']:.2f}")
        else:
            print(f"[run_weekly] Second pass by {REVIEW_MODEL} for {[r['ticker'] for r in candidates]}")
            for rec in candidates:
                job = jobs[rec["ticker"]]
                reviewed = second_pass(rec, claude, system, job["prompt_kwargs"], job["ctx"], usage)
                if reviewed is not rec:
                    for key in ("checklist_fails", "checklist_summary"):
                        reviewed[key] = rec.get(key)
                    reviewed.setdefault("evidence_table", {})["checklist"] = rec.get("checklist_summary")
                    recommendations[recommendations.index(rec)] = reviewed

    for rec in recommendations:
        gem_label = " 💎" if rec.get("is_hidden_gem") else ""
        held_label = " 📌" if rec.get("in_portfolio") else ""
        revisit_label = " 🔁" if rec.get("revisit") else ""
        drift_label = " ⚠️zona" if rec.get("zone_drift") else ""
        print(f"[run_weekly] {rec.get('ticker')}{gem_label}{held_label}{revisit_label}: {rec.get('action')} "
              f"(confidence {rec.get('confidence')}, {rec.get('model')}){drift_label}")
    for rec in ab_recs:
        main_rec = next((r for r in recommendations if r.get("ticker") == rec.get("ticker")), {})
        print(f"[run_weekly] A/B {rec.get('ticker')}: {REVIEW_MODEL} {rec.get('action')} ({rec.get('confidence')}) "
              f"vs {main_rec.get('model')} {main_rec.get('action')} ({main_rec.get('confidence')})")

    # 9c. What production actually observed (so the universe self-cleans) and every
    # analysis with its features (memory for the next run, data for the learning report)
    def features_for(rec: dict, job: dict, **extra) -> dict:
        guards = [name for name, key in (("sell_guard", "sell_guard_note"), ("concentration", "concentration_note"),
                                         ("hype", "hype_note"), ("zone_drift", "zone_drift_note")) if rec.get(key)]
        return build_features(
            job["data"]["fundamentals"], job["data"]["score"], job["data"]["category"],
            job["ctx"]["sentiment_signal"], job["checklist"],
            is_gem=job["data"]["is_gem"], in_portfolio=job["ctx"]["in_portfolio"],
            position_weight=job["ctx"]["position_weight"], prompt_version=PROMPT_VERSION,
            model_category=rec.get("category"), entry_plan=rec.get("entry_plan"),
            first_pass=rec.get("first_pass"), guards=guards or None,
            revisit_trigger=(job["origin"] or {}).get("trigger"),
            zone_drift=True if rec.get("zone_drift") else None,
            ref_zone_reached=True if rec.get("reference_zone_reached") else None,
            **extra,
        )

    features_by_ticker = {rec["ticker"]: features_for(rec, jobs[rec["ticker"]])
                          for rec in recommendations if rec.get("ticker") in jobs}
    ab_features = {rec["ticker"]: features_for(rec, jobs[rec["ticker"]], ab_shadow=True, ab_pair_model=MODEL)
                   for rec in ab_recs}
    if write_client:
        actioned = [
            r.get("ticker") for r in recommendations
            if r.get("action") not in ("NO_ACTION", "WAIT", "HOLD", None)
        ]
        record_run(write_client, raw_fundamentals, actioned_symbols=actioned, health=health)
        record_analyses(write_client, recommendations, fundamentals_map, features_by_ticker, shadow_rows)
        if ab_recs:
            record_analyses(write_client, ab_recs, fundamentals_map, ab_features)
    else:
        print(f"[run_weekly] DRY RUN — would log {len(recommendations)} analyses + {len(ab_recs)} A/B "
              f"+ {len(shadow_rows)} rows without AI")

    # 9d. Everything below works on the published subset. Weak calls stay in
    # analysis_log (so the next run still remembers them) but never reach the email,
    # the watchlist or the decision log. A revisit is published from its own floor;
    # below it the email still names it, because "this idea no longer holds" is news.
    published, dropped, revisits_below = [], [], []
    for rec in recommendations:
        if rec.get("revisit"):
            (published if (rec.get("confidence") or 0) >= params["revisit_min_confidence"]
             else revisits_below).append(rec)
        elif newsletter_worthy(rec, params["min_email_confidence"]):
            published.append(rec)
        else:
            dropped.append(rec)
    revisits_below += [{"ticker": t, "excluded_reason": reason} for t, reason in revisit_excluded.items()]
    if dropped:
        dropped_txt = ", ".join(f"{r.get('ticker')} ({r.get('confidence')})" for r in dropped)
        print(f"[run_weekly] Below confidence {params['min_email_confidence']}, not in newsletter: {dropped_txt}")

    # 10. Newsletter summary. Its schema differs from the analyses', so it cannot read
    # their cache entry, and nothing would read its own — no caching for this call.
    print("[run_weekly] Generating newsletter summary...")
    summary_system = build_system(investor_profile, run_context, cache_ttl=None)
    summary = generate_weekly_summary(claude, summary_system, published, date_str, usage=usage)

    # 11. Build and send email
    suffix = summary.get("email_subject_suffix", "")
    subject = f"[Dionice] {day_name[:3]} {date_str} | {suffix}"
    cash_line = (
        f"Gotovina: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}"
        if cash["cash_usd"] or cash["cash_eur"] else None
    )
    month_before = month_spend(db_client)
    health_line = build_health_line(
        gem_pool_size(), benchmark_coverage(),
        (month_before + usage.cost) if month_before is not None else None, params["monthly_budget_usd"],
        failed=sum(1 for r in recommendations if r.get("error")), dead=len(dead_tickers),
        next_revisit=next_revisit if db_client else None,
    )
    print(f"[run_weekly] {health_line}")

    html = build_html_email(
        summary=summary,
        recommendations=published,
        portfolio_value=None,
        portfolio_positions=positions,
        email_type="WEEKLY",
        cash_line=cash_line,
        concentration_cap=params["concentration_cap"],
        revisits_below=revisits_below,
        revisit_min_confidence=int(params["revisit_min_confidence"]),
        revisit_days=int(params["revisit_days"]),
        health_line=health_line,
    )

    print(usage.summary())
    size = len(html.encode("utf-8"))
    print(f"[run_weekly] Email size {size / 1024:.0f} KB"
          + (" — WARNING: Gmail clips messages above ~102 KB" if size > GMAIL_CLIP_BYTES else ""))
    if dry_run:
        path = os.path.join(tempfile.gettempdir(), f"dionice_dry_run_{date_str}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[run_weekly] DRY RUN — email not sent. Subject: {subject}")
        print(f"[run_weekly] DRY RUN — HTML saved to {path}")
    else:
        print(f"[run_weekly] Sending email: {subject}")
        if not send_email(subject, html):
            print("[run_weekly] Email failed to send!")
            usage.save(write_client)
            sys.exit(1)

    # 12. Save to Supabase
    if write_client:
        print("[run_weekly] Saving to Supabase...")
        expire_stale_watchlist(write_client, recommendations, published, history, today,
                               int(params["watchlist_max_age_days"]))
        save_recommendations_to_supabase(write_client, published, fundamentals_map, has_v8)
        save_newsletter_to_supabase(write_client, subject, summary, "WEEKLY")
        usage.save(write_client)

    gems_analyzed = sum(1 for r in recommendations if r.get("is_hidden_gem"))
    revisits_shown = sum(1 for r in published if r.get("revisit"))
    print(f"[run_weekly] Done. {len(recommendations)} stocks analyzed ({gems_analyzed} hidden gems, "
          f"{len(revisit_jobs)} revisits of which {revisits_shown} shown, {len(ab_recs)} A/B), "
          f"{len(published)} in the newsletter, {len(dropped)} below confidence {params['min_email_confidence']}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dionice weekly newsletter")
    parser.add_argument("--dry-run", action="store_true", help="no email, no database writes")
    parser.add_argument("--limit", type=int, default=None, help="analyse only the portfolio plus N candidates")
    parser.add_argument("--sync", action="store_true", help="plain API calls instead of a Message Batch")
    revisit_flag = parser.add_mutually_exclusive_group()
    revisit_flag.add_argument("--revisit", dest="revisit", action="store_true", default=None,
                              help="run the revisit today, whatever the day or the cycle")
    revisit_flag.add_argument("--no-revisit", dest="revisit", action="store_false",
                              help="skip the revisit even when it is due")
    args = parser.parse_args()
    main(dry_run=args.dry_run, limit=args.limit, sync=args.sync, revisit=args.revisit)
