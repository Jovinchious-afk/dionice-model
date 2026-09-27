import json
from types import SimpleNamespace

from analysis import ai_analyst
from analysis.ai_analyst import (
    analysis_schema,
    build_run_context,
    build_system,
    finalize,
    parse_message,
    request_params,
    summary_subject_suffix,
)
from analysis.scorer import score_stock


def model_json(**overrides) -> dict:
    base = {
        "category": "quality_compounder", "business_explanation": "x", "valuation_verdict": "x",
        "cycle_view": "x", "change_vs_last": "x", "investor_view": "x", "counter_argument": "x",
        "catalyst": "x", "downside_scenario": "x", "vs_cash_alternative": "x", "thesis_breakers": ["a"],
        "red_flags": ["b"], "action": "BUY_BELOW", "buy_zone": "< $95.00", "target_price": "$120.00",
        "position_size": "mala", "investment_thesis": "Teza.", "confidence": 7,
    }
    base.update(overrides)
    return base


def make_ctx(fund, params, **overrides) -> dict:
    ctx = {
        "fundamentals": fund, "score_result": score_stock(fund, "quality_compounder"),
        "in_portfolio": False, "is_hidden_gem": False, "sentiment_signal": None,
        "insider_signal": None, "congress_signal": None, "position_weight": None, "params": params,
    }
    ctx.update(overrides)
    return ctx


def test_schema_is_strict_and_matches_field_order():
    schema = analysis_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(schema["properties"])
    assert schema["properties"]["confidence"]["enum"] == list(range(1, 11))
    # One schema for held and new stocks, so every analysis shares one cache entry
    assert set(schema["properties"]["action"]["enum"]) == {"HOLD", "ADD_ON_DIP", "REDUCE", "SELL",
                                                           "BUY_BELOW", "WATCHLIST", "WAIT", "NO_ACTION"}
    # The decision comes after the analysis
    order = list(schema["properties"])
    assert order.index("counter_argument") < order.index("action") < order.index("confidence")


def test_request_params_cache_and_output_config():
    system = build_system("PROFIL", "KONTEKST", cache_ttl="1h")
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in system[0]
    assert "cache_control" not in build_system("PROFIL", "KONTEKST", cache_ttl=None)[-1]
    params = request_params(system, "prompt")
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "effort" not in params["output_config"]  # Haiku 4.5 has no effort
    review = request_params(system, "prompt", model=ai_analyst.REVIEW_MODEL, effort="medium")
    assert review["output_config"]["effort"] == "medium"
    assert review["max_tokens"] > params["max_tokens"]
    # Identical prefix for every stock: only the user message differs
    other = request_params(system, "drugi prompt")
    assert other["system"] == params["system"] and other["output_config"] == params["output_config"]


def test_not_held_stock_cannot_end_up_with_a_sell(fund, params):
    rec = finalize(model_json(action="SELL"), make_ctx(fund, params), ai_analyst.MODEL)
    assert rec["action"] == "NO_ACTION"


def test_run_context_states_the_active_parameters(params):
    params = dict(params, concentration_cap=0.3, hype_block_threshold=7, min_buy_confidence=6)
    text = build_run_context("2026-09-29", "MAKRO", None, "Pozicije: VG", 23000, params)
    assert "30% ukupnog kapitala" in text and "hype >= 7" in text and "confidence >= 6" in text


def test_finalize_takes_price_and_name_from_the_data(fund, params):
    rec = finalize(model_json(), make_ctx(fund, params), ai_analyst.MODEL)
    assert rec["evidence_table"]["current_price"] == "$100.00"
    assert rec["company_name"] == "Test Corp"
    assert rec["ticker"] == "TEST"


def test_hype_blocks_buy(fund, params):
    sentiment = {"hype_score": 8, "bullish_pct": 70, "bearish_pct": 0, "msgs_per_day": 30}
    rec = finalize(model_json(), make_ctx(fund, params, sentiment_signal=sentiment), ai_analyst.MODEL)
    assert rec["action"] == "WATCHLIST" and rec["hype_override"]


def test_confidence_floor(fund, params):
    rec = finalize(model_json(confidence=5), make_ctx(fund, params), ai_analyst.MODEL)
    assert rec["action"] == "WAIT"


def test_concentration_cap_turns_add_into_hold(fund, params):
    ctx = make_ctx(fund, params, in_portfolio=True, position_weight=0.6)
    rec = finalize(model_json(action="ADD_ON_DIP", confidence=8), ctx, ai_analyst.MODEL)
    assert rec["action"] == "HOLD"
    assert "60%" in rec["concentration_note"] and "30%" in rec["concentration_note"]
    below = finalize(model_json(action="ADD_ON_DIP", confidence=7), make_ctx(fund, params, in_portfolio=True, position_weight=0.12),
                     ai_analyst.MODEL)
    assert below["action"] == "ADD_ON_DIP"


def test_starter_plan_only_for_high_confidence_buys(fund, params):
    strong = finalize(model_json(confidence=8), make_ctx(fund, params), ai_analyst.MODEL)
    assert strong["entry_plan"] == "starter" and "50%" in strong["entry_plan_note"]
    normal = finalize(model_json(confidence=7), make_ctx(fund, params), ai_analyst.MODEL)
    assert "entry_plan" not in normal


def test_held_stock_actions_are_normalized(fund, params):
    rec = finalize(model_json(action="WATCHLIST"), make_ctx(fund, params, in_portfolio=True), ai_analyst.MODEL)
    assert rec["action"] == "HOLD"


def test_foreign_script_is_removed(fund, params):
    rec = finalize(model_json(investment_thesis="Pritisak 压力 je stvaran"), make_ctx(fund, params), ai_analyst.MODEL)
    assert "压" not in rec["investment_thesis"]


def test_unparseable_reply_becomes_a_safe_no_action(fund, params):
    message = SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn", model="m")
    result, raw = parse_message(message)
    assert result is None
    rec = finalize(result, make_ctx(fund, params), ai_analyst.MODEL, raw)
    assert rec["action"] == "NO_ACTION" and rec["confidence"] == 0 and rec["error"]


def test_parse_message_skips_thinking_blocks():
    payload = json.dumps(model_json())
    message = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                       SimpleNamespace(type="text", text=payload)], stop_reason="end_turn")
    result, _ = parse_message(message)
    assert result["action"] == "BUY_BELOW"


def test_custom_ids_are_valid_and_unique():
    ids = ai_analyst._custom_ids(["BRK.B", "BRK_B", "VG"])
    assert set(ids.values()) == {"BRK.B", "BRK_B", "VG"}
    assert all(len(cid) <= 64 and cid.replace("_", "").replace("-", "").isalnum() for cid in ids)


def test_subject_suffix_is_deterministic():
    recs = [{"action": "BUY_BELOW"}, {"action": "HOLD"}, {"action": "WATCHLIST"}, {"action": "WAIT"}]
    assert summary_subject_suffix(recs, False) == "1 BUY, 1 HOLD, 1 WATCHLIST, 0 SELL"
    assert summary_subject_suffix([{"action": "HOLD"}], True).endswith("NO TRADE")
