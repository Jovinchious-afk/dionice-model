"""
Fetches SEC Form 4 insider trading filings via edgartools — open-market
buys/sells only (transaction code P/S). Disclosure lag is ~2 business days,
far fresher than Congress trades (30-45 days).

Codes other than P (open-market purchase) and S (open-market sale) — A
(grant/award), M (option exercise), F (tax withholding), G (gift) — are
noise, not market conviction, and are ignored.
"""

import os
import time
from datetime import datetime, timedelta, timezone

from edgar import Company, set_identity

DEFAULT_DELAY = 0.3  # seconds between tickers — SEC allows up to 10 req/s, this is well under

_identity_set = False


def _ensure_identity() -> None:
    """SEC requires a contact identity on every request (fair-access policy)."""
    global _identity_set
    if _identity_set:
        return
    contact = os.environ.get("GMAIL_USER", "contact@example.com")
    set_identity(f"Dionice Model {contact}")
    _identity_set = True


def get_insider_signal(ticker: str, days_back: int = 14) -> dict | None:
    """
    Returns aggregated open-market insider buy/sell activity for a ticker in
    the last `days_back` days, or None if unavailable / no qualifying activity.
    """
    _ensure_identity()

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        company = Company(ticker)
        filings = company.get_filings(form="4", filing_date=f"{cutoff}:{today}")
    except Exception:
        return None

    buys, sells = 0, 0
    buyers: list[str] = []
    sellers: list[str] = []

    for filing in filings:
        try:
            df = filing.obj().to_dataframe()
        except Exception:
            continue
        if df.empty or "Code" not in df.columns:
            continue

        insider = df["Insider"].iloc[0] if "Insider" in df.columns else "Unknown"
        codes = set(df["Code"].dropna())

        if "P" in codes:
            buys += 1
            if insider not in buyers:
                buyers.append(insider)
        if "S" in codes:
            sells += 1
            if insider not in sellers:
                sellers.append(insider)

    if buys == 0 and sells == 0:
        return None

    if buys >= 3:
        note = f"{buys} insidera kupovalo na otvorenom tržištu u zadnjih {days_back}d — cluster buying, jak signal"
    elif buys > sells:
        note = f"{buys} insider kupnja(e) vs {sells} prodaja u zadnjih {days_back}d"
    elif sells > buys:
        note = f"Insideri uglavnom prodaju ({sells} prodaje vs {buys} kupnje) u zadnjih {days_back}d — nije nužno loše (može biti diverzifikacija), ali nije pozitivan signal"
    else:
        note = f"Mješovita insider aktivnost ({buys} kupnje, {sells} prodaje) u zadnjih {days_back}d"

    return {
        "ticker": ticker,
        "buy_count": buys,
        "sell_count": sells,
        "buyers": buyers,
        "sellers": sellers,
        "note": note,
    }


def get_insider_signals_batch(tickers: list[str], days_back: int = 14, delay: float = DEFAULT_DELAY) -> dict[str, dict]:
    """Fetches insider signals for a list of tickers. Returns dict keyed by ticker (only hits)."""
    results: dict[str, dict] = {}
    for ticker in tickers:
        signal = get_insider_signal(ticker, days_back)
        if signal:
            results[ticker] = signal
        time.sleep(delay)
    print(f"[insider_tracker] Got insider signal for {len(results)}/{len(tickers)} tickers")
    return results
