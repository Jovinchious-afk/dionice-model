"""
Dionice Portfolio Tracker — Streamlit app (5 pages)
Hosted on Streamlit Community Cloud (requires public GitHub repo)
"""

import json
import os
from datetime import datetime, timezone, timedelta

import sys
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis.portfolio import compute_holdings
from analysis.supabase_client import SupabaseClient

st.set_page_config(
    page_title="Dionice Portfolio",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Supabase connection ──────────────────────────────────────────────────────

@st.cache_resource
def get_db():
    url = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        st.error("Supabase credentials missing. Add them to .streamlit/secrets.toml or environment.")
        st.stop()
    return SupabaseClient(url, key)


def load_transactions(db) -> pd.DataFrame:
    try:
        result = db.table("transactions").select("*").order("trade_date", desc=True).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception as e:
        st.error(f"Failed to load transactions: {e}")
        return pd.DataFrame()


def load_watchlist(db) -> pd.DataFrame:
    try:
        result = db.table("watchlist").select("*").eq("status", "ACTIVE").order("suggested_at", desc=True).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception as e:
        st.error(f"Failed to load watchlist: {e}")
        return pd.DataFrame()


def load_decisions(db) -> pd.DataFrame:
    try:
        result = db.table("decisions").select("*").order("recommended_at", desc=True).limit(100).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception as e:
        st.error(f"Failed to load decisions: {e}")
        return pd.DataFrame()


def load_retired_tickers(db) -> pd.DataFrame:
    """Tickers the weekly run has repeatedly failed to fetch (see analysis/ticker_health.py)."""
    try:
        result = db.table("ticker_health").select("*").gte("consecutive_failures", 3).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception:
        return pd.DataFrame()


def load_latest_lessons(db) -> dict | None:
    try:
        result = db.table("model_lessons").select("*").order("generated_at", desc=True).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def load_newsletters(db) -> pd.DataFrame:
    try:
        result = db.table("newsletters").select("*").order("sent_at", desc=True).limit(20).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception as e:
        st.error(f"Failed to load newsletters: {e}")
        return pd.DataFrame()


def has_value(value) -> bool:
    """
    True only when a Supabase column actually holds something. pandas turns SQL
    NULL into float NaN, and bool(NaN) is True — so a plain truthiness check
    reports empty columns as filled.
    """
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() not in ("", "None", "nan", "NaT")


def format_price(value) -> str:
    if not has_value(value):
        return "Pending"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "Pending"


def text_value(value) -> str:
    return str(value) if has_value(value) else ""


def search_box(key: str, placeholder: str) -> str:
    """
    Ctrl+F in the browser cannot see text inside collapsed expanders, which is most of
    this app, so every long list gets its own search field.
    """
    return st.text_input("🔍 Traži", key=key, placeholder=placeholder).strip().lower()


def row_matches(row, query: str, columns: list[str]) -> bool:
    if not query:
        return True
    return any(query in text_value(row.get(col)).lower() for col in columns)


def filter_rows(df: pd.DataFrame, query: str, columns: list[str]) -> pd.DataFrame:
    if not query or df.empty:
        return df
    present = [c for c in columns if c in df.columns]
    mask = df.apply(lambda row: row_matches(row, query, present), axis=1)
    return df[mask]


def load_cash(db) -> dict:
    cash = {"cash_usd": 0.0, "cash_eur": 0.0}
    try:
        for row in db.table("account_settings").select("*").execute().data or []:
            if row.get("key") in cash and row.get("value") is not None:
                cash[row["key"]] = float(row["value"])
    except Exception:
        pass  # table is created by data/schema_v7.sql
    return cash


def save_cash(db, cash_usd: float, cash_eur: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.table("account_settings").upsert([
            {"key": "cash_usd", "value": cash_usd, "updated_at": now},
            {"key": "cash_eur", "value": cash_eur, "updated_at": now},
        ]).execute()
        st.success("Gotovina spremljena — agent je uzima u obzir od idućeg newslettera.")
    except Exception as e:
        st.error(f"Spremanje nije uspjelo (je li pokrenut data/schema_v7.sql u Supabaseu?): {e}")


def load_positions_meta(db) -> dict:
    try:
        return {row["symbol"]: row for row in db.table("positions_meta").select("*").execute().data or []}
    except Exception:
        return {}


def save_position_meta(db, symbol: str, fields: dict, keep_empty: bool = False) -> None:
    """Upserts the investor's thesis for a position; empty fields are skipped unless keep_empty."""
    row = {"symbol": symbol, "updated_at": datetime.now(timezone.utc).isoformat()}
    for key, value in fields.items():
        value = (value or "").strip()
        if value or keep_empty:
            row[key] = value or None
    try:
        db.table("positions_meta").upsert(row, on_conflict="symbol").execute()
        st.success(f"Teza za {symbol} spremljena.")
    except Exception as e:
        st.error(f"Spremanje teze nije uspjelo (je li pokrenut data/schema_v7.sql u Supabaseu?): {e}")


def compute_portfolio(tx_df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Current holdings plus realized P&L, via the same cost method the newsletter uses."""
    if tx_df.empty:
        return pd.DataFrame(), 0.0

    holdings = compute_holdings(tx_df.to_dict("records"))
    realized = sum(h["realized_pnl_usd"] for h in holdings.values())
    rows = [
        {
            "Symbol": h["symbol"],
            "Company": h["company_name"],
            "Shares": round(h["shares"], 4),
            "Avg Cost (USD)": round(h["avg_cost_usd"], 4),
            "Total Cost (USD)": round(h["cost_usd"], 2),
        }
        for h in holdings.values()
        if h["shares"] > 0
    ]
    return (pd.DataFrame(rows) if rows else pd.DataFrame()), realized


@st.cache_data(ttl=300)
def fetch_live_prices(symbols: tuple) -> dict:
    """Fetches latest USD prices via yfinance. Tries fast_info first, falls back to history."""
    result = {}
    for sym in symbols:
        price = None
        try:
            ticker = yf.Ticker(sym)
            try:
                price = ticker.fast_info["lastPrice"]
            except Exception:
                pass
            if not price:
                hist = ticker.history(period="5d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
        except Exception:
            pass
        result[sym] = round(float(price), 2) if price else None
    return result


# ── Sidebar navigation ───────────────────────────────────────────────────────

st.sidebar.title("📊 Dionice")
page = st.sidebar.radio(
    "Navigate",
    ["Portfolio", "Log Trade", "Watchlist", "Decisions", "Newsletteri"],
    index=0,
)
st.sidebar.markdown("---")
st.sidebar.caption("Revolut · Basic plan · 300-400 EUR/mo")

db = get_db()

# ── PAGE 1: Portfolio ────────────────────────────────────────────────────────

if page == "Portfolio":
    st.title("Portfolio")

    tx_df = load_transactions(db)
    portfolio, realized_pnl = compute_portfolio(tx_df)
    cash = load_cash(db)

    if portfolio.empty:
        st.info("No positions yet. Use 'Log Trade' to add your first trade.")
    else:
        live_prices = fetch_live_prices(tuple(portfolio["Symbol"].tolist()))

        display = portfolio.copy()

        # Numeric columns for calculations
        display["Value (USD)"] = display.apply(
            lambda r: round(r["Shares"] * live_prices.get(r["Symbol"]), 2)
            if live_prices.get(r["Symbol"]) else None,
            axis=1,
        )
        display["P&L (USD)"] = display.apply(
            lambda r: round(r["Value (USD)"] - r["Total Cost (USD)"], 2)
            if r["Value (USD)"] is not None else None,
            axis=1,
        )
        display["P&L %"] = display.apply(
            lambda r: round((r["P&L (USD)"] / r["Total Cost (USD)"]) * 100, 2)
            if r["P&L (USD)"] is not None and r["Total Cost (USD)"] > 0 else None,
            axis=1,
        )

        total_invested = portfolio["Total Cost (USD)"].sum()
        total_value = sum(
            row["Shares"] * live_prices.get(row["Symbol"])
            for _, row in portfolio.iterrows()
            if live_prices.get(row["Symbol"])
        )
        total_pnl = total_value - total_invested if total_value else None

        # % allocation per position (numeric, before string formatting)
        display["Allocation %"] = display["Value (USD)"].apply(
            lambda v: round(v / total_value * 100, 1) if total_value and v is not None else None
        )

        max_allocation = display["Allocation %"].max()
        top_symbol = (
            display.loc[display["Allocation %"].idxmax(), "Symbol"]
            if pd.notna(max_allocation) else None
        )

        # Summary metrics
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Invested", f"${total_invested:,.2f}")
        if total_pnl is not None:
            # Use plain number string (no $ prefix) so Streamlit correctly
            # colours delta red for negative and green for positive
            pnl_pct = total_pnl / total_invested * 100
            col2.metric(
                "Current Value",
                f"${total_value:,.2f}",
                delta=f"{total_pnl:+,.2f} ({pnl_pct:+.2f}%)",
            )
        else:
            col2.metric("Current Value", "N/A")
        col3.metric("Gotovina", f"${cash['cash_usd']:,.0f} + €{cash['cash_eur']:,.0f}")
        col4.metric("Positions", len(portfolio))
        st.caption(f"Realizirani P&L (prodane dionice): ${realized_pnl:+,.2f}")

        # Format columns for display (after numeric calculations)
        display["Price Now (USD)"] = display["Symbol"].map(
            lambda s: f"${live_prices.get(s):,.2f}" if live_prices.get(s) else "N/A"
        )
        display["Allocation %"] = display["Allocation %"].apply(
            lambda v: f"{v:.1f}%" if v is not None else "N/A"
        )
        for col in ["Value (USD)", "P&L (USD)"]:
            display[col] = display[col].apply(
                lambda v: f"${v:,.2f}" if v is not None else "N/A"
            )
        display["P&L %"] = display["P&L %"].apply(
            lambda v: f"{v:+.2f}%" if v is not None else "N/A"
        )
        display["Avg Cost (USD)"] = display["Avg Cost (USD)"].apply(lambda v: f"${v:,.2f}")
        display["Total Cost (USD)"] = display["Total Cost (USD)"].apply(lambda v: f"${v:,.2f}")

        st.subheader("Current Holdings")
        st.dataframe(
            display[["Symbol", "Company", "Shares", "Avg Cost (USD)", "Total Cost (USD)",
                      "Price Now (USD)", "Value (USD)", "P&L (USD)", "P&L %", "Allocation %"]],
            use_container_width=True, hide_index=True,
        )

        if top_symbol is not None and max_allocation >= 90:
            st.warning(
                f"⚠️ {top_symbol} is {max_allocation:.1f}% of portfolio value. "
                "This is single-stock concentration, not diversification — "
                "consider whether new capacity should go elsewhere."
            )

        with st.expander("📝 Teza po poziciji — zašto držiš i što bi te natjeralo da prodaš"):
            st.caption("Agent ovo čita prije svake analize, a SELL/REDUCE mora izravno pobiti tvoju tezu.")
            meta_by_symbol = load_positions_meta(db)
            thesis_symbol = st.selectbox("Pozicija", portfolio["Symbol"].tolist(), key="thesis_symbol")
            current_meta = meta_by_symbol.get(thesis_symbol, {})
            with st.form("thesis_form"):
                thesis_text = st.text_area("Zašto držim (teza)", value=text_value(current_meta.get("personal_thesis")))
                triggers_text = st.text_area(
                    "Što bi me natjeralo da prodam",
                    value=text_value(current_meta.get("sell_triggers")),
                    placeholder="npr. marže padaju 3 kvartala zaredom; zalihe rastu brže od prodaje",
                )
                macro_text = st.text_area("Makro pogled (opcionalno)", value=text_value(current_meta.get("macro_view")))
                if st.form_submit_button("Spremi tezu"):
                    save_position_meta(db, thesis_symbol, {
                        "personal_thesis": thesis_text,
                        "sell_triggers": triggers_text,
                        "macro_view": macro_text,
                    }, keep_empty=True)

    with st.expander("💵 Gotovina — ulazi u veličinu pozicija i usporedbu s držanjem gotovine"):
        with st.form("cash_form"):
            cash_usd = st.number_input("Gotovina u USD", min_value=0.0, step=100.0, value=float(cash["cash_usd"]))
            cash_eur = st.number_input("Gotovina u EUR", min_value=0.0, step=100.0, value=float(cash["cash_eur"]))
            if st.form_submit_button("Spremi gotovinu"):
                save_cash(db, cash_usd, cash_eur)

    st.subheader("Transaction History")
    if tx_df.empty:
        st.info("No transactions recorded.")
    else:
        display_cols = ["trade_date", "symbol", "company_name", "action", "shares", "price_per_share", "currency", "notes"]
        display_cols = [c for c in display_cols if c in tx_df.columns]
        st.dataframe(tx_df[display_cols], use_container_width=True, hide_index=True)

# ── PAGE 2: Log Trade ────────────────────────────────────────────────────────

elif page == "Log Trade":
    st.title("Log Trade")
    st.caption("Record a buy or sell you made on Revolut.")

    with st.form("trade_form"):
        col1, col2 = st.columns(2)
        with col1:
            symbol = st.text_input("Ticker Symbol", placeholder="e.g. VG, AAPL, MSFT").strip().upper()
            company_name = st.text_input("Company Name", placeholder="e.g. Venture Global LNG")
            action = st.selectbox("Action", ["BUY", "SELL"])
        with col2:
            shares = st.number_input("Number of Shares", min_value=0.0001, step=1.0, format="%.4f")
            price = st.number_input("Price per Share", min_value=0.0001, step=0.01, format="%.4f")
            currency = st.selectbox("Currency", ["EUR", "USD"])

        trade_date = st.date_input("Trade Date", value=datetime.now().date())
        trade_time = st.time_input("Trade Time (local)", value=datetime.now().time())
        notes = st.text_area("Notes (optional)", placeholder="e.g. Added to position after earnings dip")

        st.caption("Samo za BUY (opcionalno) — agent čita tvoju tezu prije svake analize te pozicije:")
        buy_thesis = st.text_area("Zašto kupujem (teza)", placeholder="npr. sezona uragana + data centri, dug pada")
        buy_triggers = st.text_area("Što bi me natjeralo da prodam", placeholder="npr. zalihe rastu 2x brže od prodaje")

        submitted = st.form_submit_button("Save Trade", type="primary")

    if submitted:
        if not symbol:
            st.error("Ticker symbol is required.")
        elif shares <= 0 or price <= 0:
            st.error("Shares and price must be positive.")
        else:
            trade_datetime = datetime.combine(trade_date, trade_time).replace(tzinfo=timezone.utc)
            row = {
                "symbol": symbol,
                "company_name": company_name or symbol,
                "action": action,
                "shares": shares,
                "price_per_share": price,
                "currency": currency,
                "trade_date": trade_datetime.isoformat(),
                "notes": notes or None,
            }
            try:
                db.table("transactions").insert(row).execute()
                st.success(f"✅ {action} {shares:.4f} {symbol} @ {price:.4f} {currency} saved!")
                if action == "BUY" and (buy_thesis.strip() or buy_triggers.strip()):
                    save_position_meta(db, symbol, {"personal_thesis": buy_thesis, "sell_triggers": buy_triggers})
                st.cache_resource.clear()
            except Exception as e:
                st.error(f"Failed to save trade: {e}")

# ── PAGE 3: Watchlist ────────────────────────────────────────────────────────

elif page == "Watchlist":
    st.title("Watchlist")
    st.caption("AI-suggested stocks. Update your action after you decide what to do.")

    wl_df = load_watchlist(db)

    # Manual add to watchlist
    with st.expander("➕ Add ticker to watchlist manually"):
        with st.form("watchlist_form"):
            wl_symbol = st.text_input("Ticker Symbol").strip().upper()
            wl_note = st.text_area("Why watching?", placeholder="e.g. Interesting after Q2 earnings")
            wl_submitted = st.form_submit_button("Add to Watchlist")
        if wl_submitted and wl_symbol:
            try:
                db.table("watchlist").insert({
                    "symbol": wl_symbol,
                    "action": "WATCHLIST",
                    "suggested_at": datetime.now(timezone.utc).isoformat(),
                    "thesis": wl_note,
                    "status": "ACTIVE",
                }).execute()
                st.success(f"{wl_symbol} added to watchlist.")
            except Exception as e:
                st.error(f"Failed: {e}")

    if wl_df.empty:
        st.info("No active watchlist items. Items appear automatically after each newsletter.")
    else:
        total_wl = len(wl_df)
        query = search_box("wl_search", "ticker, ime tvrtke, teza, kategorija...")
        wl_df = filter_rows(wl_df, query, ["symbol", "company_name", "thesis", "action", "category"])
        if query:
            st.caption(f"{len(wl_df)} od {total_wl} stavki")

        # Filter bar
        filter_action = st.radio(
            "Filter po akciji",
            ["Sve", "BUY / ADD ON DIP", "WATCHLIST", "WAIT", "🎯 Buy Zone Reached"],
            horizontal=True,
        )
        filter_map = {
            "BUY / ADD ON DIP": ["BUY_BELOW", "ADD_ON_DIP"],
            "WATCHLIST": ["WATCHLIST"],
            "WAIT": ["WAIT"],
        }
        if filter_action == "🎯 Buy Zone Reached":
            wl_df = wl_df[wl_df.get("buy_zone_reached_at").notna()] if "buy_zone_reached_at" in wl_df.columns else wl_df.iloc[0:0]
        elif filter_action != "Sve":
            allowed = filter_map[filter_action]
            wl_df = wl_df[wl_df["action"].isin(allowed)]

        if wl_df.empty:
            st.info("Nema stavki za odabrani filter.")
        else:
            for _, row in wl_df.iterrows():
                action_color = {
                    "BUY_BELOW": "green", "ADD_ON_DIP": "green",
                    "WATCHLIST": "orange", "WAIT": "gray", "SELL": "red",
                }.get(row.get("action", ""), "gray")

                # Age label
                age_label = ""
                try:
                    suggested = pd.to_datetime(row.get("suggested_at"))
                    if suggested.tzinfo is None:
                        suggested = suggested.replace(tzinfo=timezone.utc)
                    days_ago = (datetime.now(timezone.utc) - suggested).days
                    age_label = f" · {days_ago}d ago"
                except Exception:
                    pass

                zone_reached = has_value(row.get("buy_zone_reached_at"))

                with st.container():
                    col1, col2, col3 = st.columns([2, 3, 2])
                    with col1:
                        badge = " 🎯" if zone_reached else ""
                        st.markdown(f"**{row.get('symbol', '')}**{badge}")
                        st.caption(row.get("company_name", "") + age_label)
                        st.caption(f"Category: {row.get('category', 'N/A')}")
                    with col2:
                        st.markdown(f":{action_color}[{row.get('action', '')}] | Zone: {row.get('buy_zone', 'N/A')} | Target: {row.get('target_price', 'N/A')}")
                        if zone_reached:
                            st.caption(f"🎯 Buy zone reached {row.get('buy_zone_reached_at')} @ {format_price(row.get('buy_zone_reached_price'))}")
                        st.caption(f"Confidence: {row.get('confidence', 'N/A')}/10")
                        if row.get("thesis"):
                            st.caption(row["thesis"][:200])
                        evidence = row.get("evidence_json") or {}
                        if isinstance(evidence, str):
                            try:
                                evidence = json.loads(evidence)
                            except Exception:
                                evidence = {}
                        signal_bits = [
                            f"Altman Z: {evidence['altman_z_score']}" if evidence.get("altman_z_score") not in (None, "N/A") else None,
                            f"Rel. snaga (6mj): {evidence['relative_strength_6m']}" if evidence.get("relative_strength_6m") not in (None, "N/A") else None,
                            f"Insider: {evidence['insider_signal']}" if evidence.get("insider_signal") not in (None, "N/A") else None,
                        ]
                        signal_bits = [b for b in signal_bits if b]
                        if signal_bits:
                            st.caption(" | ".join(signal_bits))
                    with col3:
                        new_status = st.selectbox(
                            "Update status",
                            ["ACTIVE", "BOUGHT", "DISMISSED", "EXPIRED"],
                            index=["ACTIVE", "BOUGHT", "DISMISSED", "EXPIRED"].index(row.get("status", "ACTIVE")),
                            key=f"status_{row['id']}",
                        )
                        if st.button("Update", key=f"update_{row['id']}"):
                            try:
                                db.table("watchlist").update({"status": new_status}).eq("id", row["id"]).execute()
                                st.success("Updated.")
                            except Exception as e:
                                st.error(str(e))
                    st.divider()

# ── PAGE 4: Decisions ────────────────────────────────────────────────────────

elif page == "Decisions":
    st.title("Decision Log")
    st.caption("Track what the AI recommended vs what you did. Used for backtesting quality.")

    lessons = load_latest_lessons(db)
    if lessons:
        period = lessons.get("period_label", "")
        n = lessons.get("decisions_analyzed", 0)
        with st.expander(f"🧠 Naučene lekcije ({period}, {n} preporuka analizirano)", expanded=False):
            st.markdown(lessons.get("lessons_text", ""))

    retired = load_retired_tickers(db)
    if not retired.empty:
        with st.expander(f"🗑️ Automatski izbačeno iz bazena ({len(retired)} tickera)", expanded=False):
            st.caption(
                "Tickeri koje weekly run nije uspio dohvatiti 3 puta zaredom — vjerojatno "
                "delistani ili preimenovani. Vraćaju se sami ako ikad opet prorade."
            )
            cols = [c for c in ["symbol", "consecutive_failures", "last_error", "last_ok_at"] if c in retired.columns]
            st.dataframe(retired[cols], use_container_width=True, hide_index=True)

    decisions_df = load_decisions(db)

    if decisions_df.empty:
        st.info("No decisions recorded yet.")
    else:
        total = len(decisions_df)
        followed = len(decisions_df[decisions_df["user_action"] == "FOLLOWED"])
        correct_30 = len(decisions_df[decisions_df["outcome_30d"] == "correct"])
        wrong_30 = len(decisions_df[decisions_df["outcome_30d"] == "wrong"])

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total recommendations", total)
        col2.metric("You followed", f"{followed}/{total}")
        col3.metric("Bolje od S&P @30d", correct_30)
        col4.metric("Lošije od S&P @30d", wrong_30)
        st.caption(
            "Ishod se mjeri u odnosu na S&P 500 u istom razdoblju. WATCHLIST/WAIT i buy zone koje nisu "
            "dosegnute su 'neutral' — to nisu bile kupnje."
        )

        now = datetime.now(timezone.utc)

        # Highlight near-30d check notice
        near_30d_count = 0
        for _, row in decisions_df.iterrows():
            try:
                rec_at = pd.to_datetime(row.get("recommended_at"))
                if rec_at.tzinfo is None:
                    rec_at = rec_at.replace(tzinfo=timezone.utc)
                days_old = (now - rec_at).days
                if 25 <= days_old <= 35 and row.get("outcome_30d") == "pending":
                    near_30d_count += 1
            except Exception:
                pass
        if near_30d_count:
            st.info(f"🔔 {near_30d_count} preporuka je blizu 30-dnevne provjere — outcome se automatski ažurira srijedom.")

        st.subheader("All Decisions")
        query = search_box("dec_search", "ticker, akcija, teza, datum (npr. 2026-09)...")
        visible_df = filter_rows(
            decisions_df, query,
            ["symbol", "agent_action", "agent_thesis", "recommended_at", "user_action", "outcome_30d"],
        )
        if query:
            st.caption(f"{len(visible_df)} od {total} preporuka")
            if visible_df.empty:
                st.info("Nema preporuka za taj upit.")

        for _, row in visible_df.iterrows():
            # Compute age and near-30d flag
            days_old = 0
            near_30d = False
            try:
                rec_at = pd.to_datetime(row.get("recommended_at"))
                if rec_at.tzinfo is None:
                    rec_at = rec_at.replace(tzinfo=timezone.utc)
                days_old = (now - rec_at).days
                near_30d = 25 <= days_old <= 35 and row.get("outcome_30d") == "pending"
            except Exception:
                pass

            badge = " 🔔 30d check!" if near_30d else ""
            label = f"{row.get('symbol','')} — {row.get('agent_action','')} — {str(row.get('recommended_at',''))[:10]} ({days_old}d ago){badge}"

            with st.expander(label):
                if near_30d:
                    st.info("Ova preporuka je stara 25-35 dana — outcome_30d se automatski upisuje iduću srijedu.")
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Agent action:** {row.get('agent_action','')}")
                    st.markdown(f"**Buy zone:** {row.get('agent_buy_zone','N/A')}")
                    if row.get("agent_action") in ("BUY_BELOW", "ADD_ON_DIP"):
                        reached_at = row.get("buy_zone_reached_at")
                        if has_value(reached_at):
                            st.markdown(f"**Buy zone reached:** ✅ {reached_at} @ {format_price(row.get('buy_zone_reached_price'))}")
                        elif has_value(row.get("lowest_price_since_rec")):
                            st.markdown(
                                f"**Buy zone reached:** ❌ Not yet — lowest since: "
                                f"{format_price(row.get('lowest_price_since_rec'))} ({row.get('lowest_price_date','')})"
                            )
                        else:
                            st.markdown("**Buy zone reached:** ⏳ Not checked yet")
                    st.markdown(f"**Confidence:** {row.get('agent_confidence','N/A')}/10")
                    st.markdown(f"**Price at rec:** {row.get('price_at_recommendation','N/A')}")
                    st.caption(f"Thesis: {(row.get('agent_thesis') or '')[:200]}")
                with col2:
                    for suffix in ("30d", "90d", "180d"):
                        price_line = f"**{suffix} price:** {format_price(row.get('price_' + suffix))}"
                        outcome = row.get("outcome_" + suffix)
                        if has_value(row.get("price_" + suffix)) and has_value(outcome):
                            price_line += f" — {outcome}"
                        st.markdown(price_line)
                        if has_value(row.get("excess_return_" + suffix)):
                            stock_ret = float(row.get("return_" + suffix))
                            spy_ret = float(row.get("spy_return_" + suffix))
                            excess = float(row.get("excess_return_" + suffix))
                            st.caption(f"Dionica {stock_ret:+.1f}% vs S&P 500 {spy_ret:+.1f}% → {excess:+.1f} pp")

                for suffix, checkpoint_label in [("30d", "30 dana"), ("90d", "90 dana"), ("180d", "180 dana")]:
                    reasoning = row.get(f"outcome_reasoning_{suffix}")
                    if reasoning:
                        st.markdown(f"**Retrospektiva ({checkpoint_label}):**")
                        st.caption(reasoning)

                user_action = st.selectbox(
                    "Your action",
                    ["PENDING", "FOLLOWED", "IGNORED", "PARTIALLY_FOLLOWED"],
                    index=["PENDING", "FOLLOWED", "IGNORED", "PARTIALLY_FOLLOWED"].index(
                        row.get("user_action") or "PENDING"
                    ),
                    key=f"ua_{row['id']}",
                )
                raw_note = row.get("user_action_note")
                note_value = str(raw_note) if has_value(raw_note) else ""
                user_note = st.text_input(
                    "Why? (optional)",
                    value=note_value,
                    key=f"un_{row['id']}",
                )
                if st.button("Save", key=f"save_{row['id']}"):
                    try:
                        db.table("decisions").update({
                            "user_action": user_action,
                            "user_action_note": user_note or None,
                        }).eq("id", row["id"]).execute()
                        st.success("Saved.")
                    except Exception as e:
                        st.error(str(e))

# ── PAGE 5: Newsletteri ──────────────────────────────────────────────────────

elif page == "Newsletteri":
    st.title("Arhiva Newslettera")
    st.caption("Zadnjih 20 AI newslettera. Klikni za detalje.")

    nl_df = load_newsletters(db)

    if nl_df.empty:
        st.info("Nema newslettera u arhivi.")
    else:
        query = search_box("nl_search", "ticker, datum, riječ iz komentara...")

        parsed = []
        for _, row in nl_df.iterrows():
            content = row.get("content_json") or {}
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}
            parsed.append((str(row.get("sent_at", ""))[:10], row.get("subject", "—"), content))

        if query:
            def newsletter_matches(sent_at: str, subject: str, content: dict) -> bool:
                actions = content.get("top_actions") or []
                haystack = " ".join([
                    sent_at,
                    str(subject),
                    str(content.get("overall_market_comment", "")),
                    str(content.get("portfolio_note", "")),
                    str(content.get("no_trade_reason") or ""),
                    " ".join(str(a.get("ticker", "")) + " " + str(a.get("one_liner", "")) for a in actions),
                    " ".join(str(t) for t in (content.get("watchlist_this_week") or [])),
                ])
                return query in haystack.lower()

            parsed = [item for item in parsed if newsletter_matches(*item)]
            st.caption(f"{len(parsed)} od {len(nl_df)} newslettera")
            if not parsed:
                st.info("Nema newslettera za taj upit.")

        for sent_at, subject, content in parsed:
            with st.expander(f"📧 {sent_at} — {subject}"):
                col1, col2 = st.columns(2)

                with col1:
                    market_comment = content.get("overall_market_comment", "")
                    if market_comment:
                        st.markdown(f"**Tržište:** {market_comment}")

                    portfolio_note = content.get("portfolio_note", "")
                    if portfolio_note:
                        st.markdown(f"**Portfelj:** {portfolio_note}")

                    no_trade = content.get("no_trade_reason")
                    if no_trade:
                        st.info(f"⏳ Nema trgovine: {no_trade}")

                with col2:
                    top_actions = content.get("top_actions", [])
                    if top_actions:
                        st.markdown("**Top akcije:**")
                        for a in top_actions:
                            ticker = a.get("ticker", "")
                            action = a.get("action", "")
                            zone = a.get("buy_zone", "")
                            one_liner = a.get("one_liner", "")
                            zone_str = f" | {zone}" if zone and zone != "N/A" else ""
                            st.markdown(f"- **{ticker}** — {action}{zone_str}")
                            if one_liner:
                                st.caption(f"  {one_liner[:120]}")

                    watchlist_week = content.get("watchlist_this_week", [])
                    if watchlist_week:
                        st.markdown(f"**Watchlist:** {', '.join(watchlist_week)}")
