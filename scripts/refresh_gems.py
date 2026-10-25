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

The 2026-10-01 run wrote an empty pool: Yahoo stopped answering the GitHub
runner mid-screen, every failed request was skipped as "not a gem", and seven
newsletters went out without a single gem. A screen that loses most of Yahoo's
answers, or a pool far smaller than last quarter's, now keeps the old file and
leaves a ::warning:: in the Actions log instead.
"""

import io
import json
import os
import random
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
MIN_POOL = 40                   # fewer gems than this after a refresh means the screen failed
MIN_POOL_SHARE = 0.25           # so does keeping under a quarter of last quarter's pool
MAX_FAILED_SHARE = 0.5          # a stage that loses more than this share of Yahoo's answers is aborted
# Yahoo throttles after the ~30 bulk downloads of stage 2: on 2026-10-25 every one
# of the 2,472 per-symbol calls that followed failed, and a minute later they worked
# again. A run of failures is therefore a reason to pause, not a verdict on the symbols.
THROTTLE_STREAK = 5
THROTTLE_PAUSE_S = 90
MAX_PAUSES = 12

# Warrants, units, rights and preferred lines are not ordinary shares
BAD_SUFFIXES = ("W", "U", "R", "P")


class ScreeningFailed(Exception):
    """Yahoo stopped answering mid-screen (rate limit); last quarter's pool stays."""


def pool_size(raw: dict) -> int:
    return sum(len(v.get("tickers", [])) for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict))


def pick_auto_bucket(candidates: list[str], seed: str, size: int = AUTO_BUCKET_SIZE) -> list[str]:
    """
    A random sample of the names that passed every filter, sorted for a readable diff.
    Taking the first `size` of an alphabetical list (as before 2026-10-25) filled the
    bucket with tickers starting with A only.
    """
    return sorted(random.Random(seed).sample(candidates, min(size, len(candidates))))


def pool_is_plausible(new_total: int, previous_total: int) -> bool:
    """A refreshed pool replaces the old one only when it is not suspiciously small."""
    if new_total < MIN_POOL:
        return False
    return previous_total == 0 or new_total >= MIN_POOL_SHARE * previous_total


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


def _closes_and_volumes(chunk: list[str]) -> dict[str, tuple[float, float]] | None:
    """(last close, average daily volume) per symbol of one bulk download; None when the batch came back empty."""
    try:
        df = yf.download(chunk, period="1mo", progress=False, threads=True,
                         auto_adjust=True, group_by="ticker")
    except Exception:
        return None
    out: dict[str, tuple[float, float]] = {}
    for sym in chunk:
        try:
            frame = df[sym] if len(chunk) > 1 else df
            closes, volumes = frame["Close"].dropna(), frame["Volume"].dropna()
        except Exception:
            continue
        if len(closes):
            out[sym] = (float(closes.iloc[-1]), float(volumes.mean()) if len(volumes) else 0.0)
    return out or None


def filter_by_price(symbols: list[str], batch: int = 200) -> list[str]:
    """
    Stage 2 — one bulk download per batch applies the price cap and the volume floor
    together. Volume used to come from a fast_info call per symbol, the stage Yahoo
    throttled; the month of daily volumes in the same download answers it for free.
    """
    survivors: list[str] = []
    batches = failed = 0
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        batches += 1
        rows = _closes_and_volumes(chunk)
        if rows is None:
            time.sleep(20)  # a throttled batch usually answers after a pause
            rows = _closes_and_volumes(chunk)
        if rows is None:
            failed += 1
            print(f"[refresh_gems]   batch {batches} returned no prices", flush=True)
            continue
        survivors.extend(sym for sym, (close, volume) in rows.items()
                         if 0 < close < PRICE_CAP and volume >= MIN_AVG_VOLUME)
        print(f"[refresh_gems]   priced {min(i + batch, len(symbols))}/{len(symbols)}, "
              f"{len(survivors)} under ${PRICE_CAP:.0f} and liquid", flush=True)
        time.sleep(0.5)
    if batches and failed / batches > MAX_FAILED_SHARE:
        raise ScreeningFailed(f"stage 2: {failed} of {batches} price batches returned nothing")
    return survivors


