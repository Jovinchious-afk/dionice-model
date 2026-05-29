"""
AI analyst module: synthesizes fundamental scores, Reddit signals, and Congress trades
into actionable stock recommendations using Claude claude-haiku-4-5-20251001.
Every recommendation is compared against "do nothing / hold cash / add to best position".
"""

import json
import os
import re
from datetime import datetime
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"


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


def _sanitize_cyrillic(text: str) -> str:
    """Remove Cyrillic characters that occasionally sneak into Claude's Croatian output."""
    return re.sub(r"[Ѐ-ӿ]", "", text)

SYSTEM_PROMPT = """Ti si disciplinirani analitičar dioničkog tržišta koji piše na HRVATSKOM jeziku (uz financijske termine na engleskom: FCF, EBITDA, P/E, Debt/Equity, itd.).

KONTEKST ULAGAČA:
- Ulagač iz Hrvatske, platforma Revolut Basic
- UKUPNI KAPITAL: trenutna vrijednost cijelog portfelja (može biti 15.000-25.000 EUR)
- Mjesečni doprinos: 300-400 EUR dodatno svakog mjeseca
- Može prodati dio postojeće pozicije i reinvestirati u drugu dionicu (npr. prodati pola VG = ~9.000 EUR za novu poziciju)
- 3-4 transakcija mjesečno maksimalno
- NEMOJ tretirati ulagača kao da ima samo 300 EUR ukupno — to je samo miesčni dodatak

OSNOVNA FILOZOFIJA:
- Dosadni, profitabilni i podcijenjeni biznisi ispred hype-a
- Svaka preporuka mora biti bolja od "ne raditi ništa" ili "povećati najboljuu postojeću poziciju"
- "Nema kupnje ovaj tjedan" je validan i čest output — nije neuspjeh
- Reddit hype je anti-signal: ako je hype_score >= 7, maksimalna akcija je WATCHLIST, nikad BUY
- Kongresne kupnje/prodaje su slabi signali — samo izvor ideja (prijave kasne 30-45 dana)
- VAŽNO: Ako ulagač ima osobnu tezu za postojeću poziciju (geopolitika, sektorski trendovi), POŠTUJ tu tezu — ne preporučuj SELL bez iznimno jakog razloga

VALUTA PRAVILO:
- BUY ZONE i TARGET PRICE uvijek u USD s $ znakom (npr. "< $25.00") jer su sve dionice NYSE/NASDAQ listed i kotiraju u USD
- Position size je u EUR (tvoj investicijski budžet): small (50-80 EUR), normal (100-150 EUR), full (200-300 EUR)
- NIKAD ne koristiti EUR za buy_zone ili target_price

STROGA PRAVILA:
1. Preporučuj samo dionice dostupne na Revolut platformi
2. Preporučuj BUY_BELOW samo s konkretnom cijenom u USD, nikad otvoreni "kupi odmah"
3. Ako je confidence < 6, output mora biti WAIT ili WATCHLIST
4. Maksimalno 4-7 akcija po newsletteru
5. Poslovni model mora biti objašnjiv u 2-3 rečenice
6. NE preporučuj ako: hype bez fundamentala, dug raste brže od prihoda, marže padaju bez jasnog razloga

VELIČINA POZICIJE (od dostupnog kapitala):
- mala: 5-10% portfelja (spekulativno, prvi ulazak, niski confidence)
- normalna: 10-15% portfelja (solidno uvjerenje, dobar risk/reward)
- velika: 15-25% portfelja (visoko uvjerenje, jasno podcijenjenost)
- Ne ulaziti sve u jednu novu dionicu — diversifikacija je ključna

SIGNALI ZA PRODAJU (preporuči SELL ili REDUCE samo ako):
- Fundamentalna teza se promijenila (biznis se pogoršava, ne samo cijena)
- Dug raste >20% YoY bez rasta prihoda
- Operativne marže padaju 3+ uzastopna kvartala
- Problem s managementom ili masovna prodaja insajdera
- Valuacija postala ekstremna (dionica daleko iznad fer vrijednosti)
- Dividenda se reže
- Zalihe rastu puno brže od prodaje

OUTPUT FORMAT: Vraćaj SAMO valjani JSON, bez markdowna, bez teksta izvan JSONa.
Svi tekstualni opisi (thesis, catalyst, downside_scenario, itd.) MORAJU biti na HRVATSKOM jeziku.

PISMO: Koristi ISKLJUČIVO latinična slova (a-z, A-Z, hrvatska dijakritika: č,ć,š,ž,đ). NIKAD ne koristi ćirilična slova."""


