"""
Monthly deep report — runs on the 1st of each month.
Deeper portfolio review: concentration against the cap, risk, what to add/trim,
how the agent's recent calls did.

The numbers (positions, prices, P&L, weights) are computed here and shown as a
table built by code; the model writes only the commentary. Before, it received
no prices at all and had to guess the portfolio's value.
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from analysis.ai_analyst import REVIEW_MODEL, strip_foreign_script
from analysis.email_sender import send_email
from analysis.fundamentals import fetch_multiple
from analysis.investor_profile import load_investor_profile
from analysis.params import load_params
from analysis.portfolio import compute_holdings, usd_to_eur_now
from analysis.scorer import classify_category, resolve_weights, score_stock
from analysis.supabase_client import get_supabase
from analysis.usage import UsageTracker

# One call a month: Haiku named VG (Venture Global) "Vodafone Group", slipped into
# Cyrillic and suggested adding to a position above the cap, so this runs on the
# review model at medium effort (a few cents a month).
MODEL = REVIEW_MODEL
DECISIONS_DAYS = 45


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def get_recent_decisions(client) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DECISIONS_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return (client.table("decisions")
                .select("symbol,recommended_at,agent_action,agent_confidence,agent_buy_zone,outcome_30d,excess_return_30d,user_action")
                .gte("recommended_at", cutoff).order("recommended_at", desc=True).execute().data or [])
    except Exception as exc:
        print(f"[run_monthly] Could not load decisions: {exc}")
        return []


def get_approved_lessons(client) -> str | None:
    try:
        rows = (client.table("model_lessons").select("lessons_text").eq("status", "ACTIVE")
                .order("generated_at", desc=True).limit(1).execute().data or [])
        return rows[0]["lessons_text"] if rows else None
    except Exception:
        return None


def get_cash(client) -> dict[str, float]:
    cash = {"cash_usd": 0.0, "cash_eur": 0.0}
    try:
        for row in client.table("account_settings").select("*").execute().data or []:
            if row.get("key") in cash and row.get("value") is not None:
                cash[row["key"]] = float(row["value"])
    except Exception:
        pass
    return cash


def portfolio_rows(positions: list[dict], fundamentals_map: dict, scored_map: dict, cash: dict, usd_to_eur: float) -> tuple[list[dict], float]:
    """Per-position numbers plus total capital in USD (positions + cash)."""
    rows = []
    for p in positions:
        fund = fundamentals_map.get(p["symbol"], {})
        price = _num(fund.get("current_price"))
        value = p["shares"] * price if price else None
        rows.append({
            "symbol": p["symbol"],
            # Without the name Haiku once called VG (Venture Global) "Vodafone Group"
            "company": fund.get("name") or p.get("company_name") or p["symbol"],
            "shares": p["shares"],
            "avg_cost_usd": round(p["avg_cost_usd"], 2),
            "price_usd": round(price, 2) if price else None,
            "value_usd": round(value, 2) if value else None,
            "pnl_pct": round((price / p["avg_cost_usd"] - 1) * 100, 1) if price and p["avg_cost_usd"] else None,
            "realized_pnl_usd": round(p["realized_pnl_usd"], 2),
            "sector": fund.get("sector", "Unknown"),
            "category": (scored_map.get(p["symbol"]) or {}).get("category"),
            "fundamental_score": (scored_map.get(p["symbol"]) or {}).get("total_score"),
            "forward_pe": fund.get("forward_pe"),
            "op_margin": fund.get("op_margin"),
            "debt_to_equity_x": fund.get("debt_to_equity_x"),
            "revenue_growth_yoy": fund.get("revenue_growth_yoy"),
        })
    capital = sum(r["value_usd"] or 0 for r in rows) + cash["cash_usd"] + (cash["cash_eur"] / usd_to_eur if usd_to_eur else 0)
    for r in rows:
        r["weight_pct"] = round(r["value_usd"] / capital * 100, 1) if r["value_usd"] and capital else None
    return rows, capital


def positions_table_html(rows: list[dict], capital_usd: float, cash: dict, cap: float) -> str:
    body = ""
    for r in rows:
        over = r["weight_pct"] is not None and r["weight_pct"] >= cap * 100
        weight = f"{r['weight_pct']:.0f}%{' ⚠️' if over else ''}" if r["weight_pct"] is not None else "N/A"
        pnl = f"{r['pnl_pct']:+.1f}%" if r["pnl_pct"] is not None else "N/A"
        body += (
            f"<tr><td style='padding:4px 10px;'>{r['symbol']}</td><td style='padding:4px 10px;'>{r['shares']:g}</td>"
            f"<td style='padding:4px 10px;'>${r['avg_cost_usd']:,.2f}</td>"
            f"<td style='padding:4px 10px;'>{'$' + format(r['price_usd'], ',.2f') if r['price_usd'] else 'N/A'}</td>"
            f"<td style='padding:4px 10px;'>{'$' + format(r['value_usd'], ',.0f') if r['value_usd'] else 'N/A'}</td>"
            f"<td style='padding:4px 10px;'>{pnl}</td>"
            f"<td style='padding:4px 10px;{'color:#b30000;font-weight:700;' if over else ''}'>{weight}</td></tr>"
        )
    header = "".join(f"<th style='padding:6px 10px;text-align:left;'>{h}</th>"
                     for h in ("Dionica", "Komada", "Prosj. cijena", "Cijena", "Vrijednost", "P&L", "Udio"))
    return (
        f"<table style='border-collapse:collapse;width:100%;font-size:13px;'><thead><tr style='background:#f0f0f0;'>{header}</tr></thead>"
        f"<tbody>{body}</tbody></table>"
        f"<p style='font-size:13px;margin:6px 0;'>Gotovina: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f} · "
        f"Ukupni kapital ≈ ${capital_usd:,.0f} · Limit po poziciji {cap * 100:.0f}%</p>"
    )


def generate_monthly_report(claude, rows: list[dict], capital_usd: float, cash: dict, decisions: list[dict],
                            date_str: str, cap: float, lessons: str | None, profile: str | None,
                            usage: UsageTracker) -> str:
    decisions_slim = [
        {
            "datum": str(d.get("recommended_at"))[:10], "dionica": d.get("symbol"), "akcija": d.get("agent_action"),
            "confidence": d.get("agent_confidence"), "zona": d.get("agent_buy_zone"),
            "ishod_30d": d.get("outcome_30d"), "vs_sp500_30d_pp": d.get("excess_return_30d"),
            "ulagac": d.get("user_action"),
        }
        for d in decisions
    ]
    lessons_block = f"\nODOBRENE LEKCIJE IZ KVARTALNOG IZVJEŠTAJA:\n{lessons}\n" if lessons else ""
    over_cap = [r["symbol"] for r in rows if r["weight_pct"] is not None and r["weight_pct"] >= cap * 100]
    cap_rule = (
        f"POZICIJE IZNAD LIMITA: {', '.join(over_cap)} — za njih NE predlaži nikakav dokup (ni na padu cijene, "
        "ni manjim iznosom), čak i ako je agent ranije dao ADD_ON_DIP; novi novac ide u druge dionice."
        if over_cap else "Nijedna pozicija nije iznad limita."
    )

    prompt = f"""Napiši mjesečni pregled portfelja za malog ulagača (Hrvatska, Revolut Basic, 300-400 EUR mjesečno) NA HRVATSKOM.

