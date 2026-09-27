from datetime import datetime, timedelta, timezone

from analysis.sentiment_tracker import apply_market_cap, hype_score, messages_per_day

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def stamps(n: int, every: timedelta) -> list[str]:
    return [(NOW - every * i).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def test_messages_per_day_uses_span_to_now():
    # 30 messages, one every hour → oldest is 29h ago → ~24.8/day
    assert round(messages_per_day(stamps(30, timedelta(hours=1)), now=NOW), 1) == 24.8


def test_messages_per_day_quiet_stock_and_bad_input():
    assert messages_per_day(stamps(30, timedelta(days=30)), now=NOW) < 1.1
    assert messages_per_day([], now=NOW) == 0.0
    assert messages_per_day(["garbage"], now=NOW) == 0.0


def test_hype_separates_meme_small_caps_from_mega_caps_and_boring_stocks():
    # Velocities and caps measured on 2026-09-27
    assert hype_score(84.8, 4.5e12) <= 5      # NVDA
    assert hype_score(48.5, 1.8e12) <= 5      # META
    assert hype_score(0.2, 38e9) <= 2         # HIG
    assert hype_score(0.5, 16e9) <= 2         # PTC
    assert hype_score(15.2, 1.5e9) >= 7       # QUBT
    assert hype_score(25.6, 1.0e9) >= 7       # BBAI
    assert hype_score(63.8, 1.5e9) == 10      # AMC


def test_hype_without_activity_or_cap():
    assert hype_score(0, 1e9) == 1
    assert hype_score(None, None) == 1
    assert 1 <= hype_score(20, None) <= 10


def test_apply_market_cap_rewrites_score_and_summary():
    signal = {"message_count": 30, "msgs_per_day": 25.6, "bullish_pct": 53.0, "bearish_pct": 0.0, "hype_score": 1}
    apply_market_cap(signal, 1.0e9)
    assert signal["hype_score"] >= 7
    assert "Visok buzz" in signal["bull_bear_summary"]
