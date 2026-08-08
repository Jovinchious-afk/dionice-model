"""
Quarterly refresh of data/hidden_gems.json.

The sixteen themed buckets are hand-curated and stay that way — they carry the
investment theses (quantum, space, gene therapy, ...) and a machine cannot assign
a new ticker to the right theme reliably. So this script does two things instead:

  1. Prunes the themed buckets: drops names that are dead, too expensive, too
     large, illiquid, revenueless, or about to run out of cash.
  2. Fills one extra bucket, "auto_screened", from the official Nasdaq Trader
     symbol directory with fresh names that pass every filter.

Screening runs in widening-cost stages — a free text file, then batch prices,
then fast_info, then full financials — so the expensive call is only ever made
for the few hundred symbols that already passed everything cheaper.
"""

import io
import json
import os
import sys
import time
import warnings
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import requests
import yfinance as yf

from analysis.supabase_client import get_supabase
from analysis.ticker_health import DEAD_THRESHOLD, load_health

DATA_DIR = Path(__file__).parent.parent / "data"
GEMS_PATH = DATA_DIR / "hidden_gems.json"

NASDAQ_FILES = {
    "nasdaqlisted.txt": "Symbol",
    "otherlisted.txt": "ACT Symbol",
}
DIR_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/{}"

PRICE_CAP = 12.0            # the defining rule for a "gem"
MIN_MARKET_CAP = 30_000_000     # below this it is a shell, and hard_exclude drops it anyway
MAX_MARKET_CAP = 2_000_000_000  # above this it is a mid-cap, not a gem
MIN_AVG_VOLUME = 50_000         # must be liquid enough to actually trade
MIN_RUNWAY_MONTHS = 12          # cash / burn — "early but funded" vs "about to die"
AUTO_BUCKET_SIZE = 80

# Warrants, units, rights and preferred lines are not ordinary shares
BAD_SUFFIXES = ("W", "U", "R", "P")


def fetch_symbol_directory() -> list[str]:
    """Official Nasdaq/NYSE/AMEX symbol directory, minus everything untradeable."""
    symbols: set[str] = set()

    for filename, symbol_col in NASDAQ_FILES.items():
        try:
            resp = requests.get(DIR_URL.format(filename), timeout=40)
            resp.raise_for_status()
            # Last line is a "File Creation Time" footer, not data
            body = "\n".join(resp.text.strip().split("\n")[:-1])
            df = pd.read_csv(io.StringIO(body), sep="|")
        except Exception as exc:
            print(f"[refresh_gems] Could not read {filename}: {exc}")
            continue

        before = len(df)
        if "Test Issue" in df.columns:
            df = df[df["Test Issue"] != "Y"]
        if "ETF" in df.columns:
            df = df[df["ETF"] != "Y"]
        # Nasdaq flags deficient (D), delinquent (E) and bankrupt (Q) issuers for us
        if "Financial Status" in df.columns:
            df = df[~df["Financial Status"].isin(["D", "E", "Q"])]

        for raw in df[symbol_col].dropna().astype(str):
            sym = raw.strip().upper()
            if not sym or not sym.isalpha():
                continue          # drops anything with $ . / ^ etc.
            if len(sym) == 5 and sym.endswith(BAD_SUFFIXES):
                continue          # 5-letter lines ending W/U/R/P are not common stock
            symbols.add(sym)

        print(f"[refresh_gems] {filename}: {before} rows -> {len(df)} after flags")

    print(f"[refresh_gems] Directory yields {len(symbols)} candidate symbols")
    return sorted(symbols)


def filter_by_price(symbols: list[str], batch: int = 200) -> list[str]:
    """Stage 2 — one bulk download per batch; cheapest way to apply the price cap."""
    survivors: list[str] = []
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            df = yf.download(chunk, period="5d", progress=False, threads=True,
                             auto_adjust=True, group_by="ticker")
        except Exception:
            continue
        for sym in chunk:
            try:
                closes = df[sym]["Close"].dropna() if len(chunk) > 1 else df["Close"].dropna()
                if len(closes) and 0 < float(closes.iloc[-1]) < PRICE_CAP:
                    survivors.append(sym)
            except Exception:
                continue
        print(f"[refresh_gems]   priced {min(i + batch, len(symbols))}/{len(symbols)}, "
              f"{len(survivors)} under ${PRICE_CAP:.0f}", flush=True)
    return survivors


