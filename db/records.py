"""
Storage record types.

These are the persisted shapes — flatter than ScoreResult and friendlier
to a relational store. The repository converts to/from these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class WalletRecord:
    wallet: str
    username: Optional[str]
    first_seen_at: datetime
    last_scored_at: Optional[datetime]


@dataclass
class ScoreRecord:
    wallet: str
    scored_at: datetime
    classification: str
    confidence: float
    edge_score: Optional[float]
    reason_codes: list[str]
    signals: dict
    performance: dict
    leaderboard_pnl_usd: Optional[float]
    scoring_version: str = "1.0.0"


@dataclass
class PositionSnapshot:
    wallet: str
    snapshot_at: datetime
    condition_id: str
    title: str
    outcome: Optional[str]
    category: Optional[str]
    money_in_usd: float
    avg_entry_price: Optional[float]
    num_buys: Optional[int]
    first_trade_at: Optional[datetime]
    last_trade_at: Optional[datetime]


@dataclass
class LeaderboardEntryRecord:
    snapshot_at: datetime
    rank: int
    wallet: str
    username: Optional[str]
    pnl_usd: float
    volume_usd: Optional[float]


@dataclass
class SnapshotRun:
    started_at: datetime
    finished_at: Optional[datetime] = None
    wallets_scored: int = 0
    errors: int = 0
    notes: Optional[str] = None
    id: Optional[int] = None
