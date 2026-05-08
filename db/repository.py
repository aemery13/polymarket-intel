"""
Repository abstraction.

The API and snapshot job depend only on the Repository protocol.
Production wires this to Supabase; tests wire it to InMemoryRepository.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional, Protocol, runtime_checkable

from .records import (
    LeaderboardEntryRecord,
    PositionSnapshot,
    ScoreRecord,
    SnapshotRun,
    WalletRecord,
)


# ─────────────────────────────────────────────
# Protocol — every method either returns or raises
# ─────────────────────────────────────────────
@runtime_checkable
class Repository(Protocol):
    # Wallets
    def upsert_wallet(self, wallet: str, username: Optional[str], scored_at: datetime) -> None: ...
    def get_wallet(self, wallet: str) -> Optional[WalletRecord]: ...

    # Scores
    def insert_score(self, score: ScoreRecord) -> None: ...
    def get_score_history(
        self, wallet: str, since: Optional[datetime] = None, limit: int = 365,
    ) -> list[ScoreRecord]: ...
    def get_latest_score(self, wallet: str) -> Optional[ScoreRecord]: ...

    # Positions
    def insert_position_snapshots(self, positions: list[PositionSnapshot]) -> None: ...
    def get_position_history(
        self, wallet: str, since: Optional[datetime] = None,
    ) -> list[PositionSnapshot]: ...

    # Leaderboard
    def insert_leaderboard_snapshot(self, entries: list[LeaderboardEntryRecord]) -> None: ...
    def get_leaderboard_at(self, at: datetime) -> list[LeaderboardEntryRecord]: ...
    def get_latest_leaderboard(self) -> list[LeaderboardEntryRecord]: ...

    # Audit
    def start_snapshot_run(self, started_at: datetime) -> int: ...
    def finish_snapshot_run(
        self, run_id: int, finished_at: datetime,
        wallets_scored: int, errors: int, notes: Optional[str] = None,
    ) -> None: ...


# ─────────────────────────────────────────────
# In-memory implementation — tests + dev mode
# ─────────────────────────────────────────────
class InMemoryRepository:
    """Thread-safe in-memory repo. Lossy across restarts. Tests + dev only."""

    def __init__(self) -> None:
        self._wallets: dict[str, WalletRecord] = {}
        self._scores: list[ScoreRecord] = []
        self._positions: list[PositionSnapshot] = []
        self._leaderboard: list[LeaderboardEntryRecord] = []
        self._runs: list[SnapshotRun] = []
        self._lock = Lock()

    # ── wallets
    def upsert_wallet(self, wallet: str, username: Optional[str], scored_at: datetime) -> None:
        wallet = wallet.lower()
        with self._lock:
            existing = self._wallets.get(wallet)
            if existing is None:
                self._wallets[wallet] = WalletRecord(
                    wallet=wallet,
                    username=username,
                    first_seen_at=scored_at,
                    last_scored_at=scored_at,
                )
            else:
                existing.username = username or existing.username
                existing.last_scored_at = scored_at

    def get_wallet(self, wallet: str) -> Optional[WalletRecord]:
        with self._lock:
            return self._wallets.get(wallet.lower())

    # ── scores
    def insert_score(self, score: ScoreRecord) -> None:
        with self._lock:
            self._scores.append(score)

    def get_score_history(
        self, wallet: str, since: Optional[datetime] = None, limit: int = 365,
    ) -> list[ScoreRecord]:
        wallet = wallet.lower()
        with self._lock:
            rows = [s for s in self._scores if s.wallet == wallet]
        if since is not None:
            rows = [s for s in rows if s.scored_at >= since]
        rows.sort(key=lambda s: s.scored_at, reverse=True)
        return rows[:limit]

    def get_latest_score(self, wallet: str) -> Optional[ScoreRecord]:
        rows = self.get_score_history(wallet, limit=1)
        return rows[0] if rows else None

    # ── positions
    def insert_position_snapshots(self, positions: list[PositionSnapshot]) -> None:
        if not positions:
            return
        with self._lock:
            self._positions.extend(positions)

    def get_position_history(
        self, wallet: str, since: Optional[datetime] = None,
    ) -> list[PositionSnapshot]:
        wallet = wallet.lower()
        with self._lock:
            rows = [p for p in self._positions if p.wallet == wallet]
        if since is not None:
            rows = [p for p in rows if p.snapshot_at >= since]
        rows.sort(key=lambda p: p.snapshot_at, reverse=True)
        return rows

    # ── leaderboard
    def insert_leaderboard_snapshot(self, entries: list[LeaderboardEntryRecord]) -> None:
        if not entries:
            return
        with self._lock:
            self._leaderboard.extend(entries)

    def get_leaderboard_at(self, at: datetime) -> list[LeaderboardEntryRecord]:
        # Pick the closest snapshot timestamp at or before `at`
        with self._lock:
            timestamps = sorted({e.snapshot_at for e in self._leaderboard})
        candidates = [t for t in timestamps if t <= at]
        if not candidates:
            return []
        target = candidates[-1]
        with self._lock:
            rows = [e for e in self._leaderboard if e.snapshot_at == target]
        rows.sort(key=lambda e: e.rank)
        return rows

    def get_latest_leaderboard(self) -> list[LeaderboardEntryRecord]:
        if not self._leaderboard:
            return []
        return self.get_leaderboard_at(datetime.now(timezone.utc) + timedelta(days=1))

    # ── audit
    def start_snapshot_run(self, started_at: datetime) -> int:
        with self._lock:
            run = SnapshotRun(started_at=started_at, id=len(self._runs) + 1)
            self._runs.append(run)
            return run.id  # type: ignore[return-value]

    def finish_snapshot_run(
        self, run_id: int, finished_at: datetime,
        wallets_scored: int, errors: int, notes: Optional[str] = None,
    ) -> None:
        with self._lock:
            for run in self._runs:
                if run.id == run_id:
                    run.finished_at = finished_at
                    run.wallets_scored = wallets_scored
                    run.errors = errors
                    run.notes = notes
                    return
