"""
Weekly newsletter entry point — runs every Tuesday and Thursday at 15:00 CET.
Orchestrates: discovery → fundamentals → scoring → AI analysis → email.

Candidate pipeline:
  Main universe: seeded-random sample from ~700 stocks (changes every run)
  Hidden gems:   seeded-random sample from ~60 small-cap growth plays under $10
  Portfolio:     always analyzed regardless of score
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.congress_tracker import get_congress_signals, get_tickers_from_congress
from analysis.reddit_tracker import get_tickers_from_reddit, scrape_reddit
from analysis.fundamentals import fetch_multiple
from analysis.scorer import score_stock, classify_category, hard_exclude
from analysis.ai_analyst import analyze_stock, generate_weekly_summary
from analysis.email_sender import build_html_email, send_email
from analysis.stock_discovery import select_candidates
from analysis.sentiment_tracker import get_sentiment_batch
from analysis.macro_context import fetch_macro_context, format_macro_for_prompt
from analysis.supabase_client import get_supabase

GEM_PRICE_CAP = 12.0  # hidden gems must be under this price to pass through


def get_active_watchlist(client) -> dict[str, dict]:
    """Fetches active watchlist entries keyed by symbol for cross-checking."""
    try:
        result = client.table("watchlist").select("*").eq("status", "ACTIVE").execute()
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


def get_portfolio_from_supabase() -> tuple[list[dict], str]:
    """
    Fetches portfolio positions from Supabase transactions table.
    Returns (positions_list, portfolio_context_string).
    """
    client = get_supabase()
    if not client:
        return [], "Portfolio data unavailable (Supabase credentials missing)."

    try:
        result = client.table("transactions").select("*").order("trade_date").execute()
        rows = result.data or []

        holdings: dict[str, dict] = {}
        for row in rows:
            sym = row["symbol"]
            if sym not in holdings:
                holdings[sym] = {
                    "symbol": sym,
                    "company_name": row.get("company_name", sym),
                    "shares": 0,
                    "total_cost": 0.0,
                    "currency": row.get("currency", "EUR"),
                }
            shares = float(row.get("shares", 0))
            price = float(row.get("price_per_share", 0))
            if row["action"] == "BUY":
                holdings[sym]["shares"] += shares
                holdings[sym]["total_cost"] += shares * price
            elif row["action"] == "SELL":
                holdings[sym]["shares"] -= shares
                if holdings[sym]["shares"] > 0:
                    holdings[sym]["total_cost"] *= (
                        holdings[sym]["shares"] / (holdings[sym]["shares"] + shares)
                    )

        active = {k: v for k, v in holdings.items() if v["shares"] > 0}

        positions = []
        for sym, h in active.items():
            avg_cost = h["total_cost"] / h["shares"] if h["shares"] > 0 else 0
            positions.append({
                "symbol": sym,
                "company_name": h["company_name"],
                "shares": h["shares"],
                "avg_cost": f"${avg_cost:.2f}",
                "current_price": "N/A",
                "pnl_pct": "N/A",
            })

        context_parts = [
            f"{p['symbol']}: {p['shares']:.0f} shares @ avg {p['avg_cost']}"
            for p in positions
        ]
        portfolio_context = (
            "Current holdings: " + "; ".join(context_parts)
            if context_parts
            else "Portfolio is empty."
        )

        return positions, portfolio_context

    except Exception as exc:
        print(f"[run_weekly] Supabase error: {exc}")
        return [], "Portfolio data unavailable (fetch error)."


def save_recommendations_to_supabase(recommendations: list[dict]) -> None:
    """Saves actionable recommendations to watchlist and decisions tables."""
    client = get_supabase()
    if not client:
        return
    now = datetime.now(timezone.utc).isoformat()
    for rec in recommendations:
        action = rec.get("action", "NO_ACTION")
        if action in ("NO_ACTION", "WAIT"):
            continue

        ticker = rec.get("ticker", "?")

        try:
            watchlist_row = {
                "symbol": rec.get("ticker", ""),
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
            }
            client.table("watchlist").insert(watchlist_row).execute()
        except Exception as exc:
            print(f"[run_weekly] Watchlist insert failed for {ticker}: {exc}")

        try:
            ev = rec.get("evidence_table", {})
            price_str = ev.get("current_price", "0")
            try:
                price_val = float(str(price_str).replace("$", "").replace(",", ""))
            except (ValueError, TypeError):
                price_val = None

            decision_row = {
                "recommended_at": now,
                "symbol": rec.get("ticker", ""),
                "agent_action": action,
                "agent_buy_zone": rec.get("buy_zone"),
                "agent_confidence": rec.get("confidence"),
                "agent_thesis": rec.get("investment_thesis", ""),
                "user_action": "PENDING",
                "price_at_recommendation": price_val,
                "outcome_30d": "pending",
                "outcome_90d": "pending",
                "outcome_180d": "pending",
            }
            client.table("decisions").insert(decision_row).execute()
        except Exception as exc:
            print(f"[run_weekly] Decisions insert failed for {ticker}: {exc}")


def save_newsletter_to_supabase(subject: str, content: dict, email_type: str = "WEEKLY") -> None:
    client = get_supabase()
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


def main():
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    print(f"[run_weekly] Starting analysis for {date_str} ({day_name})")

    # 1. Portfolio
    print("[run_weekly] Fetching portfolio...")
    positions, portfolio_context = get_portfolio_from_supabase()
    portfolio_tickers = [p["symbol"] for p in positions]
    print(f"[run_weekly] Portfolio: {portfolio_tickers}")

    db_client = get_supabase()
    positions_meta = get_positions_meta(db_client) if db_client else {}
    active_watchlist = get_active_watchlist(db_client) if db_client else {}
    print(f"[run_weekly] Active watchlist: {list(active_watchlist.keys())}")

    # 1b. Macro context (yfinance, free)
    print("[run_weekly] Fetching macro context...")
    macro_data = {}
    try:
        macro_data = fetch_macro_context()
        macro_summary = ", ".join(f"{k}={v.get('current', 0):.1f}" for k, v in macro_data.items())
        print(f"[run_weekly] Macro: {macro_summary}")
    except Exception as exc:
        print(f"[run_weekly] Macro fetch failed (non-critical): {exc}")
    macro_text = format_macro_for_prompt(macro_data)

    # 2. Reddit + Congress signals (used as bonus signals, not primary discovery)
    print("[run_weekly] Discovering tickers from Reddit...")
    reddit_tickers = get_tickers_from_reddit(hours_back=48, min_mentions=2)
    print(f"[run_weekly] Reddit tickers: {reddit_tickers[:10]}")

    print("[run_weekly] Fetching Congress trades...")
    congress_tickers = get_tickers_from_congress(days_back=14, min_members=1)
    print(f"[run_weekly] Congress tickers: {congress_tickers[:10]}")

    # 3. Autonomous discovery — builds main + gem candidate lists
    print("[run_weekly] Running autonomous stock discovery...")
    main_candidates, gem_candidates = select_candidates(
        portfolio_tickers=portfolio_tickers,
        reddit_tickers=reddit_tickers,
        congress_tickers=congress_tickers,
        dt=today,
        max_main=25,
        max_gems=8,
    )
    all_candidates = list(dict.fromkeys(main_candidates + gem_candidates))
    print(f"[run_weekly] Total candidates to fetch: {all_candidates}")

    # 4. StockTwits sentiment for all candidates
    print("[run_weekly] Fetching StockTwits sentiment...")
    sentiment_signals = {}
    try:
        sentiment_signals = get_sentiment_batch(all_candidates)
    except Exception as exc:
        print(f"[run_weekly] StockTwits fetch failed: {exc}")

    # Also try Reddit (may 403 on GitHub Actions — best-effort)
    reddit_signals = {}
    try:
        reddit_signals = scrape_reddit(hours_back=48, target_tickers=all_candidates)
    except Exception as exc:
        print(f"[run_weekly] Reddit scrape failed (non-critical): {exc}")

    # 5. Congress signals
    print("[run_weekly] Fetching Congress signals...")
    congress_signals = {}
    try:
        congress_signals = get_congress_signals(days_back=14)
    except Exception as exc:
        print(f"[run_weekly] Congress fetch failed: {exc}")

    # 6. Fetch fundamentals for all candidates
    print("[run_weekly] Fetching fundamentals (this may take 2-3 minutes)...")
    fundamentals_map = fetch_multiple(all_candidates, delay_seconds=1.5)

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

    # 6c. Update portfolio positions with live prices from fundamentals
    for p in positions:
        fund = fundamentals_map.get(p["symbol"], {})
        price = fund.get("current_price")
        if price:
            try:
                p["current_price"] = f"${float(price):.2f}"
            except (TypeError, ValueError):
                pass

    # 6d. Compute portfolio value in EUR (USD positions / ~1.09 EUR/USD approximation)
    USD_TO_EUR = 0.92  # rough conversion — directionally correct
    portfolio_value_usd = 0.0
    for p in positions:
        price_str = p.get("current_price", "N/A")
        if price_str != "N/A":
            try:
                price_usd = float(str(price_str).replace("$", ""))
                portfolio_value_usd += float(p.get("shares", 0)) * price_usd
            except (ValueError, TypeError):
                pass
    portfolio_value_eur = round(portfolio_value_usd * USD_TO_EUR) if portfolio_value_usd > 0 else None
    if portfolio_value_eur:
        print(f"[run_weekly] Portfolio value: ~${portfolio_value_usd:,.0f} USD / ~€{portfolio_value_eur:,} EUR")

    # 6f. Build portfolio sector map for concentration check
    portfolio_sectors: dict[str, int] = {}
    for t in portfolio_tickers:
        fund = fundamentals_map.get(t, {})
        sector = fund.get("sector", "")
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

        if ticker in gem_set:
            category = "speculative_growth"
        else:
            category = classify_category(fund)

        score = score_stock(fund, category)
        scored[ticker] = {
            "fundamentals": fund,
            "score": score,
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
        in_portfolio = ticker in portfolio_tickers

        if not is_gem and total_score < 40 and not in_portfolio:
            print(f"[run_weekly] Skipping {ticker} (score {total_score} < 40, not in portfolio)")
            continue

        try:
            meta = positions_meta.get(ticker, {})

            # Watchlist cross-check context
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
                fund_sector = data["fundamentals"].get("sector", "")
                count = portfolio_sectors.get(fund_sector, 0)
                if count >= 2:
                    sector_note = f"Portfelj već ima {count} pozicije u {fund_sector} sektoru — preporuči manji position size ili WAIT ako nema iznimnog razloga."
                elif count == 1:
                    sector_note = f"Portfelj već ima 1 poziciju u {fund_sector} sektoru — napomeni diversifikacijski rizik."

            rec = analyze_stock(
                fundamentals=data["fundamentals"],
                score_result=data["score"],
                reddit_signal=reddit_signals.get(ticker),
                congress_signal=congress_signals.get(ticker),
                portfolio_context=portfolio_context,
                current_date=date_str,
                personal_thesis=meta.get("personal_thesis"),
                macro_view=meta.get("macro_view"),
                do_not_sell_until=meta.get("do_not_sell_until"),
                is_hidden_gem=is_gem,
                sentiment_signal=sentiment_signals.get(ticker),
                watchlist_context=watchlist_context,
                sector_note=sector_note,
                macro_context=macro_text,
                portfolio_value_eur=portfolio_value_eur,
            )
            recommendations.append(rec)
            gem_label = " 💎" if is_gem else ""
            print(f"[run_weekly] {ticker}{gem_label}: {rec.get('action')} (confidence {rec.get('confidence')})")
        except Exception as exc:
            print(f"[run_weekly] AI analysis failed for {ticker}: {exc}")

    # 9. Newsletter summary
    print("[run_weekly] Generating newsletter summary...")
    summary = generate_weekly_summary(recommendations, portfolio_context, date_str)

    # 10. Build and send email
    suffix = summary.get("email_subject_suffix", "")
    subject = f"[Dionice] {day_name[:3]} {date_str} | {suffix}"

    html = build_html_email(
        summary=summary,
        recommendations=recommendations,
        portfolio_value=None,
        portfolio_positions=positions,
        email_type="WEEKLY",
    )

    print(f"[run_weekly] Sending email: {subject}")
    success = send_email(subject, html)
    if not success:
        print("[run_weekly] Email failed to send!")
        sys.exit(1)

    # 11. Save to Supabase
    print("[run_weekly] Saving to Supabase...")
    save_recommendations_to_supabase(recommendations)
    save_newsletter_to_supabase(subject, summary, "WEEKLY")

    gems_analyzed = sum(1 for r in recommendations if r.get("is_hidden_gem"))
    print(f"[run_weekly] Done. {len(recommendations)} stocks analyzed ({gems_analyzed} hidden gems).")


if __name__ == "__main__":
    main()
