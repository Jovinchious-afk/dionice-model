import os
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.params import DEFAULTS  # noqa: E402
from analysis.prices import PriceBook  # noqa: E402


@pytest.fixture
def params():
    return dict(DEFAULTS)


@pytest.fixture
def fund():
    """A plain, healthy mid-cap as yfinance-derived fundamentals."""
    return {
        "symbol": "TEST", "name": "Test Corp", "sector": "Industrials", "industry": "Specialty Industrial Machinery",
        "current_price": 100.0, "market_cap": 20e9, "pe": 18.0, "forward_pe": 16.0, "peg": 1.1,
        "fcf_yield": 5.5, "revenue_growth_yoy": 0.07, "debt_equity": 45.0, "debt_to_equity_x": 0.45,
        "roe": 0.18, "dividend_yield": 0.015, "payout_ratio": 0.3, "insider_buys_24m": 1,
        "insider_unclassified_24m": 0, "insider_ownership": 0.02, "op_margin": 0.16, "net_margin": 0.11,
        "op_margin_declining_3q": False, "op_margin_quarters_pct": [16.0, 15.8, 15.5, 15.0],
        "inventory_growth_yoy": 0.03, "quarterly_revenue_growth_yoy": 0.06,
    }


def make_series(start: str, prices: list[float]) -> pd.Series:
    idx = pd.bdate_range(start=start, periods=len(prices))
    return pd.Series(prices, index=idx)


@pytest.fixture
def book_factory():
    """PriceBook preloaded with synthetic series, so no test touches the network."""
    def build(series_by_ticker: dict[str, pd.Series]) -> PriceBook:
        book = PriceBook()
        for ticker, series in series_by_ticker.items():
            book.set_series(ticker, series, datetime(2000, 1, 1, tzinfo=timezone.utc))
        return book
    return build
