"""Regression tests for the code-review findings of 2026-09-27."""

import json
from types import SimpleNamespace

from analysis import ai_analyst
from analysis.scorer import score_stock
from analysis.supabase_client import SupabaseClient
from scripts import run_weekly
from tests.test_ai_analyst import model_json


class FakeMessages:
    def __init__(self, payload: dict):
        self.payload = payload

    def create(self, **_params):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            stop_reason="end_turn", model=ai_analyst.REVIEW_MODEL,
            usage=SimpleNamespace(input_tokens=10, output_tokens=10,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def held_ctx(fund, params):
    return {
        "fundamentals": fund, "score_result": score_stock(fund, "quality_compounder"),
        "in_portfolio": True, "is_hidden_gem": False, "sentiment_signal": None,
        "insider_signal": None, "congress_signal": None, "position_weight": 0.1, "params": params,
    }


def test_second_pass_cannot_sell_a_healthy_holding(fund, params):
    """The reviewer turning ADD_ON_DIP into SELL on unchanged fundamentals must end as HOLD."""
    claude = SimpleNamespace(messages=FakeMessages(model_json(action="SELL", confidence=7)))
    first = {"ticker": "TEST", "action": "ADD_ON_DIP", "confidence": 7, "model": ai_analyst.MODEL,
             "buy_zone": "< $95.00", "investment_thesis": "x"}
    prompt_kwargs = {"fundamentals": fund, "score_result": score_stock(fund, "quality_compounder"),
                     "congress_signal": None, "insider_signal": None, "in_portfolio": True}
    reviewed = run_weekly.second_pass(first, claude, [], prompt_kwargs, held_ctx(fund, params), None)
    assert reviewed["action"] == "HOLD"
    assert "blokiran" in reviewed["sell_guard_note"]


def test_second_pass_sell_stands_when_business_deteriorates(fund, params):
    bad = dict(fund, revenue_growth_yoy=-0.12, op_margin_declining_3q=True)
    claude = SimpleNamespace(messages=FakeMessages(model_json(action="SELL", confidence=7)))
    first = {"ticker": "TEST", "action": "ADD_ON_DIP", "confidence": 7, "model": ai_analyst.MODEL}
    prompt_kwargs = {"fundamentals": bad, "score_result": score_stock(bad, "quality_compounder"),
                     "congress_signal": None, "insider_signal": None, "in_portfolio": True}
    reviewed = run_weekly.second_pass(first, claude, [], prompt_kwargs, held_ctx(bad, params), None)
    assert reviewed["action"] == "SELL"


def test_fetch_all_pages_in_a_stable_order(monkeypatch):
    seen = []

    class FakeTable:
        def __init__(self):
            self.calls = {}

        def select(self, columns):
            self.calls["select"] = columns
            return self

        def order(self, column, desc=False):
            self.calls["order"] = column
            return self

        def gte(self, *_):
            return self

        def limit(self, n):
            self.calls["limit"] = n
            return self

        def offset(self, n):
            self.calls["offset"] = n
            return self

        def execute(self):
            seen.append(dict(self.calls))
            size = 2 if self.calls["offset"] == 0 else 1
            return SimpleNamespace(data=[{"id": i} for i in range(size)])

    client = SupabaseClient("https://example.supabase.co", "key")
    monkeypatch.setattr(client, "table", lambda _name: FakeTable())
    rows = client.fetch_all("decisions", "symbol,outcome_30d", page=2)
    assert len(rows) == 3
    assert all(call["order"] == "id" for call in seen)
    assert seen[0]["select"].endswith(",id")
    assert [call["offset"] for call in seen] == [0, 2]


def test_batch_cancel_survives_network_errors(monkeypatch):
    monkeypatch.setattr(ai_analyst.time, "sleep", lambda _s: None)
    running = SimpleNamespace(id="b1", processing_status="in_progress",
                              request_counts=SimpleNamespace(succeeded=0, processing=1))

    class FakeBatches:
        def create(self, requests):
            return running

        def retrieve(self, _id):
            raise ConnectionError("network down")

        def cancel(self, _id):
            raise ConnectionError("network down")

    client = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
    result = ai_analyst.run_batch(client, {"VG": {"model": ai_analyst.MODEL}}, timeout_s=0, poll_s=0)
    assert result == {}
