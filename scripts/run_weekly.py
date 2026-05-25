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
from analysis.scorer import score_stock, classify_category
from analysis.ai_analyst import analyze_stock, generate_weekly_summary
from analysis.email_sender import build_html_email, send_email
from analysis.stock_discovery import select_candidates
from analysis.supabase_client import get_supabase

GEM_PRICE_CAP = 12.0  # hidden gems must be under this price to pass through


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
    try:
        now = datetime.now(timezone.utc).isoformat()
        for rec in recommendations:
            action = rec.get("action", "NO_ACTION")
            if action in ("NO_ACTION", "WAIT"):
                continue

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
        print(f"[run_weekly] Error saving to Supabase: {exc}")


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
        max_main=8,
        max_gems=5,
    )
    all_candidates = list(dict.fromkeys(main_candidates + gem_candidates))
    print(f"[run_weekly] Total candidates to fetch: {all_candidates}")

    # 4. Scrape Reddit signals for all candidates
    print("[run_weekly] Scraping Reddit signals...")
    reddit_signals = {}
    try:
        reddit_signals = scrape_reddit(hours_back=48, target_tickers=all_candidates)
    except Exception as exc:
        print(f"[run_weekly] Reddit scrape failed: {exc}")

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

    # 6b. Update portfolio positions with live prices from fundamentals
    for p in positions:
        fund = fundamentals_map.get(p["symbol"], {})
        price = fund.get("current_price")
        if price:
            try:
                p["current_price"] = f"${float(price):.2f}"
            except (TypeError, ValueError):
                pass

    # 7. Score each stock
    # Gems use speculative_growth category; main uses auto-classify
    print("[run_weekly] Scoring stocks...")
    scored = {}
    gem_set = set(gem_candidates)

    for ticker, fund in fundamentals_map.items():
        if fund.get("fetch_error"):
            print(f"[run_weekly] Skipping {ticker} — fetch error: {fund['fetch_error']}")
            continue

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
