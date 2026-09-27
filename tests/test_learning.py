from datetime import datetime, timezone

from analysis import learning
from tests.conftest import make_series

NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)


def flat_then(start_price: float, days: int, step: float) -> list[float]:
    return [start_price + step * i for i in range(days)]


def test_collapse_repeats_keeps_first_call_per_window():
    rows = [
        {"symbol": "GNRC", "agent_action": "ADD_ON_DIP", "recommended_at": "2026-08-01T10:00:00+00:00"},
        {"symbol": "GNRC", "agent_action": "ADD_ON_DIP", "recommended_at": "2026-08-10T10:00:00+00:00"},
        {"symbol": "GNRC", "agent_action": "REDUCE", "recommended_at": "2026-08-12T10:00:00+00:00"},
        {"symbol": "GNRC", "agent_action": "ADD_ON_DIP", "recommended_at": "2026-09-05T10:00:00+00:00"},
    ]
    kept = learning.collapse_repeats(rows, "recommended_at", "agent_action")
    assert [(r["agent_action"], r["recommended_at"][:10]) for r in kept] == [
        ("ADD_ON_DIP", "2026-08-01"), ("REDUCE", "2026-08-12"), ("ADD_ON_DIP", "2026-09-05")]


def test_spearman_and_t():
    assert learning.spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == 1.0
    assert learning.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == -1.0
    assert learning.spearman([1, 2], [1, 2]) is None
    assert learning.t_stat(0.5, 30) > 2


def test_summarize_flags_small_samples():
    s = learning.summarize([1.0, -1.0, 3.0])
    assert s["n"] == 3 and s["avg"] == 1.0 and s["beat"] == 67 and not s["enough"]
    assert learning.summarize([])["n"] == 0


def test_decision_observations_zone_study(book_factory):
    # Stock A rises steadily (zone never reached); stock B dips 10% first, then recovers
    spy = make_series("2026-06-01", flat_then(100, 200, 0.05))
    a = make_series("2026-06-01", flat_then(100, 200, 0.5))
    b_prices = flat_then(100, 10, -1.0) + flat_then(90, 190, 0.2)
    b = make_series("2026-06-01", b_prices)
    book = book_factory({"SPY": spy, "AAA": a, "BBB": b})
    decisions = [
        {"symbol": "AAA", "agent_action": "BUY_BELOW", "agent_buy_zone": "< $95.00", "agent_confidence": 8,
         "recommended_at": "2026-06-01T12:00:00+00:00", "agent_thesis": "A"},
        {"symbol": "BBB", "agent_action": "BUY_BELOW", "agent_buy_zone": "< $92.00", "agent_confidence": 6,
         "recommended_at": "2026-06-01T12:00:00+00:00", "agent_thesis": "B"},
    ]
    obs = learning.decision_observations(decisions, [], book, NOW)
    by = {o["symbol"]: o for o in obs}
    assert by["AAA"]["filled30"] is False and by["AAA"]["zone_ex30"] == 0.0
    assert by["AAA"]["ex30"] > 0
    assert by["BBB"]["filled30"] is True
    study = learning.buy_zone_study(obs, 30, 0.5)
    assert study["n"] == 2 and study["fill_rate"] == 50
    assert study["market"]["avg"] > study["zone"]["avg"]


def test_actual_vs_spy_matches_same_cash_flows(book_factory):
    spy = make_series("2026-06-01", [100.0] * 5 + [110.0] * 200)   # SPY +10%
    stock = make_series("2026-06-01", [50.0] * 5 + [60.0] * 200)   # stock +20%
    book = book_factory({"SPY": spy, "XYZ": stock})
    transactions = [{"symbol": "XYZ", "action": "BUY", "shares": 10, "price_per_share": 50,
                     "currency": "USD", "trade_date": "2026-06-01T10:00:00+00:00"}]
    result = learning.actual_vs_spy(transactions, book, NOW, lambda _day: 1.0)
    assert result["invested_usd"] == 500
    assert result["actual_gain_usd"] == 100      # 10 × (60 − 50)
    assert result["spy_gain_usd"] == 50          # $500 of SPY +10%
    assert result["difference_usd"] == 50


def test_weight_proposal_needs_significance_and_sample():
    current = {"peg": [1.0] * 5, "fcf_yield": [1.0] * 5}
    weak = [{"criterion": "peg", "n": 100, "rho": 0.05, "t": 0.5}]
    assert learning.weight_proposal(weak, current) is None
    small = [{"criterion": "peg", "n": 20, "rho": 0.6, "t": 3.2}]
    assert learning.weight_proposal(small, current) is None
    strong = [{"criterion": "peg", "n": 100, "rho": -0.3, "t": -3.1}, {"criterion": "score", "n": 100, "rho": 0.4, "t": 4.0}]
    proposal = learning.weight_proposal(strong, current)
    assert proposal["weights"]["peg"] == [0.8] * 5
    assert proposal["weights"]["fcf_yield"] == [1.0] * 5


def test_stats_markdown_handles_json_roundtrip_keys(book_factory):
    import json
    spy = make_series("2026-06-01", flat_then(100, 200, 0.05))
    a = make_series("2026-06-01", flat_then(100, 200, 0.5))
    book = book_factory({"SPY": spy, "AAA": a})
    decisions = [{"symbol": "AAA", "agent_action": "WATCHLIST", "agent_confidence": 4,
                  "recommended_at": "2026-06-01T12:00:00+00:00"}]
    stats = learning.build_stats(decisions, [], [], [], book, NOW, {"starter_fraction": 0.5},
                                 {"peg": [1.0] * 5}, lambda _d: 1.0)
    text = learning.stats_markdown(json.loads(json.dumps(stats, default=str)))
    assert "WATCHLIST" in text and "premalo podataka" in text
