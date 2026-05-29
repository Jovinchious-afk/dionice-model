"""
Fetches key macro indicators using yfinance (no API key, no extra cost).
Used to give Claude market-wide context before stock analysis.

Indicators:
  ^GSPC  — S&P 500 (bull/bear market context, YTD direction)
  ^TNX   — 10-year US Treasury yield (growth vs value rotation)
  ^VIX   — Fear index (risk-on vs risk-off)
  DX-Y.NYB — USD Dollar Index (strong USD hurts multinationals)
"""

from datetime import datetime, timezone
import yfinance as yf


INDICATORS = {
    "^GSPC":     "S&P 500",
    "^TNX":      "10Y US prinos",
    "^VIX":      "VIX (strah)",
    "DX-Y.NYB":  "USD indeks",
}


def _pct(current: float, reference: float) -> float:
    if reference == 0:
        return 0.0
    return (current - reference) / reference * 100


def fetch_macro_context() -> dict:
    """
    Returns a dict with current values and 1-month changes for each indicator.
    Falls back gracefully if any fetch fails.
    """
    results: dict = {}

    for symbol, name in INDICATORS.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1mo", auto_adjust=True)
            if hist.empty:
                continue
            current = float(hist["Close"].iloc[-1])
            month_ago = float(hist["Close"].iloc[0])
            results[symbol] = {
                "name": name,
                "current": current,
                "change_1m_pct": _pct(current, month_ago),
            }
        except Exception:
            pass

    # YTD change for S&P 500
    try:
        year_start = f"{datetime.now(timezone.utc).year}-01-01"
        sp_ytd = yf.Ticker("^GSPC").history(start=year_start, auto_adjust=True)
        if not sp_ytd.empty and "^GSPC" in results:
            ytd_start = float(sp_ytd["Close"].iloc[0])
            results["^GSPC"]["change_ytd_pct"] = _pct(results["^GSPC"]["current"], ytd_start)
    except Exception:
        pass

    return results


def format_macro_for_prompt(macro: dict) -> str:
    """
    Converts macro data into a structured text block for Claude's prompt.
    Includes interpretation hints so Claude doesn't need to derive them.
    """
    if not macro:
        return "Makro podaci nisu dostupni ovaj tjedan."

    lines = ["MAKRO KONTEKST (yfinance, automatski dohvaćeno):"]

    sp = macro.get("^GSPC", {})
    if sp:
        ytd_str = f" | YTD: {sp['change_ytd_pct']:+.1f}%" if "change_ytd_pct" in sp else ""
        trend = "uzlazni trend" if sp["change_1m_pct"] > 1 else ("silazni trend" if sp["change_1m_pct"] < -1 else "neutralno")
        lines.append(f"  S&P 500: {sp['current']:,.0f} ({sp['change_1m_pct']:+.1f}% ovaj mjes.{ytd_str}) — {trend}")

    tny = macro.get("^TNX", {})
    if tny:
        rate = tny["current"]
        if rate > 4.5:
            interp = "visoke kamate → growth dionice pod pritiskom, value/dividend favorizirani"
        elif rate > 3.5:
            interp = "umjerene kamate → neutralno za dionice"
        else:
            interp = "niske kamate → growth dionice favorizirani"
        lines.append(f"  10Y prinos: {rate:.2f}% ({tny['change_1m_pct']:+.1f}% ovaj mjes.) — {interp}")

    vix = macro.get("^VIX", {})
    if vix:
        v = vix["current"]
        if v < 15:
            interp = "nisko → tržište mirno, risk-on okruženje"
        elif v < 25:
            interp = "srednje → normalna volatilnost"
        else:
            interp = "VISOKO → strah na tržištu, risk-off, budi konzervativniji"
        lines.append(f"  VIX: {v:.1f} — {interp}")

    dxy = macro.get("DX-Y.NYB", {})
    if dxy:
        d = dxy["current"]
        interp = "jak USD → pritisak na multinacionalne prihode" if dxy["change_1m_pct"] > 1 else (
            "slab USD → pozitivno za multinacionalne" if dxy["change_1m_pct"] < -1 else "stabilan USD"
        )
        lines.append(f"  USD indeks: {d:.1f} ({dxy['change_1m_pct']:+.1f}% ovaj mjes.) — {interp}")

    lines.append(
        "  → Uzmi makro kontekst u obzir pri svakoj preporuci: "
        "pri visokim kamatama i visokom VIX-u budi konzervativniji s buy_zone i position_size."
    )

    return "\n".join(lines)
