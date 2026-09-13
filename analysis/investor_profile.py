"""
Loads the investor profile — personal checklist from book notes plus the macro
lens — that is appended to the analyst system prompt, so every analysis states how
the investor would see the stock and then argues against it.

The canonical copy lives in Supabase (table investor_profile), uploaded by
scripts/upload_investor_profile.py from private/investor_profile.md. That folder
is gitignored: the GitHub repo is public and the profile is personal.
"""

from pathlib import Path

PROFILE_ID = "main"
LOCAL_PATH = Path(__file__).parent.parent / "private" / "investor_profile.md"


def load_investor_profile(client) -> str | None:
    if client:
        try:
            rows = client.table("investor_profile").select("*").eq("id", PROFILE_ID).execute().data or []
            if rows and (rows[0].get("content") or "").strip():
                return rows[0]["content"].strip()
        except Exception as exc:
            print(f"[investor_profile] Supabase load failed ({exc}); trying local file")

    if LOCAL_PATH.exists():
        return LOCAL_PATH.read_text(encoding="utf-8").strip() or None
    return None
