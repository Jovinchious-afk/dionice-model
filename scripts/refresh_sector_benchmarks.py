"""
Quarterly: median P/E and forward P/E per sector and per industry across the
stock universe, written to data/sector_benchmarks.json.

The scorer compares a stock's P/E with these medians — the investor's brief asks
for valuation "compared with the industry", and a fixed P/E of 20 made every
software company look expensive and every bank cheap.

Runs after refresh_universe.py in the quarterly refresh workflow (the diff is
committed, so each quarter's benchmarks stay auditable in git).

A sector or industry the new run could not price keeps last quarter's medians
(marked carried_over_from): on 2026-10-01 Yahoo throttled the first 53 tickers
and the whole Communication Services sector silently fell back to a P/E of 20.
Usage: python scripts/refresh_sector_benchmarks.py [--limit N]
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "stock_universe.json"
OUT_PATH = DATA_DIR / "sector_benchmarks.json"
MIN_PEERS = 5
PE_RANGE = (0.0, 200.0)  # negative and absurd multiples say nothing about peers
DELAY = 0.3
THROTTLE_STREAK = 10     # this many unanswered tickers in a row means Yahoo is throttling
THROTTLE_PAUSE_S = 60


def universe_tickers() -> list[str]:
    raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    tickers = [t for k, v in raw.items() if not k.startswith("_") and isinstance(v, list) for t in v]
    return list(dict.fromkeys(tickers))


def fetch_rows(tickers: list[str]) -> list[dict]:
    rows = []
    streak = 0
    for i, ticker in enumerate(tickers, 1):
        for attempt in range(2):
            try:
                info = yf.Ticker(ticker).info or {}
                break
            except Exception as exc:
                info = {}
                if attempt == 0:
                    time.sleep(5)
                else:
                    print(f"[benchmarks] {ticker}: {exc}")
        if info.get("sector"):
            rows.append({
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
            })
            streak = 0
        else:
            streak += 1
            if streak >= THROTTLE_STREAK:
                print(f"[benchmarks] {streak} tickers in a row unanswered — pausing {THROTTLE_PAUSE_S}s")
                time.sleep(THROTTLE_PAUSE_S)
                streak = 0
        if i % 50 == 0:
            print(f"[benchmarks] {i}/{len(tickers)} fetched")
        time.sleep(DELAY)
    return rows


def carry_over_missing(new: dict, previous: dict) -> list[str]:
    """
    Copies last quarter's entry for every sector or industry the new run is missing,
    marked with the date it was measured, for one quarter only: an entry that was
    already carried over is dropped, so a group that left the universe does not
    live on. Returns the carried-over names.
    """
    since = (previous.get("_meta") or {}).get("refreshed_at") or "previous"
    carried = []
    for level in ("sectors", "industries"):
        for name, entry in (previous.get(level) or {}).items():
            if name not in new.setdefault(level, {}) and not entry.get("carried_over_from"):
                new[level][name] = {**entry, "carried_over_from": since}
                carried.append(name)
        new[level] = dict(sorted(new[level].items()))
    return carried


def _median(values: list) -> float | None:
    clean = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if PE_RANGE[0] < f < PE_RANGE[1]:
            clean.append(f)
    return round(statistics.median(clean), 1) if len(clean) >= MIN_PEERS else None


def build_benchmarks(rows: list[dict]) -> dict:
    groups: dict[str, dict[str, list[dict]]] = {"sectors": {}, "industries": {}}
    for row in rows:
        groups["sectors"].setdefault(row["sector"], []).append(row)
        if row.get("industry"):
            groups["industries"].setdefault(row["industry"], []).append(row)

    out: dict = {}
    for level, by_name in groups.items():
        out[level] = {}
        for name, members in sorted(by_name.items()):
            entry = {
                "n": len(members),
                "pe_median": _median([m["pe"] for m in members]),
                "forward_pe_median": _median([m["forward_pe"] for m in members]),
            }
            if entry["pe_median"] or entry["forward_pe_median"]:
                out[level][name] = entry
    return out


def main(limit: int | None = None):
    tickers = universe_tickers()
    if limit:
        tickers = tickers[:limit]
    print(f"[benchmarks] Fetching P/E for {len(tickers)} universe stocks")
    rows = fetch_rows(tickers)
    if len(rows) < min(200, len(tickers) // 2):
        print(f"[benchmarks] Only {len(rows)} usable rows — keeping the previous file")
        sys.exit(1)

    result = build_benchmarks(rows)
    try:
        previous = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    carried = carry_over_missing(result, previous)
    if carried:
        print(f"::warning::benchmarks kept last quarter's medians for {len(carried)} groups the run could not "
              f"price: {', '.join(carried[:12])}{' …' if len(carried) > 12 else ''}")
    result = {
        "_meta": {
            "description": "Median trailing and forward P/E per yfinance sector and industry across the stock universe; used by analysis/scorer.py",
            "stocks": len(rows),
            "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "min_peers": MIN_PEERS,
        },
        **result,
    }
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[benchmarks] Wrote {OUT_PATH.name}: {len(result['sectors'])} sectors, {len(result['industries'])} industries")
    for name, entry in result["sectors"].items():
        print(f"  {name:24} n={entry['n']:3}  P/E {entry['pe_median']}  fwd {entry['forward_pe_median']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh sector P/E benchmarks")
    parser.add_argument("--limit", type=int, default=None, help="only the first N tickers (testing)")
    main(limit=parser.parse_args().limit)
