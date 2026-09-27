"""
StockTwits public sentiment API — no authentication required.
Sole source of the hype signal: hype_score >= the hype threshold blocks BUY in ai_analyst.py.

The stream endpoint returns at most 30 messages, so the old score (message count / 5)
was 6 for every stock with any activity and the BUY block never fired. Hype is now
measured as message velocity — messages per day over the span the last 30 messages
cover — relative to company size: a mega-cap naturally draws more chatter than a
$1B company, and the investor's rule is aimed at retail frenzies, not at size.
Measured 2026-09-27: boring mid-caps (XYL, HIG, PTC) run 0.2-1 messages/day and
score 1-3; mega-caps (NVDA, META) 45-85/day but score 4-5; meme and speculative
small caps (GME, AMC, QUBT, BBAI, RGTI) 15-65/day and score 7-10.
"""

import math
import time
from datetime import datetime, timezone

import requests

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
REQUEST_TIMEOUT = 8
DEFAULT_DELAY = 0.5  # seconds between requests to avoid rate limiting
MIN_SPAN_HOURS = 1.0
DEFAULT_MARKET_CAP = 10e9  # used when the market cap is unknown


def messages_per_day(created_at: list[str], now: datetime | None = None) -> float:
    """Messages per day over the span from the oldest message in the stream to now."""
    stamps = []
    for raw in created_at:
        try:
            stamps.append(datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except (TypeError, ValueError):
            continue
    if not stamps:
        return 0.0
    now = now or datetime.now(timezone.utc)
    span_hours = max((now - min(stamps)).total_seconds() / 3600, MIN_SPAN_HOURS)
    return len(stamps) / (span_hours / 24)


def hype_score(msgs_per_day: float | None, market_cap: float | None) -> int:
    """1-10: message velocity per square root of market cap (in $B), on a log scale."""
    if not msgs_per_day or msgs_per_day <= 0:
        return 1
    cap_billions = (market_cap or DEFAULT_MARKET_CAP) / 1e9
    ratio = msgs_per_day / math.sqrt(max(cap_billions, 0.05))
    return int(min(10, max(1, round(4 + 3.5 * math.log10(ratio)))))


def summarize(signal: dict) -> str:
    text = (
        f"StockTwits ({signal['message_count']} poruka, ~{signal['msgs_per_day']:.1f}/dan): "
        f"{signal['bullish_pct']}% bullish, {signal['bearish_pct']}% bearish. "
        f"Hype razina: {signal['hype_score']}/10 (brzina poruka u odnosu na veličinu firme)."
    )
    if signal["hype_score"] >= 7:
        text += " ⚠️ Visok buzz za firmu te veličine — zahtijeva jače fundamentale za BUY preporuku."
    return text


def apply_market_cap(signal: dict, market_cap: float | None) -> dict:
    """Recomputes the hype score once the market cap is known (fundamentals come later in the run)."""
    signal["hype_score"] = hype_score(signal.get("msgs_per_day"), market_cap)
    signal["bull_bear_summary"] = summarize(signal)
    return signal


def get_stocktwits_sentiment(ticker: str) -> dict | None:
    """
    Fetches the StockTwits stream for a ticker and returns a sentiment summary:
    message_count, msgs_per_day, bullish_pct, bearish_pct, watchlist_count,
    hype_score (provisional until apply_market_cap) and bull_bear_summary.
    None if unavailable / rate-limited.
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

        signal = {
            "ticker": ticker,
            "message_count": total,
            "msgs_per_day": round(messages_per_day([m.get("created_at") for m in messages]), 2),
            "bullish_pct": round(bullish / total * 100, 1) if total else 0.0,
            "bearish_pct": round(bearish / total * 100, 1) if total else 0.0,
            "watchlist_count": (data.get("symbol") or {}).get("watchlist_count"),
        }
        return apply_market_cap(signal, None)

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
