"""
Statistics behind the quarterly learning report — how past calls actually did.

Everything is measured as excess return over the S&P 500 (SPY) from the day of
the call, with repeated identical calls on the same stock collapsed into one.
Sample sizes are small and one market regime dominates, so every figure carries
its n and a bootstrap interval, and nothing is proposed below MIN_N_PROPOSAL.

Two datasets:
  - decisions (since 2026-05): the published calls, with category and score
    taken from the watchlist evidence written in the same run
  - analysis_log features (since 2026-09-29): every scored stock, including the
    ones filtered out before the AI, with the ten scorer criteria — the data for
    judging the scorer weights and the filters themselves
"""

import math
import random
import statistics
from datetime import datetime, timedelta, timezone

import pandas as pd

from analysis.prices import BENCHMARK, PriceBook, parse_buy_zone

HORIZONS = (30, 90, 180)
MIN_N = 20            # below this a bucket is shown as "premalo podataka"
MIN_N_PROPOSAL = 30   # below this no parameter change may be proposed
MIN_N_WEIGHTS = 60
REPEAT_WINDOW_DAYS = 30
BUY_ACTIONS = {"BUY_BELOW", "ADD_ON_DIP"}
SHADOW_ACTIONS = {"SKIPPED_SCORE", "EXCLUDED"}
WEIGHT_STEP = 0.2


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def collapse_repeats(rows: list[dict], date_key: str, action_key: str,
                     window_days: int = REPEAT_WINDOW_DAYS) -> list[dict]:
    """
    The same call on the same stock repeated within window_days counts once (the
    first); GNRC once had six identical decision rows in four weeks. Oldest first.
    """
    kept, last_kept = [], {}
    for row in sorted(rows, key=lambda r: str(r.get(date_key) or "")):
        ts = parse_ts(row.get(date_key))
        key = (row.get("symbol"), row.get(action_key))
        previous = last_kept.get(key)
        if previous and ts and (ts - previous).days < window_days:
            continue
        kept.append(row)
        if ts:
            last_kept[key] = ts
    return kept


def preload(book: PriceBook, items: list[tuple[str, datetime]]) -> None:
    """One download per ticker, from the earliest date it is needed."""
    earliest: dict[str, datetime] = {}
    for symbol, ts in items:
        if symbol and ts and (symbol not in earliest or ts < earliest[symbol]):
            earliest[symbol] = ts
    for symbol, since in earliest.items():
        book.closes(symbol, since)
    if earliest:
        book.closes(BENCHMARK, min(earliest.values()))


def forward_excess(book: PriceBook, symbol: str, start: datetime, end: datetime) -> dict | None:
    p0, p1 = book.close_on_or_before(symbol, start), book.close_on_or_before(symbol, end)
    q0, q1 = book.close_on_or_before(BENCHMARK, start), book.close_on_or_before(BENCHMARK, end)
    if None in (p0, p1, q0, q1) or not p0 or not q0:
        return None
    ret, spy = (p1 / p0 - 1) * 100, (q1 / q0 - 1) * 100
    return {"ret": ret, "spy": spy, "excess": ret - spy}


def zone_fill(book: PriceBook, symbol: str, zone: float, start: datetime, end: datetime) -> tuple:
    """(date, price) of the first close at or below the zone between start and end, else (None, None)."""
    series = book.closes(symbol, start)
    if series is None:
        return None, None
    window = series[(series.index >= pd.Timestamp(start.date())) & (series.index <= pd.Timestamp(end.date()))]
    below = window[window <= zone]
    if below.empty:
        return None, None
    day = below.index[0].to_pydatetime().replace(tzinfo=timezone.utc)
    return day, float(below.iloc[0])


