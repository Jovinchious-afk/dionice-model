"""
Per-ticker memory across newsletter runs.

Each run used to analyse every stock from scratch, so identical numbers could
produce ADD_ON_DIP four times and then REDUCE (GNRC, Aug-Sep 2026). Every
analysis is now logged with a snapshot of its key numbers, and the next run shows
the model its own recent calls plus exactly which numbers moved since the last.

Until schema_v7.sql creates analysis_log, the decisions table stands in — calls
and prices only, no fundamentals snapshot.
"""

from datetime import datetime, timedelta, timezone

TABLE = "analysis_log"
HISTORY_DAYS = 60
MAX_CALLS_SHOWN = 4

# (key, label, kind). kind drives formatting and the "did it really move" threshold.
SNAPSHOT_FIELDS = [
    ("current_price", "cijena", "usd"),
    ("forward_pe", "forward P/E", "num"),
    ("op_margin", "op. marža", "frac"),
    ("revenue_growth_yoy", "rast prihoda", "frac"),
    ("earnings_growth_yoy", "rast dobiti", "frac"),
    ("debt_to_equity_x", "dug/kapital", "x"),
    ("fcf_yield", "FCF yield", "pct"),
    ("inventory_growth_yoy", "zalihe YoY", "frac"),
    ("shares_change_yoy", "broj dionica YoY", "frac"),
    ("analyst_target", "cilj analitičara", "usd"),
]
_THRESHOLDS = {
    "usd": ("rel", 0.02),
    "num": ("rel", 0.03),
    "frac": ("abs", 0.005),
    "x": ("abs", 0.03),
    "pct": ("abs", 0.2),
}


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def make_snapshot(fund: dict) -> dict:
    snap = {key: _num(fund.get(key)) for key, _, _ in SNAPSHOT_FIELDS}
    return {k: v for k, v in snap.items() if v is not None}


def _fmt(value: float, kind: str) -> str:
    if kind == "usd":
        return f"${value:,.2f}"
    if kind == "frac":
        return f"{value * 100:.1f}%"
    if kind == "x":
        return f"{value:.2f}x"
    if kind == "pct":
        return f"{value:.2f}%"
    return f"{value:.1f}"


def _moved(old: float, new: float, kind: str) -> bool:
    mode, limit = _THRESHOLDS[kind]
    if mode == "rel":
        return abs(new - old) > abs(old) * limit if old else new != old
    return abs(new - old) > limit


def _cutoff_iso(days: int) -> str:
    # "Z" rather than "+00:00": a literal "+" in the query string decodes as a space
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_recent_history(client, days: int = HISTORY_DAYS) -> dict[str, list[dict]]:
    """Recent calls per symbol, newest first. analysis_log wins; decisions fill the gaps."""
    if not client:
        return {}
    cutoff = _cutoff_iso(days)
    by_symbol: dict[str, list[dict]] = {}

    try:
        rows = (client.table(TABLE).select("*").gte("analyzed_at", cutoff)
                .order("analyzed_at", desc=True).execute().data or [])
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)
    except Exception as exc:
        print(f"[analysis_history] {TABLE} unavailable (run data/schema_v7.sql?): {exc}")

    try:
        rows = (client.table("decisions")
                .select("symbol,recommended_at,agent_action,agent_buy_zone,agent_confidence,price_at_recommendation")
                .gte("recommended_at", cutoff).order("recommended_at", desc=True).execute().data or [])
    except Exception as exc:
        print(f"[analysis_history] decisions fallback unavailable: {exc}")
        rows = []

    fallback: dict[str, list[dict]] = {}
    for row in rows:
        price = _num(row.get("price_at_recommendation"))
        fallback.setdefault(row["symbol"], []).append({
            "analyzed_at": row.get("recommended_at"),
            "action": row.get("agent_action"),
            "buy_zone": row.get("agent_buy_zone"),
            "confidence": row.get("agent_confidence"),
            "snapshot": {"current_price": price} if price else {},
        })
    for symbol, calls in fallback.items():
        by_symbol.setdefault(symbol, calls)

    return by_symbol


def build_previous_calls_block(history: list[dict] | None, fund: dict) -> str | None:
    if not history:
        return None

    lines = ["TVOJE PRETHODNE ANALIZE OVE DIONICE (najnovije prvo):"]
    for row in history[:MAX_CALLS_SHOWN]:
        price = _num((row.get("snapshot") or {}).get("current_price"))
        price_txt = f" | cijena ${price:,.2f}" if price else ""
        lines.append(
            f"- {str(row.get('analyzed_at') or '')[:10]}: {row.get('action')} | "
            f"zona {row.get('buy_zone') or 'N/A'} | conf {row.get('confidence', 'N/A')}{price_txt}"
        )

    last = history[0]
    old, now = last.get("snapshot") or {}, make_snapshot(fund)
    moved, fundamentals_compared = [], 0
    for key, label, kind in SNAPSHOT_FIELDS:
        if key not in old or key not in now:
            continue
        if key != "current_price":
            fundamentals_compared += 1
        if _moved(old[key], now[key], kind):
            moved.append((key, f"{label} {_fmt(old[key], kind)} → {_fmt(now[key], kind)}"))

    date = str(last.get("analyzed_at") or "")[:10]
    if moved:
        lines.append(f"PROMJENA BROJKI od {date}: " + "; ".join(text for _, text in moved))
    if fundamentals_compared and not any(key != "current_price" for key, _ in moved):
        lines.append(f"Fundamenti bez promjene od {date} — mijenjala se samo cijena.")

    lines.append(
        "PRAVILO: ako mijenjaš smjer (npr. ADD_ON_DIP/HOLD → REDUCE/SELL) ili pomičeš buy zonu, "
        "u change_vs_last navedi koja se BROJKA ili činjenica o poslovanju promijenila. "
        "Sama promjena cijene nije razlog."
    )
    return "\n".join(lines)


def record_analyses(client, recommendations: list[dict], fundamentals_by_ticker: dict[str, dict]) -> None:
    """One row per analysed ticker — every action, including WAIT and NO_ACTION."""
    if not client or not recommendations:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for rec in recommendations:
        symbol = rec.get("ticker")
        if not symbol:
            continue
        confidence = _num(rec.get("confidence"))
        # The debate no longer goes into the newsletter, so it is kept here instead
        snapshot = make_snapshot(fundamentals_by_ticker.get(symbol, {}))
        for field in ("investor_view", "counter_argument"):
            if rec.get(field):
                snapshot[field] = str(rec[field])[:600]
        rows.append({
            "analyzed_at": now,
            "symbol": symbol,
            "action": rec.get("action"),
            "confidence": int(confidence) if confidence is not None else None,
            "buy_zone": rec.get("buy_zone"),
            "target_price": rec.get("target_price"),
            "thesis": (rec.get("investment_thesis") or "")[:400],
            "model": rec.get("model"),
            "snapshot": snapshot,
        })
    try:
        client.table(TABLE).insert(rows).execute()
        print(f"[analysis_history] Logged {len(rows)} analyses")
    except Exception as exc:
        print(f"[analysis_history] Could not write {TABLE} (run data/schema_v7.sql?): {exc}")
