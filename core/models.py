"""
API response models.

Pydantic schemas that define the public contract. Stable field names
matter because agents will hard-code against them.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class HoldingPeriod(BaseModel):
    median_seconds: float
    p10_seconds: float
    p90_seconds: float
    sample_size: int


class CategoryBreakdown(BaseModel):
    dominant: str
    dominant_share: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float]


class WalletSignals(BaseModel):
    focus_ratio: float = Field(description="Total trades ÷ unique markets. >12 → bot.")
    holding_period: HoldingPeriod
    timing_cv: float = Field(description="Coefficient of variation of inter-trade gaps. <0.3 → scheduled.")
    timing_sample_size: int
    category: CategoryBreakdown
    win_rate: float = Field(ge=0.0, le=1.0)
    pnl_top1_share: float = Field(ge=0.0, le=1.0, description="Share of winning PnL from top 1 bet.")
    pnl_top3_share: float = Field(ge=0.0, le=1.0)


class WalletPerformance(BaseModel):
    leaderboard_pnl_usd: Optional[float]
    net_realised_pnl_usd: float
    wins: int
    losses: int
    exited_early: int
    open_positions_count: int
    avg_win_usd: float
    avg_loss_usd: float
    active_days: int
    trades_per_day: float
    avg_bet_usd: float
    max_bet_usd: float
    total_buy_volume_usd: float
    first_trade_iso: str
    last_trade_iso: str


class OpenPosition(BaseModel):
    condition_id: str
    title: str
    outcome: str
    category: str
    money_in_usd: float
    avg_entry_price: float
    first_trade_iso: str
    last_trade_iso: str
    num_buys: int


class Metadata(BaseModel):
    records_analyzed: dict[str, int]
    scoring_version: str = "1.0.0"
    cache_hit: bool = False
    fetched_at_iso: Optional[str] = None


class WalletScoreResponse(BaseModel):
    """The canonical response for /wallet/{address}."""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "wallet": "0xf1528f12e645462c344799b62b1b421a6a4c64aa",
            "username": "phonesculptor",
            "classification": "human",
            "confidence": 0.85,
            "edge_score": 7.8,
            "reason_codes": ["normal_market_diversity", "long_holds_median_18.4h"],
        }
    })

    wallet: str
    username: Optional[str] = None
    classification: str = Field(description="One of: human | bot | insufficient_data")
    confidence: float = Field(ge=0.0, le=1.0)
    edge_score: Optional[float] = Field(
        default=None, ge=0.0, le=10.0,
        description="0–10 score for humans only. Null for bots / insufficient_data.",
    )
    reason_codes: list[str]
    signals: Optional[WalletSignals] = None
    performance: Optional[WalletPerformance] = None
    open_positions: list[OpenPosition] = []
    metadata: Metadata


class LeaderboardEntry(BaseModel):
    username: str
    wallet: str
    pnl_usd: float
    volume_usd: Optional[float] = None


class VerifiedLeaderboardResponse(BaseModel):
    """Leaderboard filtered to wallets that passed human classification."""
    count: int
    fetched_at_iso: str
    entries: list[LeaderboardEntry]


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