def decision_observations(decisions: list[dict], watchlist_rows: list[dict], book: PriceBook,
                          now: datetime) -> list[dict]:
    """Published calls with forward excess returns, zone fills and the watchlist's category/score."""
    wl_by = {(w.get("symbol"), w.get("suggested_at")): w for w in watchlist_rows}
    rows = collapse_repeats(decisions, "recommended_at", "agent_action")
    preload(book, [(r.get("symbol"), parse_ts(r.get("recommended_at"))) for r in rows])

    out = []
    for d in rows:
        start = parse_ts(d.get("recommended_at"))
        symbol = d.get("symbol")
        if not start or not symbol:
            continue
        evidence = (wl_by.get((symbol, d.get("recommended_at"))) or {})
        score_txt = str((evidence.get("evidence_json") or {}).get("fundamental_score") or "")
        score = _num(score_txt.split("/")[0]) if "/" in score_txt else None
        obs = {
            "symbol": symbol,
            "date": start.strftime("%Y-%m-%d"),
            "action": d.get("agent_action"),
            "confidence": d.get("agent_confidence"),
            "category": evidence.get("category"),
            "score": score,
            "model": d.get("model"),
            "entry_plan": d.get("entry_plan"),
            "user_action": d.get("user_action"),
            "thesis": (d.get("agent_thesis") or "")[:300],
        }
        for h in HORIZONS:
            end = start + timedelta(days=h)
            if end <= now:
                fr = forward_excess(book, symbol, start, end)
                if fr:
                    obs[f"ex{h}"] = round(fr["excess"], 2)

        zone = parse_buy_zone(d.get("agent_buy_zone")) if obs["action"] in BUY_ACTIONS else None
        p0 = book.close_on_or_before(symbol, start) if zone else None
        if zone and p0:
            obs["zone_discount"] = round((p0 - zone) / p0 * 100, 2)
            for h in (30, 90):
                end = start + timedelta(days=h)
                if end > now:
                    continue
                fill_day, _ = zone_fill(book, symbol, zone, start, end)
                obs[f"filled{h}"] = fill_day is not None
                if fill_day is None:
                    obs[f"zone_ex{h}"] = 0.0  # never bought: the idea added nothing
                else:
                    fr = forward_excess(book, symbol, fill_day, end)
                    if fr:
                        obs[f"zone_ex{h}"] = round(fr["excess"], 2)
        out.append(obs)
    return out


def analysis_observations(analysis_rows: list[dict], book: PriceBook, now: datetime) -> list[dict]:
    """analysis_log rows that carry a features record (logged from 2026-09-29 on)."""
    rows = [r for r in analysis_rows if isinstance((r.get("snapshot") or {}).get("features"), dict)]
    rows = collapse_repeats(rows, "analyzed_at", "action")
    preload(book, [(r.get("symbol"), parse_ts(r.get("analyzed_at"))) for r in rows])

    out = []
    for r in rows:
        start = parse_ts(r.get("analyzed_at"))
        if not start:
            continue
        features = r["snapshot"]["features"]
        obs = {
            "symbol": r.get("symbol"),
            "date": start.strftime("%Y-%m-%d"),
            "action": r.get("action"),
            "confidence": r.get("confidence"),
            "model": r.get("model"),
            "shadow": r.get("action") in SHADOW_ACTIONS,
            "features": features,
        }
        for h in HORIZONS:
            end = start + timedelta(days=h)
            if end <= now:
                fr = forward_excess(book, r.get("symbol"), start, end)
                if fr:
                    obs[f"ex{h}"] = round(fr["excess"], 2)
        out.append(obs)
    return out


def bootstrap_ci(values: list[float], n_boot: int = 2000, seed: int = 7, level: float = 0.9) -> tuple:
    """Percentile bootstrap interval for the mean."""
    if len(values) < 5:
        return None, None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n_boot))
    lo = means[int((1 - level) / 2 * n_boot)]
    hi = means[int((1 + level) / 2 * n_boot) - 1]
    return round(lo, 2), round(hi, 2)


