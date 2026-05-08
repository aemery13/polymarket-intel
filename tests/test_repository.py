"""
Tests for the repository layer.

Runs against InMemoryRepository (deterministic, no network). The same
test suite would pass against SupabaseRepository given a real database;
that's the whole point of having one Repository protocol.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import InMemoryRepository
from db.records import (
    LeaderboardEntryRecord,
    PositionSnapshot,
    ScoreRecord,
)


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────
# Wallets
# ─────────────────────────────────────────────
def test_upsert_wallet_new(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xABC", username="alice", scored_at=now)
    w = repo.get_wallet("0xabc")
    assert w is not None
    assert w.wallet == "0xabc"
    assert w.username == "alice"
    assert w.first_seen_at == now


def test_upsert_wallet_updates_last_scored(repo: InMemoryRepository, now: datetime):
    later = now + timedelta(days=3)
    repo.upsert_wallet("0xabc", "alice", scored_at=now)
    repo.upsert_wallet("0xabc", "alice", scored_at=later)
    w = repo.get_wallet("0xabc")
    assert w.first_seen_at == now           # preserved
    assert w.last_scored_at == later        # updated


def test_get_wallet_case_insensitive(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xABC", "alice", now)
    assert repo.get_wallet("0xabc") is not None
    assert repo.get_wallet("0xABC") is not None


# ─────────────────────────────────────────────
# Scores
# ─────────────────────────────────────────────
def _mk_score(wallet: str, scored_at: datetime, edge: float = 7.0) -> ScoreRecord:
    return ScoreRecord(
        wallet=wallet.lower(),
        scored_at=scored_at,
        classification="human",
        confidence=0.85,
        edge_score=edge,
        reason_codes=["normal_market_diversity"],
        signals={"win_rate": 0.72, "focus_ratio": 2.1},
        performance={"net_realised_pnl_usd": 50000, "open_positions_count": 5},
        leaderboard_pnl_usd=70000.0,
    )


def test_insert_and_get_latest_score(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xabc", "alice", now)
    repo.insert_score(_mk_score("0xabc", now - timedelta(days=2), edge=6.0))
    repo.insert_score(_mk_score("0xabc", now, edge=8.0))

    latest = repo.get_latest_score("0xabc")
    assert latest is not None
    assert latest.edge_score == 8.0


def test_score_history_window(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xabc", "alice", now)
    for i in range(10):
        repo.insert_score(_mk_score("0xabc", now - timedelta(days=i), edge=float(i)))

    recent = repo.get_score_history("0xabc", since=now - timedelta(days=3))
    assert len(recent) == 4  # days 0, 1, 2, 3
    # Newest first
    assert recent[0].scored_at > recent[-1].scored_at


def test_score_history_limit(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xabc", "alice", now)
    for i in range(20):
        repo.insert_score(_mk_score("0xabc", now - timedelta(days=i)))
    rows = repo.get_score_history("0xabc", limit=5)
    assert len(rows) == 5


def test_score_history_for_unknown_wallet(repo: InMemoryRepository):
    assert repo.get_score_history("0xnone") == []
    assert repo.get_latest_score("0xnone") is None


# ─────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────
def _mk_position(wallet: str, snap: datetime, cid: str, money: float) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=wallet.lower(),
        snapshot_at=snap,
        condition_id=cid,
        title=f"Market {cid}",
        outcome="Yes",
        category="Sports",
        money_in_usd=money,
        avg_entry_price=0.55,
        num_buys=2,
        first_trade_at=snap - timedelta(hours=4),
        last_trade_at=snap - timedelta(hours=1),
    )


def test_insert_positions_and_history(repo: InMemoryRepository, now: datetime):
    repo.upsert_wallet("0xabc", "alice", now)
    day1 = now - timedelta(days=1)
    day0 = now

    repo.insert_position_snapshots([
        _mk_position("0xabc", day1, "0xc1", 100),
        _mk_position("0xabc", day1, "0xc2", 200),
    ])
    repo.insert_position_snapshots([
        _mk_position("0xabc", day0, "0xc1", 100),  # still holding
        _mk_position("0xabc", day0, "0xc3", 50),   # new entry
    ])

    rows = repo.get_position_history("0xabc", since=day1 - timedelta(hours=1))
    assert len(rows) == 4

    # Only the most recent snapshot
    rows_today = repo.get_position_history("0xabc", since=day0 - timedelta(minutes=1))
    assert len(rows_today) == 2
    cids = {r.condition_id for r in rows_today}
    assert cids == {"0xc1", "0xc3"}


def test_insert_empty_positions_is_noop(repo: InMemoryRepository):
    repo.insert_position_snapshots([])  # should not raise
    assert repo.get_position_history("0xabc") == []


# ─────────────────────────────────────────────
# Leaderboard
# ─────────────────────────────────────────────
def _mk_lb(snap: datetime, n: int = 5) -> list[LeaderboardEntryRecord]:
    return [
        LeaderboardEntryRecord(
            snapshot_at=snap,
            rank=i,
            wallet=f"0x{i:040d}",
            username=f"user{i}",
            pnl_usd=10_000 - i * 100,
            volume_usd=50_000.0,
        )
        for i in range(1, n + 1)
    ]


def test_leaderboard_at_returns_closest_prior(repo: InMemoryRepository, now: datetime):
    a = now - timedelta(days=5)
    b = now - timedelta(days=2)
    repo.insert_leaderboard_snapshot(_mk_lb(a))
    repo.insert_leaderboard_snapshot(_mk_lb(b))

    # Asking for day-3 should pick day-5 (the closest snapshot at-or-before)
    target = now - timedelta(days=3)
    rows = repo.get_leaderboard_at(target)
    assert all(r.snapshot_at == a for r in rows)
    assert [r.rank for r in rows] == [1, 2, 3, 4, 5]


def test_latest_leaderboard(repo: InMemoryRepository, now: datetime):
    a = now - timedelta(days=5)
    b = now - timedelta(days=1)
    repo.insert_leaderboard_snapshot(_mk_lb(a, n=3))
    repo.insert_leaderboard_snapshot(_mk_lb(b, n=4))

    latest = repo.get_latest_leaderboard()
    assert len(latest) == 4
    assert all(r.snapshot_at == b for r in latest)


def test_leaderboard_at_empty(repo: InMemoryRepository, now: datetime):
    assert repo.get_leaderboard_at(now) == []
    assert repo.get_latest_leaderboard() == []


# ─────────────────────────────────────────────
# Audit (snapshot runs)
# ─────────────────────────────────────────────
def test_start_and_finish_snapshot_run(repo: InMemoryRepository, now: datetime):
    rid = repo.start_snapshot_run(now)
    assert isinstance(rid, int) and rid > 0
    repo.finish_snapshot_run(
        run_id=rid, finished_at=now + timedelta(minutes=5),
        wallets_scored=42, errors=1, notes="top_n=50",
    )
    # No public getter; just confirm finish doesn't blow up


# ─────────────────────────────────────────────
# Protocol shape
# ─────────────────────────────────────────────
def test_in_memory_satisfies_protocol():
    """InMemoryRepository must satisfy the Repository protocol structurally."""
    from db import Repository
    repo = InMemoryRepository()
    assert isinstance(repo, Repository)
