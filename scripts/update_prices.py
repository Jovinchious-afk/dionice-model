"""
Weekly job: fills price_30d/90d/180d for old decisions in Supabase, scores each
checkpoint as correct/wrong (direction-aware per agent_action), writes a short
AI retrospective explaining why, and auto-detects if the user actually followed
the recommendation by cross-referencing the transactions log.
Runs every Monday via GitHub Actions.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import yfinance as yf

from analysis.supabase_client import get_supabase

MODEL = "claude-haiku-4-5-20251001"
CHECKPOINTS = [(30, "30d"), (90, "90d"), (180, "180d")]
BEARISH_ACTIONS = {"SELL", "REDUCE"}


def get_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        return getattr(info, "last_price", None)
    except Exception:
        return None


def compute_outcome(agent_action: str, price_then: float, price_now: float) -> str:
    """SELL/REDUCE are correct if price fell; every other action is correct if it rose."""
    if agent_action in BEARISH_ACTIONS:
        return "correct" if price_now < price_then else "wrong"
    return "correct" if price_now > price_then else "wrong"


def generate_outcome_reasoning(
    dec: dict, checkpoint: str, price_then: float, price_now: float, outcome: str
) -> str | None:
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        pct_change = ((price_now - price_then) / price_then) * 100 if price_then else 0

        prompt = f"""Retrospektivna analiza jedne agent preporuke, {checkpoint} nakon preporuke.

Ticker: {dec.get('symbol')}
Preporučena akcija: {dec.get('agent_action')}
Teza u trenutku preporuke: {(dec.get('agent_thesis') or '')[:400]}
Cijena kod preporuke: ${price_then:.2f}
Cijena sada ({checkpoint}): ${price_now:.2f} ({pct_change:+.1f}%)
Ocjena: {outcome.upper()}

Napiši 2-3 rečenice NA HRVATSKOM: je li teza bila dobra i koji su vjerojatni faktori
(fundamentalni, sektorski, makro) doveli do ovog ishoda. Budi konkretan i samokritičan
ako je ishod pogrešan. Bez uvoda, samo analiza."""

        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        print(f"[update_prices] Reasoning generation failed for {dec.get('symbol')}: {exc}")
        return None


def load_transactions_by_symbol(client) -> dict[str, list[dict]]:
    try:
        result = client.table("transactions").select("*").execute()
        rows = result.data or []
    except Exception as exc:
        print(f"[update_prices] Could not load transactions: {exc}")
        return {}
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row.get("symbol", ""), []).append(row)
    return by_symbol


def infer_followed(dec: dict, tx_by_symbol: dict, rec_at: datetime) -> bool:
    """True if a matching BUY (or SELL, for bearish calls) trade was logged after the recommendation."""
    wanted_action = "SELL" if dec.get("agent_action", "") in BEARISH_ACTIONS else "BUY"
    for tx in tx_by_symbol.get(dec.get("symbol", ""), []):
        if tx.get("action") != wanted_action:
            continue
        try:
            tx_date = datetime.fromisoformat(tx["trade_date"])
            if tx_date.tzinfo is None:
                tx_date = tx_date.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        if tx_date >= rec_at:
            return True
    return False


def main():
    client = get_supabase()
    if not client:
        print("[update_prices] Supabase credentials missing.")
        return
    now = datetime.now(timezone.utc)

    result = client.table("decisions").select("*").execute()
    decisions = result.data or []
    tx_by_symbol = load_transactions_by_symbol(client)

    for dec in decisions:
        rec_at_str = dec.get("recommended_at")
        if not rec_at_str:
            continue
        try:
            rec_at = datetime.fromisoformat(rec_at_str)
            if rec_at.tzinfo is None:
                rec_at = rec_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age_days = (now - rec_at).days
        ticker = dec.get("symbol", "")
        rec_price = dec.get("price_at_recommendation")
        if not ticker:
            continue

        updates = {}

        due_checkpoints = [
            (days, suffix) for days, suffix in CHECKPOINTS
            if age_days >= days and dec.get(f"price_{suffix}") is None
        ]
        if due_checkpoints and rec_price:
            price = get_current_price(ticker)
            if price:
                for _, suffix in due_checkpoints:
                    updates[f"price_{suffix}"] = price
                    outcome = compute_outcome(dec.get("agent_action", ""), float(rec_price), price)
                    updates[f"outcome_{suffix}"] = outcome
                    reasoning = generate_outcome_reasoning(dec, suffix, float(rec_price), price, outcome)
                    if reasoning:
                        updates[f"outcome_reasoning_{suffix}"] = reasoning

        if dec.get("user_action") in (None, "PENDING") and infer_followed(dec, tx_by_symbol, rec_at):
            updates["user_action"] = "FOLLOWED"
            updates["user_action_note"] = "Auto-detected iz trade loga."

        if updates:
            try:
                client.table("decisions").update(updates).eq("id", dec["id"]).execute()
                print(f"[update_prices] Updated {ticker} (age {age_days}d): {list(updates.keys())}")
            except Exception as exc:
                print(f"[update_prices] Update failed for {ticker} ({dec['id']}): {exc}")

    print("[update_prices] Done.")


if __name__ == "__main__":
    main()
