"""
Weekly job (Wednesdays): scores old recommendations and tracks buy zones.

Each decision gets price_30d/90d/180d from the close on (or just before) that
exact day — not whatever the price is when the job happens to run — and is
scored against the S&P 500 (SPY) over the same days:
  - BUY_BELOW / ADD_ON_DIP: measured from the day the buy zone was first hit;
    a zone not reached by the checkpoint is "neutral" (no trade would have happened)
  - HOLD: correct if the stock beat the index; SELL / REDUCE: correct if it lagged
  - WATCHLIST / WAIT: "neutral" — not a trade — with the returns kept for information
It also writes a short AI retrospective for correct/wrong calls, auto-detects
whether the investor followed a call from the transactions log, and checks whether
each recommended buy zone has been reached (decisions and watchlist).

Scoring version 2 (2026-09) replaced "any price rise = correct". Rows scored under
version 1 are rescored automatically once data/schema_v7.sql adds the new columns.

Usage: python scripts/update_prices.py [--dry-run]
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import pandas as pd
import yfinance as yf

from analysis.supabase_client import get_supabase

MODEL = "claude-haiku-4-5-20251001"
CHECKPOINTS = [(30, "30d"), (90, "90d"), (180, "180d")]
BEARISH_ACTIONS = {"SELL", "REDUCE"}
BUY_ZONE_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP"}
NEUTRAL_ACTIONS = {"WATCHLIST", "WAIT", "NO_ACTION", "NO_TRADE"}
BUY_ZONE_MAX_AGE_DAYS = 180  # only applies to `decisions` — watchlist is bounded by status=ACTIVE instead
BENCHMARK = "SPY"
SCORING_VERSION = 2
V7_MARKER = "scoring_version"  # column exists once data/schema_v7.sql has run


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class PriceBook:
    """
    Daily adjusted closes per ticker, fetched once from the earliest date any
    caller needs — the job asks for the same tickers dozens of times per run.
    """

    def __init__(self):
        self._closes: dict[str, pd.Series] = {}
        self._start: dict[str, pd.Timestamp] = {}

    def closes(self, ticker: str, since: datetime) -> pd.Series | None:
        start = pd.Timestamp(since.date()) - pd.Timedelta(days=10)
        if ticker not in self._closes or start < self._start[ticker]:
            try:
                raw = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"), auto_adjust=True)["Close"].dropna()
                idx = pd.DatetimeIndex(raw.index)
                if idx.tz is not None:
                    idx = idx.tz_localize(None)
                series = pd.Series(raw.values, index=idx.normalize())
            except Exception as exc:
                print(f"[update_prices] Price history failed for {ticker}: {exc}")
                series = pd.Series(dtype=float)
            self._closes[ticker] = series
            self._start[ticker] = start
        series = self._closes[ticker]
        return series if len(series) else None

    def close_on_or_before(self, ticker: str, day: datetime) -> float | None:
        series = self.closes(ticker, day)
        if series is None:
            return None
        upto = series[series.index <= pd.Timestamp(day.date())]
        return float(upto.iloc[-1]) if len(upto) else None


def parse_buy_zone(buy_zone_text: str | None) -> float | None:
    """Extracts a numeric threshold from free text like '< $135.00' or '< 128.00'."""
    if not buy_zone_text:
        return None
    match = re.search(r"[\d,]+\.?\d*", buy_zone_text.replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def check_buy_zone(book: PriceBook, ticker: str, buy_zone_text: str | None, since: datetime) -> dict | None:
    """
    Checks whether any daily close since `since` fell at/below the parsed buy zone.
    Always returns the lowest close seen since, even if the zone itself was never
    reached, so "how close did it get" is visible too.
    """
    threshold = parse_buy_zone(buy_zone_text)
    if threshold is None:
        return None
    series = book.closes(ticker, since)
    if series is None:
        return None
    closes = series[series.index >= pd.Timestamp(since.date())]
    if closes.empty:
        return None

    reached_at, reached_price = None, None
    below_zone = closes[closes <= threshold]
    if not below_zone.empty:
        reached_at = below_zone.index[0].strftime("%Y-%m-%d")
        reached_price = round(float(below_zone.iloc[0]), 2)

    return {
        "buy_zone_numeric": threshold,
        "buy_zone_reached_at": reached_at,
        "buy_zone_reached_price": reached_price,
        "lowest_price_since_rec": round(float(closes.min()), 2),
        "lowest_price_date": closes.idxmin().strftime("%Y-%m-%d"),
    }


def score_checkpoint(dec: dict, rec_at: datetime, days: int, book: PriceBook) -> dict | None:
    """Return vs the S&P 500 from entry to rec_at + days. None when prices are missing."""
    ticker, action = dec.get("symbol", ""), dec.get("agent_action") or ""
    day = rec_at + timedelta(days=days)

    end_price = book.close_on_or_before(ticker, day)
    spy_end = book.close_on_or_before(BENCHMARK, day)
    entry_price = book.close_on_or_before(ticker, rec_at)
    entry_spy = book.close_on_or_before(BENCHMARK, rec_at)
    if None in (end_price, spy_end, entry_price, entry_spy):
        return None

    filled, filled_from_zone = True, False
    if action in BUY_ZONE_ACTIONS and parse_buy_zone(dec.get("agent_buy_zone")) is not None:
        reached = parse_ts(dec.get("buy_zone_reached_at"))
        if reached and reached <= day:
            zone_price = dec.get("buy_zone_reached_price")
            spy_at_fill = book.close_on_or_before(BENCHMARK, reached)
            if zone_price and spy_at_fill:
                entry_price, entry_spy, filled_from_zone = float(zone_price), spy_at_fill, True
        else:
            filled = False

    stock_return = (end_price / entry_price - 1) * 100
    spy_return = (spy_end / entry_spy - 1) * 100
    excess = stock_return - spy_return

    if action in NEUTRAL_ACTIONS or not filled:
        outcome = "neutral"
    elif action in BEARISH_ACTIONS:
        outcome = "correct" if excess < 0 else "wrong"
    else:  # BUY_BELOW, ADD_ON_DIP, HOLD
        outcome = "correct" if excess > 0 else "wrong"

    return {
        "price": round(end_price, 2),
        "entry_price": round(entry_price, 2),
        "return": round(stock_return, 2),
        "spy_return": round(spy_return, 2),
        "excess": round(excess, 2),
        "outcome": outcome,
        "filled": filled,
        "filled_from_zone": filled_from_zone,
    }


def neutral_note(dec: dict, checkpoint: str, s: dict) -> str:
    comparison = f"dionica {s['return']:+.1f}% vs S&P 500 {s['spy_return']:+.1f}% ({s['excess']:+.1f} pp)"
    if not s["filled"]:
        return (f"Buy zona {dec.get('agent_buy_zone')} nije dosegnuta do {checkpoint} provjere — kupnje ne bi bilo. "
                f"Za informaciju, od preporuke: {comparison}.")
    return f"{dec.get('agent_action')} nije kupnja — ishod je informativan: {comparison}."


def generate_outcome_reasoning(dec: dict, checkpoint: str, s: dict) -> str | None:
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        entry_label = "cijena kad je buy zona dosegnuta" if s["filled_from_zone"] else "cijena kod preporuke"

        prompt = f"""Retrospektivna analiza jedne agent preporuke, {checkpoint} nakon preporuke.

