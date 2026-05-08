"""
db package.

`get_repository()` returns a SupabaseRepository if env is configured,
otherwise an InMemoryRepository so the API and tests still work without
any DB setup.
"""

from __future__ import annotations

import os
from typing import Optional

from .records import (
    LeaderboardEntryRecord,
    PositionSnapshot,
    ScoreRecord,
    SnapshotRun,
    WalletRecord,
)
from .repository import InMemoryRepository, Repository

__all__ = [
    "Repository",
    "InMemoryRepository",
    "WalletRecord",
    "ScoreRecord",
    "PositionSnapshot",
    "LeaderboardEntryRecord",
    "SnapshotRun",
    "get_repository",
]


_singleton: Optional[Repository] = None


def get_repository(force_in_memory: bool = False) -> Repository:
    """
    Returns the configured repository.

    - If SUPABASE_URL and SUPABASE_KEY are set and `force_in_memory` is
      false, returns a SupabaseRepository.
    - Otherwise returns a process-wide InMemoryRepository singleton.

    Why a singleton in the in-memory case: the API needs persistence
    across requests within one process. In production with Supabase,
    each call constructs a fresh client (cheap — it's just a dict-shaped
    HTTP wrapper).
    """
    global _singleton

    if not force_in_memory and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        from .supabase_repo import SupabaseRepository
        return SupabaseRepository()

    if _singleton is None:
        _singleton = InMemoryRepository()
    return _singleton
