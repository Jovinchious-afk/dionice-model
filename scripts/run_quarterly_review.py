"""
Quarterly self-review — runs 1st of Jan/Apr/Jul/Oct.
Reads every decision with at least one resolved outcome (30d/90d/180d),
asks Claude to synthesize calibration lessons from what worked and what
didn't, and stores the result in model_lessons. The latest row is read
by run_weekly.py and run_monthly.py and injected into future prompts,
so the agent is reminded of its own past misses before making new calls.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import anthropic

from analysis.supabase_client import get_supabase

MODEL = "claude-haiku-4-5-20251001"


def get_resolved_decisions(limit: int = 150) -> list[dict]:
    """Most recent resolved decisions, capped to keep the prompt (and cost) bounded as history grows."""
    client = get_supabase()
    if not client:
        return []
    try:
        result = client.table("decisions").select("*").order("recommended_at", desc=True).execute()
        rows = result.data or []
    except Exception as exc:
        print(f"[run_quarterly_review] Supabase fetch failed: {exc}")
        return []

    resolved = [
        r for r in rows
        if r.get("outcome_30d") != "pending"
        or r.get("outcome_90d") != "pending"
        or r.get("outcome_180d") != "pending"
    ]
    return resolved[:limit]


def generate_lessons(decisions: list[dict], period_label: str) -> str | None:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    slim = [
        {
            "symbol": d.get("symbol"),
            "action": d.get("agent_action"),
            "confidence": d.get("agent_confidence"),
            "thesis": (d.get("agent_thesis") or "")[:200],
            "outcome_30d": d.get("outcome_30d"),
            "outcome_90d": d.get("outcome_90d"),
            "outcome_180d": d.get("outcome_180d"),
            "reasoning_30d": (d.get("outcome_reasoning_30d") or "")[:200],
        }
        for d in decisions
    ]

    prompt = f"""Napravi kvartalni retrospektivni pregled AI agent-preporuka za dionice ({period_label}), radi kalibracije budućih preporuka. NA HRVATSKOM JEZIKU.

SVE PREPORUKE S BAREM JEDNIM POZNATIM ISHODOM ({len(slim)} preporuka):
{json.dumps(slim, indent=2, default=str)}

Zadatak: pronađi konkretne, ponovljive obrasce — ne generičke savjete. Primjeri onoga što tražimo:
- Je li određena razina confidence (npr. 6-7) sustavno precijenjena naspram 8-10?
- Je li određena akcija (BUY_BELOW, WATCHLIST, ADD_ON_DIP...) točnija od drugih?
- Postoje li ponavljajući razlozi za pogrešne pozive (iz reasoning teksta)?
- Ima li nešto specifično za dionice/sektore koje se ponavljaju?

VAŽNO — ovaj tekst se ubacuje kao dodatni kontekst u SVAKI budući pojedinačni prompt za analizu dionice (desetke puta tjedno), pa mora biti kratak da ne troši nepotrebno tokene:
- Vrati ISKLJUČIVO 4-6 bullet redaka, svaki max 1 rečenica (bez naslova, bez markdowna, bez sažetka statistike, bez uvoda/zaključka)
- Svaki redak piši kao direktnu instrukciju budućem sebi (npr. "Budi stroži prema WATCHLIST pozivima s confidence < 6 — X/Y takvih poziva bilo je pogrešno.")
- Ukupno max ~120 riječi"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "max_tokens":
        print("[run_quarterly_review] WARNING: response hit max_tokens — lessons text may be truncated.")

    return response.content[0].text.strip()


def save_lessons(period_label: str, decisions_count: int, lessons_text: str) -> None:
    client = get_supabase()
    if not client:
        print("[run_quarterly_review] Supabase credentials missing — cannot save.")
        return
    try:
        client.table("model_lessons").insert({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period_label": period_label,
            "decisions_analyzed": decisions_count,
            "lessons_text": lessons_text,
        }).execute()
        print("[run_quarterly_review] Saved lessons to Supabase.")
    except Exception as exc:
        print(f"[run_quarterly_review] Save failed: {exc}")


def main():
    today = datetime.now(timezone.utc)
    quarter = (today.month - 1) // 3 + 1
    period_label = f"Q{quarter} {today.year}"
    print(f"[run_quarterly_review] Starting quarterly review for {period_label}")

    decisions = get_resolved_decisions()
    print(f"[run_quarterly_review] Found {len(decisions)} decisions with a resolved outcome")

    if len(decisions) < 5:
        print("[run_quarterly_review] Too few resolved decisions for a meaningful review — skipping.")
        return

    lessons_text = generate_lessons(decisions, period_label)
    if not lessons_text:
        print("[run_quarterly_review] No lessons generated — skipping save.")
        return

    print("[run_quarterly_review] Lessons:")
    print(lessons_text)

    save_lessons(period_label, len(decisions), lessons_text)
    print("[run_quarterly_review] Done.")


if __name__ == "__main__":
    main()
