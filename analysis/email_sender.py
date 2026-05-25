"""
Composes and sends the weekly/monthly newsletter via Gmail SMTP.
Email is in English. Evidence table included for each recommendation.
"""

import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _pct(val: Any, decimals: int = 1) -> str:
    if val is None:
        return "N/A"
    try:
        return f"{float(val) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(val)


def _fmt(val: Any, suffix: str = "", decimals: int = 2) -> str:
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(val)


def _action_color(action: str) -> str:
    colors = {
        "BUY_BELOW": "#1a7a1a",
        "ADD_ON_DIP": "#2d8a2d",
        "SELL": "#b30000",
        "REDUCE": "#cc3300",
        "WATCHLIST": "#7a5c00",
        "WAIT": "#555555",
        "NO_ACTION": "#888888",
        "NO_TRADE": "#888888",
    }
    return colors.get(action, "#333333")


def _action_label(action: str) -> str:
    labels = {
        "BUY_BELOW": "BUY BELOW",
        "ADD_ON_DIP": "ADD ON DIP",
        "SELL": "SELL",
        "REDUCE": "REDUCE POSITION",
        "WATCHLIST": "WATCHLIST",
        "WAIT": "WAIT",
        "NO_ACTION": "NO ACTION",
        "NO_TRADE": "NO TRADE THIS WEEK",
    }
    return labels.get(action, action)


def _build_evidence_table(ev: dict) -> str:
    rows = [
        ("Price today", ev.get("current_price", "N/A")),
        ("Buy zone", ev.get("buy_zone", "N/A")),
        ("P/E", ev.get("pe", "N/A")),
        ("Forward P/E", ev.get("forward_pe", "N/A")),
        ("PEG", ev.get("peg", "N/A")),
        ("Debt/Equity", ev.get("debt_equity", "N/A")),
        ("Revenue growth", ev.get("revenue_growth", "N/A")),
        ("FCF yield", ev.get("fcf_yield", "N/A")),
        ("Op. margin", ev.get("op_margin", "N/A")),
        ("Insider signal", ev.get("insider_signal", "N/A")),
        ("Congress signal", ev.get("congress_signal", "N/A")),
        ("Reddit hype", ev.get("reddit_hype", "N/A")),
        ("Fund. score", ev.get("fundamental_score", "N/A")),
        ("Confidence", ev.get("confidence", "N/A")),
    ]
    rows_html = "".join(
        f"<tr><td style='padding:4px 10px;color:#555;font-size:13px;'>{k}</td>"
        f"<td style='padding:4px 10px;font-weight:600;font-size:13px;'>{v}</td></tr>"
        for k, v in rows
    )
    return f"""<table style='border-collapse:collapse;border:1px solid #ddd;margin:10px 0;'>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_stock_block(rec: dict) -> str:
    action = rec.get("action", "NO_ACTION")
    ticker = rec.get("ticker", "")
    company = rec.get("company_name", ticker)
    is_gem = rec.get("is_hidden_gem", False)
    color = _action_color(action)
    label = _action_label(action)
    buy_zone = rec.get("buy_zone", "N/A")
    target = rec.get("target_price", "N/A")
    position_size = rec.get("position_size", "N/A")
    confidence = rec.get("confidence", "N/A")
    thesis = rec.get("investment_thesis", "")
    valuation = rec.get("valuation_verdict", "")
    catalyst = rec.get("catalyst", "")
    downside = rec.get("downside_scenario", "")
    vs_cash = rec.get("vs_cash_alternative", "")
    red_flags = rec.get("red_flags", [])
    ev = rec.get("evidence_table", {})
    hype_note = rec.get("hype_note", "")

    gem_badge = ""
    if is_gem:
        gem_badge = "<span style='background:#6a0dad;color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-left:6px;'>💎 Hidden Gem (5-10g)</span>"

    red_flags_html = ""
    if red_flags:
        flags = "".join(f"<li style='color:#b30000;font-size:13px;'>{f}</li>" for f in red_flags)
        red_flags_html = f"<p style='margin:8px 0 4px;'><strong>Crvene zastavice:</strong></p><ul style='margin:4px 0;'>{flags}</ul>"

    hype_note_html = ""
    if hype_note:
        hype_note_html = f"<p style='background:#fff3cd;padding:6px 10px;border-radius:4px;font-size:12px;margin:6px 0;'>⚠️ {hype_note}</p>"

    return f"""
