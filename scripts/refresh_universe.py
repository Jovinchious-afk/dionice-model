"""
Quarterly refresh of data/stock_universe.json from the S&P Composite 1500.

Why an index rather than a hand-kept list: the index committee already removes
merged, acquired and delisted companies and adds new ones, so membership stays
current without anyone maintaining it. The pool is deliberately held at
TARGET_PER_SECTOR * 10 rather than taking all ~1500 names, because a bigger pool
would dilute how often any single stock actually gets sampled.

Each run keeps healthy current members, drops the dead and the departed, and
tops each sector back up with index names that have never been in the pool.
Pinned tickers (data/pinned_tickers.json) never leave.
"""

import io
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import requests

from analysis.supabase_client import get_supabase
from analysis.ticker_health import DEAD_THRESHOLD, load_health

DATA_DIR = Path(__file__).parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "stock_universe.json"
PINNED_PATH = DATA_DIR / "pinned_tickers.json"

TARGET_PER_SECTOR = 65  # x10 sectors = 650 stocks

WIKI_SOURCES = [
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
]

# GICS names -> our sector keys. Both consumer GICS sectors fold into one bucket
# because the weekday rotation is built around these ten keys.
GICS_MAP = {
    "information technology": "technology",
    "energy": "energy",
    "health care": "healthcare",
    "industrials": "industrials",
    "consumer discretionary": "consumer",
    "consumer staples": "consumer",
    "financials": "financials",
    "materials": "materials",
    "real estate": "real_estate",
    "utilities": "utilities",
    "communication services": "communication",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (dionice-model universe refresh)"}


def fetch_index_constituents() -> dict[str, list[str]]:
    """Returns {our_sector_key: [tickers]} from the three S&P index pages."""
    by_sector: dict[str, list[str]] = {v: [] for v in set(GICS_MAP.values())}
    total = 0

    for url in WIKI_SOURCES:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
        except Exception as exc:
            print(f"[refresh_universe] Could not read {url.rsplit('/', 1)[-1]}: {exc}")
            continue

        table = next((t for t in tables if "Symbol" in t.columns), None)
        if table is None:
            print(f"[refresh_universe] No constituent table found at {url.rsplit('/', 1)[-1]}")
            continue

        sector_col = next((c for c in table.columns if "Sector" in str(c)), None)
        if sector_col is None:
            print(f"[refresh_universe] No GICS sector column at {url.rsplit('/', 1)[-1]}")
            continue

        for symbol, gics in zip(table["Symbol"], table[sector_col]):
            key = GICS_MAP.get(str(gics).strip().lower())
            if not key:
                continue
            # yfinance uses a dash where the indices print a dot (BRK.B -> BRK-B)
            ticker = str(symbol).strip().upper().replace(".", "-")
            if ticker and ticker not in by_sector[key]:
                by_sector[key].append(ticker)
                total += 1

    print(f"[refresh_universe] Index constituents mapped: {total} across {len(by_sector)} sectors")
    return by_sector


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build_new_universe(
    index_by_sector: dict[str, list[str]],
    current: dict[str, list[str]],
    health: dict[str, dict],
    pinned: set[str],
) -> tuple[dict[str, list[str]], dict]:
    """
    Per sector: keep healthy current members that are still in the index, then top
    up from unused index names. When a sector is over quota, the members dropped
    first are those analysed most often without ever producing an actionable call.
    """
    dead = {s for s, r in health.items() if (r.get("consecutive_failures") or 0) >= DEAD_THRESHOLD}
    ever_used = {t for tickers in current.values() for t in tickers}

    def barrenness(ticker: str) -> tuple[int, int]:
        row = health.get(ticker, {})
        analyzed = row.get("times_analyzed") or 0
        actioned = row.get("times_actioned") or 0
        # Sort key: never-actioned-but-often-analysed goes first
        return (-(analyzed - actioned * 5), -analyzed)

    new: dict[str, list[str]] = {}
    stats = {"kept": 0, "added": 0, "dropped_dead": 0, "dropped_left_index": 0, "dropped_quota": 0}

    for sector, index_tickers in sorted(index_by_sector.items()):
        index_set = set(index_tickers)
        existing = current.get(sector, [])

        keepers, pinned_keepers = [], []
        for t in existing:
            if t in pinned:
                pinned_keepers.append(t)
                continue
            if t in dead:
                stats["dropped_dead"] += 1
                continue
            if t not in index_set:
                stats["dropped_left_index"] += 1
                continue
            keepers.append(t)

        # Trim the least productive keepers if the sector is already over quota
        room = TARGET_PER_SECTOR - len(pinned_keepers)
        if len(keepers) > room:
            keepers.sort(key=barrenness)
            stats["dropped_quota"] += len(keepers) - max(room, 0)
            keepers = keepers[: max(room, 0)]

        selected = pinned_keepers + keepers
        stats["kept"] += len(selected)

        # Top up with index names that have never been in the pool
        for t in index_tickers:
            if len(selected) >= TARGET_PER_SECTOR:
                break
            if t not in selected and t not in ever_used and t not in dead:
                selected.append(t)
                stats["added"] += 1

        # Still short (small sector): allow previously-used index names back in
        for t in index_tickers:
            if len(selected) >= TARGET_PER_SECTOR:
                break
            if t not in selected and t not in dead:
                selected.append(t)
                stats["added"] += 1

        new[sector] = selected

    # A pinned ticker outside the index (e.g. a portfolio position) has nothing to
    # "keep", so place it explicitly rather than letting it fall out of the pool.
    placed = {t for tickers in new.values() for t in tickers}
    for ticker in sorted(pinned - placed):
        sector = _sector_for(ticker, current)
        if sector and sector in new:
            new[sector].append(ticker)   # deliberately allowed to exceed the quota
            stats["pinned_forced"] = stats.get("pinned_forced", 0) + 1
        else:
            print(f"[refresh_universe] Pinned {ticker}: no sector resolved, left out of the pool")

    return new, stats


def _sector_for(ticker: str, current: dict[str, list[str]]) -> str | None:
    """Sector it already sat in, else ask yfinance. Called for a handful of pins only."""
    for sector, tickers in current.items():
        if ticker in tickers:
            return sector
    try:
        import yfinance as yf
        gics = (yf.Ticker(ticker).info or {}).get("sector") or ""
        return GICS_MAP.get(gics.strip().lower())
    except Exception:
        return None


def main():
    print("[refresh_universe] Starting quarterly universe refresh")

    index_by_sector = fetch_index_constituents()
    if sum(len(v) for v in index_by_sector.values()) < 500:
        print("[refresh_universe] Too few constituents fetched — refusing to overwrite the universe")
        sys.exit(1)

    raw_current = load_json(UNIVERSE_PATH, {})
    current = {k: v for k, v in raw_current.items() if not k.startswith("_") and isinstance(v, list)}
    meta = {k: v for k, v in raw_current.items() if k.startswith("_")}

    pinned = set(load_json(PINNED_PATH, {}).get("tickers", []))
    health = load_health(get_supabase())
    print(f"[refresh_universe] Current pool: {sum(len(v) for v in current.values())} | "
          f"health rows: {len(health)} | pinned: {len(pinned)}")

    new, stats = build_new_universe(index_by_sector, current, health, pinned)

    before = {t for v in current.values() for t in v}
    after = {t for v in new.values() for t in v}
    print(f"[refresh_universe] New pool: {len(after)} stocks")
    print(f"[refresh_universe] kept={stats['kept']} added={stats['added']} "
          f"dropped(dead)={stats['dropped_dead']} dropped(left index)={stats['dropped_left_index']} "
          f"dropped(quota)={stats['dropped_quota']}")
    entered, left = sorted(after - before), sorted(before - after)
    print(f"[refresh_universe] Entered ({len(entered)}): {entered[:25]}{' ...' if len(entered) > 25 else ''}")
    print(f"[refresh_universe] Left ({len(left)}): {left[:25]}{' ...' if len(left) > 25 else ''}")

    meta["_meta"] = {
        **(meta.get("_meta") or {}),
        "source": "S&P Composite 1500 (Wikipedia constituent tables)",
        "target_per_sector": TARGET_PER_SECTOR,
        "total": len(after),
        "refreshed_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
    }
    UNIVERSE_PATH.write_text(
        json.dumps({**meta, **new}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[refresh_universe] Wrote {UNIVERSE_PATH.name}")


if __name__ == "__main__":
    main()
