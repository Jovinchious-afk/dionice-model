"""
Quarterly learning report — runs on the 1st of Jan/Apr/Jul/Oct, after the
universe refresh.

  1. Statistics, free (analysis/learning.py): how the calls did against the
     S&P 500 by action, confidence, category and score; buy zone vs buying at
     once; which scorer criteria predicted returns; the investor's real trades
     against the same money in SPY.
  2. One call to SYNTHESIS_MODEL turns the tables into a Croatian report, 4-6
     lessons for the analyst prompt and bounded parameter proposals.
  3. Nothing changes by itself: lessons and parameters are stored as PROPOSED
     and the investor approves them on the Streamlit "Učenje" page.
  4. The report is emailed.

Replaces the earlier self-review, where Haiku read raw rows, did the arithmetic
itself and its lessons went into every prompt unreviewed.

Usage: python scripts/run_quarterly_review.py [--dry-run] [--no-ai]
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from analysis.ai_analyst import REVIEW_MODEL, parse_message
from analysis.email_sender import inline_markdown, markdown_to_html, send_email
from analysis.investor_profile import load_investor_profile
from analysis.learning import MIN_N_PROPOSAL, build_stats, stats_markdown
from analysis.params import DEFAULTS, DESCRIPTIONS, PROPOSABLE, load_params, validate_proposal
from analysis.portfolio import eur_usd_on
from analysis.prices import PriceBook
from analysis.scorer import resolve_weights
from analysis.supabase_client import get_supabase
from analysis.usage import UsageTracker

SYNTHESIS_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
LOOKBACK_DAYS = 400

SYNTHESIS_SYSTEM = """Ti si kvantitativni analitičar koji jednom po kvartalu pregledava rezultate AI sustava za preporuke dionica jednog malog ulagača iz Hrvatske (Revolut, 300-400 EUR mjesečno, value pristup: dosadne, profitabilne, podcijenjene firme). Pišeš NA HRVATSKOM.

Dobivaš tablice izračunate iz stvarnih podataka: povrat svake preporuke u odnosu na S&P 500 od dana preporuke, s brojem slučajeva (n) i 90% intervalom pouzdanosti. Tvoj posao je iz njih izvući ono što je stvarno pouzdano i odvojiti to od šuma.