<div style='border:1px solid {"#6a0dad" if is_gem else "#e0e0e0"};border-radius:8px;padding:16px;margin:16px 0;background:{"#fdf6ff" if is_gem else "#fafafa"};'>
  <div style='display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap;'>
    <span style='background:{color};color:white;padding:4px 12px;border-radius:4px;
                 font-weight:700;font-size:14px;letter-spacing:0.5px;'>{label}</span>
    <span style='font-size:20px;font-weight:700;'>{ticker}</span>
    <span style='color:#666;font-size:14px;'>{company}</span>
    {gem_badge}
  </div>
  <p style='margin:4px 0;font-size:13px;'>
    <strong>Zona kupnje:</strong> {buy_zone} &nbsp;|&nbsp;
    <strong>Ciljna cijena:</strong> {target} &nbsp;|&nbsp;
    <strong>Veličina pozicije:</strong> {position_size} &nbsp;|&nbsp;
    <strong>Pouzdanost:</strong> {confidence}/10
  </p>
  {hype_note_html}
  <p style='margin:10px 0 4px;font-size:14px;'><strong>Teza:</strong> {thesis}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>Valuacija:</strong> {valuation}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>Katalizator:</strong> {catalyst}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>Downside scenarij:</strong> {downside}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>vs. Čekanje/Cash:</strong> {vs_cash}</p>
  {red_flags_html}
  <details style='margin-top:10px;'>
    <summary style='cursor:pointer;font-size:13px;color:#555;'>Tablica dokaza ▾</summary>
    {_build_evidence_table(ev)}
  </details>
</div>"""


def build_html_email(
    summary: dict,
    recommendations: list[dict],
    portfolio_value: float | None,
    portfolio_positions: list[dict],
    email_type: str = "WEEKLY",
) -> str:
    date_str = summary.get("date", datetime.now().strftime("%Y-%m-%d"))
    market_comment = summary.get("overall_market_comment", "")
    no_trade = summary.get("no_trade_reason")
    portfolio_note = summary.get("portfolio_note", "")
    top_actions = summary.get("top_actions", [])
    watchlist = summary.get("watchlist_this_week", [])

    # Build action summary banner
    buy_count = sum(1 for r in recommendations if r.get("action") in ("BUY_BELOW", "ADD_ON_DIP"))
    sell_count = sum(1 for r in recommendations if r.get("action") in ("SELL", "REDUCE"))
    watch_count = sum(1 for r in recommendations if r.get("action") == "WATCHLIST")

    banner_items = []
    if buy_count:
        banner_items.append(f"<span style='color:#1a7a1a;font-weight:700;'>✅ {buy_count} BUY</span>")
    if sell_count:
        banner_items.append(f"<span style='color:#b30000;font-weight:700;'>🔴 {sell_count} SELL</span>")
    if watch_count:
        banner_items.append(f"<span style='color:#7a5c00;font-weight:700;'>👁 {watch_count} WATCHLIST</span>")
    if no_trade:
        banner_items.append("<span style='color:#888888;font-weight:700;'>⏳ NO TRADE</span>")
    banner_html = " &nbsp;|&nbsp; ".join(banner_items) if banner_items else "⏳ NO TRADE THIS WEEK"

    # Portfolio summary
    portfolio_html = ""
    if portfolio_positions:
        rows = "".join(
            f"<tr><td style='padding:4px 10px;'>{p.get('symbol','')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('shares','')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('avg_cost','N/A')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('current_price','N/A')}</td>"
            f"<td style='padding:4px 10px;font-weight:600;color:{'#1a7a1a' if str(p.get('pnl_pct',0)).startswith('-') == False else '#b30000'};'>"
            f"{p.get('pnl_pct','N/A')}</td></tr>"
            for p in portfolio_positions
        )
        portfolio_html = f"""
