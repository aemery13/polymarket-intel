"""
In-memory TTL cache.

Production should swap this for Redis. The interface is intentionally
trivial so the swap is one file.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Optional


class TTLCache:
    def __init__(self, default_ttl_seconds: int = 3600):
        self._store: dict[str, tuple[float, Any]] = {}
        self._default_ttl = default_ttl_seconds
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        with self._lock:
            self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live = sum(1 for exp, _ in self._store.values() if exp > now)
            return {"total_keys": len(self._store), "live_keys": live}


# Module-level singleton — fine for single-process API
cache = TTLCache(default_ttl_seconds=3600)
