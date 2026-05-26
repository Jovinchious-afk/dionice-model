"""
StockTwits public sentiment API — no authentication required.
Replaces Reddit (which blocks GitHub Actions with 403).
Returns bullish/bearish ratio and hype score per ticker.
"""

import time
import requests


STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
REQUEST_TIMEOUT = 8
DEFAULT_DELAY = 0.5  # seconds between requests to avoid rate limiting


def get_stocktwits_sentiment(ticker: str) -> dict | None:
    """
    Fetches StockTwits stream for a ticker and returns sentiment summary.

    Returns:
        {
          "ticker": str,
          "message_count": int,     # messages in the stream (last ~30)
          "bullish_pct": float,     # % of tagged messages marked Bullish
          "bearish_pct": float,     # % of tagged messages marked Bearish
          "hype_score": int,        # 1-10 (higher = more buzz; >50 msgs = high)
          "bull_bear_summary": str, # ready-to-use sentence for Claude prompt
        }
    or None if unavailable / rate-limited.
    """
    try:
        url = STOCKTWITS_URL.format(ticker=ticker)
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "dionice-model/1.0"})

        if resp.status_code == 404:
            return None  # ticker not on StockTwits
        if resp.status_code == 429:
            print(f"[sentiment] Rate limited for {ticker} — skipping")
            return None
        if resp.status_code != 200:
            return None

        data = resp.json()
        messages = data.get("messages", [])
        total = len(messages)

        bullish = sum(
            1 for m in messages
            if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bullish"
        )
        bearish = sum(
            1 for m in messages
            if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bearish"
        )

        bull_pct = round(bullish / total * 100, 1) if total else 0.0
        bear_pct = round(bearish / total * 100, 1) if total else 0.0

        # Hype score 1-10: 5 msgs = 1, 50 msgs = 10 (capped)
        hype = min(10, max(1, round(total / 5)))

        summary = (
            f"StockTwits ({total} poruka): "
            f"{bull_pct}% bullish, {bear_pct}% bearish. "
            f"Hype razina: {hype}/10."
        )
        if hype >= 7:
            summary += " ⚠️ Visok buzz — zahtijeva jače fundamentale za BUY preporuku."

        return {
            "ticker": ticker,
            "message_count": total,
            "bullish_pct": bull_pct,
            "bearish_pct": bear_pct,
            "hype_score": hype,
            "bull_bear_summary": summary,
        }

    except requests.exceptions.Timeout:
        print(f"[sentiment] Timeout for {ticker}")
        return None
    except Exception as exc:
        print(f"[sentiment] Failed for {ticker}: {type(exc).__name__}")
        return None


def get_sentiment_batch(tickers: list[str], delay: float = DEFAULT_DELAY) -> dict[str, dict]:
    """
    Fetches StockTwits sentiment for a list of tickers with a polite delay.
    Returns dict keyed by ticker symbol (only successful results).
    """
    results: dict[str, dict] = {}
    for ticker in tickers:
        result = get_stocktwits_sentiment(ticker)
        if result:
            results[ticker] = result
        time.sleep(delay)
    print(f"[sentiment] Got sentiment for {len(results)}/{len(tickers)} tickers")
    return results
