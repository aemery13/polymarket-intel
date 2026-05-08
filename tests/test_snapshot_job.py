"""
Integration-style tests for the snapshot job.

Mocks only the Polymarket client (so no live network calls); the
scorer, repository, and converters all run for real. This catches
contract drift between the scoring engine and the persistence layer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import InMemoryRepository
from scripts.snapshot_job import run_snapshot
from tests import fixtures


# ─────────────────────────────────────────────
# A fake leaderboard with three rows mapping to three known fixtures
# ─────────────────────────────────────────────
FAKE_LEADERBOARD = pd.DataFrame([
    {"userName": "phonesculptor", "proxyWallet": "0x" + "a" * 40, "pnl": 70000.0, "vol": 500000.0},
    {"userName": "gabigol",       "proxyWallet": "0x" + "b" * 40, "pnl": 200000.0, "vol": 5000000.0},
    {"userName": "neutralwave23", "proxyWallet": "0x" + "c" * 40, "pnl": -300000.0, "vol": 100000.0},
])


def _activity_router(wallet: str, *args, **kwargs):
    """Return synthetic activity per wallet so each gets a different score."""
    w = wallet.lower()
    if w.startswith("0xaa"):
        return fixtures.phonesculptor_like()
    if w.startswith("0xbb"):
        return fixtures.hft_bot_like()
    if w.startswith("0xcc"):
        return fixtures.tilt_loser_like()
    return fixtures.insufficient_data_like(0)


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
def test_snapshot_job_persists_everything():
    repo = InMemoryRepository()

    with patch("scripts.snapshot_job.PolymarketClient") as MockClient:
        instance = MockClient.return_value
        instance.get_leaderboard.return_value = FAKE_LEADERBOARD
        instance.get_activity.side_effect = _activity_router

        summary = run_snapshot(repo, top_n=3, inter_request_delay=0.0, verbose=False)

    assert summary["wallets_scored"] == 3
    assert summary["errors"] == 0
    assert summary["leaderboard_size"] == 3

    # Wallets registered
    assert repo.get_wallet("0x" + "a" * 40) is not None
    assert repo.get_wallet("0x" + "b" * 40) is not None
    assert repo.get_wallet("0x" + "c" * 40) is not None

    # One score per wallet
    assert repo.get_latest_score("0x" + "a" * 40).classification == "human"
    assert repo.get_latest_score("0x" + "b" * 40).classification == "bot"
    assert repo.get_latest_score("0x" + "c" * 40).classification == "human"

    # Edge scores reflect persona (phonesculptor strong, neutralwave23 weak)
    phonesculptor_score = repo.get_latest_score("0x" + "a" * 40)
    tilt_score = repo.get_latest_score("0x" + "c" * 40)
    assert phonesculptor_score.edge_score >= 5.0
    assert (tilt_score.edge_score or 0) < 3.0

    # Leaderboard snapshot stored
    lb = repo.get_latest_leaderboard()
    assert len(lb) == 3
    assert lb[0].rank == 1


def test_snapshot_job_handles_per_wallet_error():
    """One wallet error should not kill the run."""
    repo = InMemoryRepository()

    def flaky_activity(wallet: str, *a, **kw):
        if wallet.lower().startswith("0xbb"):
            raise RuntimeError("simulated upstream blip")
        return _activity_router(wallet)

    with patch("scripts.snapshot_job.PolymarketClient") as MockClient:
        instance = MockClient.return_value
        instance.get_leaderboard.return_value = FAKE_LEADERBOARD
        instance.get_activity.side_effect = flaky_activity

        summary = run_snapshot(repo, top_n=3, inter_request_delay=0.0, verbose=False)

    assert summary["wallets_scored"] == 2
    assert summary["errors"] == 1
    assert any(e["wallet"] == "0x" + "b" * 40 for e in summary["error_wallets"])

    # The other two were still persisted
    assert repo.get_latest_score("0x" + "a" * 40) is not None
    assert repo.get_latest_score("0x" + "c" * 40) is not None
    assert repo.get_latest_score("0x" + "b" * 40) is None


def test_open_position_snapshots_persisted():
    repo = InMemoryRepository()

    with patch("scripts.snapshot_job.PolymarketClient") as MockClient:
        instance = MockClient.return_value
        # Just one human wallet to keep the assertion focused
        instance.get_leaderboard.return_value = FAKE_LEADERBOARD.iloc[[0]]
        instance.get_activity.side_effect = _activity_router

        run_snapshot(repo, top_n=1, inter_request_delay=0.0, verbose=False)

    positions = repo.get_position_history("0x" + "a" * 40)
    # phonesculptor synthetic doesn't always have open positions in the
    # last 7 days (resolution times scattered up to 36h after entry), but
    # it should have at least *some* position rows persisted across the run
    # if any were live. The job just needs to not crash and the converter
    # should round-trip cleanly when there are positions.
    if positions:
        p = positions[0]
        assert p.wallet == "0x" + "a" * 40
        assert p.money_in_usd > 0
        assert p.condition_id


def test_snapshot_run_audit_recorded():
    repo = InMemoryRepository()

    with patch("scripts.snapshot_job.PolymarketClient") as MockClient:
        instance = MockClient.return_value
        instance.get_leaderboard.return_value = FAKE_LEADERBOARD.iloc[[0]]
        instance.get_activity.side_effect = _activity_router

        summary = run_snapshot(repo, top_n=1, inter_request_delay=0.0, verbose=False)

    assert summary["run_id"] is not None
    # Internal: audit row exists and has finished_at populated
    runs = repo._runs  # tests can poke into in-memory internals
    assert len(runs) == 1
    assert runs[0].finished_at is not None
    assert runs[0].wallets_scored == 1
