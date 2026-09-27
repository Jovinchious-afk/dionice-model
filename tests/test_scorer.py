from analysis import scorer
from analysis.scorer import resolve_weights, score_stock


def test_missing_metric_no_longer_scores_below_a_bad_one(fund):
    with_bad_peg = dict(fund, peg=4.0)
    without_peg = dict(fund, peg=None)
    assert score_stock(without_peg, "quality_compounder")["total_score"] >= \
        score_stock(with_bad_peg, "quality_compounder")["total_score"]
    breakdown = score_stock(without_peg, "quality_compounder")["breakdown"]
    assert breakdown["peg"]["raw"] is None


def test_low_coverage_is_flagged():
    empty = {"symbol": "DEAD", "sector": "Technology"}
    result = score_stock(empty, "quality_compounder")
    assert result["verdict"] == "INSUFFICIENT_DATA"
    assert result["coverage"] < 0.5


def test_insider_buys_score_actual_purchases(fund):
    buys = lambda n: score_stock(dict(fund, insider_buys_24m=n), "value_cyclical")["breakdown"]["insider_buys"]["raw"]
    assert buys(3) == 5
    assert buys(1) == 4
    assert buys(0) == 2


def test_margin_trend_penalises_three_falling_quarters(fund):
    flat = score_stock(fund, "quality_compounder")["breakdown"]["margin_trend"]["raw"]
    falling = score_stock(dict(fund, op_margin_declining_3q=True), "quality_compounder")["breakdown"]["margin_trend"]["raw"]
    assert falling == flat - 1


def test_negative_equity_debt_ratio_is_left_out(fund):
    result = score_stock(dict(fund, debt_equity=-250.0), "quality_compounder")
    assert result["breakdown"]["debt_equity"]["raw"] is None


def test_pe_is_compared_with_the_industry_median(fund, monkeypatch):
    benchmarks = {
        "sectors": {"Industrials": {"n": 60, "pe_median": 30.0, "forward_pe_median": 20.0}},
        "industries": {"Specialty Industrial Machinery": {"n": 10, "pe_median": 36.0, "forward_pe_median": 24.0}},
    }
    monkeypatch.setattr(scorer, "_benchmarks", lambda: benchmarks)
    result = score_stock(fund, "quality_compounder")
    assert result["peer_pe"] == 36.0
    assert result["breakdown"]["pe_vs_sector"]["raw"] == 5  # 18 / 36 = 0.5 → cheap vs peers
    # Too few industry peers → sector median
    benchmarks["industries"]["Specialty Industrial Machinery"]["n"] = 3
    assert score_stock(fund, "quality_compounder")["peer_pe"] == 30.0
    # Cyclicals use forward P/E against the forward median
    cyclical = score_stock(fund, "value_cyclical")
    assert cyclical["peer_pe"] == 20.0 and "forward" in cyclical["peer_pe_source"]


def test_weight_override_applies_and_ignores_malformed_rows():
    override = {"peg": [0.1, 0.1, 0.1, 0.1, 0.1], "fcf_yield": [1, 2], "unknown": [1, 1, 1, 1, 1]}
    merged = resolve_weights(override)
    assert merged["peg"] == [0.1] * 5
    assert merged["fcf_yield"] == scorer.WEIGHTS["fcf_yield"]
    assert "unknown" not in merged
    assert resolve_weights(None) is scorer.WEIGHTS
