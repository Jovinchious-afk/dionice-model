"""
Dionice Portfolio Tracker — Streamlit app (5 pages)
Hosted on Streamlit Community Cloud (requires public GitHub repo)
"""

import os
from datetime import datetime, timezone, timedelta

import sys
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def load_newsletters(db) -> pd.DataFrame:
    try:
        result = db.table("newsletters").select("*").order("sent_at", desc=True).limit(20).execute()
        if not result.data:
            return pd.DataFrame()
        return pd.DataFrame(result.data)
    except Exception as e:
        st.error(f"Failed to load newsletters: {e}")
        return pd.DataFrame()


def compute_portfolio(tx_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregates transactions into current holdings."""
    if tx_df.empty:
        return pd.DataFrame()

    holdings: dict[str, dict] = {}
    for _, row in tx_df.sort_values("trade_date").iterrows():
        sym = row["symbol"]
        if sym not in holdings:
            holdings[sym] = {
                "Symbol": sym,
                "Company": row.get("company_name", sym),
                "Shares": 0.0,
                "Total Cost (EUR)": 0.0,
                "Currency": row.get("currency", "EUR"),
            }
        shares = float(row.get("shares", 0))
        price = float(row.get("price_per_share", 0))
        if row["action"] == "BUY":
            holdings[sym]["Shares"] += shares
            holdings[sym]["Total Cost (EUR)"] += shares * price
        elif row["action"] == "SELL":
            if holdings[sym]["Shares"] > 0:
                ratio = max(0, (holdings[sym]["Shares"] - shares) / holdings[sym]["Shares"])
                holdings[sym]["Total Cost (EUR)"] *= ratio
            holdings[sym]["Shares"] = max(0, holdings[sym]["Shares"] - shares)

    rows = []
    for sym, h in holdings.items():
        if h["Shares"] > 0:
            avg_cost = h["Total Cost (EUR)"] / h["Shares"]
            rows.append({
                "Symbol": sym,
                "Company": h["Company"],
                "Shares": round(h["Shares"], 4),
                "Avg Cost (USD)": round(avg_cost, 4),
                "Total Cost (USD)": round(h["Total Cost (EUR)"], 2),
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


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
    portfolio = compute_portfolio(tx_df)

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

        # Summary metrics
        col1, col2, col3 = st.columns(3)
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
        col3.metric("Positions", len(portfolio))

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

        if len(portfolio) == 1:
            st.warning(
                "⚠️ Portfolio is 100% concentrated in one stock. "
                "Consider diversifying as new opportunities arise."
            )

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
        # Filter bar
        filter_action = st.radio(
            "Filter po akciji",
            ["Sve", "BUY / ADD ON DIP", "WATCHLIST", "WAIT"],
            horizontal=True,
        )
        filter_map = {
            "BUY / ADD ON DIP": ["BUY_BELOW", "ADD_ON_DIP"],
            "WATCHLIST": ["WATCHLIST"],
            "WAIT": ["WAIT"],
        }
        if filter_action != "Sve":
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

                with st.container():
                    col1, col2, col3 = st.columns([2, 3, 2])
                    with col1:
                        st.markdown(f"**{row.get('symbol', '')}**")
                        st.caption(row.get("company_name", "") + age_label)
                        st.caption(f"Category: {row.get('category', 'N/A')}")
                    with col2:
                        st.markdown(f":{action_color}[{row.get('action', '')}] | Zone: {row.get('buy_zone', 'N/A')} | Target: {row.get('target_price', 'N/A')}")
                        st.caption(f"Confidence: {row.get('confidence', 'N/A')}/10")
                        if row.get("thesis"):
                            st.caption(row["thesis"][:200])
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
        col3.metric("Correct at 30d", correct_30)
        col4.metric("Wrong at 30d", wrong_30)

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
            st.warning(f"🔔 {near_30d_count} preporuka je blizu 30-dnevne provjere — provjeri cijenu i ažuriraj outcome.")

        st.subheader("All Decisions")
        for _, row in decisions_df.iterrows():
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
                    st.warning("Ova preporuka je stara 25-35 dana. Provjeri trenutnu cijenu i upiši outcome_30d u Supabase.")
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Agent action:** {row.get('agent_action','')}")
                    st.markdown(f"**Buy zone:** {row.get('agent_buy_zone','N/A')}")
                    st.markdown(f"**Confidence:** {row.get('agent_confidence','N/A')}/10")
                    st.markdown(f"**Price at rec:** {row.get('price_at_recommendation','N/A')}")
                    st.caption(f"Thesis: {(row.get('agent_thesis') or '')[:200]}")
                with col2:
                    st.markdown(f"**30d price:** {row.get('price_30d','Pending')}")
                    st.markdown(f"**90d price:** {row.get('price_90d','Pending')}")
                    st.markdown(f"**180d price:** {row.get('price_180d','Pending')}")
                    st.markdown(f"**Outcome 30d:** {row.get('outcome_30d','pending')}")

                user_action = st.selectbox(
                    "Your action",
                    ["PENDING", "FOLLOWED", "IGNORED", "PARTIALLY_FOLLOWED"],
                    index=["PENDING", "FOLLOWED", "IGNORED", "PARTIALLY_FOLLOWED"].index(
                        row.get("user_action") or "PENDING"
                    ),
                    key=f"ua_{row['id']}",
                )
                user_note = st.text_input(
                    "Why? (optional)",
                    value=row.get("user_action_note") or "",
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
        for _, row in nl_df.iterrows():
            sent_at = str(row.get("sent_at", ""))[:10]
            subject = row.get("subject", "—")
            nl_type = row.get("type", "WEEKLY")
            content = row.get("content_json") or {}
            if isinstance(content, str):
                import json
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}

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
