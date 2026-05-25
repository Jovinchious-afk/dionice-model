"""
Dionice Portfolio Tracker — Streamlit app (4 pages)
Hosted on Streamlit Community Cloud (requires public GitHub repo)
"""

import os
from datetime import datetime, timezone

import sys
import pandas as pd
import streamlit as st

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
                "Avg Cost (EUR)": round(avg_cost, 4),
                "Total Cost (EUR)": round(h["Total Cost (EUR)"], 2),
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Sidebar navigation ───────────────────────────────────────────────────────

st.sidebar.title("📊 Dionice")
page = st.sidebar.radio(
    "Navigate",
    ["Portfolio", "Log Trade", "Watchlist", "Decisions"],
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
        st.subheader("Current Holdings")
        st.dataframe(portfolio, use_container_width=True, hide_index=True)

        total_invested = portfolio["Total Cost (EUR)"].sum()
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Invested", f"{total_invested:,.2f} EUR")
        col2.metric("Positions", len(portfolio))
        col3.metric("Largest position", portfolio.sort_values("Total Cost (EUR)", ascending=False).iloc[0]["Symbol"])

        # Sector concentration warning
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
        for _, row in wl_df.iterrows():
            action_color = {
                "BUY_BELOW": "green", "ADD_ON_DIP": "green",
                "WATCHLIST": "orange", "WAIT": "gray", "SELL": "red",
            }.get(row.get("action", ""), "gray")

            with st.container():
                col1, col2, col3 = st.columns([2, 3, 2])
                with col1:
                    st.markdown(f"**{row.get('symbol', '')}**")
                    st.caption(row.get("company_name", ""))
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
        # Summary stats
        total = len(decisions_df)
        followed = len(decisions_df[decisions_df["user_action"] == "FOLLOWED"])
        correct_30 = len(decisions_df[decisions_df["outcome_30d"] == "correct"])
        wrong_30 = len(decisions_df[decisions_df["outcome_30d"] == "wrong"])

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total recommendations", total)
        col2.metric("You followed", f"{followed}/{total}")
        col3.metric("Correct at 30d", correct_30)
        col4.metric("Wrong at 30d", wrong_30)

        # Editable decision log
        st.subheader("All Decisions")
        for _, row in decisions_df.iterrows():
            with st.expander(f"{row.get('symbol','')} — {row.get('agent_action','')} — {str(row.get('recommended_at',''))[:10]}"):
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

                # User action update
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
