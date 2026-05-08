"""
Polymarket API client.

Single-responsibility wrapper around the data-api endpoints that your
existing scripts hit. Handles pagination, retries, the
TRADE-and-REDEEM-must-be-separate quirk, and field type coercion.

All methods return raw dicts/DataFrames — no scoring logic lives here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://polymarket.com/",
}

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ─────────────────────────────────────────────
# Session with sane retry behaviour
# ─────────────────────────────────────────────
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


@dataclass
class PolymarketClient:
    """Thin client for Polymarket data endpoints."""

    timeout: int = 15
    page_size: int = 500
    max_records: int = 3000
    inter_page_delay: float = 0.25

    def __post_init__(self) -> None:
        self._session = _build_session()

    # ─────────────────────────────────────────────
    # Leaderboard
    # ─────────────────────────────────────────────
    def get_leaderboard(self, limit: int = 200) -> pd.DataFrame:
        url = f"{DATA_API}/v1/leaderboard?limit={limit}"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        traders = data if isinstance(data, list) else data.get("data", [])
        df = pd.DataFrame(traders)
        if "pnl" in df.columns:
            df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
        if "vol" in df.columns:
            df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
        return df

    def find_wallet(self, username: str, lb_df: Optional[pd.DataFrame] = None) -> tuple[Optional[str], float]:
        """Look up a wallet by username via leaderboard scan."""
        if lb_df is None:
            lb_df = self.get_leaderboard(limit=200)
        for _, row in lb_df.iterrows():
            name = row.get("userName") or row.get("name", "")
            if str(name).lower() == username.lower():
                return row.get("proxyWallet"), float(row.get("pnl", 0) or 0)
        return None, 0.0

    # ─────────────────────────────────────────────
    # Activity (TRADE + REDEEM must be separate calls)
    # ─────────────────────────────────────────────
    def _fetch_activity_pages(self, wallet: str, activity_type: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while len(records) < self.max_records:
            url = (
                f"{DATA_API}/activity"
                f"?user={wallet}&limit={self.page_size}"
                f"&offset={offset}&type={activity_type}"
            )
            try:
                r = self._session.get(url, timeout=self.timeout)
                r.raise_for_status()
            except requests.exceptions.HTTPError:
                break

            data = r.json()
            page = data if isinstance(data, list) else data.get("data", [])
            if not page:
                break

            records.extend(page)
            if len(page) < self.page_size:
                break

            offset += self.page_size
            time.sleep(self.inter_page_delay)

        return records

    def get_activity(self, wallet: str) -> pd.DataFrame:
        """Pull TRADE + REDEEM and return a single DataFrame."""
        trades = self._fetch_activity_pages(wallet, "TRADE")
        redeems = self._fetch_activity_pages(wallet, "REDEEM")
        return self._normalize_activity(trades + redeems)

    @staticmethod
    def _normalize_activity(records: Iterable[dict]) -> pd.DataFrame:
        df = pd.DataFrame(list(records))
        if df.empty:
            return df

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(
                pd.to_numeric(df["timestamp"], errors="coerce"),
                unit="s",
                utc=True,
            )

        for col in ("price", "size", "usdcSize"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "side" in df.columns:
            df["side"] = df["side"].astype(str).str.upper()

        return df

    # ─────────────────────────────────────────────
    # Positions (current snapshot only — disappears on redeem)
    # ─────────────────────────────────────────────
    def get_positions(self, wallet: str, limit: int = 500) -> pd.DataFrame:
        url = (
            f"{DATA_API}/positions"
            f"?user={wallet}&limit={limit}&sortBy=CASHPNL&sortDirection=DESC"
        )
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        positions = data if isinstance(data, list) else data.get("data", [])
        df = pd.DataFrame(positions)

        for col in (
            "cashPnl", "realizedPnl", "percentPnl",
            "avgPrice", "curPrice", "initialValue", "currentValue",
        ):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df
