"""
Wallet scoring engine.

Combines signals from `signals.py` into:
  - classification:   "human" | "bot" | "insufficient_data"
  - confidence:       0.0–1.0
  - edge_score:       0.0–10.0  (only for humans; null otherwise)
  - reason_codes:     list[str]  (human-readable why)

Tunables for the classifier are kept at the top of the file so they can
be tweaked from a config later without touching the logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import pandas as pd

from . import signals


# ─────────────────────────────────────────────
# Thresholds — tune here, not in the function bodies
# ─────────────────────────────────────────────
# Hard bot triggers (any one fires → bot)
FOCUS_RATIO_BOT = 12.0           # >12 trades per market = bot (Hubble Research)
HOLDING_HFT_SECONDS = 60.0       # median <60s holding = HFT
TIMING_CV_BOT = 0.3              # very regular intervals
TIMING_CV_MIN_SAMPLE = 100

# Soft bot signals (combine to bot if multiple fire)
HOLDING_MM_SECONDS = 600.0       # market-maker-like 1-10 min holds
CRYPTO_DOMINANT_SHARE = 0.85
TRADES_PER_DAY_HEAVY = 200.0

# Minimum data to score
MIN_TRADES_FOR_SCORE = 20


# ─────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────
@dataclass
class ScoreResult:
    wallet: str
    username: Optional[str] = None
    classification: str = "insufficient_data"
    confidence: float = 0.0
    edge_score: Optional[float] = None
    reason_codes: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    performance: dict = field(default_factory=dict)
    open_positions: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────
def score_wallet(
    activity: pd.DataFrame,
    wallet: str,
    username: Optional[str] = None,
    leaderboard_pnl: Optional[float] = None,
) -> ScoreResult:
    """
    Score a single wallet given its TRADE+REDEEM activity DataFrame.
    """
    result = ScoreResult(wallet=wallet, username=username)

    trades = activity[activity["type"] == "TRADE"] if "type" in activity.columns else activity
    n_trades = int(len(trades)) if not trades.empty else 0

    if n_trades < MIN_TRADES_FOR_SCORE:
        result.classification = "insufficient_data"
        result.reason_codes.append(f"only_{n_trades}_trades")
        result.metadata = {"records_analyzed": _record_counts(activity)}
        return result

    # Compute signals once
    sig = _compute_all_signals(activity)
    market_pnl = signals.reconstruct_market_pnl(activity)
    wl = signals.win_rate_stats(market_pnl)
    conc = signals.pnl_concentration(market_pnl)
    tempo = signals.activity_tempo(activity)
    open_pos = signals.open_positions(market_pnl)

    # Classify human vs bot
    classification, confidence, codes = _classify(sig, tempo)
    result.classification = classification
    result.confidence = confidence
    result.reason_codes = codes

    # Edge score for humans only
    if classification == "human":
        result.edge_score = _edge_score(wl, conc, sig, tempo)

    # Pack everything
    result.signals = {
        "focus_ratio": round(sig["focus_ratio"], 2),
        "holding_period": {
            "median_seconds": round(sig["holding"]["median_seconds"], 1),
            "p10_seconds": round(sig["holding"]["p10_seconds"], 1),
            "p90_seconds": round(sig["holding"]["p90_seconds"], 1),
            "sample_size": sig["holding"]["sample_size"],
        },
        "timing_cv": round(sig["timing"]["cv"], 3),
        "timing_sample_size": sig["timing"]["sample_size"],
        "category": {
            "dominant": sig["category"]["dominant"],
            "dominant_share": round(sig["category"]["dominant_share"], 3),
            "breakdown": {k: round(v, 3) for k, v in sig["category"]["breakdown"].items()},
        },
        "win_rate": round(wl["win_rate"], 3),
        "pnl_top1_share": round(conc["top1_share"], 3),
        "pnl_top3_share": round(conc["top3_share"], 3),
    }
    result.performance = {
        "leaderboard_pnl_usd": round(leaderboard_pnl, 0) if leaderboard_pnl is not None else None,
        "net_realised_pnl_usd": round(wl["net_realised_pnl"], 0),
        "wins": wl["wins"],
        "losses": wl["losses"],
        "exited_early": wl["exited"],
        "open_positions_count": wl["open"],
        "avg_win_usd": round(wl["avg_win"], 0),
        "avg_loss_usd": round(wl["avg_loss"], 0),
        "active_days": tempo["active_days"],
        "trades_per_day": round(tempo["trades_per_day"], 2),
        "avg_bet_usd": round(tempo["avg_bet_usd"], 0),
        "max_bet_usd": round(tempo["max_bet_usd"], 0),
        "total_buy_volume_usd": round(tempo["total_buy_volume_usd"], 0),
        "first_trade_iso": tempo["first_trade_iso"],
        "last_trade_iso": tempo["last_trade_iso"],
    }
    result.open_positions = open_pos
    result.metadata = {
        "records_analyzed": _record_counts(activity),
        "scoring_version": "1.0.0",
    }
    return result


# ─────────────────────────────────────────────
# Internal: compute every signal once
# ─────────────────────────────────────────────
def _compute_all_signals(activity: pd.DataFrame) -> dict:
    return {
        "focus_ratio": signals.focus_ratio(activity),
        "holding": signals.holding_period_stats(activity),
        "timing": signals.timing_regularity(activity),
        "category": signals.category_concentration(activity),
    }


def _record_counts(activity: pd.DataFrame) -> dict:
    if activity.empty or "type" not in activity.columns:
        return {"trades": 0, "redeems": 0}
    return {
        "trades": int((activity["type"] == "TRADE").sum()),
        "redeems": int((activity["type"] == "REDEEM").sum()),
    }


# ─────────────────────────────────────────────
# Classifier
# ─────────────────────────────────────────────
def _classify(sig: dict, tempo: dict) -> tuple[str, float, list[str]]:
    """
    Returns (classification, confidence, reason_codes).
    """
    codes: list[str] = []
    bot_score = 0.0   # accumulated evidence for bot
    human_score = 0.0

    holding_med = sig["holding"]["median_seconds"]
    avg_bet = tempo.get("avg_bet_usd", 0)

    # ── Hard bot triggers
    # focus_ratio: # of trades / # of distinct markets. Bots concentrate.
    # BUT: humans placing many small fills on one event also look concentrated.
    # If holds are long (>1h), the wallet can't be an HFT bot — soften penalty.
    if sig["focus_ratio"] > FOCUS_RATIO_BOT:
        if holding_med >= 3600:
            bot_score += 0.2
            codes.append(
                f"focus_ratio_{sig['focus_ratio']:.1f}_high_but_holds_{holding_med/3600:.1f}h"
            )
        else:
            bot_score += 0.6
            codes.append(f"focus_ratio_{sig['focus_ratio']:.1f}_above_{FOCUS_RATIO_BOT}")

    if 0 < holding_med < HOLDING_HFT_SECONDS:
        bot_score += 0.6
        codes.append(f"hft_holding_{holding_med:.0f}s")

    if (
        sig["timing"]["cv"] > 0
        and sig["timing"]["cv"] < TIMING_CV_BOT
        and sig["timing"]["sample_size"] >= TIMING_CV_MIN_SAMPLE
    ):
        bot_score += 0.5
        codes.append(f"timing_cv_{sig['timing']['cv']:.2f}_scheduled")

    # ── Soft signals
    if (
        HOLDING_HFT_SECONDS <= holding_med < HOLDING_MM_SECONDS
        and sig["category"]["dominant"] == "Crypto"
        and sig["category"]["dominant_share"] > CRYPTO_DOMINANT_SHARE
    ):
        bot_score += 0.4
        codes.append("crypto_market_maker_pattern")

    if tempo["trades_per_day"] > TRADES_PER_DAY_HEAVY:
        if avg_bet >= 250:
            bot_score += 0.1
            codes.append(
                f"high_velocity_{tempo['trades_per_day']:.0f}_per_day_but_avg_bet_${avg_bet:.0f}"
            )
        else:
            bot_score += 0.3
            codes.append(f"high_velocity_{tempo['trades_per_day']:.0f}_trades_per_day")

    # ── Human-positive signals
    if 1.0 <= sig["focus_ratio"] <= 8.0:
        human_score += 0.3
        codes.append("normal_market_diversity")

    if holding_med >= 3600:
        human_score += 0.3
        codes.append(f"long_holds_median_{holding_med/3600:.1f}h")

    if sig["timing"]["cv"] >= 1.5:
        human_score += 0.2
        codes.append("bursty_irregular_timing")

    if sig["category"]["dominant"] != "Crypto" and sig["category"]["dominant_share"] < 0.95:
        human_score += 0.1

    # Mid-to-large bets held for hours: classic human grinder placing
    # real money on sports/news events, fragmenting across many fills.
    # Tuned: $500 threshold (not $250) and +0.25 weight (not +0.4) so
    # this signal doesn't over-promote borderline cases.
    if avg_bet >= 500 and holding_med >= 3600:
        human_score += 0.25
        codes.append(f"meaningful_bets_${avg_bet:.0f}_with_long_holds")

    # Decide
    if bot_score >= 0.5 and bot_score > human_score:
        confidence = min(0.5 + bot_score / 2, 0.99)
        return "bot", round(confidence, 2), codes
    if human_score > bot_score:
        confidence = min(0.5 + human_score, 0.95)
        return "human", round(confidence, 2), codes
    return "human", 0.5, codes  # default lean human if ambiguous



# ─────────────────────────────────────────────
# Edge score (0–10) — only for humans
#
# Key principle: profitability is a hard gate. A wallet bleeding money
# should never score above ~2 even if its wins look distributed (this is
# the neutralwave23 failure mode — 40k tiny wins masking $375k of losses).
# ─────────────────────────────────────────────
def _edge_score(wl: dict, conc: dict, sig: dict, tempo: dict) -> float:
    net_pnl = wl["net_realised_pnl"]

    # ── Hard gate: must be net profitable
    if net_pnl <= 0:
        # Pity score: reflects win rate but capped at 2.0
        return round(min(wl["win_rate"] * 2.5, 2.0), 1)

    # ── Hard gate: need enough resolved markets to trust
    if conc["winning_markets"] < 10:
        return round(min(wl["win_rate"] * 3.0, 3.0), 1)

    # 35% profitability magnitude — log-scaled, $1k = 1.0, $100k = 2.0
    import math
    pnl_pts = min(math.log10(max(net_pnl, 1)) / 5.0, 1.0) * 3.5

    # 25% win rate (cap at 70%)
    wr_pts = min(wl["win_rate"], 0.70) / 0.70 * 2.5

    # 15% PnL distribution
    dist = max(0.0, 1.0 - conc["top1_share"])
    dist_pts = dist * 1.5

    # 15% sample size — winning markets, capped at 50
    sample = min(conc["winning_markets"], 50) / 50
    sample_pts = sample * 1.5

    # 10% risk:reward — avg_win / |avg_loss|, capped at 3x
    if wl["avg_loss"] < 0:
        rr = abs(wl["avg_win"] / wl["avg_loss"])
        rr_pts = min(rr / 3.0, 1.0) * 1.0
    else:
        rr_pts = 1.0  # never lost? give full credit

    total = pnl_pts + wr_pts + dist_pts + sample_pts + rr_pts
    return round(min(total, 10.0), 1)
