"""
Uploads private/investor_profile.md into the Supabase investor_profile table,
where the newsletter run reads it.

The profile holds the investor's personal checklist and macro views. The GitHub
repo is public, so the file lives in the gitignored private/ folder and only the
uploaded copy reaches GitHub Actions. Re-run after every edit of the file.

Usage: python scripts/upload_investor_profile.py
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.investor_profile import LOCAL_PATH, PROFILE_ID
from analysis.supabase_client import get_supabase


def main():
    if not LOCAL_PATH.exists():
        print(f"[upload_investor_profile] {LOCAL_PATH} not found")
        sys.exit(1)
    content = LOCAL_PATH.read_text(encoding="utf-8").strip()

    client = get_supabase()
    if not client:
        print("[upload_investor_profile] Supabase credentials missing (.env)")
        sys.exit(1)

    client.table("investor_profile").upsert({
        "id": PROFILE_ID,
        "content": content,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    print(f"[upload_investor_profile] Uploaded {len(content)} characters as profile '{PROFILE_ID}'")


if __name__ == "__main__":
    main()
