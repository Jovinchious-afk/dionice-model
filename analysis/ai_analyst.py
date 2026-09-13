"""
AI analyst module: turns fundamentals, the investor's own checklist, cycle/season
context, the model's previous calls on the same stock, StockTwits sentiment,
insider and Congress activity into structured recommendations.

Haiku 4.5 does the routine analysis. A SELL/REDUCE on a position the investor
holds is re-checked by REVIEW_MODEL (see run_weekly.apply_sell_guard).
Every recommendation is compared against "do nothing / hold cash / add to best position".
"""

import json
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
# Only re-checks sell calls on held positions that passed the deterministic sell
# guard — a handful of calls a month, so the stronger model costs cents.
REVIEW_MODEL = "claude-sonnet-5"

BULLISH_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP"}
BEARISH_ACTIONS = {"SELL", "REDUCE"}
HELD_ACTIONS = "HOLD|ADD_ON_DIP|REDUCE|SELL"
NOT_HELD_ACTIONS = "BUY_BELOW|WATCHLIST|WAIT|NO_ACTION"

# Dropped from the fundamentals JSON sent to the model. debt_equity is yfinance's
# percent figure (49.0 means 0.49x) and was repeatedly read as "49x, extreme";
# debt_to_equity_x replaces it.
_PROMPT_DROP_KEYS = {"debt_equity", "fetch_error", "cached"}


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


def _debt_to_equity_x(fundamentals: dict) -> float | None:
    de_x = fundamentals.get("debt_to_equity_x")
    if de_x is None and fundamentals.get("debt_equity") is not None:
        de_x = float(fundamentals["debt_equity"]) / 100  # cache entries written before the field existed
    return de_x


def _sanitize_cyrillic(text: str) -> str:
    """Remove Cyrillic characters that occasionally sneak into Claude's Croatian output."""
    return re.sub(r"[Ѐ-ӿ]", "", text)


def _response_text(response) -> str:
    """Joins the text blocks — with adaptive thinking the first block is a thinking block."""
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()