<h3 style='margin:16px 0 8px;'>Portfolio</h3>
<table style='border-collapse:collapse;width:100%;'>
  <thead><tr style='background:#f0f0f0;'>
    <th style='padding:6px 10px;text-align:left;'>Symbol</th>
    <th style='padding:6px 10px;text-align:left;'>Shares</th>
    <th style='padding:6px 10px;text-align:left;'>Avg cost</th>
    <th style='padding:6px 10px;text-align:left;'>Price now</th>
    <th style='padding:6px 10px;text-align:left;'>P&L %</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""

    # Stock recommendation blocks
    rec_blocks = ""
    if recommendations:
        actionable = [r for r in recommendations if r.get("action") not in ("NO_ACTION", "WAIT")]
        waiting = [r for r in recommendations if r.get("action") == "WAIT"]
        if actionable:
            rec_blocks += "".join(_build_stock_block(r) for r in actionable)
        if waiting:
            wait_list = ", ".join(f"{r.get('ticker')} ({r.get('buy_zone','?')})" for r in waiting)
            rec_blocks += f"<p style='color:#555;font-size:13px;margin:8px 0;'><strong>Čekanje bolje cijene:</strong> {wait_list}</p>"

    no_trade_html = ""
    if no_trade:
        no_trade_html = f"""
<div style='background:#f0f4ff;border-left:4px solid #888;padding:12px 16px;margin:16px 0;border-radius:4px;'>
  <strong>⏳ Nema trgovine ovaj tjedan</strong><br>
  <span style='font-size:13px;color:#555;'>{no_trade}</span>
</div>"""

    watchlist_html = ""
    if watchlist:
        items = ", ".join(f"<code>{t}</code>" for t in watchlist)
        watchlist_html = f"<p style='font-size:13px;'><strong>Watchlist ovaj tjedan:</strong> {items}</p>"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset='utf-8'></head>
<body style='font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#222;'>

  <div style='background:#1a1a2e;color:white;padding:16px 20px;border-radius:8px;margin-bottom:20px;'>
    <h1 style='margin:0;font-size:20px;'>📊 Dionice Newsletter</h1>
    <p style='margin:4px 0 0;font-size:13px;opacity:0.8;'>{email_type} | {date_str}</p>
  </div>

  <div style='background:#f8f9fa;padding:12px 16px;border-radius:6px;margin-bottom:16px;font-size:15px;'>
    {banner_html}
  </div>

  {portfolio_html}
  {f"<p style='font-size:13px;color:#555;margin:8px 0;'><strong>Napomena o portfelju:</strong> {portfolio_note}</p>" if portfolio_note else ""}
  <p style='font-size:13px;color:#555;margin:8px 0;'><strong>Tržište:</strong> {market_comment}</p>

  <h3 style='margin:20px 0 8px;border-bottom:2px solid #eee;padding-bottom:4px;'>Preporuke ovaj tjedan</h3>
  {rec_blocks or "<p style='color:#888;'>Nema akcija ovaj tjedan.</p>"}
  {no_trade_html}
  {watchlist_html}

  <hr style='margin:24px 0;border:none;border-top:1px solid #eee;'>
  <p style='font-size:11px;color:#999;'>
    Ovo je AI-generirana analiza isključivo u edukativne svrhe. Nije financijski savjet.
    Uvijek provedi vlastito istraživanje prije investiranja. Samo Revolut Basic platforma.
  </p>
</body>
</html>"""


def send_email(
    subject: str,
    html_body: str,
    to_email: str | None = None,
) -> bool:
    """Sends the newsletter via Gmail SMTP. Returns True if successful."""
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = to_email or os.environ.get("RECIPIENT_EMAIL", gmail_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipient, msg.as_string())
        print(f"[email_sender] Email sent to {recipient}")
        return True
    except Exception as exc:
        print(f"[email_sender] Failed to send email: {exc}")
        return False
