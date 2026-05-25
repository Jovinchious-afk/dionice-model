"""
Autonomous stock discovery module.
Each newsletter run independently samples candidates from two pools:
  - Main universe: ~700 S&P 500 + mid-cap stocks (different each week via seeded random)
  - Hidden gems: ~60 small/micro-cap growth plays typically under $10

No manual watchlist needed — agent picks its own candidates every run.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "stock_universe.json"
GEMS_PATH = DATA_DIR / "hidden_gems.json"

# Sector rotation by weekday: each newsletter run covers different sectors
# weekday() returns 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
SECTOR_ROTATION: dict[int, list[str]] = {
    1: ["technology", "energy", "materials", "real_estate"],                          # Utorak
    3: ["healthcare", "industrials", "consumer", "financials", "utilities", "communication"],  # Četvrtak
    6: [],  # Subota (monthly) — prazna lista = svi sektori
}


def _load_universe() -> dict[str, list[str]]:
    try:
        raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[stock_discovery] Could not load stock_universe.json: {exc}")
        return {}


def _load_gems() -> list[str]:
    try:
        raw = json.loads(GEMS_PATH.read_text(encoding="utf-8"))
        tickers = []
        for key, section in raw.items():
            if key.startswith("_"):
                continue
            if isinstance(section, dict):
                tickers.extend(section.get("tickers", []))
            elif isinstance(section, list):
                tickers.extend(section)
        return list(dict.fromkeys(tickers))  # deduplicate, preserve order
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[stock_discovery] Could not load hidden_gems.json: {exc}")
        return []


def _week_seed(dt: datetime) -> int:
    """
    Deterministic seed based on ISO year+week.
    Same seed within a week → same sample if run twice on the same day.
    Different seed each week → fresh candidates every week.
    Tuesday and Thursday in the same week get different seeds (offset by weekday).
    """
    iso = dt.isocalendar()
    return hash(f"{iso.year}-{iso.week}-{dt.weekday()}")


def sample_main_universe(n: int = 22, dt: datetime | None = None) -> list[str]:
    """
    Returns n tickers sampled from the sectors assigned to today's weekday.
    Utorak  (1): Technology, Energy, Materials, Real Estate
    Četvrtak (3): Healthcare, Industrials, Consumer, Financials, Utilities, Communication
    Ostali dani: svi sektori (manual runs, monthly)

    Same weekday+week seed → reproducible within a run, fresh every newsletter.
    """
    universe = _load_universe()
    if not universe:
        return []

    now = dt or datetime.now(timezone.utc)
    weekday = now.weekday()

    active_sectors = SECTOR_ROTATION.get(weekday)
    if active_sectors is None:
        # Weekday not in rotation map → use all sectors
        active_sectors = list(universe.keys())
    elif len(active_sectors) == 0:
        # Empty list sentinel → all sectors (Saturday monthly run)
        active_sectors = list(universe.keys())

    pool: list[str] = []
    for sector in active_sectors:
        pool.extend(universe.get(sector, []))
    pool = list(dict.fromkeys(pool))  # deduplicate

    if not pool:
        # Fallback: sample from full universe if sector filter yields nothing
        for tickers in universe.values():
            pool.extend(tickers)
        pool = list(dict.fromkeys(pool))

    sector_names = ", ".join(active_sectors) if active_sectors else "all"
    print(f"[stock_discovery] Sectors for weekday {weekday}: {sector_names} ({len(pool)} stocks in pool)")

    seed = _week_seed(now)
    rng = random.Random(seed)
    sample_size = min(n, len(pool))
    return rng.sample(pool, sample_size)


def sample_hidden_gems(n: int = 5, dt: datetime | None = None) -> list[str]:
    """
    Returns n gem tickers sampled from hidden_gems.json.
    Same seeding logic — different picks every newsletter run.
    Price check ($12 cap) happens later in run_weekly.py after fundamentals fetch.
    """
    gems = _load_gems()
    if not gems:
        return []

    seed = _week_seed(dt or datetime.now(timezone.utc)) + 1  # +1 offsets from main
    rng = random.Random(seed)
    sample_size = min(n, len(gems))
    return rng.sample(gems, sample_size)


def select_candidates(
    portfolio_tickers: list[str],
    reddit_tickers: list[str],
    congress_tickers: list[str],
    dt: datetime | None = None,
    max_main: int = 8,
    max_gems: int = 5,
) -> tuple[list[str], list[str]]:
    """
    Builds the final candidate lists for a newsletter run.

    Returns (main_candidates, gem_candidates) as separate lists so
    run_weekly.py can apply different scoring rules and mark gems distinctly.

    main_candidates: portfolio positions (always) + universe sample + top Reddit/Congress
    gem_candidates:  hidden gems sample (price filter applied after fundamentals fetch)

    Total stocks sent to fetch_multiple() = len(main) + len(gems) ≤ max_main + max_gems
    """
    now = dt or datetime.now(timezone.utc)

    # Portfolio tickers always go first — they are never dropped
    main: list[str] = list(dict.fromkeys(portfolio_tickers))

    # Fill remaining main slots from universe sample
    universe_sample = sample_main_universe(n=max_main * 3, dt=now)
    for ticker in universe_sample:
        if ticker not in main:
            main.append(ticker)
        if len(main) >= max_main + len(portfolio_tickers):
            break

    # Append top Reddit/Congress signals (max 2 each) as bonus candidates
    bonus_reddit = [t for t in reddit_tickers if t not in main][:2]
    bonus_congress = [t for t in congress_tickers if t not in main and t not in bonus_reddit][:2]
    for ticker in bonus_reddit + bonus_congress:
        if ticker not in main:
            main.append(ticker)

    # Gem candidates are kept separate
    gems = sample_hidden_gems(n=max_gems, dt=now)

    print(f"[stock_discovery] Main candidates ({len(main)}): {main}")
    print(f"[stock_discovery] Gem candidates ({len(gems)}): {gems}")

    return main, gems


def get_universe_stats() -> dict[str, Any]:
    """Returns size stats for the loaded universe — useful for debugging."""
    universe = _load_universe()
    gems = _load_gems()
    total = sum(len(v) for v in universe.values())
    return {
        "sectors": len(universe),
        "total_main_universe": total,
        "total_hidden_gems": len(gems),
        "sectors_detail": {k: len(v) for k, v in universe.items()},
    }