def analyze_stock(
    fundamentals: dict,
    score_result: dict,
    reddit_signal: dict | None,
    congress_signal: dict | None,
    portfolio_context: str,
    current_date: str,
    personal_thesis: str | None = None,
    macro_view: str | None = None,
    do_not_sell_until: str | None = None,
    is_hidden_gem: bool = False,
    sentiment_signal: dict | None = None,
    watchlist_context: str | None = None,
    sector_note: str | None = None,
) -> dict:
    """
    Analyzes a single stock and returns a structured recommendation dict.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    hype_score = (reddit_signal or {}).get("hype_score", 0)
    congress_buys = (congress_signal or {}).get("buy_count", 0)
    congress_sells = (congress_signal or {}).get("sell_count", 0)

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
            earnings_block = f"\n⚠️ UPOZORENJE: Earnings za {earnings_days} dana ({earnings_date}) — VISOK rizik volatilnosti! Ne preporučaj BUY tik pred earnings osim s iznimno visokim uvjerenjem."
        elif 8 <= earnings_days <= 30:
            earnings_block = f"\nINFO: Earnings za {earnings_days} dana ({earnings_date}) — napomeni u thesis."

    # 52-week position warning
    week52_block = ""
    week52_pos = fundamentals.get("week_52_position_pct")
    if week52_pos is not None:
        if week52_pos >= 85:
            week52_block = f"\n⚠️ 52-TJEDNA POZICIJA: {week52_pos:.0f}% od godišnjeg vrha — dionica je blizu vrha, postavi konzervativniji buy_zone i manji position size."
        elif week52_pos <= 15:
            week52_block = f"\nINFO 52-tjedna pozicija: {week52_pos:.0f}% od godišnjeg vrha — dionica je blizu godišnjeg dna, potencijalna value kupnja ako su fundamentali solidni."

    # Watchlist cross-check
    watchlist_block = ""
    if watchlist_context:
        watchlist_block = f"\nWATCHLIST POVIJEST: {watchlist_context}"

    # Sector concentration note
    sector_block = ""
    if sector_note:
        sector_block = f"\nSEKTORSKA KONCENTRACIJA: {sector_note}"

    gem_context = ""
    if is_hidden_gem:
        gem_context = """
💎 HIDDEN GEM ANALIZA — POSEBNA PRAVILA:
- Ova dionica je odabrana kao potencijalni "hidden gem" — cijena ispod $12, sektor s dugoročnim potencijalom
- Vremenski horizont: 5-10 godina (ne 6-18 mjeseci kao za mainstream dionice)
- Primjer referentnog scenarija: Nvidia 2016-2017, Amazon 2003-2005, Microsoft 2012-2014
- Dopuštene akcije: WATCHLIST (idealno za praćenje), BUY_BELOW s malom pozicijom, ili WAIT
- Pozicija mora biti SMALL (max 50-80 EUR) jer je rizik visok — ovo je spekulativna oklada
- Ako nema jasne poslovne teze ili je market cap < $50M, preporuči WAIT
- Naglasi: "Ovo je visoko rizična spekulativna pozicija. Gubitak 70-100% je moguć."
"""

    personal_context = ""
    if personal_thesis or macro_view or do_not_sell_until:
        personal_context = f"""
ULAGAČEVA OSOBNA TEZA ZA OVU DIONICU (OBAVEZNO POŠTUJ):
- Osobna teza: {personal_thesis or 'nije definirana'}
- Makro pogled: {macro_view or 'nije definiran'}
- Ne prodavati dok: {do_not_sell_until or 'nije definirano'}
UPOZORENJE: Ne preporučuj SELL ili REDUCE bez iznimno jakog razloga koji direktno proturječi ovoj tezi.
"""

    # Pre-format evidence table values — prevents raw floats appearing in Claude's output
    _f_price    = _ev_price(fundamentals.get("current_price"))
    _f_pe       = _ev(fundamentals.get("pe"), decimals=1)
    _f_fwd_pe   = _ev(fundamentals.get("forward_pe"), decimals=1)
    _f_peg      = _ev(fundamentals.get("peg"), decimals=2)
    _f_de       = _ev(fundamentals.get("debt_equity"), decimals=1)
    _f_rev      = _ev_growth(fundamentals.get("revenue_growth_yoy"))
    _f_fcf      = _ev_pct(fundamentals.get("fcf_yield"), decimals=2)
    _f_margin   = _ev_pct_decimal(fundamentals.get("op_margin"))
    _f_st       = (
        f"{sentiment_signal.get('bullish_pct', 0):.0f}%↑ / "
        f"{sentiment_signal.get('bearish_pct', 0):.0f}%↓ | "
        f"hype: {sentiment_hype}/10"
        if sentiment_signal else "N/A"
    )
    _f_earn     = (f"{earnings_days}d ({earnings_date})" if earnings_days is not None else "N/A")

    user_prompt = f"""Analiziraj ovu dionicu i vrati JSON preporuku NA HRVATSKOM JEZIKU (financijski termini mogu ostati na engleskom).

DATUM: {current_date}
{gem_context}{personal_context}{earnings_block}{week52_block}{watchlist_block}{sector_block}{sentiment_block}

FUNDAMENTALNI PODACI:
{json.dumps(fundamentals, indent=2, default=str)}

FUNDAMENTAL SCORE: {score_result.get('total_score', 0)}/100 (category: {score_result.get('category', 'unknown')})
Score breakdown: {json.dumps(score_result.get('breakdown', {}), indent=2)}