def filter_by_size_and_liquidity(symbols: list[str]) -> list[str]:
    """Stage 3 — fast_info only; market cap and volume without pulling full .info."""
    survivors = []
    for n, sym in enumerate(symbols, 1):
        try:
            fi = yf.Ticker(sym).fast_info
            mc = getattr(fi, "market_cap", None)
            vol = getattr(fi, "three_month_average_volume", None) or getattr(fi, "last_volume", None)
            if not mc or not vol:
                continue
            if MIN_MARKET_CAP <= mc <= MAX_MARKET_CAP and vol >= MIN_AVG_VOLUME:
                survivors.append(sym)
        except Exception:
            continue
        if n % 100 == 0:
            print(f"[refresh_gems]   sized {n}/{len(symbols)}, {len(survivors)} kept", flush=True)
    return survivors


def has_business_and_runway(sym: str) -> bool:
    """
    Stage 4 — the only stage that pulls full .info.

    Burn rate itself is not disqualifying: gems are classified speculative_growth,
    for which hard_exclude deliberately skips the FCF test because early-stage
    companies are expected to burn cash. What matters is whether they can survive
    long enough to matter, so this checks runway instead.
    """
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        return False

    if not (info.get("totalRevenue") or 0) > 0:
        return False

    fcf = info.get("freeCashflow")
    if fcf is None or fcf >= 0:
        return True   # profitable or cash-generating: nothing to outrun

    cash = info.get("totalCash") or 0
    annual_burn = abs(fcf)
    return (cash / annual_burn) * 12 >= MIN_RUNWAY_MONTHS if annual_burn else True


def main():
    print("[refresh_gems] Starting quarterly hidden-gems refresh")
    raw = json.loads(GEMS_PATH.read_text(encoding="utf-8"))
    health = load_health(get_supabase())
    dead = {s for s, r in health.items() if (r.get("consecutive_failures") or 0) >= DEAD_THRESHOLD}

    themed = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
    curated = {t for v in themed.values() for t in v.get("tickers", [])}
    print(f"[refresh_gems] Curated themes: {len(themed)} buckets, {len(curated)} tickers "
          f"| {len(dead)} known dead")

    symbols = fetch_symbol_directory()
    if len(symbols) < 1000:
        print("[refresh_gems] Directory too small — refusing to rewrite the gems file")
        sys.exit(1)

    # Screen the curated names and the wider market in one pass, so pruning and
    # top-up apply exactly the same bar.
    universe = sorted(set(symbols) | curated)
    print(f"[refresh_gems] Stage 2/4: pricing {len(universe)} symbols...")
    cheap = [s for s in filter_by_price(universe) if s not in dead]
    print(f"[refresh_gems] Stage 3/4: sizing {len(cheap)} symbols under ${PRICE_CAP:.0f}...")
    right_sized = filter_by_size_and_liquidity(cheap)
    print(f"[refresh_gems] Stage 4/4: checking business + runway for {len(right_sized)}...")

    passed = []
    for n, sym in enumerate(right_sized, 1):
        if has_business_and_runway(sym):
            passed.append(sym)
        if n % 50 == 0:
            print(f"[refresh_gems]   checked {n}/{len(right_sized)}, {len(passed)} passed", flush=True)
        time.sleep(0.1)
    passed_set = set(passed)
    print(f"[refresh_gems] {len(passed)} symbols pass every filter")

    # 1. Prune the curated themes in place
    out, pruned_total = {}, 0
    for key, bucket in raw.items():
        if key.startswith("_") or not isinstance(bucket, dict):
            out[key] = bucket
            continue
        keep = [t for t in bucket.get("tickers", []) if t in passed_set]
        pruned_total += len(bucket.get("tickers", [])) - len(keep)
        out[key] = {**bucket, "tickers": keep}

    # 2. Refill the auto bucket with names no theme already claims
    fresh = [s for s in passed if s not in curated][:AUTO_BUCKET_SIZE]
    out["auto_screened"] = {
        "description": (
            f"Auto-screened each quarter from the Nasdaq Trader symbol directory: "
            f"price < ${PRICE_CAP:.0f}, market cap ${MIN_MARKET_CAP/1e6:.0f}M-${MAX_MARKET_CAP/1e9:.0f}B, "
            f"volume > {MIN_AVG_VOLUME:,}/day, revenue > 0, cash runway > {MIN_RUNWAY_MONTHS} months. "
            f"Not theme-classified — the curated buckets above are hand-picked."
        ),
        "tickers": fresh,
    }

    kept = sum(len(v["tickers"]) for k, v in out.items() if not k.startswith("_"))
    out["_meta"] = {
        **(raw.get("_meta") or {}),
        "total": kept,
        "refreshed_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
        "auto_screened_count": len(fresh),
    }
    GEMS_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[refresh_gems] Pruned {pruned_total} from curated themes, "
          f"added {len(fresh)} auto-screened -> {kept} total")


if __name__ == "__main__":
    main()
