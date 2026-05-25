"""
Weekly job: fills price_30d, price_90d, price_180d for old decisions in Supabase.
Runs every Monday via GitHub Actions.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance as yf

from analysis.supabase_client import get_supabase


def get_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        return getattr(info, "last_price", None)
    except Exception:
        return None


def main():
    client = get_supabase()
    if not client:
        print("[update_prices] Supabase credentials missing.")
        return
    now = datetime.now(timezone.utc)

    result = client.table("decisions").select("*").execute()
    decisions = result.data or []

    for dec in decisions:
        rec_at_str = dec.get("recommended_at")
        if not rec_at_str:
            continue

        try:
            rec_at = datetime.fromisoformat(rec_at_str)
            if rec_at.tzinfo is None:
                rec_at = rec_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age_days = (now - rec_at).days
        ticker = dec.get("symbol", "")
        if not ticker:
            continue

        updates = {}

        if age_days >= 30 and dec.get("price_30d") is None:
            price = get_current_price(ticker)
            if price:
                updates["price_30d"] = price
                rec_price = dec.get("price_at_recommendation")
                if rec_price:
                    outcome = "correct" if price > float(rec_price) else "wrong"
                    updates["outcome_30d"] = outcome

        if age_days >= 90 and dec.get("price_90d") is None:
            price = get_current_price(ticker)
            if price:
                updates["price_90d"] = price
                rec_price = dec.get("price_at_recommendation")
                if rec_price:
                    outcome = "correct" if price > float(rec_price) else "wrong"
                    updates["outcome_90d"] = outcome

        if age_days >= 180 and dec.get("price_180d") is None:
            price = get_current_price(ticker)
            if price:
                updates["price_180d"] = price
                rec_price = dec.get("price_at_recommendation")
                if rec_price:
                    outcome = "correct" if price > float(rec_price) else "wrong"
                    updates["outcome_180d"] = outcome

        if updates:
            client.table("decisions").update(updates).eq("id", dec["id"]).execute()
            print(f"[update_prices] Updated {ticker} (age {age_days}d): {updates}")

    print("[update_prices] Done.")


if __name__ == "__main__":
    main()
