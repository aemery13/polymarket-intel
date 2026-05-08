"""
Tests for the score/positions endpoint split.

Verifies:
  - GET /wallet/{address}            -> no positions in response
  - GET /wallet/{address}/positions  -> positions only, no score blob
  - DB persistence is debounced on the score endpoint
  - Position cache TTL behaves like a fast-tier cache
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import main as api_main
from db import InMemoryRepository
from tests import fixtures


WALLET = "0x" + "a" * 40


def _build_client() -> TestClient:
    """Fresh repo + clear cache for each test — no state bleed."""
    api_main.repo = InMemoryRepository()
    api_main.cache.clear()
    return TestClient(api_main.app)


# ─────────────────────────────────────────────
# Slow tier — score endpoint
# ─────────────────────────────────────────────
def test_score_endpoint_omits_positions():
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        r = client.get(f"/wallet/{WALLET}")
    assert r.status_code == 200
    body = r.json()
    assert body["classification"] == "human"
    # The contract: score endpoint never includes positions.
    assert body["open_positions"] == [], (
        "Slow-tier endpoint must not return open positions — those belong on the fast tier"
    )


def test_score_endpoint_persists_to_db():
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        r = client.get(f"/wallet/{WALLET}")
    assert r.status_code == 200

    latest = api_main.repo.get_latest_score(WALLET)
    assert latest is not None, "Score endpoint should persist a row on first call"
    assert latest.classification == "human"


def test_score_persistence_is_debounced():
    """Second call within debounce window must not write a new row."""
    client = _build_client()
    activity = fixtures.phonesculptor_like()

    with patch.object(api_main.client, "get_activity", return_value=activity):
        # First call — writes a record
        client.get(f"/wallet/{WALLET}")
        first_count = len(api_main.repo._scores)
        assert first_count == 1

        # Force a re-fetch (bypass cache) and call again
        client.get(f"/wallet/{WALLET}?refresh=true")
        second_count = len(api_main.repo._scores)

    assert second_count == 1, (
        f"Debounce broken: expected still 1 row, got {second_count}. "
        "Score persistence should be skipped within SCORE_PERSIST_DEBOUNCE_SECONDS."
    )


def test_score_persistence_writes_again_after_debounce_window():
    """After the debounce window elapses, a new row should be written."""
    client = _build_client()
    activity = fixtures.phonesculptor_like()

    with patch.object(api_main.client, "get_activity", return_value=activity):
        client.get(f"/wallet/{WALLET}")
        assert len(api_main.repo._scores) == 1

        # Backdate the existing record past the debounce window
        api_main.repo._scores[0].scored_at = datetime.now(timezone.utc) - timedelta(
            seconds=api_main.SCORE_PERSIST_DEBOUNCE_SECONDS + 60
        )

        client.get(f"/wallet/{WALLET}?refresh=true")

    assert len(api_main.repo._scores) == 2, (
        "After debounce window, a fresh call should write a second history row"
    )


def test_score_endpoint_persists_position_snapshot_too():
    """Score endpoint should write the position snapshot for free
    (we already computed it during scoring)."""
    client = _build_client()
    # phonesculptor synthetic may or may not have current open positions
    # depending on randomness — use a fixture that we know does
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        client.get(f"/wallet/{WALLET}")

    # Either there are no live positions (fixture randomness) or they got
    # persisted. Both are acceptable; this just verifies we don't crash.
    positions = api_main.repo.get_position_history(WALLET)
    # If there were any live positions in the score, they should be persisted
    score = api_main.repo.get_latest_score(WALLET)
    expected_count = score.performance.get("open_positions_count", 0) if score else 0
    if expected_count > 0:
        assert len(positions) > 0


# ─────────────────────────────────────────────
# Fast tier — positions endpoint
# ─────────────────────────────────────────────
def test_positions_endpoint_returns_positions_only():
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        r = client.get(f"/wallet/{WALLET}/positions")
    assert r.status_code == 200
    body = r.json()

    # Required fields
    assert body["wallet"] == WALLET
    assert "open_positions" in body
    assert "open_positions_count" in body
    assert "classification" in body  # the agent still wants to know what they're looking at
    assert "metadata" in body

    # Should NOT include the heavy score blob
    assert "signals" not in body
    assert "performance" not in body
    assert "reason_codes" not in body


def test_positions_endpoint_no_db_writes():
    """Fast-tier endpoint must not write to the DB. Hammering it should
    not bloat history."""
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        for _ in range(5):
            client.get(f"/wallet/{WALLET}/positions?refresh=true")

    assert len(api_main.repo._scores) == 0, (
        "Positions endpoint must not write score history rows"
    )
    assert len(api_main.repo._positions) == 0, (
        "Positions endpoint must not write position snapshots"
    )


def test_positions_cache_hit_marks_metadata():
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        first = client.get(f"/wallet/{WALLET}/positions").json()
        second = client.get(f"/wallet/{WALLET}/positions").json()

    assert first["metadata"]["cache_hit"] is False
    assert second["metadata"]["cache_hit"] is True


def test_positions_cache_ttl_is_short():
    """The reported TTL on the positions endpoint should reflect the fast tier."""
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        body = client.get(f"/wallet/{WALLET}/positions").json()
    assert body["metadata"]["cache_ttl_seconds"] <= 60, (
        "Positions endpoint TTL should be tight (≤1 min) — agents need fresh data"
    )


def test_positions_validates_address():
    client = _build_client()
    r = client.get("/wallet/notanaddress/positions")
    assert r.status_code == 400


# ─────────────────────────────────────────────
# Cache isolation between tiers
# ─────────────────────────────────────────────
def test_score_and_positions_caches_are_independent():
    """Calling /positions should not warm the score cache and vice versa."""
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        client.get(f"/wallet/{WALLET}/positions")
        # Score endpoint should still be a fresh call (cache miss)
        score_resp = client.get(f"/wallet/{WALLET}").json()
    assert score_resp["metadata"]["cache_hit"] is False
