"""
Lightweight Supabase REST API client using only the built-in requests library.
Replaces the supabase Python package (which requires C++ build tools on Windows).
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()


class SupabaseTable:
    def __init__(self, base_url: str, key: str, table: str):
        clean = base_url.rstrip("/").removesuffix("/rest/v1")
        self._url = f"{clean}/rest/v1/{table}"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self._filters = []

    def select(self, columns: str = "*"):
        self._select = columns
        self._filters = []
        self._order_col = None
        self._order_desc = False
        self._limit_val = None
        return self

    def eq(self, column: str, value):
        self._filters.append(f"{column}=eq.{value}")
        return self

    def order(self, column: str, desc: bool = False):
        self._order_col = column
        self._order_desc = desc
        return self

    def limit(self, n: int):
        self._limit_val = n
        return self

    def insert(self, row: dict):
        self._insert_data = row
        return self

    def update(self, data: dict):
        self._update_data = data
        return self

    def execute(self):
        if hasattr(self, "_insert_data"):
            resp = requests.post(self._url, json=self._insert_data, headers=self._headers, timeout=15)
            resp.raise_for_status()
            return type("Result", (), {"data": resp.json()})()

        if hasattr(self, "_update_data"):
            params = "&".join(getattr(self, "_filters", []))
            url = f"{self._url}?{params}" if params else self._url
            resp = requests.patch(url, json=self._update_data, headers=self._headers, timeout=15)
            resp.raise_for_status()
            return type("Result", (), {"data": resp.json()})()

        # SELECT
        params = f"select={getattr(self, '_select', '*')}"
        for f in getattr(self, "_filters", []):
            params += f"&{f}"
        if getattr(self, "_order_col", None):
            direction = "desc" if self._order_desc else "asc"
            params += f"&order={self._order_col}.{direction}"
        if getattr(self, "_limit_val", None):
            params += f"&limit={self._limit_val}"
        resp = requests.get(f"{self._url}?{params}", headers=self._headers, timeout=15)
        resp.raise_for_status()
        return type("Result", (), {"data": resp.json()})()


class SupabaseClient:
    def __init__(self, url: str, key: str):
        self._url = url.rstrip("/")
        self._key = key

    def table(self, name: str) -> SupabaseTable:
        return SupabaseTable(self._url, self._key, name)


def get_supabase() -> SupabaseClient | None:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    return SupabaseClient(url, key)
