"""
Thursday revisit, every other Thursday (revisit_every_weeks): the newsletter's own
BUY ideas of the last 60 days, and the watchlist ideas whose buy zone the price has
reached, analysed again. The cycle counts from the last revisit that actually ran,
so a Thursday whose run failed is made up the following Thursday.

Ideas used to be fire-and-forget: of the 14 BUY/ADD calls and reached zones from
Aug-Oct 2026, the random universe sample had re-checked only four. A reached zone
is the moment that matters most - measured on 2026-10-25, ideas whose zone was
reached lagged the S&P 500 by 3.2pp after 30 days (n=16) and led it by 20.8pp
after 90 days (n=8) - so every revisit answers one question: did the value change,
or only the price? Revisits run on REVIEW_MODEL; whether one reaches the email is
decided by revisit_min_confidence.

Zone drift is the failure the first test run showed: when the price fell into an
old zone, the routine model lowered the zone (ATI $170 → $145, CENX $32 → $28)
instead of judging the value - chasing the price down, which the investor's rule
("the zone follows value, not price") forbids. zone_drift() flags it.
"""

from datetime import date, datetime, timedelta, timezone

from analysis.prices import parse_buy_zone

BUY_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP"}
ZONE_TOLERANCE = 1.02   # a price at most 2% above the zone counts as "at the zone"
DRIFT_THRESHOLD = 0.95  # a new zone 5%+ below the old one, while the price is at the old one
# A revisit is due once more than (weeks × 7 − this) days have passed since the last
# one: GitHub starts the job at a different hour every time, so exactly 14 days is too strict
CYCLE_SLACK_DAYS = 4
REVISIT_WEEKDAY = 3     # Thursday, the day after update_prices marks reached buy zones
RUN_HOUR_UTC = 13       # the newsletter's cron hour, for dating future runs


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _num(value) -> float | None:
    try:
        v = float(str(value).replace("$", "").replace(",", "")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _day(value) -> str:
    """'2026-09-22…' → '22. 9.'"""
    ts = _ts(value)
    return f"{ts.day}. {ts.month}." if ts else str(value or "")


def _evidence_price(row: dict) -> float | None:
    """The price a watchlist row was written at, from its evidence table ("$121.51")."""
    evidence = row.get("evidence_json")
    return _num(evidence.get("current_price")) if isinstance(evidence, dict) else None


def last_revisit_at(history: dict[str, list[dict]]) -> datetime | None:
    """When the last revisit ran: the newest logged analysis that was one (features.revisit_trigger)."""
    latest = None
    for rows in history.values():
        for row in rows:
            if ((row.get("snapshot") or {}).get("features") or {}).get("revisit_trigger"):
                ts = _ts(row.get("analyzed_at"))
                if ts and (latest is None or ts > latest):
                    latest = ts
    return latest


def revisit_due(last: datetime | None, now: datetime, every_weeks: int) -> bool:
    """True when the revisit cycle has come round: never ran yet, or ran more than a cycle ago."""
    if last is None:
        return True
    return now - last > timedelta(days=max(int(every_weeks), 1) * 7 - CYCLE_SLACK_DAYS)


def next_revisit_on(last: datetime | None, now: datetime, every_weeks: int) -> date | None:
    """The Thursday after today on which the next revisit will run (for the health line)."""
    day = now.date()
    for _ in range(7 * 8):
        day += timedelta(days=1)
        if day.weekday() != REVISIT_WEEKDAY:
            continue
        if revisit_due(last, datetime(day.year, day.month, day.day, RUN_HOUR_UTC, tzinfo=timezone.utc), every_weeks):
            return day
    return None


def _origin(symbol: str, trigger: str, ts: datetime, action, zone, confidence, thesis, price) -> dict:
    return {
        "symbol": symbol, "trigger": trigger, "date": ts.isoformat(), "action": action,
        "buy_zone": zone, "confidence": confidence, "thesis": (thesis or "")[:500],
        "price_at_call": price, "zone_reached_at": None, "zone_reached_price": None,
    }


def _mark_reached(origin: dict, reached_at, reached_price) -> None:
    origin["trigger"] = "zone_reached"
    origin["zone_reached_at"] = str(reached_at)[:10] if reached_at else None
    origin["zone_reached_price"] = _num(reached_price)


def select_revisits(
    decisions: list[dict],
    watchlist_rows: list[dict],
    now: datetime,
    held: set[str],
    dead: set[str] | None = None,
    days: int = 60,
    max_n: int = 20,
    prices: dict[str, float] | None = None,
) -> dict[str, dict]:
    """
    {symbol: origin} for this Thursday. Sources: every BUY_BELOW/ADD_ON_DIP decision
    of the last `days` (newest call per symbol), and every active agent watchlist row
    whose zone was reached in that window - by the Wednesday scoring job, or right now
    according to `prices`. Held positions are analysed every run anyway and dead
    tickers cannot be; reached zones come first, then the newest calls.
    """
    cutoff = now - timedelta(days=days)
    prices = prices or {}
    origins: dict[str, dict] = {}

    for d in sorted(decisions, key=lambda r: str(r.get("recommended_at") or "")):
        ts = _ts(d.get("recommended_at"))
        symbol = d.get("symbol")
        if not symbol or not ts or ts < cutoff or d.get("agent_action") not in BUY_ACTIONS:
            continue
        origin = _origin(symbol, "buy_call", ts, d.get("agent_action"), d.get("agent_buy_zone"),
                         d.get("agent_confidence"), d.get("agent_thesis"), _num(d.get("price_at_recommendation")))
        reached = _ts(d.get("buy_zone_reached_at"))
        if reached and reached >= ts:
            _mark_reached(origin, d.get("buy_zone_reached_at"), d.get("buy_zone_reached_price"))
        origins[symbol] = origin

    for w in watchlist_rows:
        symbol = w.get("symbol")
        if not symbol or w.get("status", "ACTIVE") != "ACTIVE" or w.get("confidence") is None:
            continue  # manual rows carry no agent call to revisit
        reached = _ts(w.get("buy_zone_reached_at"))
        if reached and reached >= cutoff:
            origin = origins.get(symbol)
            if origin is None:
                ts = _ts(w.get("suggested_at")) or reached
                origin = _origin(symbol, "zone_reached", ts, w.get("action"), w.get("buy_zone"),
                                 w.get("confidence"), w.get("thesis"), _evidence_price(w))
            _mark_reached(origin, w.get("buy_zone_reached_at"), w.get("buy_zone_reached_price"))
            origins[symbol] = origin

    # Zones crossed since the Wednesday check: ATI fell through its zone on the
    # Thursday after one, which the weekly job would only have seen six days later.
    for symbol, origin in origins.items():
        zone, price = parse_buy_zone(origin.get("buy_zone")), prices.get(symbol)
        if origin["trigger"] != "zone_reached" and zone and price and price <= zone:
            _mark_reached(origin, now.date().isoformat(), price)
    for w in watchlist_rows:
        symbol = w.get("symbol")
        if (not symbol or symbol in origins or w.get("status", "ACTIVE") != "ACTIVE"
                or w.get("confidence") is None or w.get("action") not in BUY_ACTIONS | {"WATCHLIST"}):
            continue
        zone, price = parse_buy_zone(w.get("buy_zone")), prices.get(symbol)
        ts = _ts(w.get("suggested_at"))
        if zone and price and price <= zone and ts and ts >= now - timedelta(days=90):
            origin = _origin(symbol, "zone_reached", ts, w.get("action"), w.get("buy_zone"),
                             w.get("confidence"), w.get("thesis"), _evidence_price(w))
            _mark_reached(origin, now.date().isoformat(), price)
            origins[symbol] = origin

    skip = set(held) | set(dead or ())
    ranked = sorted(
        (o for s, o in origins.items() if s not in skip),
        key=lambda o: (o["trigger"] != "zone_reached", -(_ts(o["zone_reached_at"] or o["date"]) or now).timestamp()),
    )
    return {o["symbol"]: o for o in ranked[:max_n]}


def revisit_prompt_block(origin: dict, price_now: float | None) -> str:
    """The revisit context the model sees with the stock's data."""
    price_then = _num(origin.get("price_at_call"))
    lines = [
        "🔁 PONOVNA PROVJERA TVOJE RANIJE IDEJE (četvrtak):",
        f"- Prva preporuka {str(origin.get('date'))[:10]}: {origin.get('action')} | zona {origin.get('buy_zone') or 'N/A'} | "
        f"confidence {origin.get('confidence')}" + (f" | cijena tada ${price_then:,.2f}" if price_then else ""),
    ]
    if origin.get("thesis"):
        lines.append(f"- Teza tada: {origin['thesis']}")
    if origin.get("zone_reached_at"):
        reached_price = _num(origin.get("zone_reached_price"))
        lines.append(f"- Zona je dosegnuta {origin['zone_reached_at']}" + (f" pri ${reached_price:,.2f}" if reached_price else ""))
    else:
        zone = parse_buy_zone(origin.get("buy_zone"))
        if zone and price_now:
            lines.append(f"- Zona još nije dosegnuta: cijena ${price_now:,.2f} je {(price_now / zone - 1) * 100:+.1f}% iznad zone")
    if price_then and price_now:
        lines.append(f"- Cijena od prve preporuke: {(price_now / price_then - 1) * 100:+.1f}%")
    lines += [
        "ZADATAK: odluči je li ideja i dalje vrijedna novca. Pitanje je je li se promijenila VRIJEDNOST "
        "(brojke, smjernice uprave, vijesti), a ne samo cijena.",
        "- Ako je cijena pala u zonu, a vrijednost je ista, to je trenutak koji je zona čekala: potvrdi akciju i zonu.",
        "- Ne spuštaj zonu zato što je cijena pala (sustav to označava). Zonu mijenjaj samo uz promjenu brojki "
        "i navedi koju u change_vs_last.",
        "- Ako teza više ne vrijedi, reci to jasno: WAIT ili NO_ACTION s niskim confidenceom.",
        "- Mjereno u ovom sustavu: ideje kojima je zona dosegnuta kratkoročno su zaostajale za S&P 500, a na 90 dana "
        "su ga pretekle. Kratkoročni pad sam po sebi nije dokaz da je teza pogrešna.",
    ]
    return "\n".join(lines)


def origin_line(origin: dict, price_now: float | None, spy_change_pct: float | None = None) -> str:
    """Deterministic 'first call → today' line for the email."""
    price_then = _num(origin.get("price_at_call"))
    line = f"Prva preporuka {_day(origin.get('date'))}: {origin.get('action')} {origin.get('buy_zone') or ''}".rstrip()
    if price_then:
        line += f" @ ${price_then:,.2f}"
    if origin.get("confidence") is not None:
        line += f" ({origin.get('confidence')}/10)"
    if price_now:
        line += f" → danas ${price_now:,.2f}"
        if price_then:
            spy = f"; S&P 500 {spy_change_pct:+.1f}%" if spy_change_pct is not None else ""
            line += f" ({(price_now / price_then - 1) * 100:+.1f}%{spy})"
    if origin.get("zone_reached_at"):
        reached_price = _num(origin.get("zone_reached_price"))
        line += f" · zona dosegnuta {_day(origin['zone_reached_at'])}" + (f" @ ${reached_price:,.2f}" if reached_price else "")
    return line


def zone_drift(reference_zone: str | None, new_zone: str | None, price: float | None) -> dict | None:
    """
    The zone was lowered by DRIFT_THRESHOLD or more while the price already sat at
    the old zone - following the price instead of the value. None otherwise.
    """
    ref, new = parse_buy_zone(reference_zone), parse_buy_zone(new_zone)
    if not ref or not new or not price:
        return None
    if price > ref * ZONE_TOLERANCE or new >= ref * DRIFT_THRESHOLD:
        return None
    return {"from": ref, "to": new, "pct": round((new / ref - 1) * 100, 1), "price": round(price, 2)}


def zone_reached_now(reference_zone: str | None, price: float | None) -> bool:
    ref = parse_buy_zone(reference_zone)
    return bool(ref and price and price <= ref * ZONE_TOLERANCE)


def stale_watchlist_rows(
    rows_by_symbol: dict[str, list[dict]],
    now: datetime,
    analysed: dict[str, dict],
    published: set[str],
    keep_actions: set[str],
    recently_analysed: set[str],
    max_age_days: int = 90,
) -> list[dict]:
    """
    Active agent watchlist rows that no longer describe a live idea:
      - the stock was analysed this run and the new call is not an idea any more
        (not published, or an action the watchlist does not track);
      - not analysed this run, older than max_age_days, and no analysis in the
        history window either - nobody has looked at it for months.
    """
    stale = []
    for symbol, rows in rows_by_symbol.items():
        rec = analysed.get(symbol)
        if rec is not None:
            if symbol not in published or rec.get("action") not in keep_actions:
                stale.extend(rows)
            continue
        if symbol in recently_analysed:
            continue
        for row in rows:
            ts = _ts(row.get("suggested_at"))
            if ts and (now - ts).days > max_age_days:
                stale.append(row)
    return stale