REDDIT SIGNAL:
- Hype score: {hype_score}/10
- Mentions (48h): {(reddit_signal or {}).get('mention_count', 0)}
- Posts with fundamental analysis: {(reddit_signal or {}).get('posts_with_fa', 0)}
- Verdict: {(reddit_signal or {}).get('verdict', 'No data')}

CONGRESS TRADES (last 14 days — weak signal):
- Members buying: {congress_buys}
- Members selling: {congress_sells}
- Note: {(congress_signal or {}).get('note', 'No recent congress trades')}

INVESTOR PORTFOLIO CONTEXT:
{portfolio_context}

Return ONLY this JSON structure (no markdown, no text outside JSON):
{{
  "ticker": "{fundamentals.get('ticker', fundamentals.get('symbol', ''))}",
  "company_name": "{fundamentals.get('name', '')}",
  "category": "<quality_compounder|value_cyclical|turnaround|speculative_growth|dividend_defensive>",
  "action": "<BUY_BELOW|ADD_ON_DIP|WAIT|WATCHLIST|SELL|REDUCE|NO_ACTION>",
  "buy_zone": "<e.g. '< $25.00' or 'N/A'>",
  "target_price": "<e.g. '$32.00' or 'N/A'>",
  "position_size": "<small (50-80 EUR)|normal (100-150 EUR)|full (200-300 EUR)|N/A>",
  "business_explanation": "<2-3 sentences explaining what the company does and how it makes money>",
  "investment_thesis": "<max 5 sentences: why this stock, why now>",
  "valuation_verdict": "<cheap/fair/expensive vs sector with 2-3 key numbers>",
  "catalyst": "<what specific event or trend could unlock value in 6-18 months>",
  "downside_scenario": "<what must go wrong to lose 30-50% — be specific>",
  "vs_cash_alternative": "<is this better than doing nothing? Better than adding to existing best position? Explain in 2 sentences>",
  "thesis_breakers": ["<uvjet 1 koji bi poništio tezu — kratko>", "<uvjet 2>", "<uvjet 3>"],
  "red_flags": ["<list of 1-3 specific concerns>"],
  "hype_override": <true if hype_score >= 7 forced downgrade>,
  "confidence": <integer 1-10>,
  "revolut_available": true,
  "evidence_table": {{
    "current_price": "{_f_price}",
    "buy_zone": "<same as buy_zone above>",
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
    "fundamental_score": "{score_result.get('total_score', 0)}/100",
    "confidence": "<same as confidence above>/10"
  }}
}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if Claude added them
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip().rstrip("```").strip()

    # Sanitize any Cyrillic characters that sneak in
    raw_text = _sanitize_cyrillic(raw_text)

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        result = {
            "ticker": fundamentals.get("symbol", ""),
            "action": "NO_ACTION",
            "confidence": 0,
            "error": f"Failed to parse AI response: {raw_text[:200]}",
        }

    result["is_hidden_gem"] = is_hidden_gem

    # Override evidence_table fields we control — prevents Claude from mangling them
    ev = result.setdefault("evidence_table", {})
    ev["stocktwits"] = _f_st
    ev["earnings_in"] = _f_earn

    # Hard override: hype block (Reddit or StockTwits)
    effective_hype = max((reddit_signal or {}).get("hype_score", 0), sentiment_hype)
    if effective_hype >= 7:
        if result.get("action") in ("BUY_BELOW", "ADD_ON_DIP"):
            result["action"] = "WATCHLIST"
            result["hype_override"] = True
            result["hype_note"] = f"Action downgraded from BUY to WATCHLIST — hype score {effective_hype}/10"

    # Hard override: low confidence
    if result.get("confidence", 10) < 6:
        if result.get("action") in ("BUY_BELOW", "ADD_ON_DIP"):
            result["action"] = "WAIT"

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
        }
        for r in all_recommendations
    ]

    prompt = f"""Na temelju individualnih analiza dionica, napravi sažetak tjednog newslettera NA HRVATSKOM JEZIKU.

DATUM: {current_date}
PORTFELJ: {portfolio_context}

INDIVIDUALNE ANALIZE:
{json.dumps(slim_recs, indent=2, default=str)}

Pravila:
- Odaberi max 4-7 ukupnih akcija (BUY_BELOW, ADD_ON_DIP, WATCHLIST, WAIT, SELL, REDUCE, NO_TRADE)
- Ako nijedna dionica ne zadovoljava kriterije kvalitete, summary mora biti NO_TRADE s objašnjenjem
- Prioritet: postojeće portfolio pozicije prvo, zatim nove ideje
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
  "portfolio_note": "<napomena o VG poziciji ili koncentraciji portfelja — NA HRVATSKOM>",
  "email_subject_suffix": "<npr. '1 BUY, 2 WATCHLIST, 0 SELL'>"
}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip().rstrip("```").strip()

    try:
        start = raw_text.index("{")
        end = raw_text.rindex("}") + 1
        return json.loads(raw_text[start:end])
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