DATUM: {date_str}
POZICIJE (izračunato iz podataka; udio = pozicija / ukupni kapital s gotovinom): {rows}
GOTOVINA: ${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f} | UKUPNI KAPITAL ≈ ${capital_usd:,.0f}
LIMIT UDJELA JEDNE POZICIJE: {cap * 100:.0f}% ukupnog kapitala
{cap_rule}
PREPORUKE AGENTA U ZADNJIH {DECISIONS_DAYS} DANA: {decisions_slim}
{lessons_block}
Pravila: ne predlaži prodaju zbog pada cijene, nego samo ako se promijenila vrijednost poslovanja; poštuj limit koncentracije; tablicu pozicija ne ponavljaj (već je u mailu); firme zovi imenom iz podataka.

Odjeljci (HTML, jednostavni inline stilovi, bez CSS frameworka, bez <html>/<body> omotača):
1. Koncentracija i rizik portfelja
2. Pregled svake pozicije: je li teza netaknuta, drži/dodaj/smanji i zašto
3. Što napraviti idući mjesec: konkretno, uz gotovinu i novi novac
4. Kako je agent radio: što pokazuju preporuke i njihovi ishodi
5. Zaključak u 3-5 točaka

Ton: izravan, analitičan, bez praznih fraza. Najviše 600 riječi."""

    system = ("Ti si disciplinirani analitičar portfelja koji piše hrvatskim standardnim jezikom "
              "(\"što\", ne \"šta\"), isključivo latinicom.")
    if profile:
        system += f"\n\nPROFIL ULAGAČA (kontekst, ne razlog za odluku):\n{profile}"
    response = claude.messages.create(model=MODEL, max_tokens=16000, system=system,
                                      output_config={"effort": "medium"},
                                      messages=[{"role": "user", "content": prompt}])
    usage.add_message(response, fallback_model=MODEL)
    if response.stop_reason == "max_tokens":
        print("[run_monthly] WARNING: response hit max_tokens — report may be truncated.")

    html = strip_foreign_script("".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip())
    # Strip markdown code fences if Claude wrapped the HTML
    if html.startswith("```"):
        parts = html.split("```")
        html = parts[1] if len(parts) > 1 else html
        if html.startswith("html"):
            html = html[4:]
        html = html.strip()
    return html


def main(dry_run: bool = False):
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    print(f"[run_monthly] Starting monthly deep report for {date_str}{' — DRY RUN' if dry_run else ''}")

    client = get_supabase()
    if not client:
        print("[run_monthly] Supabase credentials missing.")
        return
    params = load_params(client)
    usage = UsageTracker("monthly")

    rows = client.table("transactions").select("*").order("trade_date").execute().data or []
    # Shared with the weekly run: cost resets on a full exit, EUR trades converted to USD
    positions = [h for h in compute_holdings(rows).values() if h["shares"] > 0]
    tickers = [p["symbol"] for p in positions]
    print(f"[run_monthly] Portfolio tickers: {tickers}")
    if not tickers:
        print("[run_monthly] No portfolio positions found — skipping.")
        return

    fundamentals_map = fetch_multiple(tickers, delay_seconds=2.0)
    weights = resolve_weights(params.get("scorer_weights"))
    scored_map = {}
    for ticker, fund in fundamentals_map.items():
        if not fund.get("fetch_error"):
            category = classify_category(fund)
            scored_map[ticker] = {"total_score": score_stock(fund, category, weights)["total_score"], "category": category}

    cash = get_cash(client)
    table_rows, capital_usd = portfolio_rows(positions, fundamentals_map, scored_map, cash, usd_to_eur_now())
    cap = params["concentration_cap"]

    print("[run_monthly] Generating monthly report...")
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    commentary = generate_monthly_report(claude, table_rows, capital_usd, cash, get_recent_decisions(client), date_str,
                                         cap, get_approved_lessons(client), load_investor_profile(client), usage)

    month_name = today.strftime("%m/%Y")
    subject = f"[Dionice] Mjesečni pregled portfelja — {month_name}"
    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset='utf-8'></head>
<body style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#222;'>
  <div style='background:#1a1a2e;color:white;padding:16px 20px;border-radius:8px;margin-bottom:20px;'>
    <h1 style='margin:0;font-size:20px;'>📊 Dionice — mjesečni pregled portfelja</h1>
    <p style='margin:4px 0 0;font-size:13px;opacity:0.8;'>{date_str}</p>
  </div>
  {positions_table_html(table_rows, capital_usd, cash, cap)}
  {commentary}
  <hr style='margin:24px 0;border:none;border-top:1px solid #eee;'>
  <p style='font-size:11px;color:#999;'>
    AI-generirana analiza isključivo u edukativne svrhe. Nije financijski savjet.
  </p>
</body>
</html>"""

    print(usage.summary())
    if dry_run:
        path = os.path.join(tempfile.gettempdir(), f"dionice_monthly_{date_str}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_html)
        print(f"[run_monthly] DRY RUN — email not sent. HTML saved to {path}")
        return
    print(f"[run_monthly] Sending email: {subject}")
    send_email(subject, full_html)
    usage.save(client)
    print("[run_monthly] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monthly portfolio review")
    parser.add_argument("--dry-run", action="store_true", help="no email, no database writes")
    main(dry_run=parser.parse_args().dry_run)
