"""
Composes and sends the weekly/monthly newsletter and the quarterly learning
report via Gmail SMTP. Text is Croatian; an evidence table is included for each
recommendation.
"""

import html
import os
import re
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


def inline_markdown(text: str) -> str:
    """Escapes HTML and turns **bold** into <strong>."""
    text = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def markdown_to_html(md: str) -> str:
    """
    The small markdown subset the learning report uses (headings, bullets, pipe
    tables, bold, paragraphs) as inline-styled HTML that Gmail renders.
    """
    out: list[str] = []
    in_list = False
    table: list[list[str]] = []

    def flush_table():
        if not table:
            return
        header, *body = table
        head = "".join(f"<th style='padding:4px 8px;text-align:left;background:#f0f0f0;'>{inline_markdown(c)}</th>" for c in header)
        rows = "".join(
            "<tr>" + "".join(f"<td style='padding:4px 8px;border-top:1px solid #eee;'>{inline_markdown(c)}</td>" for c in r) + "</tr>"
            for r in body
        )
        out.append(f"<table style='border-collapse:collapse;font-size:13px;margin:6px 0 12px;'><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        table.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):
                table.append(cells)
            continue
        flush_table()
        if line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul style='margin:4px 0 10px;padding-left:20px;'>")
                in_list = True
            out.append(f"<li style='font-size:14px;margin:3px 0;'>{inline_markdown(line.lstrip()[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        if heading:
            size = {1: 20, 2: 17, 3: 15, 4: 14}[len(heading.group(1))]
            out.append(f"<h3 style='font-size:{size}px;margin:16px 0 6px;'>{inline_markdown(heading.group(2))}</h3>")
        elif line.strip():
            out.append(f"<p style='font-size:14px;margin:6px 0;'>{inline_markdown(line)}</p>")
    flush_table()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _has_text(value) -> bool:
    return bool(value) and str(value).strip().upper() not in ("N/A", "NONE", "NULL")


def _action_color(action: str) -> str:
    colors = {
        "BUY_BELOW": "#1a7a1a",
        "ADD_ON_DIP": "#2d8a2d",
        "HOLD": "#1f5f8b",
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
        "HOLD": "HOLD (DRŽI)",
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
        ("Dug/kapital", ev.get("debt_equity", "N/A")),
        ("Revenue growth", ev.get("revenue_growth", "N/A")),
        ("FCF yield", ev.get("fcf_yield", "N/A")),
        ("Op. margin", ev.get("op_margin", "N/A")),
        ("Insider (14 dana)", ev.get("insider_signal", "N/A")),
        ("Congress signal", ev.get("congress_signal", "N/A")),
        ("StockTwits", ev.get("stocktwits", "N/A")),
        ("Earnings za", ev.get("earnings_in", "N/A")),
        ("Checklist ulagača", ev.get("checklist", "N/A")),
        ("Sezonalnost", ev.get("seasonality", "N/A")),
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

    thesis_breakers = rec.get("thesis_breakers", [])
    thesis_breakers_html = ""
    if thesis_breakers:
        items = "".join(f"<li style='font-size:13px;margin:2px 0;'>{b}</li>" for b in thesis_breakers[:3])
        thesis_breakers_html = (
            "<div style='background:#fff8e1;border-left:4px solid #f59e0b;"
            "padding:10px 14px;margin:8px 0;border-radius:0 4px 4px 0;'>"
            "<strong style='font-size:13px;'>⚡ Što bi promijenilo preporuku:</strong>"
            f"<ul style='margin:4px 0;padding-left:18px;'>{items}</ul></div>"
        )

    red_flags_html = ""
    if red_flags:
        flags = "".join(f"<li style='color:#b30000;font-size:13px;'>{f}</li>" for f in red_flags)
        red_flags_html = f"<p style='margin:8px 0 4px;'><strong>Crvene zastavice:</strong></p><ul style='margin:4px 0;'>{flags}</ul>"

    hype_note_html = ""
    if hype_note:
        hype_note_html = f"<p style='background:#fff3cd;padding:6px 10px;border-radius:4px;font-size:12px;margin:6px 0;'>⚠️ {hype_note}</p>"

    guard_html = ""
    for icon, key in (("🛡️", "sell_guard_note"), ("⚖️", "concentration_note")):
        if rec.get(key):
            guard_html += (
                "<p style='background:#e8f1fa;border-left:4px solid #1f5f8b;padding:8px 12px;"
                f"border-radius:0 4px 4px 0;font-size:13px;margin:8px 0;'>{icon} {rec[key]}</p>"
            )
    if rec.get("entry_plan_note"):
        guard_html += (
            "<p style='background:#eaf7ea;border-left:4px solid #1a7a1a;padding:8px 12px;"
            f"border-radius:0 4px 4px 0;font-size:13px;margin:8px 0;'>🎯 {rec['entry_plan_note']}</p>"
        )
    if rec.get("zone_drift_note"):
        guard_html += (
            "<p style='background:#fff3e0;border-left:4px solid #e67e00;padding:8px 12px;"
            f"border-radius:0 4px 4px 0;font-size:13px;margin:8px 0;'>⚠️ {rec['zone_drift_note']}</p>"
        )
    if rec.get("second_pass_note"):
        guard_html += f"<p style='font-size:12px;color:#555;margin:6px 0;'>🔎 {rec['second_pass_note']}</p>"
    revisit_html = (
        "<p style='background:#f3f0ff;border-left:4px solid #5b4bb7;padding:8px 12px;border-radius:0 4px 4px 0;"
        f"font-size:13px;margin:8px 0;'>🔁 {rec['revisit_line']}</p>"
        if rec.get("revisit_line") else ""
    )

    # investor_view / counter_argument are deliberately not rendered: the model still writes them
    # (and they are stored in analysis_log), but they made the newsletter long enough for Gmail to clip it

    context_html = ""
    for field_label, field_key in (("Ciklus/sezona", "cycle_view"), ("Promjena od zadnje analize", "change_vs_last")):
        if _has_text(rec.get(field_key)):
            context_html += f"<p style='margin:6px 0;font-size:13px;'><strong>{field_label}:</strong> {rec.get(field_key)}</p>"

    checklist_fails = rec.get("checklist_fails") or []
    checklist_html = ""
    if checklist_fails:
        fail_items = "".join(f"<li style='font-size:13px;margin:2px 0;'>{item}</li>" for item in checklist_fails)
        summary_txt = rec.get("checklist_summary", "")
        checklist_html = (
            f"<p style='margin:8px 0 4px;font-size:13px;'><strong>Checklist ulagača ({summary_txt}) — ne prolazi:</strong></p>"
            f"<ul style='margin:4px 0;padding-left:18px;'>{fail_items}</ul>"
        )

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
  {revisit_html}
  {hype_note_html}
  {guard_html}
  <p style='margin:10px 0 4px;font-size:14px;'><strong>Teza:</strong> {thesis}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>Valuacija:</strong> {valuation}</p>
  {context_html}
  <p style='margin:6px 0;font-size:13px;'><strong>Katalizator:</strong> {catalyst}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>Downside scenarij:</strong> {downside}</p>
  <p style='margin:6px 0;font-size:13px;'><strong>vs. Čekanje/Cash:</strong> {vs_cash}</p>
  {thesis_breakers_html}
  {checklist_html}
  {red_flags_html}
  <details style='margin-top:10px;'>
    <summary style='cursor:pointer;font-size:13px;color:#555;'>Tablica dokaza ▾</summary>
    {_build_evidence_table(ev)}
  </details>
</div>"""


def _build_gem_block(rec: dict) -> str:
    """
    A short hidden-gem card, whatever the action: gems are shown at any confidence,
    but as WAIT they used to collapse into a single ticker on the waiting line.
    """
    action = rec.get("action", "NO_ACTION")
    flags = rec.get("red_flags") or []
    flags_html = "".join(f"<li style='font-size:12px;color:#b30000;margin:1px 0;'>{f}</li>" for f in flags[:2])
    zone_bits = []
    if _has_text(rec.get("buy_zone")):
        zone_bits.append(f"<strong>Zona:</strong> {rec.get('buy_zone')}")
    if _has_text(rec.get("target_price")):
        zone_bits.append(f"<strong>Cilj:</strong> {rec.get('target_price')}")
    zone_bits.append("<strong>Pozicija:</strong> najviše 1-2% kapitala")
    return f"""
<div style='border:1px solid #6a0dad;border-radius:8px;padding:12px 14px;margin:10px 0;background:#fdf6ff;'>
  <div style='margin-bottom:6px;'>
    <span style='background:{_action_color(action)};color:white;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;'>{_action_label(action)}</span>
    <span style='font-size:17px;font-weight:700;margin-left:6px;'>{rec.get('ticker', '')}</span>
    <span style='color:#666;font-size:13px;'>{rec.get('company_name', '')}</span>
    <span style='font-size:12px;color:#6a0dad;margin-left:6px;'>💎 {rec.get('confidence', 'N/A')}/10</span>
  </div>
  <p style='margin:4px 0;font-size:13px;'><strong>Čime se bavi:</strong> {rec.get('business_explanation', '')}</p>
  <p style='margin:4px 0;font-size:13px;'><strong>Teza:</strong> {rec.get('investment_thesis', '')}</p>
  {f"<ul style='margin:4px 0;padding-left:18px;'>{flags_html}</ul>" if flags_html else ""}
  <p style='margin:4px 0;font-size:12px;color:#555;'>{' &nbsp;|&nbsp; '.join(zone_bits)}</p>
</div>"""


def _build_revisit_card(rec: dict) -> str:
    """
    A re-confirmed idea that is not a buy right now (WATCHLIST/WAIT at 6+): first call
    → today, what changed and the thesis. Full blocks for all of them made the first
    test email 118 KB, past the ~102 KB at which Gmail clips a message.
    """
    action = rec.get("action", "NO_ACTION")
    zone_bits = [f"<strong>{label}:</strong> {rec.get(key)}" for label, key in (("Zona", "buy_zone"), ("Cilj", "target_price"))
                 if _has_text(rec.get(key))]
    drift = (f"<p style='margin:4px 0;font-size:12px;color:#8a4b00;'>⚠️ {rec['zone_drift_note']}</p>"
             if rec.get("zone_drift_note") else "")
    return f"""
<div style='border:1px solid #c9c1f0;border-radius:8px;padding:12px 14px;margin:10px 0;background:#faf9ff;'>
  <div style='margin-bottom:6px;'>
    <span style='background:{_action_color(action)};color:white;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;'>{_action_label(action)}</span>
    <span style='font-size:17px;font-weight:700;margin-left:6px;'>{rec.get('ticker', '')}</span>
    <span style='color:#666;font-size:13px;'>{rec.get('company_name', '')}</span>
    <span style='font-size:12px;color:#5b4bb7;margin-left:6px;'>{rec.get('confidence', 'N/A')}/10</span>
  </div>
  <p style='margin:4px 0;font-size:12px;color:#4a3f8f;'>🔁 {rec.get('revisit_line', '')}</p>
  {drift}
  <p style='margin:4px 0;font-size:13px;'><strong>Što se promijenilo:</strong> {rec.get('change_vs_last', '')}</p>
  <p style='margin:4px 0;font-size:13px;'><strong>Teza:</strong> {rec.get('investment_thesis', '')}</p>
  {f"<p style='margin:4px 0;font-size:12px;color:#555;'>{' &nbsp;|&nbsp; '.join(zone_bits)}</p>" if zone_bits else ""}
</div>"""


def _ticker_list(recs: list[dict], with_zone: bool = True) -> str:
    items = []
    for r in recs:
        if r.get("excluded_reason"):
            items.append(f"{r.get('ticker')} (ispala iz analize: {r['excluded_reason']})")
            continue
        detail = [f"{r.get('confidence')}/10"]
        if with_zone and _has_text(r.get("buy_zone")):
            detail.append(str(r.get("buy_zone")))
        items.append(f"{r.get('ticker')} ({', '.join(detail)})")
    return ", ".join(items)


# Full analysis blocks: positions, buys and sells always; WATCHLIST only from this
# confidence (lower ones go on one line — the 4-5 group has not beaten the index)
FULL_BLOCK_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP", "HOLD", "SELL", "REDUCE"}
FULL_BLOCK_WATCHLIST_CONFIDENCE = 6
MAX_GEM_BLOCKS = 3


def _weight_cell(position: dict, cap: float | None) -> str:
    weight = position.get("weight")
    if weight is None:
        return "<td style='padding:4px 10px;'>N/A</td>"
    over = cap is not None and weight >= cap
    style = "color:#b30000;font-weight:700;" if over else ""
    return f"<td style='padding:4px 10px;{style}'>{weight * 100:.0f}%{' ⚠️' if over else ''}</td>"


def build_html_email(
    summary: dict,
    recommendations: list[dict],
    portfolio_value: float | None,
    portfolio_positions: list[dict],
    email_type: str = "WEEKLY",
    cash_line: str | None = None,
    concentration_cap: float | None = None,
    revisits_below: list[dict] | None = None,
    revisit_min_confidence: int = 6,
    revisit_days: int = 60,
    health_line: str | None = None,
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
    hold_count = sum(1 for r in recommendations if r.get("action") == "HOLD")

    banner_items = []
    if buy_count:
        banner_items.append(f"<span style='color:#1a7a1a;font-weight:700;'>✅ {buy_count} BUY</span>")
    if sell_count:
        banner_items.append(f"<span style='color:#b30000;font-weight:700;'>🔴 {sell_count} SELL</span>")
    if hold_count:
        banner_items.append(f"<span style='color:#1f5f8b;font-weight:700;'>📌 {hold_count} HOLD</span>")
    if watch_count:
        banner_items.append(f"<span style='color:#7a5c00;font-weight:700;'>👁 {watch_count} WATCHLIST</span>")
    if no_trade:
        banner_items.append("<span style='color:#888888;font-weight:700;'>⏳ NO TRADE</span>")
    banner_html = " &nbsp;|&nbsp; ".join(banner_items) if banner_items else "⏳ NO TRADE THIS WEEK"

    # Portfolio summary
    cash_html = f"<p style='font-size:13px;margin:6px 0;'><strong>💵 {cash_line}</strong></p>" if cash_line else ""
    portfolio_html = ""
    if portfolio_positions:
        rows = "".join(
            f"<tr><td style='padding:4px 10px;'>{p.get('symbol','')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('shares','')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('avg_cost','N/A')}</td>"
            f"<td style='padding:4px 10px;'>{p.get('current_price','N/A')}</td>"
            f"<td style='padding:4px 10px;font-weight:600;color:{'#b30000' if str(p.get('pnl_pct', '')).startswith('-') else '#1a7a1a'};'>"
            f"{p.get('pnl_pct','N/A')}</td>"
            f"{_weight_cell(p, concentration_cap)}</tr>"
            for p in portfolio_positions
        )
        cap_note = (
            f"<p style='font-size:12px;color:#555;margin:4px 0;'>Udio = pozicija / (sve pozicije + gotovina). "
            f"Limit po poziciji: {concentration_cap * 100:.0f}% — iznad njega agent ne predlaže dokup.</p>"
            if concentration_cap else ""
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
    <th style='padding:6px 10px;text-align:left;'>Udio</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
{cap_note}
{cash_html}"""

    # Thursday revisits: ideas of the last weeks analysed again, in their own section.
    # Buys get the full block, re-confirmed ideas that wait for their zone a short card.
    revisit_recs = sorted((r for r in recommendations if r.get("revisit")),
                          key=lambda r: (r.get("action") not in FULL_BLOCK_ACTIONS, -(r.get("confidence") or 0)))
    revisit_html = ""
    if revisit_recs or revisits_below:
        revisit_html = (
            "<h3 style='margin:20px 0 6px;border-bottom:2px solid #e3ddff;padding-bottom:4px;'>"
            f"🔁 Ponovna provjera — ideje iz zadnjih {revisit_days} dana</h3>"
            "<p style='font-size:12px;color:#555;margin:4px 0 8px;'>BUY/ADD preporuke i ideje kojima je cijena dosegnula "
            f"zonu, ponovno analizirane jačim modelom. Puna analiza od pouzdanosti {revisit_min_confidence}/10.</p>"
        )
        revisit_html += "".join(_build_stock_block(r) if r.get("action") in FULL_BLOCK_ACTIONS else _build_revisit_card(r)
                                for r in revisit_recs)
        if revisits_below:
            revisit_html += (
                f"<p style='color:#555;font-size:13px;margin:8px 0;'><strong>Ispod {revisit_min_confidence}/10 "
                f"(ideja trenutno ne vrijedi kupnje):</strong> {_ticker_list(revisits_below)}</p>"
            )

    # Hidden gems: a short card each, whatever the action, at most MAX_GEM_BLOCKS
    gem_recs = sorted((r for r in recommendations if r.get("is_hidden_gem") and not r.get("revisit")),
                      key=lambda r: -(r.get("confidence") or 0))
    gem_html = ""
    if gem_recs:
        gem_html = ("<h3 style='margin:20px 0 6px;border-bottom:2px solid #ead6f7;padding-bottom:4px;'>"
                    "💎 Hidden gems — horizont 5-10 godina, visok rizik</h3>")
        gem_html += "".join(_build_gem_block(r) for r in gem_recs[:MAX_GEM_BLOCKS])
        if gem_recs[MAX_GEM_BLOCKS:]:
            gem_html += (f"<p style='color:#555;font-size:12px;margin:6px 0;'><strong>Ostali gemovi:</strong> "
                         f"{_ticker_list(gem_recs[MAX_GEM_BLOCKS:], with_zone=False)}</p>")

    # Everything else: full blocks for what can be acted on, one line for the rest
    rec_blocks = ""
    others = [r for r in recommendations if not r.get("revisit") and not r.get("is_hidden_gem")]
    full = [r for r in others if r.get("action") in FULL_BLOCK_ACTIONS
            or (r.get("action") == "WATCHLIST" and (r.get("confidence") or 0) >= FULL_BLOCK_WATCHLIST_CONFIDENCE)]
    watching = [r for r in others if r.get("action") == "WATCHLIST" and r not in full]
    waiting = [r for r in others if r.get("action") == "WAIT"]
    rec_blocks += "".join(_build_stock_block(r) for r in full)
    if watching:
        rec_blocks += (f"<p style='color:#555;font-size:13px;margin:8px 0;'><strong>Prati zonu (sustav javlja kad je "
                       f"dosegnuta):</strong> {_ticker_list(watching)}</p>")
    if waiting:
        rec_blocks += (f"<p style='color:#555;font-size:13px;margin:8px 0;'><strong>Čekanje (bez zone ili prije "
                       f"događaja):</strong> {_ticker_list(waiting)}</p>")

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

  {revisit_html}
  <h3 style='margin:20px 0 8px;border-bottom:2px solid #eee;padding-bottom:4px;'>Preporuke ovaj tjedan</h3>
  {rec_blocks or "<p style='color:#888;'>Nema akcija ovaj tjedan.</p>"}
  {gem_html}
  {no_trade_html}
  {watchlist_html}

  <hr style='margin:24px 0;border:none;border-top:1px solid #eee;'>
  {f"<p style='font-size:11px;color:#888;margin:4px 0 10px;'>{html.escape(health_line)}</p>" if health_line else ""}
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
