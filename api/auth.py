"""
API-key authentication and rate limiting.

Designed to be optional in v1 — the API works without a key (free tier
with strict rate limit) and works better with one (higher limit, future
paid features).

How it works:
- A request without `X-API-Key` is treated as anonymous (free tier limits).
- A request with a valid key gets the tier configured for that key.
- Invalid keys are rejected with 401.

Storage: keys live in env vars in v1 (see API_KEYS env). When you wire
up Supabase for self-service signup, swap _load_keys_from_env() for a
DB-backed loader. Interface stays the same.

Tier definitions:
- "free"     : 30 req/min, no API key required
- "starter"  : 600 req/min, API key required
- "pro"      : 6000 req/min, API key required
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request


# ─────────────────────────────────────────────
# Tier definitions
# ─────────────────────────────────────────────
@dataclass(frozen=True)
class Tier:
    name: str
    requests_per_minute: int
    daily_call_quota: Optional[int] = None  # None = unlimited within rate


TIERS: dict[str, Tier] = {
    "free":    Tier("free", requests_per_minute=30, daily_call_quota=500),
    "starter": Tier("starter", requests_per_minute=600, daily_call_quota=50_000),
    "pro":     Tier("pro", requests_per_minute=6_000, daily_call_quota=None),
}


# ─────────────────────────────────────────────
# Key registry — env-backed for v1
# ─────────────────────────────────────────────
def _load_keys_from_env() -> dict[str, str]:
    """
    Parse API_KEYS env var. Format: "key1:tier,key2:tier,..."

    Example:
        API_KEYS="pmi_abc123:starter,pmi_xyz789:pro"

    Returns: {api_key: tier_name}
    """
    raw = os.environ.get("API_KEYS", "")
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            continue
        key, tier = entry.split(":", 1)
        key = key.strip()
        tier = tier.strip()
        if tier in TIERS:
            keys[key] = tier
    return keys


_API_KEYS: dict[str, str] = _load_keys_from_env()


# ─────────────────────────────────────────────
# Rate limiter — sliding-window per identity
# ─────────────────────────────────────────────
class _SlidingWindow:
    """
    Simple in-process sliding-window rate limiter.

    For multi-worker production, swap for Redis with `INCR + EXPIRE` or
    `ZADD + ZREMRANGEBYSCORE`. The interface is just `allow(identity, limit)`.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, identity: str, requests_per_minute: int) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds_if_denied)."""
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            bucket = self._buckets[identity]
            # Drop entries older than the window
            while bucket and bucket[0] < window_start:
                bucket.popleft()

            if len(bucket) >= requests_per_minute:
                retry_after = max(1, int(bucket[0] + 60 - now))
                return False, retry_after

            bucket.append(now)
            return True, 0


_limiter = _SlidingWindow()


# ─────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────
def auth_dependency(request: Request) -> dict:
    """
    Inspect the request, identify the caller (API key or anonymous IP),
    enforce rate limit, and return a context dict the handler can read.

    Returns:
        {
            "tier": str,
            "identity": str,        # api key or anon:<ip>
            "is_authenticated": bool,
        }

    Raises HTTPException 401 if an invalid key is provided.
    Raises HTTPException 429 if the rate limit is exceeded.
    """
    api_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
    is_authenticated = False
    tier_name = "free"
    identity: str

    if api_key:
        if api_key not in _API_KEYS:
            raise HTTPException(401, detail="Invalid API key.")
        tier_name = _API_KEYS[api_key]
        identity = f"key:{api_key[:8]}…"  # use a prefix so logs aren't full keys
        is_authenticated = True
    else:
        # Anonymous: identify by client IP
        ip = request.client.host if request.client else "unknown"
        identity = f"anon:{ip}"

    tier = TIERS[tier_name]
    allowed, retry_after = _limiter.allow(identity, tier.requests_per_minute)
    if not allowed:
        raise HTTPException(
            429,
            detail=f"Rate limit exceeded. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    return {"tier": tier_name, "identity": identity, "is_authenticated": is_authenticated}


# ─────────────────────────────────────────────
# Helpers for tests + admin
# ─────────────────────────────────────────────
def register_test_key(key: str, tier: str = "starter") -> None:
    """Register a key in-memory. For tests only."""
    if tier not in TIERS:
        raise ValueError(f"Unknown tier: {tier}")
    _API_KEYS[key] = tier


def reset_for_tests() -> None:
    """Clear all registered keys and rate-limit state. Tests only."""
    _API_KEYS.clear()
    _API_KEYS.update(_load_keys_from_env())
    with _limiter._lock:
        _limiter._buckets.clear()
