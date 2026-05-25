"""
Monthly deep report — runs first Saturday of each month.
Provides deeper portfolio analysis: sector concentration, risk, what to add/trim.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.fundamentals import fetch_multiple
from analysis.scorer import score_stock, classify_category
from analysis.email_sender import send_email
from analysis.ai_analyst import analyze_stock

from analysis.supabase_client import get_supabase

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"


def get_all_decisions_from_supabase() -> list[dict]:
    client = get_supabase()
    if not client:
        return []
    try:
        result = client.table("decisions").select("*").order("recommended_at", desc=True).limit(50).execute()
        return result.data or []
    except Exception:
        return []


def get_portfolio_from_supabase() -> tuple[list[dict], str]:
    client = get_supabase()
    if not client:
        return [], "Portfolio unavailable."
    try:
        result = client.table("transactions").select("*").order("trade_date").execute()
        rows = result.data or []
        holdings: dict[str, dict] = {}
        for row in rows:
            sym = row["symbol"]
            if sym not in holdings:
                holdings[sym] = {"symbol": sym, "shares": 0, "total_cost": 0.0, "currency": row.get("currency", "EUR")}
            shares = float(row.get("shares", 0))
            price = float(row.get("price_per_share", 0))
            if row["action"] == "BUY":
                holdings[sym]["shares"] += shares
                holdings[sym]["total_cost"] += shares * price
            elif row["action"] == "SELL":
                holdings[sym]["shares"] -= shares
        active = {k: v for k, v in holdings.items() if v["shares"] > 0}
        positions = list(active.values())
        context = "Holdings: " + "; ".join(
            f"{p['symbol']} {p['shares']:.0f} shares" for p in positions
        ) if positions else "Empty portfolio."
        return positions, context
    except Exception as exc:
        print(f"[run_monthly] Supabase error: {exc}")
        return [], "Portfolio unavailable."


def generate_monthly_report(
    portfolio_positions: list[dict],
    fundamentals_map: dict,
    scored_map: dict,
    decisions: list[dict],
    date_str: str,
) -> str:
    """
    Uses Claude to generate a deep monthly HTML report.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    positions_summary = []
    for p in portfolio_positions:
        sym = p["symbol"]
        avg_cost = p["total_cost"] / p["shares"] if p["shares"] > 0 else 0
        fund = fundamentals_map.get(sym, {})
        score = scored_map.get(sym, {})
        positions_summary.append({
            "symbol": sym,
            "shares": p["shares"],
            "avg_cost_eur": round(avg_cost, 2),
            "sector": fund.get("sector", "Unknown"),
            "category": score.get("category", "Unknown"),
            "fundamental_score": score.get("total_score", "N/A"),
            "pe": fund.get("pe"),
            "op_margin": fund.get("op_margin"),
            "debt_equity": fund.get("debt_equity"),
            "revenue_growth_yoy": fund.get("revenue_growth_yoy"),
        })

    prompt = f"""Generate a monthly portfolio deep-dive report for a small retail investor (Croatia, Revolut Basic, 300-400 EUR/month).

DATE: {date_str}
PORTFOLIO POSITIONS: {positions_summary}
RECENT AGENT DECISIONS (last 50): {decisions[:20]}

Write a comprehensive HTML report covering:
1. Portfolio overview: current composition, total value estimate, sector concentration
2. Risk assessment: concentration risk, single-stock risk (is VG still 100% of portfolio?), macro risks
3. Position review: for each holding — is the investment thesis still intact? Should we hold/add/reduce?
4. What to do next month: specific actions with reasoning
5. Agent performance review: look at past decisions and their outcomes (from decisions table)
6. Monthly conclusion: 3-5 bullet points of key takeaways

Tone: direct, analytical, no fluff. Maximum 600 words. Format as clean HTML (no CSS frameworks, simple inline styles).
English language."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def main():
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    print(f"[run_monthly] Starting monthly deep report for {date_str}")

    positions, portfolio_context = get_portfolio_from_supabase()
    tickers = [p["symbol"] for p in positions]
    print(f"[run_monthly] Portfolio tickers: {tickers}")

    if not tickers:
        print("[run_monthly] No portfolio positions found — skipping.")
        return

    fundamentals_map = fetch_multiple(tickers, delay_seconds=2.0)
    scored_map = {}
    for ticker, fund in fundamentals_map.items():
        if not fund.get("fetch_error"):
            category = classify_category(fund)
            score = score_stock(fund, category)
            scored_map[ticker] = {"total_score": score["total_score"], "category": category}

    decisions = get_all_decisions_from_supabase()

    print("[run_monthly] Generating monthly report...")
    html_report = generate_monthly_report(positions, fundamentals_map, scored_map, decisions, date_str)

    month_name = today.strftime("%B %Y")
    subject = f"[Dionice] Monthly Deep Report — {month_name}"

    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset='utf-8'></head>
<body style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#222;'>
  <div style='background:#1a1a2e;color:white;padding:16px 20px;border-radius:8px;margin-bottom:20px;'>
    <h1 style='margin:0;font-size:20px;'>📊 Dionice — Monthly Deep Report</h1>
    <p style='margin:4px 0 0;font-size:13px;opacity:0.8;'>{date_str}</p>
  </div>
  {html_report}
  <hr style='margin:24px 0;border:none;border-top:1px solid #eee;'>
  <p style='font-size:11px;color:#999;'>
    AI-generated analysis for educational purposes only. Not financial advice.
  </p>
</body>
</html>"""

    print(f"[run_monthly] Sending email: {subject}")
    send_email(subject, full_html)
    print("[run_monthly] Done.")


if __name__ == "__main__":
    main()
