"""
Weekly newsletter entry point — runs every Tuesday and Thursday.
Orchestrates: discovery → fundamentals → scoring → checklist and cycle context →
AI analysis (with memory of its own previous calls) → sell guard → email.

Candidate pipeline:
  Main universe: random sector-rotating sample (changes every run)
  Hidden gems:   random sample from the small-cap pool
  Portfolio:     always analyzed regardless of score

Usage: python scripts/run_weekly.py [--dry-run] [--limit N]
  --dry-run  no email and no database writes; the HTML goes to a temp file
  --limit N  analyse only the portfolio plus N universe candidates and one gem
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.congress_tracker import get_congress_signals, get_tickers_from_congress
from analysis.insider_tracker import get_insider_signals_batch
from analysis.fundamentals import fetch_multiple
from analysis.scorer import score_stock, classify_category, hard_exclude
from analysis.ai_analyst import (
    BEARISH_ACTIONS,
    REVIEW_MODEL,
    analyze_stock,
    generate_weekly_summary,
)
from analysis.analysis_history import build_previous_calls_block, load_recent_history, record_analyses
from analysis.checklist import build_checklist, checklist_summary, deterioration_signals, format_checklist
from analysis.email_sender import build_html_email, send_email
from analysis.investor_profile import load_investor_profile
from analysis.portfolio import compute_holdings, usd_to_eur_now
from analysis.sector_context import format_context
from analysis.stock_discovery import select_candidates
from analysis.sentiment_tracker import get_sentiment_batch
from analysis.macro_context import fetch_macro_context, format_macro_for_prompt
from analysis.supabase_client import get_supabase
from analysis.ticker_health import get_dead_tickers, load_health, record_run

GEM_PRICE_CAP = 12.0  # hidden gems must be under this price to pass through
DECISION_REPEAT_DAYS = 30
DECISION_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP", "WATCHLIST", "HOLD", "SELL", "REDUCE"}
# The watchlist tracks buy zones; HOLD/SELL/REDUCE concern positions already owned
WATCHLIST_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP", "WATCHLIST"}


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def get_latest_lessons(client) -> str | None:
    """Fetches the most recent quarterly self-review lessons text, if any."""
    try:
        result = client.table("model_lessons").select("*").order("generated_at", desc=True).limit(1).execute()
        rows = result.data or []
        return rows[0]["lessons_text"] if rows else None
    except Exception as exc:
        print(f"[run_weekly] Could not load model_lessons: {exc}")
        return None


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
        })
    return positions


def build_portfolio_context(positions: list[dict], cash: dict[str, float], portfolio_value_eur: float | None) -> str:
    parts = [
        f"{p['symbol']}: {p['shares']:g} dionica @ prosječno {p['avg_cost']} "
        f"(sada {p['current_price']}, P&L {p['pnl_pct']})"
        for p in positions
    ]
    context = ("Pozicije: " + "; ".join(parts)) if parts else "Nema otvorenih pozicija."
    if cash["cash_usd"] or cash["cash_eur"]:
        context += f" | Gotovina spremna za ulaganje: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}"
    if portfolio_value_eur:
        context += f" | Ukupni kapital (pozicije + gotovina) ≈ €{portfolio_value_eur:,.0f}"
    return context


def apply_sell_guard(rec: dict, analysis_kwargs: dict, fund: dict) -> dict:
    """
    A SELL/REDUCE on a held position must rest on a deterioration in the business,
    not on price — the investor's own rule, and the gap behind the GNRC REDUCE of
    2026-09-10. With no deterioration in the data the call becomes HOLD; with some,
    REVIEW_MODEL re-runs the analysis and its verdict stands.
    """
    original = rec.get("action")
    ticker = rec.get("ticker")
    signals = deterioration_signals(fund)

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
        review = analyze_stock(**analysis_kwargs, model=REVIEW_MODEL, review_note=note)
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
    return review


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


def save_recommendations_to_supabase(client, recommendations: list[dict]) -> None:
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
                        "category": rec.get("category", ""),
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

        try:
            price_val = _num(str(rec.get("evidence_table", {}).get("current_price", "")).replace("$", "").replace(",", ""))
            client.table("decisions").insert({
                "recommended_at": now,
                "symbol": ticker,
                "agent_action": action,
                "agent_buy_zone": rec.get("buy_zone"),
                "agent_confidence": rec.get("confidence"),
                "agent_thesis": rec.get("investment_thesis", ""),
                "user_action": "PENDING",
                "price_at_recommendation": price_val,
                "outcome_30d": "pending",
                "outcome_90d": "pending",
                "outcome_180d": "pending",
            }).execute()
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


def main(dry_run: bool = False, limit: int | None = None):
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    print(f"[run_weekly] Starting analysis for {date_str} ({day_name}){' — DRY RUN' if dry_run else ''}")

    db_client = get_supabase()
    write_client = None if dry_run else db_client

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
        print("[run_weekly] Loaded quarterly self-review lessons")

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
    all_candidates = list(dict.fromkeys(main_candidates + gem_candidates))
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

    # 6b. Hard exclude structurally broken stocks (portfolio positions bypass)
    print("[run_weekly] Applying hard exclusion filter...")
    filtered_fundamentals = {}
    for ticker, fund in fundamentals_map.items():
        if fund.get("fetch_error"):
            print(f"[run_weekly] Skipping {ticker} — fetch error: {fund['fetch_error']}")
            continue
        if ticker in portfolio_tickers:
            filtered_fundamentals[ticker] = fund  # always keep portfolio positions
            continue
        cat = "speculative_growth" if ticker in set(gem_candidates) else classify_category(fund)
        should_ex, reason = hard_exclude(fund, cat)
        if should_ex:
            print(f"[run_weekly] Hard exclude {ticker}: {reason}")
        else:
            filtered_fundamentals[ticker] = fund
    fundamentals_map = filtered_fundamentals
    print(f"[run_weekly] {len(fundamentals_map)} stocks passed hard exclusion filter")

    # 6c. Live prices, P&L and total capital (positions + cash) in EUR at today's rate
    usd_to_eur = usd_to_eur_now()
    positions_value_usd = 0.0
    for p in positions:
        price = _num(fundamentals_map.get(p["symbol"], {}).get("current_price"))
        if not price:
            continue
        p["current_price"] = f"${price:.2f}"
        positions_value_usd += p["shares"] * price
        if p["avg_cost_usd"] > 0:
            p["pnl_pct"] = f"{(price / p['avg_cost_usd'] - 1) * 100:+.1f}%"
    capital_eur = (positions_value_usd + cash["cash_usd"]) * usd_to_eur + cash["cash_eur"]
    portfolio_value_eur = round(capital_eur) if capital_eur > 0 else None
    if portfolio_value_eur:
        print(f"[run_weekly] Capital: positions ${positions_value_usd:,.0f} + cash → ~€{portfolio_value_eur:,} (USD→EUR {usd_to_eur:.4f})")
    portfolio_context = build_portfolio_context(positions, cash, portfolio_value_eur)

    # 6f. Build portfolio sector map for concentration check
    portfolio_sectors: dict[str, int] = {}
    for t in portfolio_tickers:
        sector = fundamentals_map.get(t, {}).get("sector", "")
        if sector and sector not in ("Unknown", ""):
            portfolio_sectors[sector] = portfolio_sectors.get(sector, 0) + 1
    print(f"[run_weekly] Portfolio sectors: {portfolio_sectors}")

    # 7. Score each stock
    # Gems use speculative_growth category; main uses auto-classify
    print("[run_weekly] Scoring stocks...")
    scored = {}
    gem_set = set(gem_candidates)

    for ticker, fund in fundamentals_map.items():
        # Hidden gem price check — drop if above cap
        if ticker in gem_set:
            try:
                price = float(fund.get("current_price") or 0)
                if price >= GEM_PRICE_CAP:
                    print(f"[run_weekly] Gem {ticker} price ${price:.2f} ≥ ${GEM_PRICE_CAP} — skipping")
                    continue
            except (TypeError, ValueError):
                pass  # price unknown — let it through

        category = "speculative_growth" if ticker in gem_set else classify_category(fund)
        scored[ticker] = {
            "fundamentals": fund,
            "score": score_stock(fund, category),
            "category": category,
            "is_gem": ticker in gem_set,
        }

    # 8. AI analysis
    # Main stocks: skip score < 40 unless in portfolio
    # Gems: always send to AI (they're pre-selected as candidates)
    print("[run_weekly] Running AI analysis...")
    recommendations = []
    for ticker, data in scored.items():
        total_score = data["score"].get("total_score", 0)
        is_gem = data["is_gem"]
        in_portfolio = ticker in positions_by_symbol

        if not is_gem and total_score < 40 and not in_portfolio:
            print(f"[run_weekly] Skipping {ticker} (score {total_score} < 40, not in portfolio)")
            continue

        try:
            fund = data["fundamentals"]
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
                position_line = (
                    f"Pozicija: {position['shares']:g} dionica @ prosječno {position['avg_cost']} | "
                    f"sada {position['current_price']} | P&L {position['pnl_pct']}"
                )

            checklist_items = build_checklist(fund, data["category"])
            analysis_kwargs = dict(
                fundamentals=fund,
                score_result=data["score"],
                congress_signal=congress_signals.get(ticker),
                insider_signal=insider_signals.get(ticker),
                portfolio_context=portfolio_context,
                current_date=date_str,
                personal_thesis=meta.get("personal_thesis"),
                macro_view=meta.get("macro_view"),
                do_not_sell_until=meta.get("do_not_sell_until"),
                sell_triggers=meta.get("sell_triggers"),
                is_hidden_gem=is_gem,
                sentiment_signal=sentiment_signals.get(ticker),
                watchlist_context=watchlist_context,
                sector_note=sector_note,
                macro_context=macro_text,
                lessons_context=lessons_text,
                portfolio_value_eur=portfolio_value_eur,
                in_portfolio=in_portfolio,
                position_line=position_line,
                previous_calls=build_previous_calls_block(history.get(ticker), fund),
                checklist_text=format_checklist(checklist_items),
                cycle_context=format_context(fund),
                investor_profile=investor_profile,
            )
            rec = analyze_stock(**analysis_kwargs)
            if in_portfolio and rec.get("action") in BEARISH_ACTIONS:
                rec = apply_sell_guard(rec, analysis_kwargs, fund)

            rec["checklist_fails"] = [text for status, text in checklist_items if status == "✗"]
            rec["checklist_summary"] = checklist_summary(checklist_items)
            rec.setdefault("evidence_table", {})["checklist"] = rec["checklist_summary"]
            recommendations.append(rec)

            gem_label = " 💎" if is_gem else ""
            held_label = " 📌" if in_portfolio else ""
            print(f"[run_weekly] {ticker}{gem_label}{held_label}: {rec.get('action')} (confidence {rec.get('confidence')}, {rec.get('model')})")
        except Exception as exc:
            print(f"[run_weekly] AI analysis failed for {ticker}: {exc}")

    # 8b. Record what production actually observed, so the universe self-cleans,
    # and log every analysis so the next run can see its own previous calls.
    if write_client:
        actioned = [
            r.get("ticker") for r in recommendations
            if r.get("action") not in ("NO_ACTION", "WAIT", "HOLD", None)
        ]
        record_run(write_client, raw_fundamentals, actioned_symbols=actioned, health=health)
        record_analyses(write_client, recommendations, fundamentals_map)

    # 9. Newsletter summary
    print("[run_weekly] Generating newsletter summary...")
    summary = generate_weekly_summary(recommendations, portfolio_context, date_str)

    # 10. Build and send email
    suffix = summary.get("email_subject_suffix", "")
    subject = f"[Dionice] {day_name[:3]} {date_str} | {suffix}"
    cash_line = (
        f"Gotovina: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}"
        if cash["cash_usd"] or cash["cash_eur"] else None
    )

    html = build_html_email(
        summary=summary,
        recommendations=recommendations,
        portfolio_value=None,
        portfolio_positions=positions,
        email_type="WEEKLY",
        cash_line=cash_line,
    )

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
            sys.exit(1)

    # 11. Save to Supabase
    if write_client:
        print("[run_weekly] Saving to Supabase...")
        save_recommendations_to_supabase(write_client, recommendations)
        save_newsletter_to_supabase(write_client, subject, summary, "WEEKLY")

    gems_analyzed = sum(1 for r in recommendations if r.get("is_hidden_gem"))
    print(f"[run_weekly] Done. {len(recommendations)} stocks analyzed ({gems_analyzed} hidden gems).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dionice weekly newsletter")
    parser.add_argument("--dry-run", action="store_true", help="no email, no database writes")
    parser.add_argument("--limit", type=int, default=None, help="analyse only the portfolio plus N candidates")
    args = parser.parse_args()
    main(dry_run=args.dry_run, limit=args.limit)