def _extract_json(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.index("{"), text.rindex("}") + 1
    return json.loads(text[start:end])


SYSTEM_PROMPT = """Ti si disciplinirani analitičar dioničkog tržišta koji piše na HRVATSKOM jeziku (uz financijske termine na engleskom: FCF, EBITDA, P/E, Debt/Equity, itd.).

KONTEKST ULAGAČA:
- Ulagač iz Hrvatske, platforma Revolut Basic
- UKUPNI KAPITAL: vrijednost pozicija + gotovina, naveden u promptu
- Može koristiti gotovinu, prodati dio pozicija i reinvestirati, ili uložiti novi novac (300-400 EUR/mj)
- NEMOJ tretirati ulagača kao da ima samo 300 EUR — to je NOVI novac koji dolazi, nije ukupni kapital
- 3-4 transakcija mjesečno maksimalno

OSNOVNA FILOZOFIJA:
- Dosadni, profitabilni i podcijenjeni biznisi ispred hype-a
- Svaka preporuka mora biti bolja od "ne raditi ništa" ili "povećati najbolju postojeću poziciju"
- "Nema kupnje ovaj tjedan" je validan i čest output — nije neuspjeh
- Hype je anti-signal: ako je hype_score >= 7 (StockTwits), nema kupnje
- Kongresne kupnje/prodaje su slabi signali — samo izvor ideja (prijave kasne 30-45 dana)
- Ako ulagač ima osobnu tezu za poziciju, POŠTUJ je — ne preporučuj SELL bez razloga koji joj izravno proturječi

VALUTA PRAVILO:
- BUY ZONE i TARGET PRICE uvijek u USD s $ znakom (npr. "< $25.00") jer sve dionice kotiraju u USD
- Position size kao % portfelja I kao EUR iznos (iznosi su izračunati u promptu)
- NIKAD ne koristiti EUR za buy_zone ili target_price

VELIČINA POZICIJE (jedina pravila; EUR iznosi su u promptu):
- mala: 3-5% portfelja — spekulativno, prvi ulazak, niži confidence
- normalna: 7-10% portfelja — solidno uvjerenje, dobar risk/reward
- velika: 12-20% portfelja — visoko uvjerenje, jasna podcijenjenost
- hidden gem: najviše 1-2% portfelja — gubitak 70-100% je moguć
- Ne ulaziti sve u jednu novu dionicu — diversifikacija je ključna
- Revolut naplaćuje ~1.5% FX konverziju (EUR→USD) — minimalni upside za isplativost je 5%+

BUY ZONE:
- Temelji je na procjeni vrijednosti (forward P/E vs sektor i vlastita povijest, FCF yield, PEG), NE na postotku ispod trenutne cijene
- Ne spuštaj zonu samo zato što je cijena pala; ako mijenjaš zonu u odnosu na zadnju analizu, objasni zašto u change_vs_last

AKCIJE:
- Dionice koje ulagač NE drži: BUY_BELOW, WATCHLIST, WAIT, NO_ACTION
- Dionice koje ulagač DRŽI: HOLD (zadano), ADD_ON_DIP, REDUCE, SELL
- HOLD je normalan ishod za dobru poziciju — ne traži akciju radi akcije

STROGA PRAVILA:
1. Preporučuj samo dionice dostupne na Revolut platformi
2. BUY_BELOW samo s konkretnom cijenom u USD, nikad otvoreni "kupi odmah"
3. Ako je confidence < 6, nema kupnje: WAIT ili WATCHLIST (HOLD za poziciju koju ulagač drži)
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

OUTPUT FORMAT: Vraćaj SAMO valjani JSON, bez markdowna, bez teksta izvan JSONa.
Svi tekstualni opisi MORAJU biti na HRVATSKOM jeziku.

PISMO: Koristi ISKLJUČIVO latinična slova (a-z, A-Z, hrvatska dijakritika: č,ć,š,ž,đ). NIKAD ne koristi ćirilična slova."""


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


def _normalize_action(result: dict, in_portfolio: bool) -> None:
    """HOLD/REDUCE/SELL only make sense for stocks the investor owns, BUY_BELOW only for new ones."""
    if in_portfolio:
        mapping = {"BUY_BELOW": "ADD_ON_DIP", "WATCHLIST": "HOLD", "WAIT": "HOLD", "NO_ACTION": "HOLD"}
    else:
        mapping = {"ADD_ON_DIP": "BUY_BELOW", "HOLD": "WAIT", "SELL": "NO_ACTION", "REDUCE": "NO_ACTION"}
    if result.get("action") in mapping:
        result["action"] = mapping[result["action"]]


def analyze_stock(
    fundamentals: dict,
    score_result: dict,
    congress_signal: dict | None,
    insider_signal: dict | None,
    portfolio_context: str,
    current_date: str,
    personal_thesis: str | None = None,
    macro_view: str | None = None,
    do_not_sell_until: str | None = None,
    is_hidden_gem: bool = False,
    sentiment_signal: dict | None = None,
    watchlist_context: str | None = None,
    sector_note: str | None = None,
    macro_context: str | None = None,
    portfolio_value_eur: float | None = None,
    lessons_context: str | None = None,
    in_portfolio: bool = False,
    position_line: str | None = None,
    sell_triggers: str | None = None,
    previous_calls: str | None = None,
    checklist_text: str | None = None,
    cycle_context: str | None = None,
    investor_profile: str | None = None,
    model: str = MODEL,
    review_note: str | None = None,
) -> dict:
    """
    Analyzes a single stock and returns a structured recommendation dict.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    symbol = fundamentals.get("symbol", "")

    congress_buys = (congress_signal or {}).get("buy_count", 0)
    congress_sells = (congress_signal or {}).get("sell_count", 0)

    # Real SEC Form 4 insider signal (open-market buys/sells only)
    if insider_signal:
        _f_insider = insider_signal.get("note", "")
    else:
        _f_insider = "Nema insider Form 4 aktivnosti u zadnjih 14 dana"

    # StockTwits sentiment block
    sentiment_block = ""
    sentiment_hype = 0
    if sentiment_signal:
        sentiment_hype = sentiment_signal.get("hype_score", 0)
        sentiment_block = f"\nSTOCKTWITS SENTIMENT:\n{sentiment_signal.get('bull_bear_summary', '')}"

    # Earnings calendar block
    earnings_block = ""
    earnings_days = fundamentals.get("next_earnings_days")
    earnings_date = fundamentals.get("next_earnings_date", "N/A")
    if earnings_days is not None:
        if 0 <= earnings_days <= 7:
            earnings_block = f"\n⚠️ UPOZORENJE: Earnings za {earnings_days} dana ({earnings_date}) — VISOK rizik volatilnosti! Ne preporučaj kupnju tik pred earnings osim s iznimno visokim uvjerenjem."
        elif 8 <= earnings_days <= 30:
            earnings_block = f"\nINFO: Earnings za {earnings_days} dana ({earnings_date}) — napomeni u thesis."

    # 52-week position warning
    week52_block = ""
    week52_pos = fundamentals.get("week_52_position_pct")
    if week52_pos is not None:
        if week52_pos >= 85:
            week52_block = f"\n⚠️ 52-TJEDNA POZICIJA: {week52_pos:.0f}% od godišnjeg raspona — dionica je blizu vrha, postavi konzervativniji buy_zone i manji position size."
        elif week52_pos <= 15:
            week52_block = f"\nINFO 52-tjedna pozicija: {week52_pos:.0f}% od godišnjeg raspona — dionica je blizu godišnjeg dna, potencijalna value prilika ako su fundamentali solidni."

    watchlist_block = f"\nWATCHLIST POVIJEST: {watchlist_context}" if watchlist_context else ""
    sector_block = f"\nSEKTORSKA KONCENTRACIJA: {sector_note}" if sector_note else ""
    macro_block = f"\n{macro_context}" if macro_context else ""
    lessons_block = (
        f"\nNAUČENE LEKCIJE IZ PROŠLIH PREPORUKA (kvartalni self-review):\n{lessons_context}"
        if lessons_context else ""
    )

    if in_portfolio:
        holding_block = f"\n📌 ULAGAČ DRŽI OVU DIONICU. {position_line or ''}\nDopuštene akcije: HOLD (zadano), ADD_ON_DIP, REDUCE, SELL."
        allowed_actions = HELD_ACTIONS
    else:
        holding_block = "\nUlagač NE drži ovu dionicu. Dopuštene akcije: BUY_BELOW, WATCHLIST, WAIT, NO_ACTION."
        allowed_actions = NOT_HELD_ACTIONS

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

    review_block = f"\n🔎 DRUGA PROVJERA PRODAJE:\n{review_note}" if review_note else ""
    previous_block = (
        f"\n{previous_calls}" if previous_calls
        else "\nTVOJE PRETHODNE ANALIZE: nema (prva analiza ove dionice u zadnjih 60 dana)."
    )
    checklist_block = f"\n{checklist_text}" if checklist_text else ""
    cycle_block = f"\nCIKLUS I SEZONA:\n{cycle_context}" if cycle_context else ""

    # Pre-format evidence table values — prevents raw floats appearing in Claude's output
    de_x = _debt_to_equity_x(fundamentals)
    _f_price = _ev_price(fundamentals.get("current_price"))
    _f_pe = _ev(fundamentals.get("pe"), decimals=1)
    _f_fwd_pe = _ev(fundamentals.get("forward_pe"), decimals=1)
    _f_peg = _ev(fundamentals.get("peg"), decimals=2)
    _f_de = _ev(de_x, decimals=2, suffix="x")
    _f_rev = _ev_growth(fundamentals.get("revenue_growth_yoy"))
    _f_fcf = _ev_pct(fundamentals.get("fcf_yield"), decimals=2)
    _f_margin = _ev_pct_decimal(fundamentals.get("op_margin"))
    _f_st = (
        f"{sentiment_signal.get('bullish_pct', 0):.0f}%↑ / "
        f"{sentiment_signal.get('bearish_pct', 0):.0f}%↓ | "
        f"hype: {sentiment_hype}/10"
        if sentiment_signal else "N/A"
    )
    _f_earn = (f"{earnings_days}d ({earnings_date})" if earnings_days is not None else "N/A")
    seasonality = fundamentals.get("seasonality")
    _f_season = (
        f"{seasonality['window']}: medijan {seasonality['median_excess_pp']:+.1f} pp vs S&P, "
        f"{seasonality['beat_spy_years']}/{seasonality['years']} god."
        if seasonality else "N/A"
    )

    prompt_fund = {k: v for k, v in fundamentals.items() if k not in _PROMPT_DROP_KEYS and v is not None}
    if de_x is not None:
        prompt_fund["debt_to_equity_x"] = round(de_x, 2)
    breakdown = ", ".join(
        f"{name}={b.get('raw')}×{b.get('weight')}"
        for name, b in (score_result.get("breakdown") or {}).items()
    )

    user_prompt = f"""Analiziraj ovu dionicu i vrati JSON preporuku NA HRVATSKOM JEZIKU (financijski termini mogu ostati na engleskom).

DATUM: {current_date}
{macro_block}
{lessons_block}
{_position_guide(portfolio_value_eur)}
{holding_block}{gem_context}{personal_context}{review_block}{earnings_block}{week52_block}{watchlist_block}{sector_block}{sentiment_block}
{previous_block}
{checklist_block}
{cycle_block}

FUNDAMENTALNI PODACI:
{json.dumps(prompt_fund, ensure_ascii=False, separators=(",", ":"), default=str)}

FUNDAMENTAL SCORE: {score_result.get('total_score', 0)}/100 (kategorija: {score_result.get('category', 'unknown')})
Score breakdown (ocjena 0-5 × težina): {breakdown}

CONGRESS TRADES (zadnjih 14 dana — slab signal):
- Članovi kupuju: {congress_buys}
- Članovi prodaju: {congress_sells}
- Napomena: {(congress_signal or {}).get('note', 'Nema nedavnih kongresnih trgovanja')}

INSIDER TRADING (SEC Form 4, kupnje/prodaje na tržištu, zadnjih 14 dana — svježiji signal od Kongresa, 2 dana kašnjenja):
- {_f_insider}

INVESTOR PORTFOLIO CONTEXT:
{portfolio_context}

Vrati SAMO ovu JSON strukturu (bez markdowna, bez teksta izvan JSONa):
{{
  "ticker": "{symbol}",
  "company_name": "{fundamentals.get('name', '')}",
  "category": "<quality_compounder|value_cyclical|turnaround|speculative_growth|dividend_defensive>",
  "action": "<{allowed_actions}>",
  "buy_zone": "<npr. '< $25.00' ili 'N/A'>",
  "target_price": "<npr. '$32.00' ili 'N/A'>",
  "position_size": "<npr. 'mala (3-5% / €500-750)' — stvarni iznosi iz VELIČINA POZICIJE; 'N/A' za HOLD/WAIT/WATCHLIST>",
  "business_explanation": "<2 rečenice: čime se firma bavi i kako zarađuje>",
  "investment_thesis": "<max 3 rečenice: zašto ova dionica, zašto sada>",
  "valuation_verdict": "<jeftina/fer/skupa vs sektor s 2-3 ključne brojke, 1 rečenica>",
  "cycle_view": "<gdje je dionica u ciklusu/sezoni i što to znači za idućih 3-6 mjeseci, 1 rečenica; 'nije ciklička' ako nije>",
  "investor_view": "<kako bi ULAGAČ ocijenio dionicu kroz svoj checklist i makro pogled, 1-2 rečenice; 'N/A' ako profil ulagača nije naveden>",
  "counter_argument": "<najjači protuargument ulagačevom pogledu — podaci, povijest, što tržište već zna, 1-2 rečenice; 'N/A' ako profil nije naveden>",
  "change_vs_last": "<što se promijenilo od tvoje zadnje analize (brojke/činjenice) i zašto akcija ostaje ili se mijenja, 1 rečenica; 'prva analiza' ako je nema>",
  "catalyst": "<konkretan događaj ili trend koji može otključati vrijednost u 6-18 mjeseci, 1 rečenica>",
  "downside_scenario": "<što mora poći po zlu za gubitak 30-50%, konkretno, 1-2 rečenice>",
  "vs_cash_alternative": "<je li bolje od ne raditi ništa / od dodavanja u najbolju postojeću poziciju, 1-2 rečenice>",
  "thesis_breakers": ["<uvjet 1 koji bi poništio tezu — kratko>", "<uvjet 2>"],
  "red_flags": ["<1-3 konkretna rizika, kratko>"],
  "hype_override": <true ako je hype_score >= 7 spustio akciju>,
  "confidence": <cijeli broj 1-10>,
  "revolut_available": true,
  "evidence_table": {{
    "current_price": "{_f_price}",
    "buy_zone": "<isto kao buy_zone>",
    "pe": "{_f_pe}",
    "forward_pe": "{_f_fwd_pe}",
    "peg": "{_f_peg}",
    "debt_equity": "{_f_de}",
    "revenue_growth": "{_f_rev}",
    "fcf_yield": "{_f_fcf}",
    "op_margin": "{_f_margin}",
    "insider_signal": "<Buying/Selling/Neutral>",
    "congress_signal": "Weak — {congress_buys} buy(s), {congress_sells} sell(s)",
    "stocktwits": "{_f_st}",
    "earnings_in": "{_f_earn}",
    "altman_z_score": "<iz podataka ili N/A>",
    "relative_strength_6m": "<iz podataka ili N/A>",
    "fundamental_score": "{score_result.get('total_score', 0)}/100",
    "confidence": "<isto kao confidence>/10"
  }}
}}"""

    system = SYSTEM_PROMPT
    if investor_profile:
        system += "\n\n" + investor_profile

    # Haiku's complete responses measured 1261-1841 output tokens before the four
    # short fields were added; the review model thinks adaptively, which needs room.
    response = client.messages.create(
        model=model,
        max_tokens=3000 if model == MODEL else 16000,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    if response.stop_reason == "max_tokens":
        print(f"[ai_analyst] WARNING: analyze_stock hit max_tokens for {symbol} ({model}) — response may be truncated.")

    raw_text = _sanitize_cyrillic(_response_text(response))
    try:
        result = _extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        result = {
            "action": "HOLD" if in_portfolio else "NO_ACTION",
            "confidence": 0,
            "error": f"Failed to parse AI response: {raw_text[:200]}",
        }

    result["ticker"] = symbol or result.get("ticker", "")
    result["is_hidden_gem"] = is_hidden_gem
    result["in_portfolio"] = in_portfolio
    result["model"] = model

    # Override evidence_table fields we control — real data instead of Claude's guess
    ev = result.setdefault("evidence_table", {})
    ev["stocktwits"] = _f_st
    ev["earnings_in"] = _f_earn
    ev["insider_signal"] = _f_insider
    ev["debt_equity"] = _f_de
    ev["seasonality"] = _f_season
    altman_z = fundamentals.get("altman_z_score")
    ev["altman_z_score"] = f"{altman_z:.2f}" if altman_z is not None else "N/A"
    rel_strength = fundamentals.get("relative_strength_6m")
    ev["relative_strength_6m"] = f"{rel_strength:+.1f}pp" if rel_strength is not None else "N/A"

    _normalize_action(result, in_portfolio)
    no_buy_action = "HOLD" if in_portfolio else "WATCHLIST"

    # Hard override: hype block (StockTwits)
    if sentiment_hype >= 7 and result.get("action") in BULLISH_ACTIONS:
        result["action"] = no_buy_action
        result["hype_override"] = True
        result["hype_note"] = f"Kupnja spuštena na {no_buy_action} — hype score {sentiment_hype}/10"

    # Hard override: low confidence
    try:
        confidence = float(result.get("confidence", 10))
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 6 and result.get("action") in BULLISH_ACTIONS:
        result["action"] = "HOLD" if in_portfolio else "WAIT"

    return result


def generate_weekly_summary(
    all_recommendations: list[dict],
    portfolio_context: str,
    current_date: str,
) -> dict:
    """
    Generates the final newsletter summary from all individual stock recommendations.
    Enforces max 4-7 actions and ensures NO_TRADE is considered.
    Output is in Croatian (same system prompt as analyze_stock).
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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
        }
        for r in all_recommendations
    ]

    prompt = f"""Na temelju individualnih analiza dionica, napravi sažetak tjednog newslettera NA HRVATSKOM JEZIKU.

DATUM: {current_date}
PORTFELJ: {portfolio_context}

INDIVIDUALNE ANALIZE:
{json.dumps(slim_recs, ensure_ascii=False, indent=2, default=str)}

Pravila:
- Odaberi max 4-7 ukupnih akcija (BUY_BELOW, ADD_ON_DIP, HOLD, WATCHLIST, WAIT, SELL, REDUCE, NO_TRADE)
- Ako nijedna dionica ne zadovoljava kriterije kvalitete, summary mora biti NO_TRADE s objašnjenjem
- Prioritet: postojeće pozicije u portfelju prvo (in_portfolio=true), zatim nove ideje
- Ako je sell_guard_note postavljen, spomeni ga u portfolio_note
- overall_market_comment i portfolio_note piši NA HRVATSKOM JEZIKU
- no_trade_reason piši NA HRVATSKOM ako postoji

Vrati SAMO valjani JSON (bez markdowna, bez teksta izvan JSONa):
{{
  "date": "{current_date}",
  "overall_market_comment": "<2 rečenice o trenutnom tržišnom okruženju — NA HRVATSKOM>",
  "top_actions": [
    {{
      "rank": 1,
      "ticker": "...",
      "action": "...",
      "buy_zone": "...",
      "one_liner": "<jedna rečenica zašto — NA HRVATSKOM>"
    }}
  ],
  "watchlist_this_week": ["TICKER1", "TICKER2"],
  "no_trade_reason": "<null ili objašnjenje zašto nema jakih kupnji ovaj tjedan — NA HRVATSKOM>",
  "portfolio_note": "<napomena o pozicijama u portfelju (HOLD/ADD/REDUCE), koncentraciji i gotovini — NA HRVATSKOM>",
  "email_subject_suffix": "<npr. '1 BUY, 2 HOLD, 2 WATCHLIST, 0 SELL'>"
}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "max_tokens":
        print("[ai_analyst] WARNING: generate_weekly_summary hit max_tokens — response may be truncated.")

    try:
        return _extract_json(_sanitize_cyrillic(_response_text(response)))
    except (json.JSONDecodeError, ValueError):
        return {
            "date": current_date,
            "overall_market_comment": "Tržišna analiza trenutno nije dostupna.",
            "top_actions": [],
            "watchlist_this_week": [],
            "no_trade_reason": None,
            "portfolio_note": "",
            "email_subject_suffix": "0 BUY, 0 SELL",
        }
