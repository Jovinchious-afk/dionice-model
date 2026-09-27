"""
AI analyst module: turns fundamentals, the investor's own checklist, cycle/season
context, the model's previous calls on the same stock, StockTwits sentiment,
insider and Congress activity into structured recommendations.

Haiku 4.5 does the routine analysis, sent through the Message Batches API (half
price) with the shared part of the prompt cached. REVIEW_MODEL re-checks the few
calls that matter most: a SELL/REDUCE on a held position (the sell guard in
run_weekly) and BUY candidates (the second pass).

Replies are constrained to a JSON schema (structured outputs), so a reply can no
longer fail to parse — before this, about 6% of analyses were lost that way. The
evidence table is filled from the data, never copied by the model: it once wrote
AFL's $121.51 as "121,51", which was stored as $12,151.

Every recommendation is compared against "do nothing / hold cash / add to best
position", and the deterministic rules (hype block, confidence floor,
concentration cap, starter entry plan) are applied in finalize().
"""

import json
import re
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
# Re-checks sell calls on held positions that passed the deterministic sell guard,
# and BUY candidates before they reach the newsletter.
REVIEW_MODEL = "claude-sonnet-5"
PROMPT_VERSION = 3

BULLISH_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP"}
BEARISH_ACTIONS = {"SELL", "REDUCE"}
HELD_ACTIONS = ["HOLD", "ADD_ON_DIP", "REDUCE", "SELL"]
NOT_HELD_ACTIONS = ["BUY_BELOW", "WATCHLIST", "WAIT", "NO_ACTION"]
# One schema for every stock: the schema is part of the cached prompt prefix, so
# separate held / not-held schemas split the cache in two. The prompt names the
# allowed actions per stock and _normalize_action maps anything else.
ALL_ACTIONS = HELD_ACTIONS + NOT_HELD_ACTIONS
CATEGORIES = ["quality_compounder", "value_cyclical", "turnaround", "speculative_growth", "dividend_defensive"]

# Dropped from the fundamentals JSON sent to the model. debt_equity is yfinance's
# percent figure (49.0 means 0.49x) and was repeatedly read as "49x, extreme";
# debt_to_equity_x replaces it. Seasonality already appears in the cycle block.
_PROMPT_DROP_KEYS = {"debt_equity", "fetch_error", "cached", "seasonality"}

BATCH_TIMEOUT_S = 75 * 60
BATCH_POLL_S = 30


# --- Evidence table formatters ---
def _ev(val, decimals: int = 1, suffix: str = "") -> str:
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(val)


def _ev_pct(val, decimals: int = 1) -> str:
    """val is already a percentage value (e.g. fcf_yield=7.74)."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}%"
    except (TypeError, ValueError):
        return str(val)


def _ev_pct_decimal(val, decimals: int = 1) -> str:
    """val is a decimal fraction (e.g. op_margin=0.1557) → converts to %."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(val)


def _ev_growth(val, decimals: int = 1) -> str:
    """val is a decimal fraction → shows as ±X.X% YoY."""
    if val is None:
        return "N/A"
    try:
        pct = float(val) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.{decimals}f}% YoY"
    except (TypeError, ValueError):
        return str(val)


def _ev_price(val) -> str:
    if val is None:
        return "N/A"
    try:
        return f"${float(val):.2f}"
    except (TypeError, ValueError):
        return str(val)


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _debt_to_equity_x(fundamentals: dict) -> float | None:
    de_x = fundamentals.get("debt_to_equity_x")
    if de_x is None and fundamentals.get("debt_equity") is not None:
        de_x = float(fundamentals["debt_equity"]) / 100  # cache entries written before the field existed
    return de_x


_FOREIGN_SCRIPT = re.compile(r"[Ѐ-ӿ぀-ヿ㐀-䶿一-鿿가-힯]")


