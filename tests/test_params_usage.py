from types import SimpleNamespace

from analysis.params import DEFAULTS, load_params, validate_proposal
from analysis.usage import UsageTracker, usage_cost


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def order(self, *_, **__):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return FakeQuery(self.rows)


def test_load_params_newest_active_value_wins():
    rows = [
        {"key": "min_email_confidence", "value": 6, "decided_at": "2026-10-02"},
        {"key": "min_email_confidence", "value": 4, "decided_at": "2026-07-02"},
        {"key": "concentration_cap", "value": "0.25", "decided_at": "2026-10-01"},
        {"key": "not_a_param", "value": 1, "decided_at": "2026-10-01"},
    ]
    params = load_params(FakeClient(rows))
    assert params["min_email_confidence"] == 6
    assert params["concentration_cap"] == 0.25
    assert "not_a_param" not in params
    assert load_params(None) == DEFAULTS


def test_proposal_guardrails():
    assert validate_proposal("min_email_confidence", 5, 6)[0]
    assert not validate_proposal("min_email_confidence", 5, 7)[0]      # more than one step
    assert not validate_proposal("min_email_confidence", 5, 5)[0]      # no change
    assert not validate_proposal("min_email_confidence", 5, 5.5)[0]    # must be whole
    assert not validate_proposal("concentration_cap", 0.3, 0.4)[0]     # investor's policy
    assert not validate_proposal("hype_block_threshold", 5, 4)[0]      # below the floor
    assert validate_proposal("starter_fraction", 0.5, 0.75)[0]


def test_usage_cost_with_cache_and_batch():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 0}
    # Haiku 4.5: $1 in + $0.5 out + $0.1 cache read
    assert round(usage_cost("claude-haiku-4-5-20251001", usage), 4) == 1.6
    assert round(usage_cost("claude-haiku-4-5-20251001", usage, batch=True), 4) == 0.8
    write_1h = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
                "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
    assert round(usage_cost("claude-sonnet-5", write_1h), 4) == 4.0  # $2 × 2


def test_tracker_accumulates_per_model():
    tracker = UsageTracker("weekly")
    msg = SimpleNamespace(model="claude-haiku-4-5-20251001",
                          usage=SimpleNamespace(input_tokens=1000, output_tokens=500,
                                                cache_read_input_tokens=0, cache_creation_input_tokens=0))
    tracker.add_message(msg, batch=True)
    tracker.add_message(msg, batch=True)
    row = tracker.totals[("claude-haiku-4-5-20251001", True)]
    assert row["calls"] == 2 and row["input_tokens"] == 2000
    assert round(tracker.cost, 6) == round(2 * (1000 * 1 + 500 * 5) / 1e6 * 0.5, 6)
