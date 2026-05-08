"""Polymarket wallet intelligence — core scoring logic."""

from .client import PolymarketClient
from .models import (
    WalletScoreResponse,
    VerifiedLeaderboardResponse,
    LeaderboardEntry,
)
from .scorer import ScoreResult, score_wallet

__version__ = "1.0.0"

__all__ = [
    "PolymarketClient",
    "WalletScoreResponse",
    "VerifiedLeaderboardResponse",
    "LeaderboardEntry",
    "ScoreResult",
    "score_wallet",
]
