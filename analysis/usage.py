"""
Token usage and cost per run, so the monthly AI budget is measured rather than
guessed. Each job collects the usage of every Claude response it receives and
writes one row per (model, batch) to llm_usage at the end of the run.

Prices are USD per million tokens (input, output). Cache writes cost 1.25x the
input price (5-minute TTL) or 2x (1-hour TTL), cache reads 0.1x, and the Message
Batches API halves everything.
"""

from datetime import datetime, timezone

TABLE = "llm_usage"

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
}
UNKNOWN_MODEL_PRICE = (5.0, 25.0)  # errs on the expensive side


def _get(obj, name: str) -> int:
    value = getattr(obj, name, None) if not isinstance(obj, dict) else obj.get(name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_cost(model: str, usage, batch: bool = False) -> float:
    """Cost in USD of one response's usage object."""
    price_in, price_out = PRICES.get(model, UNKNOWN_MODEL_PRICE)
    cache_read = _get(usage, "cache_read_input_tokens")
    cache_write = _get(usage, "cache_creation_input_tokens")
    creation = getattr(usage, "cache_creation", None) if not isinstance(usage, dict) else usage.get("cache_creation")
    write_1h = _get(creation, "ephemeral_1h_input_tokens") if creation is not None else 0
    write_5m = max(cache_write - write_1h, 0)

    cost = (
        _get(usage, "input_tokens") * price_in
        + _get(usage, "output_tokens") * price_out
        + cache_read * price_in * 0.1
        + write_5m * price_in * 1.25
        + write_1h * price_in * 2.0
    ) / 1_000_000
    return cost * (0.5 if batch else 1.0)


class UsageTracker:
    """Accumulates usage per (model, batch) for one job run."""

    def __init__(self, job: str):
        self.job = job
        self.totals: dict[tuple[str, bool], dict] = {}

    def add(self, model: str, usage, batch: bool = False) -> None:
        if usage is None:
            return
        row = self.totals.setdefault((model, batch), {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0,
        })
        row["calls"] += 1
        row["input_tokens"] += _get(usage, "input_tokens")
        row["output_tokens"] += _get(usage, "output_tokens")
        row["cache_write_tokens"] += _get(usage, "cache_creation_input_tokens")
        row["cache_read_tokens"] += _get(usage, "cache_read_input_tokens")
        row["cost_usd"] += usage_cost(model, usage, batch)

    def add_message(self, message, batch: bool = False, fallback_model: str | None = None) -> None:
        """Adds a Message; the model that actually served it wins (server-side fallbacks)."""
        if message is None:
            return
        self.add(getattr(message, "model", None) or fallback_model or "", getattr(message, "usage", None), batch)

    @property
    def cost(self) -> float:
        return sum(r["cost_usd"] for r in self.totals.values())

    def summary(self) -> str:
        if not self.totals:
            return f"[usage] {self.job}: no Claude calls"
        parts = [
            f"{model}{' (batch)' if batch else ''}: {r['calls']} calls, in {r['input_tokens']:,} "
            f"(+{r['cache_read_tokens']:,} cached, {r['cache_write_tokens']:,} written), "
            f"out {r['output_tokens']:,} → ${r['cost_usd']:.3f}"
            for (model, batch), r in sorted(self.totals.items())
        ]
        return f"[usage] {self.job}: ${self.cost:.3f} total | " + " | ".join(parts)

    def save(self, client) -> None:
        if not client or not self.totals:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {"created_at": now, "job": self.job, "model": model, "batch": batch,
             **{k: (round(v, 6) if k == "cost_usd" else v) for k, v in r.items()}}
            for (model, batch), r in self.totals.items()
        ]
        try:
            client.table(TABLE).insert(rows).execute()
        except Exception as exc:
            print(f"[usage] Could not write {TABLE} (run data/schema_v8.sql?): {exc}")


def month_spend(client, now: datetime | None = None) -> float | None:
    """USD spent on Claude this calendar month, or None before schema_v8.sql."""
    if not client:
        return None
    now = now or datetime.now(timezone.utc)
    month_start = now.strftime("%Y-%m-01T00:00:00Z")
    try:
        rows = client.table(TABLE).select("cost_usd").gte("created_at", month_start).execute().data or []
    except Exception:
        return None
    return round(sum(float(r.get("cost_usd") or 0) for r in rows), 4)
