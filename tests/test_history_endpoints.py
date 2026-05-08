"""
Tests for the history endpoints — /wallet/{address}/history etc.

Uses TestClient + a pre-populated InMemoryRepository injected into
api.main.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api import main as api_main
from db import InMemoryRepository
from db.records import (
    LeaderboardEntryRecord,
    PositionSnapshot,
    ScoreRecord,
)


WALLET = "0x" + "a" * 40


def _seed_repo(repo: InMemoryRepository) -> None:
    now = datetime.now(timezone.utc)

    # Wallet
    repo.upsert_wallet(WALLET, "alice", scored_at=now)

    # 5 days of scores
    for i in range(5):
        repo.insert_score(ScoreRecord(
            wallet=WALLET,
            scored_at=now - timedelta(days=i),
            classification="human",
            confidence=0.85,
            edge_score=7.0 + i * 0.1,
            reason_codes=["normal_market_diversity"],
            signals={"win_rate": 0.7 + i * 0.01, "focus_ratio": 2.0},
            performance={"net_realised_pnl_usd": 50000 + i * 1000, "open_positions_count": 3},
            leaderboard_pnl_usd=70000.0,
        ))

    # 3 days of position snapshots
    for i in range(3):
        snap = now - timedelta(days=i)
        repo.insert_position_snapshots([
            PositionSnapshot(
                wallet=WALLET,
                snapshot_at=snap,
                condition_id=f"0xcond{j}",
                title=f"Market {j}",
                outcome="Yes",
                category="Sports",
                money_in_usd=1000.0 * (j + 1),
                avg_entry_price=0.55,
                num_buys=2,
                first_trade_at=snap - timedelta(hours=4),
                last_trade_at=snap - timedelta(hours=1),
            )
            for j in range(2)
        ])

    # Leaderboard snapshots: yesterday + today
    for d in [now - timedelta(days=1), now]:
        repo.insert_leaderboard_snapshot([
            LeaderboardEntryRecord(
                snapshot_at=d, rank=i, wallet=f"0xrank{i:039d}",
                username=f"user{i}", pnl_usd=100000 - i * 1000, volume_usd=500000.0,
            )
            for i in range(1, 6)
        ])


def _build_client() -> TestClient:
    """Replace api.main.repo with a fresh seeded in-memory repo for the test."""
    fresh_repo = InMemoryRepository()
    _seed_repo(fresh_repo)
    api_main.repo = fresh_repo
    api_main.cache.clear()
    return TestClient(api_main.app)


# ─────────────────────────────────────────────
# /wallet/{address}/history
# ─────────────────────────────────────────────
def test_wallet_history_returns_time_series():
    client = _build_client()
    r = client.get(f"/wallet/{WALLET}/history?days=10")
    assert r.status_code == 200
    body = r.json()
    assert body["wallet"] == WALLET
    assert body["count"] == 5
    assert len(body["history"]) == 5

    # Newest first
    timestamps = [row["scored_at"] for row in body["history"]]
    assert timestamps == sorted(timestamps, reverse=True)

    # Required fields are present
    row = body["history"][0]
    for key in ("classification", "edge_score", "win_rate", "net_realised_pnl_usd"):
        assert key in row


def test_wallet_history_window_filters():
    client = _build_client()
    r = client.get(f"/wallet/{WALLET}/history?days=2")
    assert r.status_code == 200
    body = r.json()
    # days=2 should cover "today" and "yesterday" — 3 rows max
    # (the rows from days 0, 1, and possibly 2 if it falls inside the window)
    assert 1 <= body["count"] <= 3


def test_wallet_history_validates_address():
    client = _build_client()
    r = client.get("/wallet/notanaddress/history")
    assert r.status_code == 400


def test_wallet_history_unknown_returns_empty():
    client = _build_client()
    r = client.get("/wallet/0x" + "f" * 40 + "/history")
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ─────────────────────────────────────────────
# /wallet/{address}/positions/history
# ─────────────────────────────────────────────
def test_positions_history_grouped_by_snapshot():
    client = _build_client()
    r = client.get(f"/wallet/{WALLET}/positions/history?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["wallet"] == WALLET
    assert body["snapshot_count"] == 3
    # Each snapshot has 2 positions in our seed
    for snap in body["snapshots"]:
        assert len(snap["positions"]) == 2
        for p in snap["positions"]:
            assert "condition_id" in p
            assert p["money_in_usd"] > 0


# ─────────────────────────────────────────────
# /leaderboard/historical
# ─────────────────────────────────────────────
def test_leaderboard_historical_latest():
    client = _build_client()
    r = client.get("/leaderboard/historical?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert body["entries"][0]["rank"] == 1


def test_leaderboard_historical_at_date():
    client = _build_client()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.get(f"/leaderboard/historical?date={yesterday}&limit=2")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["snapshot_at"] is not None


def test_leaderboard_historical_bad_date():
    client = _build_client()
    r = client.get("/leaderboard/historical?date=garbage")
    assert r.status_code == 400


# ─────────────────────────────────────────────
# /snapshots/latest
# ─────────────────────────────────────────────
def test_snapshots_latest_health_probe():
    client = _build_client()
    r = client.get("/snapshots/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["latest_snapshot_at"] is not None
    assert body["leaderboard_size"] == 5
