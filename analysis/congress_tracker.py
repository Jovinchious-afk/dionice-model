"""
Fetches US Congress member stock trades from House and Senate Stock Watcher APIs.
Used as a weak idea-generation signal only — NOT as a buy signal.
Disclosures can lag 30-45 days; amounts are ranges; context is unknown.
"""

import requests
from datetime import datetime, timedelta, timezone
from typing import Any


HOUSE_API = "https://housestockwatcher.com/api/transactions_all"
SENATE_API = "https://senatestockwatcher.com/api/transactions_all"

REQUEST_TIMEOUT = 15


def _fetch_json(url: str) -> list[dict]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        print(f"[congress_tracker] API nedostupan — preskačem ({url.split('/')[2]})")
        return []
    except Exception as exc:
        print(f"[congress_tracker] Fetch failed: {type(exc).__name__}")
        return []


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _normalize_trade_type(raw: str) -> str:
    raw_lower = (raw or "").lower()
    if "sale" in raw_lower or "sell" in raw_lower:
        return "SELL"
    if "purchase" in raw_lower or "buy" in raw_lower:
        return "BUY"
    return "UNKNOWN"


def fetch_house_trades(days_back: int = 14) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    raw = _fetch_json(HOUSE_API)
    trades = []
    for item in raw:
        disclosure_str = item.get("disclosure_date") or item.get("transaction_date", "")
        disclosure_date = _parse_date(disclosure_str)
        if not disclosure_date or disclosure_date < cutoff:
            continue
        ticker = (item.get("ticker") or "").strip().upper()
        if not ticker or ticker in ("N/A", "--", ""):
            continue
        trades.append({
            "chamber": "HOUSE",
            "member": item.get("representative", "Unknown"),
            "ticker": ticker,
            "trade_type": _normalize_trade_type(item.get("type", "")),
            "amount_range": item.get("amount", "Unknown"),
            "disclosure_date": disclosure_date.strftime("%Y-%m-%d"),
            "transaction_date": item.get("transaction_date", ""),
        })
    return trades


def fetch_senate_trades(days_back: int = 14) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    raw = _fetch_json(SENATE_API)
    trades = []
    for item in raw:
        disclosure_str = item.get("disclosure_date") or item.get("transaction_date", "")
        disclosure_date = _parse_date(disclosure_str)
        if not disclosure_date or disclosure_date < cutoff:
            continue
        ticker = (item.get("ticker") or "").strip().upper()
        if not ticker or ticker in ("N/A", "--", ""):
            continue
        trades.append({
            "chamber": "SENATE",
            "member": item.get("senator", "Unknown"),
            "ticker": ticker,
            "trade_type": _normalize_trade_type(item.get("type", "")),
            "amount_range": item.get("amount", "Unknown"),
            "disclosure_date": disclosure_date.strftime("%Y-%m-%d"),
            "transaction_date": item.get("transaction_date", ""),
        })
    return trades


def get_congress_signals(days_back: int = 14) -> dict[str, dict]:
    """
    Returns aggregated congress signals per ticker.
    Signal strength is always 'weak' — it's an idea source, not a buy trigger.

    Returns dict: {ticker: {buy_count, sell_count, members, signal, trades[]}}
    """
    all_trades = fetch_house_trades(days_back) + fetch_senate_trades(days_back)

    aggregated: dict[str, dict] = {}
    for trade in all_trades:
        ticker = trade["ticker"]
        if ticker not in aggregated:
            aggregated[ticker] = {
                "ticker": ticker,
                "buy_count": 0,
                "sell_count": 0,
                "members": [],
                "signal": "weak",  # always weak — see module docstring
                "signal_note": "Congress disclosure lag 30-45 days. Amounts in ranges. Context unknown.",
                "trades": [],
            }
        if trade["trade_type"] == "BUY":
            aggregated[ticker]["buy_count"] += 1
        elif trade["trade_type"] == "SELL":
            aggregated[ticker]["sell_count"] += 1
        member = trade["member"]
        if member not in aggregated[ticker]["members"]:
            aggregated[ticker]["members"].append(member)
        aggregated[ticker]["trades"].append(trade)

    # Add qualitative note for multi-member buys
    for ticker, data in aggregated.items():
        if data["buy_count"] >= 3:
            data["note"] = f"{data['buy_count']} members buying — worth adding to watchlist scan"
        elif data["sell_count"] >= 3:
            data["note"] = f"{data['sell_count']} members selling — potential negative sentiment"
        else:
            data["note"] = "Low member count — treat as background noise only"

    return aggregated


def get_tickers_from_congress(days_back: int = 14, min_members: int = 1) -> list[str]:
    """Returns a list of ticker symbols mentioned in congress trades."""
    signals = get_congress_signals(days_back)
    return [
        ticker for ticker, data in signals.items()
        if len(data["members"]) >= min_members
    ]