def fetch_with_pauses(fetch, symbols: list[str], label: str, pause_s: float = THROTTLE_PAUSE_S):
    """
    Yields (symbol, fetch(symbol) or None). THROTTLE_STREAK failures in a row mean
    Yahoo is throttling, not that the symbols are bad: pause and retry the symbol
    that hit the limit. Raises ScreeningFailed after MAX_PAUSES pauses, or when most
    symbols got no answer at all.
    """
    streak = pauses = unanswered = 0
    for sym in symbols:
        while True:
            try:
                result = fetch(sym)
            except Exception:
                streak += 1
                if streak < THROTTLE_STREAK:
                    unanswered += 1
                    yield sym, None
                    break
                pauses += 1
                if pauses > MAX_PAUSES:
                    raise ScreeningFailed(f"{label}: Yahoo still refusing after {MAX_PAUSES} pauses")
                print(f"[refresh_gems]   {streak} failures in a row — Yahoo is throttling, pausing {pause_s:.0f}s",
                      flush=True)
                time.sleep(pause_s)
                streak = 0
                continue
            streak = 0
            yield sym, result
            break
    if symbols and unanswered / len(symbols) > MAX_FAILED_SHARE:
        raise ScreeningFailed(f"{label}: no answer for {unanswered} of {len(symbols)} symbols")


def _market_cap(sym: str) -> float | None:
    return getattr(yf.Ticker(sym).fast_info, "market_cap", None)


def filter_by_size(symbols: list[str]) -> list[str]:
    """Stage 3 — market cap from fast_info, the cheapest per-symbol call."""
    survivors = []
    for n, (sym, market_cap) in enumerate(fetch_with_pauses(_market_cap, symbols, "stage 3"), 1):
        if market_cap and MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP:
            survivors.append(sym)
        if n % 100 == 0:
            print(f"[refresh_gems]   sized {n}/{len(symbols)}, {len(survivors)} kept", flush=True)
        time.sleep(0.1)
    return survivors


def has_business_and_runway(info: dict | None) -> bool | None:
    """
    Stage 4 verdict from full .info. None means Yahoo did not answer.

    Burn rate itself is not disqualifying: gems are classified speculative_growth,
    for which hard_exclude deliberately skips the FCF test because early-stage
    companies are expected to burn cash. What matters is whether they can survive
    long enough to matter, so this checks runway instead.
    """
    if not info:
        return None

    if not (info.get("totalRevenue") or 0) > 0:
        return False

    fcf = info.get("freeCashflow")
    if fcf is None or fcf >= 0:
        return True   # profitable or cash-generating: nothing to outrun

    cash = info.get("totalCash") or 0
    annual_burn = abs(fcf)
    return (cash / annual_burn) * 12 >= MIN_RUNWAY_MONTHS if annual_burn else True


def _info(sym: str) -> dict:
    info = yf.Ticker(sym).info or {}
    if not info:
        raise ValueError("empty .info")  # an empty answer counts toward the throttling streak
    return info


def screen(universe: list[str], dead: set[str]) -> list[str]:
    """Stages 2-4; raises ScreeningFailed when Yahoo stops answering."""
    print(f"[refresh_gems] Stage 2/4: pricing {len(universe)} symbols...")
    cheap = [s for s in filter_by_price(universe) if s not in dead]
    print(f"[refresh_gems] Stage 3/4: sizing {len(cheap)} liquid symbols under ${PRICE_CAP:.0f}...")
    right_sized = filter_by_size(cheap)
    print(f"[refresh_gems] Stage 4/4: checking business + runway for {len(right_sized)}...")

    passed = []
    for n, (sym, info) in enumerate(fetch_with_pauses(_info, right_sized, "stage 4"), 1):
        if has_business_and_runway(info):
            passed.append(sym)
        if n % 50 == 0:
            print(f"[refresh_gems]   checked {n}/{len(right_sized)}, {len(passed)} passed", flush=True)
        time.sleep(0.1)
    return passed


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
    try:
        passed = screen(universe, dead)
    except ScreeningFailed as exc:
        print(f"::warning::refresh_gems kept last quarter's pool ({pool_size(raw)} names) — {exc}")
        return
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
    fresh = pick_auto_bucket([s for s in passed if s not in curated], pd.Timestamp.utcnow().strftime("%Y-%m-%d"))
    out["auto_screened"] = {
        "description": (
            f"Auto-screened each quarter from the Nasdaq Trader symbol directory: "
            f"price < ${PRICE_CAP:.0f}, market cap ${MIN_MARKET_CAP/1e6:.0f}M-${MAX_MARKET_CAP/1e9:.0f}B, "
            f"volume > {MIN_AVG_VOLUME:,}/day, revenue > 0, cash runway > {MIN_RUNWAY_MONTHS} months. "
            f"Not theme-classified — the curated buckets above are hand-picked."
        ),
        "tickers": fresh,
    }

    kept = pool_size(out)
    if not pool_is_plausible(kept, pool_size(raw)):
        print(f"::warning::refresh_gems kept last quarter's pool — the new one would hold only {kept} names "
              f"(was {pool_size(raw)})")
        return
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