def summarize(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    lo, hi = bootstrap_ci(values)
    return {
        "n": len(values),
        "avg": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "beat": round(sum(1 for v in values if v > 0) / len(values) * 100),
        "ci_low": lo,
        "ci_high": hi,
        "enough": len(values) >= MIN_N,
    }


def confidence_bucket(value) -> str | None:
    c = _num(value)
    if c is None or c <= 0:
        return None
    return "1-3" if c <= 3 else "4-5" if c <= 5 else "6-7" if c <= 7 else "8-10"


def score_bucket(value) -> str | None:
    s = _num(value)
    if s is None:
        return None
    return "<40" if s < 40 else "40-49" if s < 50 else "50-59" if s < 60 else "60+"


def hype_bucket(value) -> str | None:
    h = _num(value)
    if h is None:
        return None
    return "1-3" if h <= 3 else "4-6" if h <= 6 else "7+"


def bucket_table(obs: list[dict], key_fn, horizon: int) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for o in obs:
        key = key_fn(o)
        value = o.get(f"ex{horizon}")
        if key is None or value is None:
            continue
        groups.setdefault(str(key), []).append(value)
    return [{"bucket": k, **summarize(v)} for k, v in sorted(groups.items())]


def spearman(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        r = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return round(num / den, 3) if den else None


def t_stat(rho: float | None, n: int) -> float | None:
    if rho is None or n < 5 or abs(rho) >= 1:
        return None
    return round(rho * math.sqrt((n - 2) / (1 - rho ** 2)), 2)


def buy_zone_study(obs: list[dict], horizon: int = 30, fraction: float = 0.5) -> dict:
    """
    Per BUY idea: buying at the market on the day of the call, waiting for the zone
    (an unfilled zone adds nothing), or `fraction` now and the rest at the zone.
    """
    buys = [o for o in obs if o.get("action") in BUY_ACTIONS and o.get(f"ex{horizon}") is not None
            and o.get(f"zone_ex{horizon}") is not None]
    if not buys:
        return {"n": 0}
    market = [o[f"ex{horizon}"] for o in buys]
    zone = [o[f"zone_ex{horizon}"] for o in buys]
    split = [fraction * m + (1 - fraction) * z for m, z in zip(market, zone)]
    filled = [o for o in buys if o.get(f"filled{horizon}")]
    unfilled = [o for o in buys if not o.get(f"filled{horizon}")]
    return {
        "n": len(buys),
        "median_zone_discount": round(statistics.median(o["zone_discount"] for o in buys), 1),
        "fill_rate": round(len(filled) / len(buys) * 100),
        "market": summarize(market),
        "zone": summarize(zone),
        "split": summarize(split),
        "unfilled_from_call": summarize([o[f"ex{horizon}"] for o in unfilled]),
        "filled_from_call": summarize([o[f"ex{horizon}"] for o in filled]),
        "fraction": fraction,
    }


def criteria_ic(analysis_obs: list[dict], horizon: int) -> list[dict]:
    """Rank correlation of each scorer criterion (and the total score) with excess return."""
    rows = [o for o in analysis_obs if o.get(f"ex{horizon}") is not None]
    if not rows:
        return []
    names = sorted({name for o in rows for name in (o["features"].get("criteria") or {})})
    out = []
    for name in ["score"] + names:
        xs, ys = [], []
        for o in rows:
            x = o["features"].get("score") if name == "score" else (o["features"].get("criteria") or {}).get(name)
            if x is not None:
                xs.append(float(x))
                ys.append(o[f"ex{horizon}"])
        rho = spearman(xs, ys)
        out.append({"criterion": name, "n": len(xs), "rho": rho, "t": t_stat(rho, len(xs))})
    return out


def weight_proposal(ic_rows: list[dict], current_weights: dict) -> dict | None:
    """
    Nudges a criterion's weight by ±WEIGHT_STEP in every category when its rank
    correlation with returns is significant (|t| >= 2) on at least MIN_N_WEIGHTS
    observations. Returns the full new matrix plus the reasons, or None.
    """
    changes = {}
    for row in ic_rows:
        name = row["criterion"]
        if name == "score" or name not in current_weights or row["n"] < MIN_N_WEIGHTS or row["t"] is None:
            continue
        if abs(row["t"]) >= 2:
            changes[name] = 1 + WEIGHT_STEP if row["rho"] > 0 else 1 - WEIGHT_STEP
    if not changes:
        return None
    new = {k: [round(min(1.5, max(0.1, w * changes.get(k, 1.0))), 2) for w in row]
           for k, row in current_weights.items()}
    reasons = [f"{k}: ×{factor:.1f} (rho {next(r['rho'] for r in ic_rows if r['criterion'] == k)})"
               for k, factor in changes.items()]
    return {"weights": new, "reason": "; ".join(reasons)}


def actual_vs_spy(transactions: list[dict], book: PriceBook, now: datetime, eur_usd_on) -> dict | None:
    """
    The investor's real trades against the same money moved in and out of SPY on the
    same days: every BUY of $X buys $X of SPY, every SELL of $Y sells $Y of SPY.
    """
    rows = sorted(transactions, key=lambda t: str(t.get("trade_date") or ""))
    if not rows:
        return None
    preload(book, [(t.get("symbol"), parse_ts(t.get("trade_date"))) for t in rows])
    spy_shares = 0.0
    shares: dict[str, float] = {}
    invested = received = 0.0
    for t in rows:
        day = parse_ts(t.get("trade_date"))
        qty = _num(t.get("shares")) or 0.0
        price = _num(t.get("price_per_share")) or 0.0
        if str(t.get("currency") or "USD").upper() == "EUR":
            price *= eur_usd_on(str(t.get("trade_date") or ""))
        spy = book.close_on_or_before(BENCHMARK, day) if day else None
        if not day or not spy:
            continue
        amount = qty * price
        symbol = t.get("symbol")
        if t.get("action") == "BUY":
            invested += amount
            shares[symbol] = shares.get(symbol, 0.0) + qty
            spy_shares += amount / spy
        elif t.get("action") == "SELL":
            received += amount
            shares[symbol] = shares.get(symbol, 0.0) - qty
            spy_shares -= amount / spy

    holdings_value = 0.0
    for symbol, qty in shares.items():
        if qty > 1e-9:
            price = book.close_on_or_before(symbol, now)
            if price is None:
                return None
            holdings_value += qty * price
    spy_now = book.close_on_or_before(BENCHMARK, now)
    if not spy_now or not invested:
        return None
    actual_gain = holdings_value + received - invested
    spy_gain = spy_shares * spy_now + received - invested
    return {
        "invested_usd": round(invested, 2),
        "actual_gain_usd": round(actual_gain, 2),
        "spy_gain_usd": round(spy_gain, 2),
        "difference_usd": round(actual_gain - spy_gain, 2),
        "actual_pct": round(actual_gain / invested * 100, 2),
        "spy_pct": round(spy_gain / invested * 100, 2),
    }


def best_worst(obs: list[dict], k: int = 5) -> tuple[list[dict], list[dict]]:
    """Best and worst BUY/ADD calls by the longest measured horizon."""
    scored = []
    for o in obs:
        if o.get("action") not in BUY_ACTIONS:
            continue
        horizon = next((h for h in (180, 90, 30) if o.get(f"ex{h}") is not None), None)
        if horizon:
            scored.append({**{k2: o.get(k2) for k2 in ("symbol", "date", "action", "confidence", "thesis")},
                           "horizon": horizon, "excess": o[f"ex{horizon}"]})
    scored.sort(key=lambda o: o["excess"])
    return scored[-k:][::-1], scored[:k]


def build_stats(decisions: list[dict], watchlist_rows: list[dict], analysis_rows: list[dict],
                transactions: list[dict], book: PriceBook, now: datetime, params: dict,
                current_weights: dict, eur_usd_on) -> dict:
    obs = decision_observations(decisions, watchlist_rows, book, now)
    aobs = analysis_observations(analysis_rows, book, now)

    stats: dict = {
        "generated_at": now.strftime("%Y-%m-%d"),
        "decisions_total": len(decisions),
        "decisions_collapsed": len(obs),
        "first_call": min((o["date"] for o in obs), default=None),
        "overview": {h: summarize([o.get(f"ex{h}") for o in obs]) for h in HORIZONS},
        "by_action": {h: bucket_table(obs, lambda o: o.get("action"), h) for h in (30, 90)},
        "by_confidence": {h: bucket_table(obs, lambda o: confidence_bucket(o.get("confidence")), h) for h in (30, 90)},
        "by_category": {h: bucket_table(obs, lambda o: o.get("category"), h) for h in (30, 90)},
        "by_score": {h: bucket_table(obs, lambda o: score_bucket(o.get("score")), h) for h in (30, 90)},
        "by_model": {30: bucket_table(obs, lambda o: o.get("model"), 30)},
        "by_entry_plan": {30: bucket_table([o for o in obs if o.get("action") in BUY_ACTIONS],
                                           lambda o: o.get("entry_plan") or "zona", 30)},
        "confidence_rank_corr": {},
        "buy_zone": {h: buy_zone_study(obs, h, params.get("starter_fraction", 0.5)) for h in (30, 90)},
        "analysis_rows": len(aobs),
        "analysis_shadow_rows": sum(1 for o in aobs if o["shadow"]),
        "by_hype": {30: bucket_table(aobs, lambda o: hype_bucket(o["features"].get("hype")), 30)},
        "ai_vs_filtered": {30: bucket_table(aobs, lambda o: "filtrirano (bez AI)" if o["shadow"] else "AI analiza", 30)},
        "criteria_ic": {h: criteria_ic(aobs, h) for h in (30, 90)},
        "parse_failures": sum(1 for r in analysis_rows if r.get("model") and (r.get("confidence") in (0, None))),
        "analyses_logged": sum(1 for r in analysis_rows if r.get("model")),
    }
    for h in (30, 90):
        pairs = [(o.get("confidence"), o.get(f"ex{h}")) for o in obs if (o.get("confidence") or 0) > 0]
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        n = sum(1 for p in pairs if p[1] is not None)
        stats["confidence_rank_corr"][h] = {"rho": rho, "n": n, "t": t_stat(rho, n)}

    stats["actual_vs_spy"] = actual_vs_spy(transactions, book, now, eur_usd_on)
    best, worst = best_worst(obs)
    stats["best_calls"], stats["worst_calls"] = best, worst
    ic_for_weights = stats["criteria_ic"][90] or stats["criteria_ic"][30]
    stats["weight_proposal"] = weight_proposal(ic_for_weights, current_weights)
    return stats


def _fmt_summary(s: dict) -> str:
    if not s or not s.get("n"):
        return "n=0"
    ci = f" [{s['ci_low']:+.1f}, {s['ci_high']:+.1f}]" if s.get("ci_low") is not None else ""
    flag = "" if s.get("enough") else " (premalo podataka)"
    return f"n={s['n']}, prosjek {s['avg']:+.1f}pp{ci}, medijan {s['median']:+.1f}pp, bolje od S&P {s['beat']}%{flag}"


def _table(title: str, rows: list[dict]) -> list[str]:
    if not rows:
        return [f"**{title}:** nema podataka", ""]
    lines = [f"**{title}**", "", "| Grupa | n | Prosjek vs S&P | 90% interval | Medijan | Bolje od S&P |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        if not r.get("n"):
            continue
        ci = f"{r['ci_low']:+.1f} do {r['ci_high']:+.1f}" if r.get("ci_low") is not None else "—"
        flag = "" if r.get("enough") else " ⚠️"
        lines.append(f"| {r['bucket']}{flag} | {r['n']} | {r['avg']:+.1f}pp | {ci} | {r['median']:+.1f}pp | {r['beat']}% |")
    lines.append("")
    return lines


def stats_markdown(stats: dict) -> str:
    """Compact Croatian markdown of the statistics, for the AI prompt, the email and Streamlit."""
    lines = [
        f"Podaci: {stats['decisions_total']} preporuka od {stats.get('first_call')}, "
        f"{stats['decisions_collapsed']} nakon spajanja ponovljenih poziva. Povrat se mjeri od dana preporuke "
        f"kao razlika prema S&P 500 (pp). ⚠️ = manje od 20 slučajeva.",
        "",
    ]
    for h in HORIZONS:
        lines.append(f"- **Sve preporuke, {h} dana:** {_fmt_summary(stats['overview'].get(h) or stats['overview'].get(str(h), {}))}")
    lines.append("")
    for title, key in (("Po akciji", "by_action"), ("Po confidenceu", "by_confidence"),
                       ("Po kategoriji", "by_category"), ("Po fundamentalnom scoreu", "by_score")):
        for h in (30, 90):
            rows = (stats[key].get(h) or stats[key].get(str(h)) or [])
            lines += _table(f"{title} — {h} dana", rows)

    for h in (30, 90):
        c = stats["confidence_rank_corr"].get(h) or stats["confidence_rank_corr"].get(str(h)) or {}
        if c.get("rho") is not None:
            lines.append(f"- Rang-korelacija confidence ↔ povrat ({h} dana): rho {c['rho']:+.2f}, n={c['n']}, t={c['t']}")
    lines.append("")

    for h in (30, 90):
        z = stats["buy_zone"].get(h) or stats["buy_zone"].get(str(h)) or {}
        if not z.get("n"):
            continue
        lines += [
            f"**Buy zona — {h} dana ({z['n']} BUY/ADD ideja):** zona medijalno {z['median_zone_discount']:.1f}% ispod cijene, "
            f"dosegnuta u {z['fill_rate']}% slučajeva.",
            f"- Kupnja odmah po tržišnoj cijeni: {_fmt_summary(z['market'])}",
            f"- Čekanje zone (nedosegnuta zona = 0): {_fmt_summary(z['zone'])}",
            f"- {z['fraction'] * 100:.0f}% odmah + ostatak na zoni: {_fmt_summary(z['split'])}",
            f"- Ideje čija zona NIJE dosegnuta, od dana preporuke: {_fmt_summary(z['unfilled_from_call'])}",
            f"- Ideje čija zona JE dosegnuta, od dana preporuke: {_fmt_summary(z['filled_from_call'])}",
            "",
        ]

    lines += _table("Model koji je donio odluku — 30 dana", stats["by_model"].get(30) or stats["by_model"].get("30") or [])
    lines += _table("Plan ulaska (BUY/ADD) — 30 dana", stats["by_entry_plan"].get(30) or stats["by_entry_plan"].get("30") or [])

    lines.append(f"**Podaci s obilježjima (analysis_log):** {stats['analysis_rows']} opažanja, od toga "
                 f"{stats['analysis_shadow_rows']} dionica filtriranih prije AI analize.")
    lines += _table("Hype razina — 30 dana", stats["by_hype"].get(30) or stats["by_hype"].get("30") or [])
    lines += _table("AI analiza vs filtrirano prije AI-a — 30 dana",
                    stats["ai_vs_filtered"].get(30) or stats["ai_vs_filtered"].get("30") or [])
    for h in (30, 90):
        ic = stats["criteria_ic"].get(h) or stats["criteria_ic"].get(str(h)) or []
        usable = [r for r in ic if r.get("rho") is not None]
        if usable:
            lines.append(f"**Kriteriji scorea vs povrat ({h} dana, Spearman rho; |t| ≥ 2 ≈ značajno):** " + ", ".join(
                f"{r['criterion']} {r['rho']:+.2f} (n={r['n']}, t={r['t']})" for r in usable))
            lines.append("")

    actual = stats.get("actual_vs_spy")
    if actual:
        lines += [
            f"**Tvoj stvarni portfelj vs isti novac u S&P 500:** uloženo ${actual['invested_usd']:,.0f}; "
            f"tvoj rezultat {actual['actual_gain_usd']:+,.0f} USD ({actual['actual_pct']:+.1f}%), "
            f"S&P 500 s istim uplatama/isplatama {actual['spy_gain_usd']:+,.0f} USD ({actual['spy_pct']:+.1f}%) → "
            f"razlika {actual['difference_usd']:+,.0f} USD.",
            "",
        ]
    lines.append(f"Operativno: {stats['analyses_logged']} AI analiza u logu, {stats['parse_failures']} bez valjanog odgovora.")
    return "\n".join(lines)