Ticker: {dec.get('symbol')}
Preporučena akcija: {dec.get('agent_action')}
Teza u trenutku preporuke: {(dec.get('agent_thesis') or '')[:400]}
Ulaz ({entry_label}): ${s['entry_price']:.2f}
Cijena na {checkpoint}: ${s['price']:.2f} ({s['return']:+.1f}%)
S&P 500 u istom razdoblju: {s['spy_return']:+.1f}% → razlika {s['excess']:+.1f} pp
Ocjena (u odnosu na S&P 500): {s['outcome'].upper()}

Napiši 2-3 rečenice NA HRVATSKOM: je li teza bila dobra i koji su vjerojatni faktori
(fundamentalni, sektorski, makro) doveli do ovog ishoda u odnosu na tržište. Budi konkretan
i samokritičan ako je ishod pogrešan. Bez uvoda, samo analiza."""

        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None
    except Exception as exc:
        print(f"[update_prices] Reasoning generation failed for {dec.get('symbol')}: {exc}")
        return None


def load_transactions_by_symbol(client) -> dict[str, list[dict]]:
    try:
        rows = client.table("transactions").select("*").execute().data or []
    except Exception as exc:
        print(f"[update_prices] Could not load transactions: {exc}")
        return {}
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row.get("symbol", ""), []).append(row)
    return by_symbol


def load_active_watchlist(client) -> list[dict]:
    try:
        return client.table("watchlist").select("*").eq("status", "ACTIVE").execute().data or []
    except Exception as exc:
        print(f"[update_prices] Could not load watchlist: {exc}")
        return []


def _write(client, table: str, row_id, updates: dict, label: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[update_prices] DRY RUN {table} {label}: {updates}")
        return
    try:
        client.table(table).update(updates).eq("id", row_id).execute()
        print(f"[update_prices] Updated {table} {label}: {list(updates.keys())}")
    except Exception as exc:
        print(f"[update_prices] Update failed for {table} {label} ({row_id}): {exc}")


def update_decision_buy_zones(client, decisions: list[dict], now: datetime, book: PriceBook, dry_run: bool) -> None:
    for dec in decisions:
        if dec.get("agent_action") not in BUY_ZONE_ACTIONS or dec.get("buy_zone_reached_at"):
            continue
        rec_at = parse_ts(dec.get("recommended_at"))
        if not rec_at or (now - rec_at).days > BUY_ZONE_MAX_AGE_DAYS:
            continue

        info = check_buy_zone(book, dec.get("symbol", ""), dec.get("agent_buy_zone"), rec_at)
        if not info:
            continue
        dec.update(info)  # the checkpoint scoring below needs the fill date
        _write(client, "decisions", dec["id"], info, f"{dec.get('symbol')} buy zone", dry_run)


def update_watchlist_buy_zones(client, rows: list[dict], book: PriceBook, dry_run: bool) -> None:
    for row in rows:
        if row.get("action") not in BUY_ZONE_ACTIONS or row.get("buy_zone_reached_at"):
            continue
        suggested_at = parse_ts(row.get("suggested_at"))
        if not suggested_at:
            continue

        info = check_buy_zone(book, row.get("symbol", ""), row.get("buy_zone"), suggested_at)
        if info:
            _write(client, "watchlist", row["id"], info, f"{row.get('symbol')} buy zone", dry_run)


def infer_followed(dec: dict, tx_by_symbol: dict, rec_at: datetime) -> bool:
    """True if a matching BUY (or SELL, for bearish calls) trade was logged after the recommendation."""
    wanted_action = "SELL" if dec.get("agent_action", "") in BEARISH_ACTIONS else "BUY"
    for tx in tx_by_symbol.get(dec.get("symbol", ""), []):
        if tx.get("action") != wanted_action:
            continue
        tx_date = parse_ts(tx.get("trade_date"))
        if tx_date and tx_date >= rec_at:
            return True
    return False


def main(dry_run: bool = False):
    client = get_supabase()
    if not client:
        print("[update_prices] Supabase credentials missing.")
        return
    now = datetime.now(timezone.utc)

    decisions = client.table("decisions").select("*").execute().data or []
    has_v7 = bool(decisions) and V7_MARKER in decisions[0]
    if decisions and not has_v7:
        print("[update_prices] data/schema_v7.sql not applied yet — scoring without the benchmark columns")
    tx_by_symbol = load_transactions_by_symbol(client)
    watchlist_rows = load_active_watchlist(client)

    # Fetch each ticker's history once, from the earliest date anything needs
    book = PriceBook()
    earliest: dict[str, datetime] = {}
    dated = [(d.get("symbol"), parse_ts(d.get("recommended_at"))) for d in decisions]
    dated += [(w.get("symbol"), parse_ts(w.get("suggested_at"))) for w in watchlist_rows]
    for symbol, ts in dated:
        if symbol and ts and (symbol not in earliest or ts < earliest[symbol]):
            earliest[symbol] = ts
    for symbol, since in earliest.items():
        book.closes(symbol, since)
    if earliest:
        book.closes(BENCHMARK, min(earliest.values()))
    print(f"[update_prices] Price history loaded for {len(earliest)} tickers + {BENCHMARK}")

    # Buy zones first: scoring a BUY call needs to know when its zone was hit
    update_decision_buy_zones(client, decisions, now, book, dry_run)
    update_watchlist_buy_zones(client, watchlist_rows, book, dry_run)

    for dec in decisions:
        rec_at = parse_ts(dec.get("recommended_at"))
        ticker = dec.get("symbol", "")
        if not rec_at or not ticker:
            continue
        age_days = (now - rec_at).days
        action = dec.get("agent_action") or ""
        rescore = has_v7 and (dec.get(V7_MARKER) or 0) < SCORING_VERSION

        updates: dict = {}
        all_due_scored = True
        for days, suffix in CHECKPOINTS:
            if age_days < days:
                continue
            if dec.get(f"price_{suffix}") is not None and not rescore:
                continue
            s = score_checkpoint(dec, rec_at, days, book)
            if not s:
                all_due_scored = False
                continue

            updates[f"price_{suffix}"] = s["price"]
            updates[f"outcome_{suffix}"] = s["outcome"]
            if has_v7:
                updates[f"return_{suffix}"] = s["return"]
                updates[f"spy_return_{suffix}"] = s["spy_return"]
                updates[f"excess_return_{suffix}"] = s["excess"]
                if s["filled_from_zone"]:
                    updates["entry_price"] = s["entry_price"]
            if s["outcome"] == "neutral":
                updates[f"outcome_reasoning_{suffix}"] = neutral_note(dec, suffix, s)
            elif not dry_run:
                reasoning = generate_outcome_reasoning(dec, suffix, s)
                if reasoning:
                    updates[f"outcome_reasoning_{suffix}"] = reasoning

        if rescore and all_due_scored:
            updates[V7_MARKER] = SCORING_VERSION

        if action != "HOLD" and dec.get("user_action") in (None, "PENDING") and infer_followed(dec, tx_by_symbol, rec_at):
            updates["user_action"] = "FOLLOWED"
            updates["user_action_note"] = "Auto-detected iz trade loga."

        if updates:
            _write(client, "decisions", dec["id"], updates, f"{ticker} (age {age_days}d)", dry_run)

    print("[update_prices] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score old recommendations and track buy zones")
    parser.add_argument("--dry-run", action="store_true", help="print updates instead of writing; no AI calls")
    main(dry_run=parser.parse_args().dry_run)