PRAVILA:
- Svaki zaključak mora se oslanjati na brojke iz tablica. Navedi n i interval. Ne računaj ništa što nije u tablicama.
- Ako je n manji od 20 ili interval prelazi nulu, reci jasno da je to naznaka, a ne dokaz.
- Uzorak pokriva kratko razdoblje i jedan tržišni režim: ono što je radilo u rastućem tržištu ne mora raditi u padajućem. Napomeni to gdje je važno.
- Lekcije (lessons): 0-6 rečenica, svaka je izravna uputa analitičaru koji piše preporuke (npr. "Kod confidence 4-5 radije WAIT nego WATCHLIST: ta skupina je zaostala za S&P 500 (n=30, -1.7pp)."). Samo lekcije koje podaci podupiru; bez općenitih savjeta. Bolje manje lekcija nego nepouzdane.
- Prijedlozi parametara: samo ključevi s popisa, najviše jedan korak po kvartalu, i samo ako relevantna skupina ima barem 30 slučajeva i interval ne prelazi nulu. Ako ništa ne zadovoljava uvjete, vrati prazan popis — to je sasvim u redu.
- Izvještaj (report_markdown) čita ulagač: jasno, konkretno, bez žargona gdje nije nužan, 400-700 riječi. Odjeljci s naslovima (##): Sažetak (3-5 točaka), Što je radilo, Što nije radilo, Buy zona i plan ulaska, Confidence i filteri, Tvoj portfelj vs S&P 500, Predložene promjene, Što pratimo idući kvartal.
- Kad podaci proturječe ulagačevim pravilima ili tezama, reci to otvoreno, s brojkama.

PISMO: isključivo latinica."""


def synthesis_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "report_markdown": {"type": "string"},
            "lessons": {"type": "array", "items": {"type": "string"}},
            "parameter_proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": sorted(PROPOSABLE)},
                        "proposed_value": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["key", "proposed_value", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["report_markdown", "lessons", "parameter_proposals"],
        "additionalProperties": False,
    }


def period_label(today: datetime) -> str:
    """The quarter just ended when run in the first half of a quarter's first month, else the current one."""
    q = (today.month - 1) // 3 + 1
    if today.month in (1, 4, 7, 10) and today.day <= 15:
        q, year = (q - 1, today.year) if q > 1 else (4, today.year - 1)
        return f"Q{q} {year}"
    return f"Q{q} {today.year} (u tijeku, {today:%Y-%m-%d})"


def params_block(params: dict) -> str:
    lines = ["TRENUTNI PARAMETRI (ključ | vrijednost | zadano | dopušteni raspon i korak | opis):"]
    for key, spec in PROPOSABLE.items():
        lines.append(f"- {key} | {params.get(key)} | {DEFAULTS[key]} | {spec['min']}–{spec['max']}, korak {spec['step']} | {DESCRIPTIONS[key]}")
    lines.append(f"- concentration_cap | {params.get('concentration_cap')} | ulagačeva politika, ne predlaži promjenu | {DESCRIPTIONS['concentration_cap']}")
    return "\n".join(lines)


def calls_block(title: str, calls: list[dict]) -> str:
    if not calls:
        return f"{title}: nema"
    lines = [f"{title}:"]
    for c in calls:
        lines.append(f"- {c['date']} {c['symbol']} {c['action']} (conf {c['confidence']}): {c['excess']:+.1f}pp "
                     f"vs S&P nakon {c['horizon']} dana. Teza: {c['thesis']}")
    return "\n".join(lines)


def synthesize(claude, stats: dict, stats_md: str, params: dict, lessons: str | None,
               profile: str | None, usage: UsageTracker) -> dict | None:
    weight_note = ""
    if stats.get("weight_proposal"):
        weight_note = f"\nAUTOMATSKI IZRAČUNAT PRIJEDLOG TEŽINA SCOREA (samo ga objasni u izvještaju): {stats['weight_proposal']['reason']}"
    prompt = f"""STATISTIKA PREPORUKA:
{stats_md}

{params_block(params)}

TRENUTNO ODOBRENE LEKCIJE: {lessons or 'nema'}

{calls_block("NAJBOLJE KUPNJE", stats.get("best_calls") or [])}

{calls_block("NAJLOŠIJE KUPNJE", stats.get("worst_calls") or [])}
{weight_note}

Napiši kvartalni izvještaj, lekcije i prijedloge parametara prema shemi."""

    system = SYNTHESIS_SYSTEM + (f"\n\nPROFIL ULAGAČA (kontekst):\n{profile}" if profile else "")
    request = {
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": synthesis_schema()}},
    }
    try:
        # A declined request is re-run server-side on Anthropic's recommended fallback model
        message = claude.beta.messages.create(
            model=SYNTHESIS_MODEL, betas=[FALLBACK_BETA], fallbacks="default", **request,
        )
        usage.add_message(message, fallback_model=SYNTHESIS_MODEL)
        if message.stop_reason == "refusal":
            raise RuntimeError("synthesis model declined the request")
    except Exception as exc:
        print(f"[quarterly] {SYNTHESIS_MODEL} failed ({exc}) — retrying with {REVIEW_MODEL}")
        message = claude.messages.create(model=REVIEW_MODEL, **request)
        usage.add_message(message, fallback_model=REVIEW_MODEL)
    result, raw = parse_message(message)
    if result is None:
        print(f"[quarterly] Synthesis reply unreadable: {raw[:300]}")
    else:
        result["model"] = getattr(message, "model", SYNTHESIS_MODEL)
    return result


def checked_proposals(result: dict | None, stats: dict, params: dict) -> list[dict]:
    """AI proposals that pass the bounds and step limits, plus the computed weight proposal."""
    proposals = []
    for p in (result or {}).get("parameter_proposals") or []:
        ok, why = validate_proposal(p.get("key"), params.get(p.get("key")), p.get("proposed_value"))
        if not ok:
            print(f"[quarterly] Proposal rejected by guardrails: {why}")
            continue
        spec = PROPOSABLE[p["key"]]
        proposals.append({"key": p["key"], "value": spec["type"](p["proposed_value"]), "reason": p.get("reason", "")})
    weights = stats.get("weight_proposal")
    if weights:
        proposals.append({"key": "scorer_weights", "value": weights["weights"],
                          "reason": f"Kriteriji s značajnom korelacijom s povratom (n ≥ {MIN_N_PROPOSAL}): {weights['reason']}"})
    return proposals


def save_results(client, label: str, report_md: str, stats: dict, lessons: list[str],
                 proposals: list[dict], model: str | None) -> bool:
    """Stores the report and its PROPOSED lessons/parameters. False before schema_v8.sql."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        saved = client.table("learning_reports").insert({
            "created_at": now, "period_label": label, "report_markdown": report_md,
            "stats_json": json.loads(json.dumps(stats, default=str)), "model": model,
        }).execute().data or []
    except Exception as exc:
        print(f"[quarterly] Could not save the report (run data/schema_v8.sql?): {exc}")
        return False
    report_id = saved[0]["id"] if saved else None

    # Proposals nobody acted on last quarter are superseded by this report's
    for table in ("model_params", "model_lessons"):
        try:
            client.table(table).update({"status": "RETIRED" if table == "model_params" else "REJECTED"}) \
                .eq("status", "PROPOSED").execute()
        except Exception as exc:
            print(f"[quarterly] Could not retire old proposals in {table}: {exc}")

    if lessons:
        try:
            client.table("model_lessons").insert({
                "generated_at": now, "period_label": label,
                "decisions_analyzed": stats.get("decisions_collapsed"),
                "lessons_text": "\n".join(f"- {line.lstrip('- ').strip()}" for line in lessons),
                "status": "PROPOSED", "report_id": report_id,
            }).execute()
        except Exception as exc:
            print(f"[quarterly] Could not save lessons: {exc}")
    for p in proposals:
        try:
            client.table("model_params").insert({
                "key": p["key"], "value": p["value"], "status": "PROPOSED", "reason": p["reason"],
                "report_id": report_id, "created_at": now,
            }).execute()
        except Exception as exc:
            print(f"[quarterly] Could not save proposal {p['key']}: {exc}")
    return True


def build_email(label: str, report_md: str, stats_md: str, lessons: list[str], proposals: list[dict],
                params: dict, saved: bool, dry_run: bool = False) -> str:
    lessons_html = "".join(
        f"<li style='font-size:14px;margin:3px 0;'>{inline_markdown(line.lstrip('- ').strip())}</li>" for line in lessons
    ) if lessons else "<li>Nema dovoljno pouzdanih lekcija ovaj kvartal.</li>"
    proposal_rows = "".join(
        f"<tr><td style='padding:4px 8px;'>{p['key']}</td><td style='padding:4px 8px;'>{params.get(p['key']) if p['key'] != 'scorer_weights' else 'trenutne težine'}</td>"
        f"<td style='padding:4px 8px;font-weight:700;'>{p['value'] if p['key'] != 'scorer_weights' else 'nove težine'}</td>"
        f"<td style='padding:4px 8px;font-size:13px;'>{inline_markdown(p['reason'])}</td></tr>"
        for p in proposals
    )
    proposals_html = (
        f"<table style='border-collapse:collapse;font-size:14px;'><thead><tr style='background:#f0f0f0;'>"
        f"<th style='padding:4px 8px;text-align:left;'>Parametar</th><th style='padding:4px 8px;text-align:left;'>Sada</th>"
        f"<th style='padding:4px 8px;text-align:left;'>Prijedlog</th><th style='padding:4px 8px;text-align:left;'>Zašto</th>"
        f"</tr></thead><tbody>{proposal_rows}</tbody></table>"
        if proposals else "<p style='font-size:14px;'>Nema prijedloga promjena — podaci još ne opravdavaju promjenu parametara.</p>"
    )
    if dry_run:
        approve_note = "Probni izvještaj (dry run) — ništa nije spremljeno ni poslano."
    elif saved:
        approve_note = "Ništa se ne mijenja samo od sebe: lekcije i parametre odobravaš na stranici <strong>Učenje</strong> u aplikaciji."
    else:
        approve_note = "⚠️ Prijedlozi nisu spremljeni jer data/schema_v8.sql još nije pokrenut u Supabaseu."
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'></head>
<body style='font-family:Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;color:#222;'>
  <div style='background:#1a1a2e;color:white;padding:16px 20px;border-radius:8px;margin-bottom:20px;'>
    <h1 style='margin:0;font-size:20px;'>🧠 Dionice — kvartalni izvještaj učenja</h1>
    <p style='margin:4px 0 0;font-size:13px;opacity:0.8;'>{label}</p>
  </div>
  <p style='background:#e8f1fa;border-left:4px solid #1f5f8b;padding:8px 12px;font-size:13px;'>{approve_note}</p>
  {markdown_to_html(report_md)}
  <h3 style='font-size:17px;margin:18px 0 6px;'>Predložene lekcije za analitičara</h3>
  <ul style='padding-left:20px;'>{lessons_html}</ul>
  <h3 style='font-size:17px;margin:18px 0 6px;'>Predložene promjene parametara</h3>
  {proposals_html}
  <details style='margin-top:18px;'><summary style='cursor:pointer;font-size:14px;'>Sve tablice ▾</summary>
  {markdown_to_html(stats_md)}
  </details>
  <hr style='margin:24px 0;border:none;border-top:1px solid #eee;'>
  <p style='font-size:11px;color:#999;'>AI-generirana analiza isključivo u edukativne svrhe. Nije financijski savjet.</p>
</body></html>"""


def main(dry_run: bool = False, no_ai: bool = False):
    today = datetime.now(timezone.utc)
    label = period_label(today)
    print(f"[quarterly] Learning report {label}{' — DRY RUN' if dry_run else ''}{' — no AI' if no_ai else ''}")

    client = get_supabase()
    if not client:
        print("[quarterly] Supabase credentials missing.")
        return
    params = load_params(client)
    usage = UsageTracker("quarterly")
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    decisions = client.fetch_all("decisions", gte=("recommended_at", cutoff))
    watchlist = client.fetch_all("watchlist", "symbol,suggested_at,category,evidence_json", gte=("suggested_at", cutoff))
    analysis = client.fetch_all("analysis_log", "symbol,analyzed_at,action,confidence,model,snapshot",
                                gte=("analyzed_at", cutoff))
    transactions = client.fetch_all("transactions")
    print(f"[quarterly] Loaded {len(decisions)} decisions, {len(watchlist)} watchlist rows, "
          f"{len(analysis)} analysis_log rows, {len(transactions)} transactions")

    stats = build_stats(decisions, watchlist, analysis, transactions, PriceBook(), today, params,
                        resolve_weights(params.get("scorer_weights")), eur_usd_on)
    stats_md = stats_markdown(stats)
    print(stats_md)

    mature = (stats["overview"].get(30) or {}).get("n", 0)
    result = None
    if no_ai:
        print("[quarterly] --no-ai: statistics only")
    elif mature < 5:
        print(f"[quarterly] Only {mature} calls are 30+ days old — no AI synthesis this time")
    else:
        try:
            claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            lessons_now = None
            try:
                rows = (client.table("model_lessons").select("lessons_text").eq("status", "ACTIVE")
                        .order("generated_at", desc=True).limit(1).execute().data or [])
                lessons_now = rows[0]["lessons_text"] if rows else None
            except Exception:
                pass
            result = synthesize(claude, stats, stats_md, params, lessons_now, load_investor_profile(client), usage)
        except Exception as exc:
            print(f"[quarterly] AI synthesis failed: {exc}")

    report_md = (result or {}).get("report_markdown") or (
        "## Sažetak\n- Ovaj izvještaj sadrži samo statistiku (AI sažetak nije napravljen).\n\n" )
    lessons = [line for line in (result or {}).get("lessons") or [] if line.strip()]
    proposals = checked_proposals(result, stats, params)
    print(usage.summary())

    saved = False
    if not dry_run:
        saved = save_results(client, label, report_md, stats, lessons, proposals, (result or {}).get("model"))
        usage.save(client)
    html_body = build_email(label, report_md, stats_md, lessons, proposals, params, saved, dry_run)

    if dry_run:
        path = os.path.join(tempfile.gettempdir(), f"dionice_learning_{today:%Y-%m-%d}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_body)
        print(f"[quarterly] DRY RUN — report saved to {path}")
        print(f"[quarterly] Lessons: {lessons}")
        print(f"[quarterly] Proposals: {[(p['key'], p['value'] if p['key'] != 'scorer_weights' else '…') for p in proposals]}")
        return
    send_email(f"[Dionice] Kvartalni izvještaj učenja — {label}", html_body)
    print("[quarterly] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quarterly learning report")
    parser.add_argument("--dry-run", action="store_true", help="no email, no database writes")
    parser.add_argument("--no-ai", action="store_true", help="statistics only, no Claude call")
    args = parser.parse_args()
    main(dry_run=args.dry_run, no_ai=args.no_ai)
