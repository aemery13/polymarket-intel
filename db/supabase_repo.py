"""
Supabase-backed Repository implementation.

Same interface as InMemoryRepository — hot-swappable.

Connection: set SUPABASE_URL and SUPABASE_KEY env vars (use the
service-role key for the snapshot job, anon key for read-only API
deployments).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from .records import (
    LeaderboardEntryRecord,
    PositionSnapshot,
    ScoreRecord,
    WalletRecord,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # Supabase returns ISO 8601 strings
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class SupabaseRepository:
    """Thin wrapper around supabase-py client. All methods are sync."""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None) -> None:
        try:
            from supabase import Client, create_client
        except ImportError as e:
            raise RuntimeError(
                "supabase package not installed. `pip install supabase`."
            ) from e

        url = url or os.environ.get("SUPABASE_URL")
        key = key or os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set to use SupabaseRepository."
            )

        self._client: Client = create_client(url, key)

    # ─────────────────────────────────────────
    # Wallets
    # ─────────────────────────────────────────
    def upsert_wallet(self, wallet: str, username: Optional[str], scored_at: datetime) -> None:
        wallet = wallet.lower()
        # Insert if missing; update last_scored_at + username always.
        # `upsert` with on_conflict updates the named columns.
        self._client.table("wallets").upsert(
            {
                "wallet": wallet,
                "username": username,
                "last_scored_at": _iso(scored_at),
            },
            on_conflict="wallet",
        ).execute()

    def get_wallet(self, wallet: str) -> Optional[WalletRecord]:
        res = (
            self._client.table("wallets")
            .select("wallet, username, first_seen_at, last_scored_at")
            .eq("wallet", wallet.lower())
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        r = rows[0]
        return WalletRecord(
            wallet=r["wallet"],
            username=r.get("username"),
            first_seen_at=_parse_iso(r["first_seen_at"]),  # type: ignore[arg-type]
            last_scored_at=_parse_iso(r.get("last_scored_at")),
        )

    # ─────────────────────────────────────────
    # Scores
    # ─────────────────────────────────────────
    def insert_score(self, score: ScoreRecord) -> None:
        self._client.table("wallet_scores").insert({
            "wallet": score.wallet.lower(),
            "scored_at": _iso(score.scored_at),
            "classification": score.classification,
            "confidence": score.confidence,
            "edge_score": score.edge_score,
            "reason_codes": score.reason_codes,
            "signals": score.signals,
            "performance": score.performance,
            "leaderboard_pnl_usd": score.leaderboard_pnl_usd,
            "scoring_version": score.scoring_version,
        }).execute()

    def get_score_history(
        self, wallet: str, since: Optional[datetime] = None, limit: int = 365,
    ) -> list[ScoreRecord]:
        q = (
            self._client.table("wallet_scores")
            .select("*")
            .eq("wallet", wallet.lower())
            .order("scored_at", desc=True)
            .limit(limit)
        )
        if since is not None:
            q = q.gte("scored_at", _iso(since))
        res = q.execute()
        return [_score_from_row(r) for r in (res.data or [])]

    def get_latest_score(self, wallet: str) -> Optional[ScoreRecord]:
        res = (
            self._client.table("wallet_scores")
            .select("*")
            .eq("wallet", wallet.lower())
            .order("scored_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return _score_from_row(rows[0]) if rows else None

    # ─────────────────────────────────────────
    # Positions
    # ─────────────────────────────────────────
    def insert_position_snapshots(self, positions: list[PositionSnapshot]) -> None:
        if not positions:
            return
        rows = [
            {
                "wallet": p.wallet.lower(),
                "snapshot_at": _iso(p.snapshot_at),
                "condition_id": p.condition_id,
                "title": p.title,
                "outcome": p.outcome,
                "category": p.category,
                "money_in_usd": p.money_in_usd,
                "avg_entry_price": p.avg_entry_price,
                "num_buys": p.num_buys,
                "first_trade_at": _iso(p.first_trade_at) if p.first_trade_at else None,
                "last_trade_at": _iso(p.last_trade_at) if p.last_trade_at else None,
            }
            for p in positions
        ]
        # Insert in chunks of 500 to stay under PostgREST request size limits
        for i in range(0, len(rows), 500):
            self._client.table("open_position_snapshots").insert(rows[i : i + 500]).execute()

    def get_position_history(
        self, wallet: str, since: Optional[datetime] = None,
    ) -> list[PositionSnapshot]:
        q = (
            self._client.table("open_position_snapshots")
            .select("*")
            .eq("wallet", wallet.lower())
            .order("snapshot_at", desc=True)
        )
        if since is not None:
            q = q.gte("snapshot_at", _iso(since))
        res = q.execute()
        return [_position_from_row(r) for r in (res.data or [])]

    # ─────────────────────────────────────────
    # Leaderboard
    # ─────────────────────────────────────────
    def insert_leaderboard_snapshot(self, entries: list[LeaderboardEntryRecord]) -> None:
        if not entries:
            return
        rows = [
            {
                "snapshot_at": _iso(e.snapshot_at),
                "rank": e.rank,
                "wallet": e.wallet.lower(),
                "username": e.username,
                "pnl_usd": e.pnl_usd,
                "volume_usd": e.volume_usd,
            }
            for e in entries
        ]
        for i in range(0, len(rows), 500):
            self._client.table("leaderboard_snapshots").insert(rows[i : i + 500]).execute()

    def get_leaderboard_at(self, at: datetime) -> list[LeaderboardEntryRecord]:
        # Find the closest snapshot_at <= `at`, then return all rows with that ts
        res = (
            self._client.table("leaderboard_snapshots")
            .select("snapshot_at")
            .lte("snapshot_at", _iso(at))
            .order("snapshot_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return []
        target = rows[0]["snapshot_at"]
        res = (
            self._client.table("leaderboard_snapshots")
            .select("*")
            .eq("snapshot_at", target)
            .order("rank")
            .execute()
        )
        return [_lb_from_row(r) for r in (res.data or [])]

    def get_latest_leaderboard(self) -> list[LeaderboardEntryRecord]:
        # Use the view defined in schema.sql
        res = self._client.table("latest_leaderboard").select("*").execute()
        return [_lb_from_row(r) for r in (res.data or [])]

    # ─────────────────────────────────────────
    # Audit
    # ─────────────────────────────────────────
    def start_snapshot_run(self, started_at: datetime) -> int:
        res = (
            self._client.table("snapshot_runs")
            .insert({"started_at": _iso(started_at)})
            .execute()
        )
        return int((res.data or [{}])[0].get("id", 0))

    def finish_snapshot_run(
        self, run_id: int, finished_at: datetime,
        wallets_scored: int, errors: int, notes: Optional[str] = None,
    ) -> None:
        self._client.table("snapshot_runs").update({
            "finished_at": _iso(finished_at),
            "wallets_scored": wallets_scored,
            "errors": errors,
            "notes": notes,
        }).eq("id", run_id).execute()


# ─────────────────────────────────────────────
# Row → dataclass helpers
# ─────────────────────────────────────────────
def _score_from_row(r: dict[str, Any]) -> ScoreRecord:
    return ScoreRecord(
        wallet=r["wallet"],
        scored_at=_parse_iso(r["scored_at"]),  # type: ignore[arg-type]
        classification=r["classification"],
        confidence=float(r["confidence"]),
        edge_score=float(r["edge_score"]) if r.get("edge_score") is not None else None,
        reason_codes=r.get("reason_codes") or [],
        signals=r.get("signals") or {},
        performance=r.get("performance") or {},
        leaderboard_pnl_usd=(
            float(r["leaderboard_pnl_usd"]) if r.get("leaderboard_pnl_usd") is not None else None
        ),
        scoring_version=r.get("scoring_version", "1.0.0"),
    )


def _position_from_row(r: dict[str, Any]) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=r["wallet"],
        snapshot_at=_parse_iso(r["snapshot_at"]),  # type: ignore[arg-type]
        condition_id=r["condition_id"],
        title=r["title"],
        outcome=r.get("outcome"),
        category=r.get("category"),
        money_in_usd=float(r["money_in_usd"]),
        avg_entry_price=(
            float(r["avg_entry_price"]) if r.get("avg_entry_price") is not None else None
        ),
        num_buys=r.get("num_buys"),
        first_trade_at=_parse_iso(r.get("first_trade_at")),
        last_trade_at=_parse_iso(r.get("last_trade_at")),
    )


def _lb_from_row(r: dict[str, Any]) -> LeaderboardEntryRecord:
    return LeaderboardEntryRecord(
        snapshot_at=_parse_iso(r["snapshot_at"]),  # type: ignore[arg-type]
        rank=int(r["rank"]),
        wallet=r["wallet"],
        username=r.get("username"),
        pnl_usd=float(r["pnl_usd"]),
        volume_usd=(
            float(r["volume_usd"]) if r.get("volume_usd") is not None else None
        ),
    )
