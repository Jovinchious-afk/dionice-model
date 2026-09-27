"""
Tunable model parameters.

The code defaults below are what the model runs on until the investor approves a
change. The quarterly learning report (scripts/run_quarterly_review.py) proposes
new values from measured outcomes; the proposals sit in the model_params table as
PROPOSED and only an ACTIVE row — approved on the Streamlit "Učenje" page —
overrides a default. Every change is a new row, so the next report can measure
results before and after it.
"""

import json

TABLE = "model_params"

DEFAULTS: dict = {
    "min_email_confidence": 5,
    "min_ai_score": 40,
    "min_buy_confidence": 6,
    "hype_block_threshold": 7,
    "concentration_cap": 0.30,
    "starter_min_confidence": 8,
    "starter_fraction": 0.5,
    "second_pass_min_confidence": 6,
    "second_pass_max_per_run": 5,
    "monthly_budget_usd": 4.5,
    "scorer_weights": None,  # None = analysis.scorer.WEIGHTS
}

# Croatian descriptions, shown on the Streamlit page and given to the learning report
DESCRIPTIONS: dict[str, str] = {
    "min_email_confidence": "Najmanji confidence da nova ideja uđe u newsletter (pozicije i hidden gemovi uvijek ulaze)",
    "min_ai_score": "Najmanji fundamentalni score (0-100) da dionica ide na AI analizu",
    "min_buy_confidence": "Ispod ovog confidencea nema kupnje (BUY_BELOW/ADD_ON_DIP postaju WAIT/HOLD)",
    "hype_block_threshold": "StockTwits hype (1-10) od kojeg je kupnja blokirana",
    "concentration_cap": "Najveći udio jedne pozicije u ukupnom kapitalu; iznad njega nema ADD_ON_DIP",
    "starter_min_confidence": "Od ovog confidencea preporuka kaže: dio pozicije kupi odmah, ostatak na buy zoni",
    "starter_fraction": "Koji dio pozicije se kupuje odmah kad vrijedi pravilo gornjeg reda",
    "second_pass_min_confidence": "BUY kandidati od ovog confidencea idu na drugu provjeru jačim modelom",
    "second_pass_max_per_run": "Najviše toliko drugih provjera po newsletteru",
    "monthly_budget_usd": "Mjesečni budžet za AI (USD); iznad njega se druga provjera preskače",
    "scorer_weights": "Težine kriterija fundamentalnog scorea po kategoriji",
}

# What the learning report may propose, with hard bounds and the largest change
# allowed per quarter. Everything else (concentration cap, budget) is the
# investor's own policy and is only changed by hand.
PROPOSABLE: dict[str, dict] = {
    "min_email_confidence": {"type": int, "min": 3, "max": 8, "step": 1},
    "min_ai_score": {"type": int, "min": 25, "max": 60, "step": 5},
    "min_buy_confidence": {"type": int, "min": 5, "max": 8, "step": 1},
    "hype_block_threshold": {"type": int, "min": 5, "max": 9, "step": 1},
    "starter_min_confidence": {"type": int, "min": 6, "max": 10, "step": 1},
    "starter_fraction": {"type": float, "min": 0.25, "max": 0.75, "step": 0.25},
    "second_pass_min_confidence": {"type": int, "min": 5, "max": 9, "step": 1},
}


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def active_rows(client) -> list[dict]:
    """ACTIVE parameter rows, newest decision first. Empty before schema_v8.sql."""
    if not client:
        return []
    try:
        return (client.table(TABLE).select("*").eq("status", "ACTIVE")
                .order("decided_at", desc=True).execute().data or [])
    except Exception as exc:
        print(f"[params] {TABLE} unavailable (run data/schema_v8.sql?): {exc}")
        return []


def load_params(client) -> dict:
    """Code defaults overlaid with the newest ACTIVE value per key."""
    params = dict(DEFAULTS)
    seen = set()
    for row in active_rows(client):
        key = row.get("key")
        if key in DEFAULTS and key not in seen:
            params[key] = _decode(row.get("value"))
            seen.add(key)
    if seen:
        print(f"[params] Active overrides: { {k: params[k] for k in sorted(seen) if k != 'scorer_weights'} }"
              + (" + scorer_weights" if "scorer_weights" in seen else ""))
    return params


def validate_proposal(key: str, current, proposed) -> tuple[bool, str]:
    """Bounds and step limits for a proposed change; (ok, reason)."""
    spec = PROPOSABLE.get(key)
    if spec is None:
        return False, f"{key} se ne može mijenjati automatskim prijedlogom"
    try:
        value = spec["type"](proposed)
        base = spec["type"](current)
    except (TypeError, ValueError):
        return False, f"{key}: vrijednost {proposed!r} nije broj"
    if spec["type"] is int and float(proposed) != value:
        return False, f"{key}: mora biti cijeli broj"
    if not spec["min"] <= value <= spec["max"]:
        return False, f"{key}: {value} je izvan dopuštenog raspona {spec['min']}–{spec['max']}"
    if value == base:
        return False, f"{key}: prijedlog je jednak trenutnoj vrijednosti"
    if abs(value - base) > spec["step"] + 1e-9:
        return False, f"{key}: promjena {base} → {value} je veća od dopuštenog koraka {spec['step']} po kvartalu"
    return True, ""
