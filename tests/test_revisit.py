"""Thursday revisit, zone drift, watchlist hygiene, refresh guards, the new email sections and the A/B statistics."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis import learning
from analysis.analysis_history import load_recent_history
from analysis.email_sender import build_html_email
from analysis.revisit import origin_line, select_revisits, stale_watchlist_rows, zone_drift
from scripts import refresh_gems, refresh_sector_benchmarks, run_weekly
from tests.conftest import make_series

NOW = datetime(2026, 10, 29, 13, tzinfo=timezone.utc)


def decision(symbol, action, zone, day, conf=7, price=40.0, **extra):
    return {"symbol": symbol, "agent_action": action, "agent_buy_zone": zone, "agent_confidence": conf,
            "recommended_at": f"2026-{day}T17:00:00+00:00", "agent_thesis": f"Teza {symbol}",
            "price_at_recommendation": price, **extra}


def wl(symbol, action, zone, day, reached=None, conf=6):
    return {"id": f"id-{symbol}", "symbol": symbol, "action": action, "buy_zone": zone, "confidence": conf,
            "suggested_at": f"2026-{day}T17:00:00+00:00", "status": "ACTIVE", "thesis": "x",
            "buy_zone_reached_at": reached, "buy_zone_reached_price": 22.39 if reached else None}


def test_revisit_picks_recent_buy_calls_and_reached_zones_first():
    decisions = [
        decision("CENX", "BUY_BELOW", "< $32.00", "09-22", price=39.48),
        decision("ATI", "BUY_BELOW", "< $170.00", "09-15", price=185.93),
        decision("OLD", "BUY_BELOW", "< $10.00", "07-01"),            # outside the 60 days
        decision("VG", "ADD_ON_DIP", "< $12.50", "09-24"),            # held: analysed anyway
        decision("SKIP", "WATCHLIST", "< $50.00", "10-01"),           # not a buy call
    ]
    watchlist = [wl("CSWC", "BUY_BELOW", "< $22.50", "09-22", reached="2026-10-20"),
                 wl("FTNT", "WATCHLIST", "< $145.00", "09-29")]
    prices = {"ATI": 156.88, "CENX": 33.0, "FTNT": 140.0}
    picked = select_revisits(decisions, watchlist, NOW, held={"VG"}, dead={"XXX"}, prices=prices)
    assert set(picked) == {"CENX", "ATI", "CSWC", "FTNT"}
    # ATI fell through its zone after the Wednesday check: caught by the fresh price
    assert picked["ATI"]["trigger"] == "zone_reached" and picked["ATI"]["zone_reached_price"] == 156.88
    assert picked["CSWC"]["trigger"] == "zone_reached" and picked["FTNT"]["trigger"] == "zone_reached"
    assert picked["CENX"]["trigger"] == "buy_call" and picked["CENX"]["price_at_call"] == 39.48
    assert list(picked)[-1] == "CENX"  # reached zones come first
    assert len(select_revisits(decisions, watchlist, NOW, held=set(), max_n=2, prices=prices)) == 2


def test_origin_line_and_zone_drift():
    origin = {"date": "2026-09-22T17:00:00+00:00", "action": "BUY_BELOW", "buy_zone": "< $32.00",
              "confidence": 7, "price_at_call": 39.48, "zone_reached_at": "2026-10-23", "zone_reached_price": 31.42}
    line = origin_line(origin, 31.42, spy_change_pct=1.2)
    assert line.startswith("Prva preporuka 22. 9.: BUY_BELOW < $32.00 @ $39.48 (7/10) → danas $31.42 (-20.4%")
    assert "S&P 500 +1.2%" in line and "zona dosegnuta 23. 10." in line
    assert zone_drift("< $170.00", "< $145.00", 156.88)["pct"] == -14.7
    assert zone_drift("< $170.00", "< $165.00", 156.88) is None   # less than 5% lower
    assert zone_drift("< $170.00", "< $145.00", 185.0) is None    # price not at the old zone
    assert zone_drift("< $170.00", "N/A", 156.88) is None


def test_stale_watchlist_rows():
    rows = {
        "ATI": [{"id": 1, "symbol": "ATI", "suggested_at": "2026-09-15T00:00:00+00:00"}],
        "EMN": [{"id": 2, "symbol": "EMN", "suggested_at": "2026-10-20T00:00:00+00:00"}],
        "CMCSA": [{"id": 3, "symbol": "CMCSA", "suggested_at": "2026-06-04T00:00:00+00:00"}],
        "MTCH": [{"id": 4, "symbol": "MTCH", "suggested_at": "2026-06-11T00:00:00+00:00"}],
    }
    analysed = {"ATI": {"action": "WAIT"}, "EMN": {"action": "BUY_BELOW"}}
    stale = stale_watchlist_rows(rows, NOW, analysed, published={"EMN"},
                                 keep_actions={"BUY_BELOW", "ADD_ON_DIP", "WATCHLIST"},
                                 recently_analysed={"MTCH"}, max_age_days=90)
    # ATI: the new call is WAIT; CMCSA: months old and nobody looked at it. EMN is live, MTCH was re-analysed.
    assert sorted(r["id"] for r in stale) == [1, 3]


def test_gem_pool_guard_and_throttle_pauses(monkeypatch):
    assert not refresh_gems.pool_is_plausible(0, 242)
    assert not refresh_gems.pool_is_plausible(50, 242)        # under a quarter of the old pool
    assert refresh_gems.pool_is_plausible(120, 242)
    assert not refresh_gems.pool_is_plausible(30, 0)          # below the absolute floor
    monkeypatch.setattr(refresh_gems.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def flaky(sym):
        calls["n"] += 1
        if 3 <= calls["n"] < 3 + refresh_gems.THROTTLE_STREAK:  # a throttled stretch
            raise RuntimeError("Too Many Requests")
        return f"ok-{sym}"

    symbols = [f"S{i}" for i in range(20)]
    out = list(refresh_gems.fetch_with_pauses(flaky, symbols, "test"))
    # The symbol that hit the limit is retried after the pause; only the first failures are lost
    assert [s for s, r in out] == symbols
    assert sum(1 for _, r in out if r is None) == refresh_gems.THROTTLE_STREAK - 1

    def always_fails(_sym):
        raise RuntimeError("Too Many Requests")

    with pytest.raises(refresh_gems.ScreeningFailed):
        list(refresh_gems.fetch_with_pauses(always_fails, [str(i) for i in range(200)], "test"))


def test_auto_gem_bucket_is_a_random_sample_not_the_start_of_the_alphabet():
    names = [f"{letter}{i}" for letter in "ABCDEFGH" for i in range(50)]
    picked = refresh_gems.pick_auto_bucket(names, "2026-10-25")
    assert len(picked) == refresh_gems.AUTO_BUCKET_SIZE and picked == sorted(picked)
    assert len({p[0] for p in picked}) > 4                         # many letters, not only "A"
    assert picked == refresh_gems.pick_auto_bucket(names, "2026-10-25")  # reproducible on the day
    assert refresh_gems.pick_auto_bucket(names[:10], "x") == sorted(names[:10])


def test_benchmarks_carry_missing_groups_over_for_one_quarter():
    previous = {"_meta": {"refreshed_at": "2026-09-27"},
                "sectors": {"Communication Services": {"n": 35, "pe_median": 20.5, "forward_pe_median": 12.5},
                            "Gone": {"n": 5, "pe_median": 10.0, "carried_over_from": "2026-06-30"}},
                "industries": {}}
    new = {"sectors": {"Energy": {"n": 66, "pe_median": 16.2, "forward_pe_median": 12.6}}, "industries": {}}
    carried = refresh_sector_benchmarks.carry_over_missing(new, previous)
    assert carried == ["Communication Services"]
    assert new["sectors"]["Communication Services"]["carried_over_from"] == "2026-09-27"
    assert "Gone" not in new["sectors"]  # already carried once: dropped


def rec(ticker, action, conf, **extra):
    base = {"ticker": ticker, "company_name": f"{ticker} Inc.", "action": action, "confidence": conf,
            "buy_zone": "< $30.00", "target_price": "$40.00", "position_size": "mala", "investment_thesis": "Teza.",
            "valuation_verdict": "fer", "catalyst": "k", "downside_scenario": "d", "vs_cash_alternative": "v",
            "business_explanation": f"{ticker} prodaje stvari.", "red_flags": ["rizik"], "evidence_table": {}}
    base.update(extra)
    return base


def test_email_sections_for_revisits_gems_watchlist_and_health():
    revisit = rec("CENX", "BUY_BELOW", 6, revisit={"trigger": "zone_reached"},
                  revisit_line="Prva preporuka 22. 9.: BUY_BELOW < $32.00", zone_drift_note="Zona spuštena s $32.00")
    waiting_revisit = rec("HIG", "WATCHLIST", 7, revisit={"trigger": "zone_reached"},
                          revisit_line="Prva preporuka 20. 8.: ADD_ON_DIP < $130.00", change_vs_last="Vrijednost ista.")
    gems = [rec(f"G{i}", "WAIT", i, is_hidden_gem=True) for i in range(1, 6)]
    others = [rec("EMN", "BUY_BELOW", 6), rec("SYF", "WATCHLIST", 5), rec("AXP", "WATCHLIST", 7),
              rec("AMZN", "WAIT", 5)]
    html = build_html_email(
        summary={"date": "2026-10-29", "overall_market_comment": "m", "portfolio_note": "", "no_trade_reason": None,
                 "watchlist_this_week": []},
        recommendations=[waiting_revisit, revisit, *gems, *others], portfolio_value=None, portfolio_positions=[],
        revisits_below=[rec("ATI", "WAIT", 5), {"ticker": "BPOP", "excluded_reason": "Altman Z < 1.8"}],
        health_line="Zdravlje sustava: gemova u bazenu 0 ⚠️",
    )
    assert "Ponovna provjera" in html and "Prva preporuka 22. 9." in html and "Zona spuštena" in html
    # The buy comes first as a full block; the waiting idea gets the short card
    assert html.index("CENX") < html.index("HIG Inc.")
    assert "Što se promijenilo:</strong> Vrijednost ista." in html
    assert "ATI (5/10, &lt; $30.00)" in html or "ATI (5/10, < $30.00)" in html
    assert "BPOP (ispala iz analize: Altman Z < 1.8)" in html
    # Gems: three cards by confidence, the rest on one line
    assert html.count("Čime se bavi:") == 3 and "G5 prodaje stvari." in html and "Ostali gemovi" in html
    # WATCHLIST 7 gets a full block, WATCHLIST 5 one line
    assert "AXP Inc." in html and "Prati zonu" in html and "SYF (5/10" in html
    assert "Zdravlje sustava: gemova u bazenu 0" in html


def test_health_line_warns_about_an_empty_gem_pool():
    line = run_weekly.build_health_line(0, {"sectors": 10, "industries": 45, "carried_over": 0}, 2.31, 8.0,
                                        failed=1, dead=3)
    assert "gemova u bazenu 0 ⚠️" in line and "10/11 sektora ⚠️" in line
    assert "$2.31 od $8.00" in line and "analiza bez odgovora 1 ⚠️" in line
    ok = run_weekly.build_health_line(118, {"sectors": 11, "industries": 50, "carried_over": 0}, None, 8.0, 0, 0)
    assert "⚠️" not in ok


def test_ab_sample_leaves_out_positions_gems_and_revisits():
    def job(held=False, gem=False):
        return {"ctx": {"in_portfolio": held, "is_hidden_gem": gem}}

    jobs = {"VG": job(held=True), "G1": job(gem=True), "CENX": job(), "A": job(), "B": job()}
    picked = run_weekly.choose_ab_sample(jobs, {"CENX": {}}, 6)
    assert sorted(picked) == ["A", "B"]
    assert run_weekly.choose_ab_sample(jobs, {}, 0) == []


def analysis_row(symbol, day, model, action, conf, ab=False):
    features = {"score": 60, "category": "value_cyclical"}
    if ab:
        features["ab_shadow"] = True
    return {"symbol": symbol, "analyzed_at": f"{day}T18:00:00+00:00", "model": model, "action": action,
            "confidence": conf, "snapshot": {"features": features}}


def test_ab_rows_are_compared_but_kept_out_of_everything_else(book_factory):
    spy = make_series("2026-08-03", [100 + 0.05 * i for i in range(120)])
    up = make_series("2026-08-03", [50 + 0.5 * i for i in range(120)])
    down = make_series("2026-08-03", [50 - 0.2 * i for i in range(120)])
    book = book_factory({"SPY": spy, "UPP": up, "DWN": down})
    rows = [
        analysis_row("UPP", "2026-08-10", "claude-haiku-5-5", "WATCHLIST", 5),
        analysis_row("UPP", "2026-08-10", "claude-sonnet-5-5", "BUY_BELOW", 7, ab=True),
        analysis_row("DWN", "2026-08-10", "claude-haiku-5-5", "BUY_BELOW", 7),
        analysis_row("DWN", "2026-08-10", "claude-sonnet-5-5", "WAIT", 4, ab=True),
    ]
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    obs = learning.analysis_observations(rows, book, now)
    assert len(obs) == 2 and all(o["model"] == "claude-haiku-5-5" for o in obs)
    ab = learning.ab_comparison(rows, book, now)
    assert ab["pairs"] == 2 and ab["measured"] == 2 and ab["agree_pct"] == 0
    assert ab["models"]["shadow"]["conf6"]["n"] == 1 and ab["models"]["shadow"]["conf6"]["avg"] > 0
    assert ab["models"]["main"]["conf6"]["avg"] < 0
    text = learning.stats_markdown({**learning.build_stats([], [], rows, [], book, now, {}, {}, lambda _d: 1.0)})
    assert "A/B: claude-haiku-5-5 vs claude-sonnet-5-5" in text


def test_history_ignores_ab_rows():
    rows = [analysis_row("UPP", "2026-10-20", "claude-haiku-5-5", "WATCHLIST", 5),
            analysis_row("UPP", "2026-10-20", "claude-sonnet-5-5", "BUY_BELOW", 7, ab=True)]

    class FakeTable:
        def select(self, *_a):
            return self

        def gte(self, *_a):
            return self

        def order(self, *_a, **_k):
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    client = SimpleNamespace(fetch_all=lambda *_a, **_k: rows, table=lambda _name: FakeTable())
    history = load_recent_history(client)
    assert [r["model"] for r in history["UPP"]] == ["claude-haiku-5-5"]
