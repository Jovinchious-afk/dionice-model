"""
The investor's own checklist (book notes in "Dionice - Biljeske.xlsx", Lynch /
Malkiel style), reduced to the checks the data can actually answer.

Two uses:
  - build_checklist(): ✓ / ✗ / ~ lines injected into the analysis prompt, so the
    model argues from the investor's rules rather than generic ones
  - deterioration_signals(): the sell guard. A SELL/REDUCE on a position the
    investor holds only goes through when the business itself got worse —
    never on price, relative strength or sentiment alone
"""

from analysis.scorer import BALANCE_SHEET_EXEMPT_SECTORS

PASS, FAIL, MIXED = "✓", "✗", "~"


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def sales_growth(fund: dict) -> float | None:
    """Quarter-on-same-quarter growth when available — it matches the inventory comparison."""
    q = fund.get("quarterly_revenue_growth_yoy")
    return q if q is not None else fund.get("revenue_growth_yoy")


def inventory_rule_broken(inventory_growth: float, sales: float) -> bool:
    """The investor's rule: inventories growing twice as fast as sales = sell."""
    return inventory_growth > 0.10 and inventory_growth > 2 * max(sales, 0.0)


def build_checklist(fund: dict, category: str | None = None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    sector = (fund.get("sector") or "").strip().lower()
    balance_sheet_meaningful = sector not in BALANCE_SHEET_EXEMPT_SECTORS

    inst = fund.get("institutional_ownership")
    if inst is not None:
        if inst > 1.0:
            # 13F filings lag a quarter and are divided by today's share count: buybacks shrink
            # the denominator and lent-out shares get counted twice, so >100% is a data artifact
            items.append((MIXED, f"Institucije drže {inst * 100:.0f}% — nepouzdan podatak "
                                 "(13F prijave kasne kvartal, otkupi i posuđene dionice napuhuju postotak); ne tretiraj kao crvenu zastavicu"))
        else:
            status = PASS if inst < 0.5 else (MIXED if inst < 0.8 else FAIL)
            items.append((status, f"Institucije drže {inst * 100:.0f}% (pravilo: što manje, ispod 50%)"))

    analysts = fund.get("analyst_count")
    if analysts is not None:
        status = PASS if analysts <= 5 else (MIXED if analysts <= 15 else FAIL)
        items.append((status, f"Prati je {analysts:.0f} analitičara (pravilo: što manje to bolje)"))

    peg = fund.get("peg")
    if peg is not None and peg > 0:
        status = PASS if peg <= 1 else (MIXED if peg <= 2 else FAIL)
        items.append((status, f"PEG {peg:.2f} (pravilo: P/E ≈ pola stope rasta, tj. PEG 0.5 idealno; iznad 2 skupo)"))

    buys = fund.get("insider_buys_24m")
    if buys is not None:
        # Yahoo lists some filings with no description; with those present, "0 buys" is not certain
        unclassified = fund.get("insider_unclassified_24m") or 0
        status = PASS if buys > 0 else (MIXED if unclassified else FAIL)
        unclassified_txt = f", {unclassified} transakcija bez opisa" if unclassified else ""
        items.append((status, f"Kupnje insidera na tržištu u ~2 god: {buys}{unclassified_txt} (pravilo: nijedna kupnja uprave u 2 god = oprez; prodaje nisu signal)"))

    debt_change = fund.get("debt_change_yoy")
    if debt_change is not None:
        status = PASS if debt_change <= -0.02 else (FAIL if debt_change > 0.20 else MIXED)
        items.append((status, f"Dug YoY {_pct(debt_change)} (pravilo: dug treba padati)"))

    shares_change = fund.get("shares_change_yoy")
    if shares_change is not None:
        status = PASS if shares_change <= -0.01 else (FAIL if shares_change > 0.02 else MIXED)
        items.append((status, f"Broj dionica YoY {_pct(shares_change)} (pravilo: pada = otkupi; raste = razvodnjavanje)"))

    inventory, sales = fund.get("inventory_growth_yoy"), sales_growth(fund)
    if inventory is not None and sales is not None:
        is_auto = (fund.get("industry") or "") == "Auto Manufacturers"
        if inventory_rule_broken(inventory, sales) and not is_auto:
            status = FAIL
        elif inventory <= sales:
            status = PASS
        else:
            status = MIXED
        note = "; ne vrijedi za auto industriju" if is_auto else ""
        items.append((status, f"Zalihe YoY {_pct(inventory)} vs prodaja {_pct(sales)} (pravilo: zalihe 2x brže od prodaje = prodaj{note})"))

    current_ratio = fund.get("current_ratio")
    if balance_sheet_meaningful and current_ratio is not None:
        status = PASS if current_ratio >= 2 else (MIXED if current_ratio >= 1 else FAIL)
        items.append((status, f"Current ratio {current_ratio:.2f} (pravilo: najmanje 2:1)"))

    de = fund.get("debt_to_equity_x")
    if balance_sheet_meaningful and de is not None:
        status = PASS if de <= 0.33 else (MIXED if de <= 1.0 else FAIL)
        items.append((status, f"Dug/kapital {de:.2f}x (pravilo: idealno 25% dug / 75% kapital ≈ 0.33x)"))

    growth = fund.get("revenue_growth_yoy")
    if growth is not None:
        status = FAIL if growth < 0 or growth > 0.25 else (MIXED if growth > 0.20 else PASS)
        items.append((status, f"Rast prihoda {_pct(growth)} (pravilo: do 20% zdravo, iznad 25% oprez, iznad 50% opasno)"))

    declining = fund.get("op_margin_declining_3q")
    if declining is True:
        items.append((FAIL, f"Operativna marža pada 3 kvartala zaredom {fund.get('op_margin_quarters_pct')}"))
    elif declining is False:
        items.append((PASS, "Operativna marža ne pada 3 kvartala zaredom"))

    cut = fund.get("dividend_cut")
    if cut is True:
        items.append((FAIL, "Dividenda smanjena ili ukinuta (pravilo: kloni se)"))
    elif cut is False:
        items.append((PASS, "Dividenda nije smanjena"))

    years_listed = fund.get("years_listed")
    if years_listed is not None and years_listed < 2:
        items.append((FAIL, f"Na burzi tek {years_listed:.1f} god (pravilo: ne kupuj IPO prije 2 god financijskih izvješća)"))

    pe, forward_pe = fund.get("pe"), fund.get("forward_pe")
    if category == "value_cyclical" and pe and forward_pe:
        items.append((MIXED, f"Ciklička: trailing P/E {pe:.1f} vs forward {forward_pe:.1f} (visok trailing uz nizak forward = oporavak dobiti; nizak P/E na vrhu ciklusa = upozorenje)"))

    return items


def checklist_summary(items: list[tuple[str, str]]) -> str:
    counts = {s: sum(1 for status, _ in items if status == s) for s in (PASS, FAIL, MIXED)}
    return f"{PASS}{counts[PASS]} {FAIL}{counts[FAIL]} {MIXED}{counts[MIXED]}"


def format_checklist(items: list[tuple[str, str]]) -> str | None:
    if not items:
        return None
    lines = [f"CHECKLIST ULAGAČA (izračunato iz podataka, {checklist_summary(items)}):"]
    lines.extend(f"{status} {text}" for status, text in items)
    return "\n".join(lines)


def deterioration_signals(fund: dict) -> list[str]:
    """
    Evidence that the business itself got worse. Mirrors the sell signals in the
    analyst system prompt; price, relative strength and sentiment are deliberately
    absent, because "don't sell because the price fell" is the investor's own rule.
    """
    signals: list[str] = []
    sector = (fund.get("sector") or "").strip().lower()

    growth = fund.get("revenue_growth_yoy")
    if growth is not None and growth < 0:
        signals.append(f"prihod pada {_pct(growth)} YoY")

    if fund.get("op_margin_declining_3q"):
        signals.append(f"operativna marža pada 3 kvartala zaredom {fund.get('op_margin_quarters_pct')}")

    earnings = fund.get("earnings_growth_yoy")
    if earnings is not None and earnings < -0.25:
        signals.append(f"dobit pada {_pct(earnings)} YoY")

    debt_change = fund.get("debt_change_yoy")
    if debt_change is not None and debt_change > 0.20 and (growth is None or growth < 0.05):
        signals.append(f"dug {_pct(debt_change)} YoY bez rasta prihoda")

    inventory, sales = fund.get("inventory_growth_yoy"), sales_growth(fund)
    if (inventory is not None and sales is not None and inventory_rule_broken(inventory, sales)
            and fund.get("industry") != "Auto Manufacturers"):
        signals.append(f"zalihe {_pct(inventory)} vs prodaja {_pct(sales)} YoY")

    if fund.get("dividend_cut"):
        signals.append("dividenda smanjena ili ukinuta")

    z = fund.get("altman_z_score")
    if z is not None and z < 1.81 and sector not in BALANCE_SHEET_EXEMPT_SECTORS:
        signals.append(f"Altman Z {z:.2f} u zoni stresa")

    forward_pe, peg = fund.get("forward_pe"), fund.get("peg")
    if forward_pe is not None and forward_pe > 50 and (peg is None or peg > 3):
        signals.append(f"ekstremna valuacija (forward P/E {forward_pe:.0f})")

    return signals