def strip_foreign_script(value):
    """
    Removes non-Latin characters that occasionally sneak into Claude's Croatian output —
    Cyrillic, and CJK after a Chinese word ("压力 je stvaran") reached a live newsletter.
    """
    if isinstance(value, str):
        return _FOREIGN_SCRIPT.sub("", value)
    if isinstance(value, list):
        return [strip_foreign_script(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_foreign_script(v) for k, v in value.items()}
    return value


def _response_text(message) -> str:
    """Joins the text blocks — with adaptive thinking the first block is a thinking block."""
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text").strip()


def _extract_json(raw_text: str) -> dict:
    """Last-resort parser for replies that did not use structured outputs."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.index("{"), text.rindex("}") + 1
    return json.loads(text[start:end])


def parse_message(message) -> tuple[dict | None, str]:
    """(parsed JSON or None, raw text). Refusals and truncated replies return None."""
    raw = _response_text(message)
    if getattr(message, "stop_reason", None) in ("max_tokens", "refusal"):
        print(f"[ai_analyst] WARNING: reply stopped with {message.stop_reason} ({getattr(message, 'model', '')})")
    try:
        return json.loads(raw), raw
    except (json.JSONDecodeError, ValueError):
        try:
            return _extract_json(raw), raw
        except (json.JSONDecodeError, ValueError):
            return None, raw


SYSTEM_PROMPT = """Ti si disciplinirani analitičar dioničkog tržišta koji piše na HRVATSKOM jeziku (uz financijske termine na engleskom: FCF, EBITDA, P/E, Debt/Equity, itd.).

KONTEKST ULAGAČA:
- Ulagač iz Hrvatske, platforma Revolut Basic
- UKUPNI KAPITAL: vrijednost pozicija + gotovina, naveden u kontekstu newslettera
- Može koristiti gotovinu, prodati dio pozicija i reinvestirati, ili uložiti novi novac (300-400 EUR/mj)
- NEMOJ tretirati ulagača kao da ima samo 300 EUR — to je NOVI novac koji dolazi, nije ukupni kapital
- 3-4 transakcija mjesečno maksimalno

OSNOVNA FILOZOFIJA:
- Dosadni, profitabilni i podcijenjeni biznisi ispred hype-a
- Svaka preporuka mora biti bolja od "ne raditi ništa" ili "povećati najbolju postojeću poziciju"
- "Nema kupnje ovaj tjedan" je validan i čest output — nije neuspjeh
- Hype je anti-signal: StockTwits hype iznad praga (naveden u kontekstu) znači nema kupnje
- Kongresne kupnje/prodaje su slabi signali — samo izvor ideja (prijave kasne 30-45 dana)
- Ako ulagač ima osobnu tezu za poziciju, POŠTUJ je — ne preporučuj SELL bez razloga koji joj izravno proturječi
- Diversifikacija: nijedna pozicija ne smije prijeći limit udjela u kapitalu (naveden u kontekstu)

VALUTA PRAVILO:
- BUY ZONE i TARGET PRICE uvijek u USD s $ znakom (npr. "< $25.00") jer sve dionice kotiraju u USD
- Position size kao % portfelja I kao EUR iznos (iznosi su izračunati u kontekstu)
- NIKAD ne koristiti EUR za buy_zone ili target_price

VELIČINA POZICIJE (jedina pravila; EUR iznosi su u kontekstu):
- mala: 3-5% portfelja — spekulativno, prvi ulazak, niži confidence
- normalna: 7-10% portfelja — solidno uvjerenje, dobar risk/reward
- velika: 12-20% portfelja — visoko uvjerenje, jasna podcijenjenost
- hidden gem: najviše 1-2% portfelja — gubitak 70-100% je moguć
- Ne ulaziti sve u jednu novu dionicu — diversifikacija je ključna
- Revolut naplaćuje ~1.5% FX konverziju (EUR→USD) — minimalni upside za isplativost je 5%+

BUY ZONE:
- Temelji je na procjeni vrijednosti (forward P/E vs industrija i vlastita povijest, FCF yield, PEG), NE na postotku ispod trenutne cijene
- Ne spuštaj zonu samo zato što je cijena pala; ako mijenjaš zonu u odnosu na zadnju analizu, objasni zašto u change_vs_last

AKCIJE:
- Dionice koje ulagač NE drži: BUY_BELOW, WATCHLIST, WAIT, NO_ACTION
- Dionice koje ulagač DRŽI: HOLD (zadano), ADD_ON_DIP, REDUCE, SELL
- HOLD je normalan ishod za dobru poziciju — ne traži akciju radi akcije

STROGA PRAVILA:
1. Preporučuj samo dionice dostupne na Revolut platformi
2. BUY_BELOW samo s konkretnom cijenom u USD, nikad otvoreni "kupi odmah"
3. Ako je confidence ispod praga za kupnju (naveden u kontekstu), nema kupnje: WAIT ili WATCHLIST (HOLD za poziciju koju ulagač drži)
4. Maksimalno 4-7 akcija po newsletteru
5. Poslovni model mora biti objašnjiv u 2-3 rečenice
6. NE preporučuj kupnju ako: hype bez fundamentala, dug raste brže od prihoda, marže padaju bez jasnog razloga

SIGNALI ZA PRODAJU (SELL ili REDUCE samo ako se promijenila VRIJEDNOST, ne cijena):
- Poslovanje se pogoršava: prihod pada, operativne marže padaju 3+ uzastopna kvartala, dobit pada
- Dug raste >20% YoY bez rasta prihoda
- Zalihe rastu 2x brže od prodaje
- Dividenda se reže
- Problem s managementom
- Valuacija postala ekstremna (daleko iznad fer vrijednosti)
- Pad cijene, negativna relativna snaga ili loš sentiment SAMI NISU razlog za prodaju
- Sustav automatski blokira SELL/REDUCE za pozicije u portfelju ako podaci ne pokazuju pogoršanje poslovanja

CIKLIČKE I SEZONSKE DIONICE:
- Prvo odredi tip: spori rast, brzi rast, ciklička, turnaround
- Kod cikličkih: visok trailing P/E na dnu ciklusa uz nizak forward P/E znači oporavak dobiti, ne skupoću; nizak P/E na vrhu ciklusa je upozorenje; prati zalihe
- Uzmi u obzir sezonu i pokretače iz bloka CIKLUS I SEZONA; povijesna sezonalnost je slab signal i tržište je već zna

PODACI (automatski izračunati):
- debt_to_equity_x: dug/kapital kao omjer (0.49 = dug iznosi 49% kapitala — umjereno, NIJE 49x)
- altman_z_score: rizik bankrota. Z > 2.99 sigurno, 1.81-2.99 sivo, < 1.81 distress. Za speculative_growth nizak Z je uobičajen — spomeni u red_flags samo ako je ekstremno nizak (< 0). Ne vrijedi za banke, osiguranja, REIT-ove i utility
- relative_strength_6m: dionica minus S&P 500 u zadnjih 6 mjeseci, u postotnim poenima
- inventory_growth_yoy, shares_change_yoy, debt_change_yoy, quarterly_revenue_growth_yoy: zadnji kvartal vs isti kvartal godinu ranije
- insider_buys_24m / insider_sells_24m: kupnje/prodaje insidera na tržištu u ~2 godine
- seasonality: isti kalendarski prozor u prošlim godinama vs S&P 500
- news_headlines: nedavni naslovi — procijeni sam relevantnost
- institutional_ownership: iz 13F prijava koje kasne kvartal, podijeljeno s današnjim brojem dionica. Vrijednost iznad 100% je greška u podacima (otkupi smanjili nazivnik, posuđene dionice brojane dvaput) — ne koristi je kao crvenu zastavicu
- Score usporedba P/E: medijan industrije ili sektora iz cijelog univerzuma dionica (naveden uz score)

PROFIL ULAGAČA (pozadinski kontekst, NE ide u newsletter):
- Profil je leća koja širi pogled, a ne razlog za odluku. Akciju određuju podaci, checklist i valuacija
- investor_view i counter_argument su interni: uvijek ih napiši i sukobi jedan s drugim, ali ulagač ih ne čita u mailu
- Kad se ulagačev makro pogled sukobi s brojkama, brojke pobjeđuju. Makro bilješke su pisane 10/2024 i mogu biti zastarjele; pravila o dionicama iz knjiga vrijede trajno

ODGOVOR: JSON prema zadanoj shemi. Svi tekstualni opisi MORAJU biti na HRVATSKOM jeziku.

PISMO: Koristi ISKLJUČIVO latinična slova (a-z, A-Z, hrvatska dijakritika: č,ć,š,ž,đ). NIKAD ćirilica, kineski, japanski ni korejski znakovi."""

FIELD_GUIDE = """POLJA ODGOVORA (redom kojim ih pišeš — prvo analiza, pa odluka):
- category: quality_compounder | value_cyclical | turnaround | speculative_growth | dividend_defensive
- business_explanation: 2 rečenice — čime se firma bavi i kako zarađuje
- valuation_verdict: jeftina/fer/skupa vs industrija s 2-3 ključne brojke, 1 rečenica
- cycle_view: gdje je dionica u ciklusu/sezoni i što to znači za idućih 3-6 mjeseci, 1 rečenica; "nije ciklička" ako nije
- change_vs_last: što se promijenilo od tvoje zadnje analize (brojke/činjenice) i zašto akcija ostaje ili se mijenja, 1 rečenica; "prva analiza" ako je nema
- investor_view: INTERNO — kako bi ULAGAČ ocijenio dionicu kroz svoj checklist i makro pogled, 1-2 rečenice; "N/A" ako profil nije naveden
- counter_argument: INTERNO — najjači protuargument ulagačevom pogledu (podaci, povijest, što tržište već zna), 1-2 rečenice; "N/A" ako profil nije naveden
- catalyst: konkretan događaj ili trend koji može otključati vrijednost u 6-18 mjeseci, 1 rečenica
- downside_scenario: što mora poći po zlu za gubitak 30-50%, konkretno, 1-2 rečenice
- vs_cash_alternative: je li bolje od ne raditi ništa / od dodavanja u najbolju postojeću poziciju, 1-2 rečenice
- thesis_breakers: 2 uvjeta koja bi poništila tezu — kratko
- red_flags: 1-3 konkretna rizika, kratko
- action: jedna od dopuštenih akcija navedenih uz dionicu
- buy_zone: npr. "< $25.00" ili "N/A"
- target_price: npr. "$32.00" ili "N/A"
- position_size: npr. "mala (3-5% / €500-750)" — stvarni iznosi iz VELIČINA POZICIJE; "N/A" za HOLD/WAIT/WATCHLIST
- investment_thesis: najviše 3 rečenice — zašto ova dionica, zašto sada
- confidence: cijeli broj 1-10"""


def analysis_schema() -> dict:
    """JSON schema for one stock analysis; property order is the order Claude writes them."""
    text = {"type": "string"}
    text_list = {"type": "array", "items": {"type": "string"}}
    properties = {
        "category": {"type": "string", "enum": CATEGORIES},
        "business_explanation": text,
        "valuation_verdict": text,
        "cycle_view": text,
        "change_vs_last": text,
        "investor_view": text,
        "counter_argument": text,
        "catalyst": text,
        "downside_scenario": text,
        "vs_cash_alternative": text,
        "thesis_breakers": text_list,
        "red_flags": text_list,
        "action": {"type": "string", "enum": ALL_ACTIONS},
        "buy_zone": text,
        "target_price": text,
        "position_size": text,
        "investment_thesis": text,
        "confidence": {"type": "integer", "enum": list(range(1, 11))},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_market_comment": {"type": "string"},
        "top_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "ticker": {"type": "string"},
                    "action": {"type": "string"},
                    "buy_zone": {"type": "string"},
                    "one_liner": {"type": "string"},
                },
                "required": ["rank", "ticker", "action", "buy_zone", "one_liner"],
                "additionalProperties": False,
            },
        },
        "watchlist_this_week": {"type": "array", "items": {"type": "string"}},
        "no_trade_reason": {"type": "string"},
        "portfolio_note": {"type": "string"},
    },
    "required": ["overall_market_comment", "top_actions", "watchlist_this_week", "no_trade_reason", "portfolio_note"],
    "additionalProperties": False,
}


def _position_guide(portfolio_value_eur: float | None) -> str:
    if not portfolio_value_eur or portfolio_value_eur <= 0:
        return ("VELIČINA POZICIJE: vrijednost portfelja nepoznata — koristi postotke "
                "(mala 3-5%, normalna 7-10%, velika 12-20%, hidden gem 1-2%)")
    v = portfolio_value_eur
    return (
        f"VELIČINA POZICIJE (ukupni kapital ≈ €{v:,.0f}):\n"
        f"  - mala (3-5%): €{v * 0.03:,.0f}–€{v * 0.05:,.0f} — spekulativno/prvi ulazak\n"
        f"  - normalna (7-10%): €{v * 0.07:,.0f}–€{v * 0.10:,.0f} — solidno uvjerenje\n"
        f"  - velika (12-20%): €{v * 0.12:,.0f}–€{v * 0.20:,.0f} — visoko uvjerenje\n"
        f"  - hidden gem (1-2%): €{v * 0.01:,.0f}–€{v * 0.02:,.0f}"
    )


def build_run_context(
    current_date: str,
    macro_context: str | None,
    lessons_context: str | None,
    portfolio_context: str,
    portfolio_value_eur: float | None,
    params: dict,
) -> str:
    """Everything that is identical for every stock in one newsletter run — the cached part of the prompt."""
    lessons_block = (
        f"\nNAUČENE LEKCIJE IZ PROŠLIH PREPORUKA (kvartalni izvještaj učenja, odobrio ulagač):\n{lessons_context}\n"
        if lessons_context else ""
    )
    return f"""KONTEKST OVOG NEWSLETTERA (isti za sve dionice u ovom izdanju):
DATUM: {current_date}
{macro_context or "Makro podaci nisu dostupni ovaj tjedan."}
{lessons_block}
{_position_guide(portfolio_value_eur)}

PORTFELJ ULAGAČA: {portfolio_context}

PRAVILA KOJA SUSTAV PROVODI AUTOMATSKI NAKON TVOJE ANALIZE:
- Kupnja (BUY_BELOW/ADD_ON_DIP) samo uz confidence >= {params['min_buy_confidence']}; ispod toga WAIT (HOLD za poziciju koju ulagač drži)
- StockTwits hype >= {params['hype_block_threshold']}/10 blokira kupnju
- Nijedna pozicija ne smije prijeći {params['concentration_cap'] * 100:.0f}% ukupnog kapitala: za poziciju koja je već iznad limita ADD_ON_DIP se mijenja u HOLD, zato ga tada ne predlaži
- Kupnje s confidence >= {params['starter_min_confidence']} dobivaju plan ulaska: ~{params['starter_fraction'] * 100:.0f}% pozicije odmah, ostatak na buy zoni"""


def build_system(investor_profile: str | None, run_context: str, cache_ttl: str | None = "5m") -> list[dict]:
    """
    System blocks: the stable instructions and the run context, cached as one prefix.
    cache_ttl None leaves caching off — for a one-off call nothing would read the entry.
    """
    stable = SYSTEM_PROMPT + "\n\n" + FIELD_GUIDE
    if investor_profile:
        stable += "\n\n" + investor_profile
    context_block = {"type": "text", "text": run_context}
    if cache_ttl:
        context_block["cache_control"] = {"type": "ephemeral"} if cache_ttl == "5m" else {"type": "ephemeral", "ttl": cache_ttl}
    return [{"type": "text", "text": stable}, context_block]


def build_stock_prompt(
    fundamentals: dict,
    score_result: dict,
    congress_signal: dict | None,
    insider_signal: dict | None,
    personal_thesis: str | None = None,
    macro_view: str | None = None,
    do_not_sell_until: str | None = None,
    is_hidden_gem: bool = False,
    sentiment_signal: dict | None = None,
    watchlist_context: str | None = None,
    sector_note: str | None = None,
    in_portfolio: bool = False,
    position_line: str | None = None,
    sell_triggers: str | None = None,
    previous_calls: str | None = None,
    checklist_text: str | None = None,
    cycle_context: str | None = None,
    review_note: str | None = None,
) -> str:
    """The stock-specific part of the prompt (the user message)."""
    symbol = fundamentals.get("symbol", "")
    congress_buys = (congress_signal or {}).get("buy_count", 0)
    congress_sells = (congress_signal or {}).get("sell_count", 0)
    insider_text = insider_signal.get("note", "") if insider_signal else "Nema insider Form 4 aktivnosti u zadnjih 14 dana"

    sentiment_block = f"\nSTOCKTWITS SENTIMENT:\n{sentiment_signal.get('bull_bear_summary', '')}" if sentiment_signal else ""

    earnings_block = ""
    earnings_days = fundamentals.get("next_earnings_days")
    earnings_date = fundamentals.get("next_earnings_date", "N/A")
    if earnings_days is not None:
        if 0 <= earnings_days <= 7:
            earnings_block = f"\n⚠️ UPOZORENJE: Earnings za {earnings_days} dana ({earnings_date}) — VISOK rizik volatilnosti! Ne preporučaj kupnju tik pred earnings osim s iznimno visokim uvjerenjem."
        elif 8 <= earnings_days <= 30:
            earnings_block = f"\nINFO: Earnings za {earnings_days} dana ({earnings_date}) — napomeni u thesis."

    week52_block = ""
    week52_pos = fundamentals.get("week_52_position_pct")
    if week52_pos is not None:
        if week52_pos >= 85:
            week52_block = f"\n⚠️ 52-TJEDNA POZICIJA: {week52_pos:.0f}% od godišnjeg raspona — dionica je blizu vrha, postavi konzervativniji buy_zone i manji position size."
        elif week52_pos <= 15:
            week52_block = f"\nINFO 52-tjedna pozicija: {week52_pos:.0f}% od godišnjeg raspona — dionica je blizu godišnjeg dna, potencijalna value prilika ako su fundamentali solidni."

    watchlist_block = f"\nWATCHLIST POVIJEST: {watchlist_context}" if watchlist_context else ""
    sector_block = f"\nSEKTORSKA KONCENTRACIJA: {sector_note}" if sector_note else ""

    if in_portfolio:
        holding_block = f"\n📌 ULAGAČ DRŽI OVU DIONICU. {position_line or ''}\nDopuštene akcije: {', '.join(HELD_ACTIONS)} (HOLD je zadano)."
    else:
        holding_block = f"\nUlagač NE drži ovu dionicu. Dopuštene akcije: {', '.join(NOT_HELD_ACTIONS)}."

    gem_context = ""
    if is_hidden_gem:
        gem_context = """
💎 HIDDEN GEM ANALIZA — POSEBNA PRAVILA:
- Ova dionica je odabrana kao potencijalni "hidden gem" — cijena ispod $12, sektor s dugoročnim potencijalom
- Vremenski horizont: 5-10 godina (ne 6-18 mjeseci kao za mainstream dionice)
- Primjer referentnog scenarija: Nvidia 2016-2017, Amazon 2003-2005, Microsoft 2012-2014
- Dopuštene akcije: WATCHLIST (idealno za praćenje), BUY_BELOW s malom pozicijom, ili WAIT
- Pozicija najviše 1-2% portfelja jer je rizik visok — ovo je spekulativna oklada
- Ako nema jasne poslovne teze ili je market cap < $50M, preporuči WAIT
- Naglasi: "Ovo je visoko rizična spekulativna pozicija. Gubitak 70-100% je moguć."
"""

    personal_context = ""
    if personal_thesis or macro_view or do_not_sell_until or sell_triggers:
        personal_context = f"""
ULAGAČEVA OSOBNA TEZA ZA OVU DIONICU (OBAVEZNO POŠTUJ):
- Osobna teza: {personal_thesis or 'nije definirana'}
- Makro pogled: {macro_view or 'nije definiran'}
- Ne prodavati dok: {do_not_sell_until or 'nije definirano'}
- Što bi ulagača natjeralo na prodaju: {sell_triggers or 'nije definirano'}
UPOZORENJE: Ne preporučuj SELL ili REDUCE ako se nije ostvario neki od ulagačevih razloga za prodaju ili ako podaci izravno ne pobijaju tezu.
"""

    review_block = f"\n🔎 DRUGA PROVJERA:\n{review_note}" if review_note else ""
    previous_block = (
        f"\n{previous_calls}" if previous_calls
        else "\nTVOJE PRETHODNE ANALIZE: nema (prva analiza ove dionice u zadnjih 60 dana)."
    )
    checklist_block = f"\n{checklist_text}" if checklist_text else ""
    cycle_block = f"\nCIKLUS I SEZONA:\n{cycle_context}" if cycle_context else ""

    prompt_fund = {k: v for k, v in fundamentals.items() if k not in _PROMPT_DROP_KEYS and v is not None}
    de_x = _debt_to_equity_x(fundamentals)
    if de_x is not None:
        prompt_fund["debt_to_equity_x"] = round(de_x, 2)
    breakdown = ", ".join(
        f"{name}={b.get('raw') if b.get('raw') is not None else 'n/a'}×{b.get('weight')}"
        for name, b in (score_result.get("breakdown") or {}).items()
    )
    peer_line = f" | P/E uspoređen s: {score_result['peer_pe']} ({score_result.get('peer_pe_source', '')})" if score_result.get("peer_pe") else ""
    coverage = score_result.get("coverage")
    coverage_line = f" | pokrivenost podacima {coverage * 100:.0f}%" if coverage is not None else ""

    return f"""Analiziraj dionicu {symbol} ({fundamentals.get('name', '')}) NA HRVATSKOM JEZIKU (financijski termini mogu ostati na engleskom).
{holding_block}{gem_context}{personal_context}{review_block}{earnings_block}{week52_block}{watchlist_block}{sector_block}{sentiment_block}
{previous_block}
{checklist_block}
{cycle_block}

FUNDAMENTALNI PODACI:
{json.dumps(prompt_fund, ensure_ascii=False, separators=(",", ":"), default=str)}

FUNDAMENTAL SCORE: {score_result.get('total_score', 0)}/100 (kategorija: {score_result.get('category', 'unknown')}){peer_line}{coverage_line}
Score breakdown (ocjena 1-5 × težina; n/a = podatak ne postoji): {breakdown}

CONGRESS TRADES (zadnjih 14 dana — slab signal):
- Članovi kupuju: {congress_buys}
- Članovi prodaju: {congress_sells}
- Napomena: {(congress_signal or {}).get('note', 'Nema nedavnih kongresnih trgovanja')}

INSIDER TRADING (SEC Form 4, kupnje/prodaje na tržištu, zadnjih 14 dana — svježiji signal od Kongresa, 2 dana kašnjenja):
- {insider_text}"""


def request_params(
    system: list[dict],
    user_prompt: str,
    model: str = MODEL,
    effort: str | None = None,
) -> dict:
    """Messages API parameters for one analysis — used as-is by messages.create and by batches."""
    output_config: dict = {"format": {"type": "json_schema", "schema": analysis_schema()}}
    if effort and model != MODEL:  # Haiku 4.5 has no effort parameter
        output_config["effort"] = effort
    return {
        "model": model,
        # Haiku's complete answers measured 1.3-1.9K output tokens; the review model
        # thinks adaptively before answering, which needs more room.
        "max_tokens": 4000 if model == MODEL else 16000,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
        "output_config": output_config,
    }


def _normalize_action(result: dict, in_portfolio: bool) -> None:
    """HOLD/REDUCE/SELL only make sense for stocks the investor owns, BUY_BELOW only for new ones."""
    if in_portfolio:
        mapping = {"BUY_BELOW": "ADD_ON_DIP", "WATCHLIST": "HOLD", "WAIT": "HOLD", "NO_ACTION": "HOLD"}
    else:
        mapping = {"ADD_ON_DIP": "BUY_BELOW", "HOLD": "WAIT", "SELL": "NO_ACTION", "REDUCE": "NO_ACTION"}
    if result.get("action") in mapping:
        result["action"] = mapping[result["action"]]


def build_evidence(ctx: dict, rec: dict) -> dict:
    """The evidence table shown in the email, straight from the data."""
    fundamentals = ctx["fundamentals"]
    score_result = ctx["score_result"]
    sentiment = ctx.get("sentiment_signal")
    congress = ctx.get("congress_signal") or {}
    insider = ctx.get("insider_signal")
    seasonality = fundamentals.get("seasonality")
    earnings_days = fundamentals.get("next_earnings_days")
    altman_z = _num(fundamentals.get("altman_z_score"))
    rel_strength = _num(fundamentals.get("relative_strength_6m"))
    return {
        "current_price": _ev_price(fundamentals.get("current_price")),
        "buy_zone": rec.get("buy_zone") or "N/A",
        "pe": _ev(fundamentals.get("pe")),
        "forward_pe": _ev(fundamentals.get("forward_pe")),
        "peg": _ev(fundamentals.get("peg"), decimals=2),
        "debt_equity": _ev(_debt_to_equity_x(fundamentals), decimals=2, suffix="x"),
        "revenue_growth": _ev_growth(fundamentals.get("revenue_growth_yoy")),
        "fcf_yield": _ev_pct(fundamentals.get("fcf_yield"), decimals=2),
        "op_margin": _ev_pct_decimal(fundamentals.get("op_margin")),
        "insider_signal": insider.get("note", "") if insider else "Nema insider Form 4 aktivnosti u zadnjih 14 dana",
        "congress_signal": f"Weak — {congress.get('buy_count', 0)} buy(s), {congress.get('sell_count', 0)} sell(s)",
        "stocktwits": (
            f"{sentiment.get('bullish_pct', 0):.0f}%↑ / {sentiment.get('bearish_pct', 0):.0f}%↓ | "
            f"~{sentiment.get('msgs_per_day', 0):.1f} poruka/dan | hype: {sentiment.get('hype_score', 0)}/10"
            if sentiment else "N/A"
        ),
        "earnings_in": f"{earnings_days}d ({fundamentals.get('next_earnings_date')})" if earnings_days is not None else "N/A",
        "seasonality": (
            f"{seasonality['window']}: medijan {seasonality['median_excess_pp']:+.1f} pp vs S&P, "
            f"{seasonality['beat_spy_years']}/{seasonality['years']} god."
            if seasonality else "N/A"
        ),
        "altman_z_score": f"{altman_z:.2f}" if altman_z is not None else "N/A",
        "relative_strength_6m": f"{rel_strength:+.1f}pp" if rel_strength is not None else "N/A",
        "fundamental_score": f"{score_result.get('total_score', 0)}/100 ({score_result.get('category', '')})",
        "confidence": f"{rec.get('confidence', 'N/A')}/10",
    }


def finalize(result: dict | None, ctx: dict, model: str, raw_text: str = "") -> dict:
    """
    Turns the model's JSON into a recommendation and applies every deterministic
    rule: allowed actions, hype block, confidence floor, concentration cap and
    the starter entry plan. ctx holds the stock's data and the run parameters.
    """
    fundamentals = ctx["fundamentals"]
    in_portfolio = ctx.get("in_portfolio", False)
    params = ctx["params"]

    if result is None:
        rec = {
            "action": "HOLD" if in_portfolio else "NO_ACTION",
            "confidence": 0,
            "error": f"Failed to parse AI response: {raw_text[:200]}",
        }
    else:
        rec = strip_foreign_script(result)
        try:
            rec["confidence"] = int(min(10, max(1, round(float(rec.get("confidence", 1))))))
        except (TypeError, ValueError):
            rec["confidence"] = 1

    rec["ticker"] = fundamentals.get("symbol", "")
    # Claude has renamed companies in the newsletter (BEN as "Franklin Templeton"),
    # so the name always comes from the data
    rec["company_name"] = fundamentals.get("name") or rec["ticker"]
    rec["is_hidden_gem"] = ctx.get("is_hidden_gem", False)
    rec["in_portfolio"] = in_portfolio
    rec["model"] = model
    if result is None:
        rec["evidence_table"] = build_evidence(ctx, rec)
        return rec

    _normalize_action(rec, in_portfolio)
    no_buy_action = "HOLD" if in_portfolio else "WATCHLIST"
    confidence = rec["confidence"]

    hype = (ctx.get("sentiment_signal") or {}).get("hype_score", 0)
    if hype >= params["hype_block_threshold"] and rec.get("action") in BULLISH_ACTIONS:
        rec["action"] = no_buy_action
        rec["hype_override"] = True
        rec["hype_note"] = f"Kupnja spuštena na {no_buy_action} — StockTwits hype {hype}/10 (prag {params['hype_block_threshold']})"

    if confidence < params["min_buy_confidence"] and rec.get("action") in BULLISH_ACTIONS:
        rec["action"] = "HOLD" if in_portfolio else "WAIT"

    weight = ctx.get("position_weight")
    cap = params["concentration_cap"]
    if in_portfolio and rec.get("action") == "ADD_ON_DIP" and weight is not None and weight >= cap:
        rec["action"] = "HOLD"
        rec["concentration_note"] = (
            f"ADD_ON_DIP blokiran: pozicija je već {weight * 100:.0f}% ukupnog kapitala (limit {cap * 100:.0f}%). "
            "Teza ostaje — ovo nije poziv na prodaju, nego na to da novi novac ide u druge dionice."
        )

    if rec.get("action") in BULLISH_ACTIONS and confidence >= params["starter_min_confidence"]:
        fraction = params["starter_fraction"]
        price = _num(fundamentals.get("current_price"))
        price_txt = f" (oko ${price:,.2f})" if price else ""
        rec["entry_plan"] = "starter"
        rec["entry_plan_note"] = (
            f"Plan ulaska: ~{fraction * 100:.0f}% pozicije odmah po tržišnoj cijeni{price_txt}, "
            f"ostatak kad cijena dođe u zonu {rec.get('buy_zone') or 'N/A'}. Kod visokog uvjerenja čekanje "
            "cijele zone često propusti rast — kvartalni izvještaj učenja mjeri koja strategija bolje radi."
        )

    rec["evidence_table"] = build_evidence(ctx, rec)
    return rec


def call_claude(client: anthropic.Anthropic, params: dict, usage=None):
    """One synchronous Messages API call; usage is recorded when a tracker is given."""
    message = client.messages.create(**params)
    if usage is not None:
        usage.add_message(message, batch=False, fallback_model=params["model"])
    return message


def analyze_stock(
    client: anthropic.Anthropic,
    system: list[dict],
    prompt_kwargs: dict,
    ctx: dict,
    model: str = MODEL,
    review_note: str | None = None,
    effort: str | None = None,
    usage=None,
) -> dict:
    """Analyzes a single stock synchronously and returns a finalized recommendation."""
    user_prompt = build_stock_prompt(**prompt_kwargs, review_note=review_note)
    params = request_params(system, user_prompt, model=model, effort=effort)
    message = call_claude(client, params, usage)
    result, raw = parse_message(message)
    return finalize(result, ctx, model, raw)


def prewarm_cache(client: anthropic.Anthropic, system: list[dict], usage=None) -> None:
    """
    Writes the shared prefix to the cache once before the batch. Batch requests run
    concurrently and caching is best-effort there: in 3-request test batches 0 of 3
    read the cache without this and 1 of 3 with it. That is why the batch uses the
    5-minute TTL — a 1.25x write pays off above ~22% hits, the 1-hour TTL's 2x write
    only above ~53%. llm_usage records the real reads and writes of every run.
    max_tokens=0 is not accepted together with structured outputs, hence 1.
    """
    params = request_params(system, "warmup")
    params["max_tokens"] = 1
    try:
        message = call_claude(client, params, usage)
        written = getattr(message.usage, "cache_creation_input_tokens", 0) or 0
        read = getattr(message.usage, "cache_read_input_tokens", 0) or 0
        print(f"[ai_analyst] Cache pre-warm: {written:,} tokens written, {read:,} already cached")
    except Exception as exc:
        print(f"[ai_analyst] Cache pre-warm failed (non-critical): {exc}")


def _custom_ids(keys: list[str]) -> dict[str, str]:
    """Batch custom_ids must match ^[a-zA-Z0-9_-]{1,64}$ and be unique."""
    out: dict[str, str] = {}
    for i, key in enumerate(keys):
        cid = re.sub(r"[^A-Za-z0-9_-]", "_", key)[:60] or f"req{i}"
        while cid in out:
            cid = f"{cid[:56]}_{i}"
        out[cid] = key
    return out


def run_batch(client: anthropic.Anthropic, requests: dict[str, dict], usage=None,
              timeout_s: int = BATCH_TIMEOUT_S, poll_s: int = BATCH_POLL_S) -> dict:
    """
    Sends all requests as one Message Batch (half price) and waits for it.
    Returns {key: Message} for the requests that succeeded; the caller retries the
    rest synchronously. A batch still running after timeout_s is cancelled.
    """
    if not requests:
        return {}
    id_map = _custom_ids(list(requests))
    try:
        batch = client.messages.batches.create(
            requests=[{"custom_id": cid, "params": requests[key]} for cid, key in id_map.items()]
        )
    except Exception as exc:
        print(f"[ai_analyst] Batch creation failed ({exc}) — falling back to synchronous calls")
        return {}

    print(f"[ai_analyst] Batch {batch.id}: {len(requests)} requests submitted")
    started = time.monotonic()
    last_report = 0.0
    while batch.processing_status != "ended":
        elapsed = time.monotonic() - started
        if elapsed > timeout_s:
            print(f"[ai_analyst] Batch still running after {timeout_s // 60} min — cancelling")
            try:
                client.messages.batches.cancel(batch.id)
            except Exception as exc:
                print(f"[ai_analyst] Cancel failed: {exc}")
            for _ in range(12):  # cancellation settles within a few minutes
                time.sleep(15)
                try:
                    batch = client.messages.batches.retrieve(batch.id)
                except Exception as exc:
                    print(f"[ai_analyst] Batch status check failed: {exc}")
                    continue
                if batch.processing_status == "ended":
                    break
            break
        if elapsed - last_report >= 120:
            counts = batch.request_counts
            print(f"[ai_analyst] Batch {batch.processing_status}: {counts.succeeded} done, "
                  f"{counts.processing} processing ({elapsed / 60:.0f} min)")
            last_report = elapsed
        time.sleep(poll_s)
        try:
            batch = client.messages.batches.retrieve(batch.id)
        except Exception as exc:
            print(f"[ai_analyst] Batch status check failed: {exc}")

    messages: dict = {}
    if batch.processing_status != "ended":
        return messages
    try:
        for item in client.messages.batches.results(batch.id):
            key = id_map.get(item.custom_id)
            if key is None:
                continue
            if item.result.type == "succeeded":
                messages[key] = item.result.message
                if usage is not None:
                    usage.add_message(item.result.message, batch=True, fallback_model=requests[key]["model"])
            else:
                print(f"[ai_analyst] Batch request {key}: {item.result.type}")
    except Exception as exc:
        print(f"[ai_analyst] Reading batch results failed: {exc}")
    print(f"[ai_analyst] Batch finished in {(time.monotonic() - started) / 60:.1f} min: "
          f"{len(messages)}/{len(requests)} succeeded")
    return messages


def summary_subject_suffix(recommendations: list[dict], no_trade: bool) -> str:
    """Deterministic subject line — Claude used to invent a different format every run."""
    counts = {"BUY": 0, "HOLD": 0, "WATCHLIST": 0, "SELL": 0}
    for rec in recommendations:
        action = rec.get("action")
        if action in BULLISH_ACTIONS:
            counts["BUY"] += 1
        elif action == "HOLD":
            counts["HOLD"] += 1
        elif action == "WATCHLIST":
            counts["WATCHLIST"] += 1
        elif action in BEARISH_ACTIONS:
            counts["SELL"] += 1
    text = ", ".join(f"{n} {name}" for name, n in counts.items())
    return f"{text} — NO TRADE" if no_trade and counts["BUY"] == 0 else text


def generate_weekly_summary(
    client: anthropic.Anthropic,
    system: list[dict],
    all_recommendations: list[dict],
    current_date: str,
    usage=None,
) -> dict:
    """Generates the newsletter summary from the individual recommendations."""
    slim_recs = [
        {
            "ticker": r.get("ticker"),
            "action": r.get("action"),
            "confidence": r.get("confidence"),
            "buy_zone": r.get("buy_zone"),
            "target_price": r.get("target_price"),
            "category": r.get("category"),
            "is_hidden_gem": r.get("is_hidden_gem", False),
            "in_portfolio": r.get("in_portfolio", False),
            "sell_guard_note": r.get("sell_guard_note"),
            "concentration_note": r.get("concentration_note"),
            "entry_plan_note": r.get("entry_plan_note"),
        }
        for r in all_recommendations
    ]

    prompt = f"""Na temelju individualnih analiza dionica, napravi sažetak tjednog newslettera NA HRVATSKOM JEZIKU.
Ova poruka NIJE analiza jedne dionice: vrati sažetak prema zadanoj shemi.

DATUM: {current_date}

INDIVIDUALNE ANALIZE:
{json.dumps(slim_recs, ensure_ascii=False, indent=2, default=str)}

Pravila:
- top_actions: odaberi max 4-7 ukupnih akcija (BUY_BELOW, ADD_ON_DIP, HOLD, WATCHLIST, WAIT, SELL, REDUCE, NO_TRADE); buy_zone "N/A" ako je nema
- Ako nijedna dionica ne zadovoljava kriterije kvalitete, no_trade_reason objašnjava zašto nema jakih kupnji; inače prazan string
- Prioritet: postojeće pozicije u portfelju prvo (in_portfolio=true), zatim nove ideje
- Ako je sell_guard_note ili concentration_note postavljen, spomeni ga u portfolio_note
- overall_market_comment: 2 rečenice o trenutnom tržišnom okruženju
- portfolio_note: napomena o pozicijama u portfelju (HOLD/ADD/REDUCE), koncentraciji i gotovini
- Sve piši NA HRVATSKOM"""

    params = {
        "model": MODEL,
        "max_tokens": 2000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
    }
    try:
        message = call_claude(client, params, usage)
        summary, _ = parse_message(message)
    except Exception as exc:
        print(f"[ai_analyst] Weekly summary failed: {exc}")
        summary = None
    if not summary:
        summary = {
            "overall_market_comment": "Tržišna analiza trenutno nije dostupna.",
            "top_actions": [],
            "watchlist_this_week": [],
            "no_trade_reason": "",
            "portfolio_note": "",
        }
    summary = strip_foreign_script(summary)
    summary["date"] = current_date
    summary["no_trade_reason"] = summary.get("no_trade_reason") or None
    summary["email_subject_suffix"] = summary_subject_suffix(all_recommendations, bool(summary["no_trade_reason"]))
    return summary
