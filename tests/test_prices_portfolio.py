from datetime import datetime, timezone
from types import SimpleNamespace

from analysis import fundamentals, portfolio
from analysis.email_sender import build_html_email, markdown_to_html
from analysis.prices import parse_buy_zone
from scripts.update_prices import score_checkpoint
from tests.conftest import make_series


def test_parse_buy_zone():
    assert parse_buy_zone("< $1,234.50") == 1234.5
    assert parse_buy_zone("< 128.00") == 128.0
    assert parse_buy_zone("N/A") is None
    assert parse_buy_zone(None) is None


def test_starter_call_counts_from_the_recommendation_day(book_factory):
    spy = make_series("2026-06-01", [100.0] * 60)
    stock = make_series("2026-06-01", [100.0] + [110.0] * 59)
    book = book_factory({"SPY": spy, "AAA": stock})
    rec_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    zone_call = {"symbol": "AAA", "agent_action": "BUY_BELOW", "agent_buy_zone": "< $90.00"}
    assert score_checkpoint(zone_call, rec_at, 30, book)["outcome"] == "neutral"   # zone never hit
    starter = dict(zone_call, entry_plan="starter")
    scored = score_checkpoint(starter, rec_at, 30, book)
    assert scored["outcome"] == "correct" and round(scored["excess"], 1) == 10.0


def test_compute_holdings_resets_cost_after_full_exit(monkeypatch):
    monkeypatch.setattr(portfolio, "eur_usd_on", lambda _d=None: 1.0)
    rows = [
        {"symbol": "XYZ", "action": "BUY", "shares": 10, "price_per_share": 100.0, "currency": "USD", "trade_date": "2026-01-10"},
        {"symbol": "XYZ", "action": "SELL", "shares": 10, "price_per_share": 130.0, "currency": "USD", "trade_date": "2026-02-10"},
        {"symbol": "XYZ", "action": "BUY", "shares": 5, "price_per_share": 120.0, "currency": "USD", "trade_date": "2026-03-10"},
    ]
    h = portfolio.compute_holdings(rows)["XYZ"]
    assert round(h["avg_cost_usd"], 2) == 120.0
    assert round(h["realized_pnl_usd"], 2) == 300.0


def test_dead_ticker_is_a_fetch_error(monkeypatch, tmp_path):
    monkeypatch.setattr(fundamentals, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(fundamentals.yf, "Ticker", lambda _s: SimpleNamespace(info={"trailingPegRatio": None}))
    data = fundamentals.fetch_fundamentals("ANSS")
    assert data["fetch_error"] and "delisted" in data["fetch_error"]


def test_markdown_to_html_basics():
    html = markdown_to_html("## Naslov\n- **bitno** <x>\n\n| a | b |\n|---|---|\n| 1 | 2 |\nTekst")
    assert "<h3" in html and "<strong>bitno</strong>" in html and "&lt;x&gt;" in html
    assert "<table" in html and "<td" in html and "Tekst" in html


def test_email_shows_guard_notes_and_weights():
    rec = {"action": "HOLD", "ticker": "AAA", "company_name": "Example Corp", "confidence": 7,
           "concentration_note": "ADD_ON_DIP blokiran: pozicija je već 60% ukupnog kapitala",
           "evidence_table": {}}
    html = build_html_email({"date": "2026-09-29"}, [rec], None,
                            [{"symbol": "AAA", "shares": 100, "avg_cost": "$50.00", "current_price": "$45.00",
                              "pnl_pct": "-10.0%", "weight": 0.6}], concentration_cap=0.3)
    assert "60% ⚠️" in html and "ADD_ON_DIP blokiran" in html and "Limit po poziciji: 30%" in html
