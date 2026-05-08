"""
Tests for the API-key auth and rate-limit middleware.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from api import main as api_main
from api import auth as auth_mod
from db import InMemoryRepository
from tests import fixtures


WALLET = "0x" + "a" * 40


def _build_client() -> TestClient:
    api_main.repo = InMemoryRepository()
    api_main.cache.clear()
    auth_mod.reset_for_tests()
    return TestClient(api_main.app)


# ─────────────────────────────────────────────
# Anonymous (free tier) — works, but limited
# ─────────────────────────────────────────────
def test_anonymous_can_call_api():
    client = _build_client()
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        r = client.get(f"/wallet/{WALLET}")
    assert r.status_code == 200


def test_invalid_api_key_rejected():
    client = _build_client()
    r = client.get(f"/wallet/{WALLET}", headers={"X-API-Key": "pmi_definitely_not_real"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


def test_valid_api_key_authenticates():
    client = _build_client()
    auth_mod.register_test_key("pmi_test_key_starter", tier="starter")
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        r = client.get(f"/wallet/{WALLET}", headers={"X-API-Key": "pmi_test_key_starter"})
    assert r.status_code == 200


# ─────────────────────────────────────────────
# Rate limiting — free tier
# ─────────────────────────────────────────────
def test_free_tier_rate_limits_eventually():
    client = _build_client()
    free_limit = auth_mod.TIERS["free"].requests_per_minute

    # Health endpoint isn't behind auth, so we hit a real one
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        statuses = []
        for _ in range(free_limit + 5):
            r = client.get(f"/wallet/{WALLET}")
            statuses.append(r.status_code)

    assert 200 in statuses, "Some requests should succeed"
    assert 429 in statuses, f"Expected to hit free-tier rate limit ({free_limit}/min)"


def test_starter_tier_does_not_rate_limit_at_free_levels():
    client = _build_client()
    auth_mod.register_test_key("pmi_starter_key", tier="starter")
    free_limit = auth_mod.TIERS["free"].requests_per_minute

    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        # Make `free_limit + 5` requests as starter — none should 429
        for _ in range(free_limit + 5):
            r = client.get(f"/wallet/{WALLET}", headers={"X-API-Key": "pmi_starter_key"})
            assert r.status_code != 429


def test_429_includes_retry_after_header():
    client = _build_client()
    free_limit = auth_mod.TIERS["free"].requests_per_minute
    with patch.object(api_main.client, "get_activity", return_value=fixtures.phonesculptor_like()):
        for _ in range(free_limit + 1):
            last = client.get(f"/wallet/{WALLET}")
    assert last.status_code == 429
    assert "retry-after" in {k.lower() for k in last.headers.keys()}


# ─────────────────────────────────────────────
# Auth doesn't break unauthed health endpoints
# ─────────────────────────────────────────────
def test_root_endpoint_unauthenticated():
    client = _build_client()
    r = client.get("/")
    assert r.status_code == 200
    # Hitting it many times shouldn't 429 — it's not auth-gated
    for _ in range(200):
        r = client.get("/")
        assert r.status_code == 200


def test_snapshots_latest_unauthenticated():
    client = _build_client()
    r = client.get("/snapshots/latest")
    assert r.status_code == 200


# ─────────────────────────────────────────────
# Loading from env
# ─────────────────────────────────────────────
def test_keys_load_from_env_format():
    """Verify the env-parsing logic without messing with real env."""
    import os
    os.environ["API_KEYS"] = "pmi_a:starter,pmi_b:pro,pmi_invalid:nonexistent_tier,malformed"
    try:
        keys = auth_mod._load_keys_from_env()
    finally:
        del os.environ["API_KEYS"]

    assert keys == {"pmi_a": "starter", "pmi_b": "pro"}, (
        f"Expected only valid entries, got {keys}"
    )
