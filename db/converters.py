"""
Convert between ScoreResult (in-memory scoring output) and the storage
records the repository persists.

Lives in db/ rather than core/ to avoid a circular import — core
should not know about persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core.scorer import ScoreResult

from .records import LeaderboardEntryRecord, PositionSnapshot, ScoreRecord


def score_to_record(score: ScoreResult, scored_at: Optional[datetime] = None) -> ScoreRecord:
    scored_at = scored_at or datetime.now(timezone.utc)
    perf = score.performance or {}
    return ScoreRecord(
        wallet=score.wallet.lower(),
        scored_at=scored_at,
        classification=score.classification,
        confidence=score.confidence,
        edge_score=score.edge_score,
        reason_codes=list(score.reason_codes),
        signals=score.signals or {},
        performance=perf,
        leaderboard_pnl_usd=perf.get("leaderboard_pnl_usd"),
        scoring_version=score.metadata.get("scoring_version", "1.0.0"),
    )


def positions_to_snapshots(
    score: ScoreResult, snapshot_at: Optional[datetime] = None
) -> list[PositionSnapshot]:
    snapshot_at = snapshot_at or datetime.now(timezone.utc)
    out: list[PositionSnapshot] = []
    for p in score.open_positions:
        out.append(PositionSnapshot(
            wallet=score.wallet.lower(),
            snapshot_at=snapshot_at,
            condition_id=str(p["condition_id"]),
            title=str(p["title"]),
            outcome=p.get("outcome") or None,
            category=p.get("category") or None,
            money_in_usd=float(p["money_in_usd"]),
            avg_entry_price=(
                float(p["avg_entry_price"]) if p.get("avg_entry_price") is not None else None
            ),
            num_buys=int(p["num_buys"]) if p.get("num_buys") is not None else None,
            first_trade_at=_safe_iso_to_dt(p.get("first_trade_iso")),
            last_trade_at=_safe_iso_to_dt(p.get("last_trade_iso")),
        ))
    return out


def leaderboard_to_records(
    leaderboard_df, snapshot_at: Optional[datetime] = None,
) -> list[LeaderboardEntryRecord]:
    snapshot_at = snapshot_at or datetime.now(timezone.utc)
    out: list[LeaderboardEntryRecord] = []
    for rank, (_, row) in enumerate(leaderboard_df.iterrows(), start=1):
        wallet = row.get("proxyWallet")
        if not wallet:
            continue
        out.append(LeaderboardEntryRecord(
            snapshot_at=snapshot_at,
            rank=rank,
            wallet=str(wallet).lower(),
            username=str(row.get("userName")) if row.get("userName") else None,
            pnl_usd=float(row.get("pnl") or 0),
            volume_usd=(
                float(row.get("vol")) if row.get("vol") is not None else None
            ),
        ))
    return out


def _safe_iso_to_dt(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
