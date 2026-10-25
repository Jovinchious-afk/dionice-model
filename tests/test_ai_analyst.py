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
    # Effort is pinned per model (the cache is keyed on it); thinking needs room in max_tokens
    assert params["output_config"]["effort"] == ai_analyst.ANALYST_EFFORT
    review = request_params(system, "prompt", model=ai_analyst.REVIEW_MODEL)
    assert review["output_config"]["effort"] == ai_analyst.REVIEW_EFFORT
    assert params["max_tokens"] == review["max_tokens"] >= 16000
    assert request_params(system, "prompt", model=ai_analyst.REVIEW_MODEL, effort="high")["output_config"]["effort"] == "high"
    # Identical prefix for every stock: only the user message differs
    other = request_params(system, "drugi prompt")
    assert other["system"] == params["system"] and other["output_config"] == params["output_config"]


def test_zone_lowered_while_price_sits_at_the_old_zone_is_flagged(fund, params):
    # CENX on 2026-10-25: zone < $32 from September, price $31.42, new zone < $28
    at_zone = dict(fund, current_price=31.42)
    rec = finalize(model_json(buy_zone="< $28.00"), make_ctx(at_zone, params, reference_zone="< $32.00"),
                   ai_analyst.MODEL)
    assert rec["zone_drift"]["from"] == 32.0 and rec["zone_drift"]["to"] == 28.0
    assert "$32.00" in rec["zone_drift_note"] and rec["reference_zone_reached"]
    # Kept zone, or a price still far above the old zone: nothing to flag
    kept = finalize(model_json(buy_zone="< $32.00"), make_ctx(at_zone, params, reference_zone="< $32.00"),
                    ai_analyst.MODEL)
    assert "zone_drift" not in kept and kept["reference_zone_reached"]
    above = finalize(model_json(buy_zone="< $28.00"), make_ctx(dict(fund, current_price=40.0), params,
                                                               reference_zone="< $32.00"), ai_analyst.MODEL)
    assert "zone_drift" not in above and not above["reference_zone_reached"]


def test_review_model_calls_carry_the_refusal_fallback():
    seen = {}

    class Recorder:
        def __init__(self, name):
            self.name = name

        def create(self, **params):
            seen[self.name] = params
            return SimpleNamespace(model=params["model"], usage=None)

    client = SimpleNamespace(messages=Recorder("plain"), beta=SimpleNamespace(messages=Recorder("beta")))
    ai_analyst.call_claude(client, {"model": ai_analyst.REVIEW_MODEL, "messages": []})
    assert seen["beta"]["fallbacks"] == "default" and seen["beta"]["betas"] == [ai_analyst.FALLBACK_BETA]
    # Claude Haiku 5.5 has no server-side fallback: plain endpoint, no fallbacks parameter
    ai_analyst.call_claude(client, {"model": ai_analyst.MODEL, "messages": []})
    assert "fallbacks" not in seen["plain"]


def test_summary_gets_the_business_description_and_runs_on_the_review_model():
    seen = {}

    class Recorder:
        def create(self, **params):
            seen.update(params)
            payload = {"overall_market_comment": "x", "top_actions": [], "watchlist_this_week": [],
                       "no_trade_reason": "", "portfolio_note": ""}
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                                   stop_reason="end_turn", model=params["model"], usage=None)

    client = SimpleNamespace(messages=Recorder(), beta=SimpleNamespace(messages=Recorder()))
    rec = {"ticker": "CARG", "company_name": "CarGurus, Inc.", "action": "HOLD", "confidence": 7,
           "business_explanation": "Online tržište rabljenih automobila."}
    ai_analyst.generate_weekly_summary(client, [], [rec], "2026-10-25")
    assert seen["model"] == ai_analyst.REVIEW_MODEL
    assert "CarGurus, Inc." in seen["messages"][0]["content"]
    assert "Online tržište rabljenih automobila." in seen["messages"][0]["content"]


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
    # Price already inside the zone: buy now, the rest over the following weeks
    in_zone = finalize(model_json(confidence=8, buy_zone="< $110.00"), make_ctx(fund, params), ai_analyst.MODEL)
    assert "već u zoni" in in_zone["entry_plan_note"] and "50%" in in_zone["entry_plan_note"]


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
