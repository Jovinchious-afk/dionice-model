"""
Tracks per-ticker fetch health so the stock universe cleans itself.

Only production evidence counts. A local yfinance sweep reported live blue chips
(BK, MMC, CTRA, HOLX) as delisted, so "is this ticker dead" is decided solely by
what the GitHub Actions run actually observes, accumulated over several runs.

A single failure is usually a transient Yahoo hiccup or a rate limit, so a ticker
is only retired after DEAD_THRESHOLD consecutive failures, and one successful
fetch brings it straight back.
"""

from datetime import datetime, timezone

DEAD_THRESHOLD = 3
TABLE = "ticker_health"


def load_health(client) -> dict[str, dict]:
    """Returns the full health table keyed by symbol. Empty dict on any failure."""
    if not client:
        return {}
    try:
        result = client.table(TABLE).select("*").execute()
        return {row["symbol"]: row for row in (result.data or [])}
    except Exception as exc:
        print(f"[ticker_health] Could not load health table: {exc}")
        return {}


def get_dead_tickers(client, health: dict[str, dict] | None = None) -> set[str]:
    """Symbols that have failed DEAD_THRESHOLD times in a row and should be skipped."""
    health = load_health(client) if health is None else health
    return {
        sym for sym, row in health.items()
        if (row.get("consecutive_failures") or 0) >= DEAD_THRESHOLD
    }


def record_run(
    client,
    fundamentals_map: dict[str, dict],
    actioned_symbols: list[str] | None = None,
    health: dict[str, dict] | None = None,
) -> list[str]:
    """
    Writes one run's outcome for every fetched ticker in a single upsert.

    Fetch results and actioned counts are folded together deliberately: upsert
    replaces whole rows, so writing them in two passes would let the second write
    clobber the counters raised by the first.

    Returns the symbols that crossed DEAD_THRESHOLD on this run.
    """
    if not client or not fundamentals_map:
        return []

    health = load_health(client) if health is None else health
    actioned = set(actioned_symbols or [])
    now = datetime.now(timezone.utc).isoformat()
    rows, newly_retired = [], []

    for symbol, fund in fundamentals_map.items():
        prior = health.get(symbol, {})
        prior_failures = prior.get("consecutive_failures") or 0
        error = fund.get("fetch_error")

        if error:
            failures = prior_failures + 1
            if prior_failures < DEAD_THRESHOLD <= failures:
                newly_retired.append(symbol)
            rows.append({
                "symbol": symbol,
                "consecutive_failures": failures,
                "last_error": str(error)[:300],
                "last_ok_at": prior.get("last_ok_at"),
                "last_checked_at": now,
                "times_analyzed": prior.get("times_analyzed") or 0,
                "times_actioned": prior.get("times_actioned") or 0,
            })
        else:
            rows.append({
                "symbol": symbol,
                "consecutive_failures": 0,
                "last_error": None,
                "last_ok_at": now,
                "last_checked_at": now,
                "times_analyzed": (prior.get("times_analyzed") or 0) + 1,
                "times_actioned": (prior.get("times_actioned") or 0) + (1 if symbol in actioned else 0),
            })

    try:
        client.table(TABLE).upsert(rows).execute()
        print(f"[ticker_health] Recorded {len(rows)} tickers ({len(actioned)} actioned)")
    except Exception as exc:
        print(f"[ticker_health] Could not write health rows: {exc}")
        return []

    if newly_retired:
        print(f"[ticker_health] Retired after {DEAD_THRESHOLD} consecutive failures: {newly_retired}")
    return newly_retired
