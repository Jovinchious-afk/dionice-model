"""
Cycle and season context for a stock, from data/sector_context.json.

Ratios are a snapshot: they cannot tell the model that a generator maker's demand
arrives with the US hurricane season, or that a steelmaker's low P/E is a warning
at the top of the cycle rather than a bargain. That knowledge is written by hand
per yfinance `industry`, with per-ticker overrides where one company's drivers
are specific enough to matter. The computed 10-year seasonality from
fundamentals.py is appended as a deliberately weak signal.
"""

import json
from functools import lru_cache
from pathlib import Path

CONTEXT_PATH = Path(__file__).parent.parent / "data" / "sector_context.json"
CYCLICAL_TYPES = {"cyclical", "commodity", "seasonal_cyclical"}
CYCLICAL_SECTORS = {"energy", "basic materials"}


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sector_context] Could not load {CONTEXT_PATH.name}: {exc}")
        return {}


def get_context(fund: dict) -> dict | None:
    data = _load()
    industry = data.get("industries", {}).get(fund.get("industry") or "")
    ticker = data.get("tickers", {}).get((fund.get("symbol") or "").upper())
    if not industry and not ticker:
        return None
    return {**(industry or {}), **(ticker or {})}


def is_cyclical(fund: dict) -> bool:
    ctx = get_context(fund)
    if ctx and ctx.get("type") in CYCLICAL_TYPES:
        return True
    return (fund.get("sector") or "").strip().lower() in CYCLICAL_SECTORS


def format_seasonality(seas: dict) -> str:
    return (
        f"Povijesna sezonalnost ({seas['window']}, {seas['years']} god.): medijan "
        f"{seas['median_excess_pp']:+.1f} pp vs S&P 500, bolja od indeksa "
        f"{seas['beat_spy_years']}/{seas['years']} god., raspon "
        f"{seas['worst_excess_pp']:+.0f} do {seas['best_excess_pp']:+.0f} pp"
    )


def format_context(fund: dict) -> str | None:
    """Prompt block text, or None when there is nothing to say."""
    lines = []
    ctx = get_context(fund)
    if ctx:
        lines.append(f"Tip: {ctx.get('type', 'n/a')}")
        for key, label in (("drivers", "Pokretači"), ("season", "Sezona"), ("watch", "Prati")):
            if ctx.get(key):
                lines.append(f"{label}: {ctx[key]}")
    seas = fund.get("seasonality")
    if seas:
        lines.append(format_seasonality(seas) + " — slab signal, tržište sezonu već zna")
    return "\n".join(lines) if lines else None
