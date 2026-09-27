"""
Daily adjusted closes per ticker, fetched once per job from the earliest date any
caller needs — the scoring job and the learning report ask for the same tickers
dozens of times per run.
"""

import re
from datetime import datetime

import pandas as pd
import yfinance as yf

BENCHMARK = "SPY"


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


class PriceBook:
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
                print(f"[prices] Price history failed for {ticker}: {exc}")
                series = pd.Series(dtype=float)
            self._closes[ticker] = series
            self._start[ticker] = start
        series = self._closes[ticker]
        return series if len(series) else None

    def set_series(self, ticker: str, series: pd.Series, since: datetime) -> None:
        """Injects a series (tests, or prices already downloaded elsewhere)."""
        self._closes[ticker] = series
        self._start[ticker] = pd.Timestamp(since.date()) - pd.Timedelta(days=10)

    def close_on_or_before(self, ticker: str, day: datetime) -> float | None:
        series = self.closes(ticker, day)
        if series is None:
            return None
        upto = series[series.index <= pd.Timestamp(day.date())]
        return float(upto.iloc[-1]) if len(upto) else None

    def last_date(self, ticker: str, since: datetime) -> pd.Timestamp | None:
        series = self.closes(ticker, since)
        return series.index[-1] if series is not None else None
