"""
Signal calculators.

Each function is a pure transform: takes a normalized activity DataFrame
(from PolymarketClient.get_activity), returns a single number or dict.

This module is the core IP. The scoring engine combines these.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────
# Market categorisation
# ─────────────────────────────────────────────
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Politics": [
        "election", "president", "senate", "congress", "vote", "trump", "biden",
        "harris", "democrat", "republican", "minister", "government", "political",
    ],
    "Crypto": [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi", "solana",
        "coinbase", "binance", "nft", "blockchain",
    ],
    "Sports": [
        "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball",
        "tennis", "golf", "ufc", "mma", "world cup", "olympics", "championship",
        "spread", "nuggets", "lakers", "celtics", "knicks", "oilers", "flyers",
        "timberwolves", "hawks", "thunder", "76ers", "psg", "bayern", "fc ",
        "win on", "vs.",
    ],
    "Economics": [
        "fed", "interest rate", "inflation", "gdp", "recession", "cpi", "jobs",
        "unemployment", "treasury", "economy", "tariff",
    ],
    "Science/Tech": [
        "ai", "artificial intelligence", "spacex", "nasa", "climate", "temperature",
        "hurricane", "earthquake", "pandemic", "covid",
    ],
    "Pop Culture": [
        "oscar", "grammy", "award", "celebrity", "movie", "album", "taylor swift",
        "kardashian", "youtube", "tiktok",
    ],
}


def categorise(title: object) -> str:
    if not isinstance(title, str):
        return "Other"
    t = title.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    return "Other"


# ─────────────────────────────────────────────
# Helpers — split activity types
# ─────────────────────────────────────────────
def _trades(activity: pd.DataFrame) -> pd.DataFrame:
    if activity.empty or "type" not in activity.columns:
        return activity
    return activity[activity["type"] == "TRADE"].copy()


def _redeems(activity: pd.DataFrame) -> pd.DataFrame:
    if activity.empty or "type" not in activity.columns:
        return activity.iloc[0:0].copy()
    return activity[activity["type"] == "REDEEM"].copy()


def _buys(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "side" not in trades.columns:
        return trades
    return trades[trades["side"] == "BUY"].copy()


def _sells(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "side" not in trades.columns:
        return trades.iloc[0:0].copy()
    return trades[trades["side"] == "SELL"].copy()


def _size_col(df: pd.DataFrame) -> str:
    return "usdcSize" if "usdcSize" in df.columns else "size"


# ─────────────────────────────────────────────
# 1. Focus ratio — trades per unique market
# ─────────────────────────────────────────────
def focus_ratio(activity: pd.DataFrame) -> float:
    """
    trades / unique markets.

    Bots concentrate huge trade volume on a tiny number of markets
    (e.g. 50,000 trades on 200 BTC up/down markets → ratio 250).
    Humans typically sit between 2 and 10.
    """
    trades = _trades(activity)
    if trades.empty or "conditionId" not in trades.columns:
        return 0.0
    unique_markets = trades["conditionId"].nunique()
    if unique_markets == 0:
        return 0.0
    return float(len(trades) / unique_markets)


# ─────────────────────────────────────────────
# 2. Holding period — median seconds per market position
# ─────────────────────────────────────────────
def holding_period_stats(activity: pd.DataFrame) -> dict[str, float]:
    """
    For each (conditionId, outcome) pair that has both an entry and exit,
    compute holding duration in seconds.

    Entry = first BUY timestamp.
    Exit  = first SELL or REDEEM timestamp after entry.

    Returns median + percentiles. HFT bots sit under 60s, market-maker bots
    under 600s, humans typically above 3,600s.
    """
    if activity.empty or "conditionId" not in activity.columns:
        return _empty_holding_stats()

    trades = _trades(activity)
    redeems = _redeems(activity)

    if trades.empty:
        return _empty_holding_stats()

    buys = _buys(trades)
    sells = _sells(trades)

    if buys.empty:
        return _empty_holding_stats()

    group_cols = ["conditionId"]
    if "outcome" in buys.columns:
        group_cols.append("outcome")

    entries = buys.groupby(group_cols)["timestamp"].min().rename("entry_ts")

    exit_frames = []
    if not sells.empty:
        sell_exit = sells.groupby(group_cols)["timestamp"].min().rename("exit_ts")
        exit_frames.append(sell_exit)
    if not redeems.empty and all(c in redeems.columns for c in group_cols):
        redeem_exit = redeems.groupby(group_cols)["timestamp"].min().rename("exit_ts")
        exit_frames.append(redeem_exit)

    if not exit_frames:
        return _empty_holding_stats()

    exits = pd.concat(exit_frames).groupby(level=list(range(len(group_cols)))).min()

    merged = pd.concat([entries, exits], axis=1).dropna()
    if merged.empty:
        return _empty_holding_stats()

    durations = (merged["exit_ts"] - merged["entry_ts"]).dt.total_seconds()
    durations = durations[durations >= 0]
    if durations.empty:
        return _empty_holding_stats()

    return {
        "median_seconds": float(durations.median()),
        "p10_seconds": float(durations.quantile(0.10)),
        "p90_seconds": float(durations.quantile(0.90)),
        "sample_size": int(len(durations)),
    }


def _empty_holding_stats() -> dict[str, float]:
    return {"median_seconds": 0.0, "p10_seconds": 0.0, "p90_seconds": 0.0, "sample_size": 0}


# ─────────────────────────────────────────────
# 3. Trade timing regularity — coefficient of variation
# ─────────────────────────────────────────────
def timing_regularity(activity: pd.DataFrame, min_trades: int = 20) -> dict[str, float]:
    """
    Compute coefficient of variation (std/mean) of inter-trade intervals.

    Bots that fire on a fixed schedule (every 5 min, every 1 hour) have
    very low CV. Humans have bursty, irregular trading with high CV.

    Rule of thumb: CV < 0.5  →  scheduled (bot)
                  CV  > 1.5  →  bursty   (human)
    """
    trades = _trades(activity)
    if trades.empty or "timestamp" not in trades.columns or len(trades) < min_trades:
        return {"cv": 0.0, "sample_size": 0, "median_gap_seconds": 0.0}

    ts = trades.sort_values("timestamp")["timestamp"]
    deltas = ts.diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]

    if len(deltas) < min_trades or deltas.mean() == 0:
        return {"cv": 0.0, "sample_size": int(len(deltas)), "median_gap_seconds": 0.0}

    cv = float(deltas.std() / deltas.mean())
    return {
        "cv": cv,
        "sample_size": int(len(deltas)),
        "median_gap_seconds": float(deltas.median()),
    }


# ─────────────────────────────────────────────
# 4. Category concentration
# ─────────────────────────────────────────────
def category_concentration(activity: pd.DataFrame) -> dict[str, object]:
    trades = _trades(activity)
    if trades.empty or "title" not in trades.columns:
        return {"dominant": "Unknown", "dominant_share": 0.0, "breakdown": {}}

    cats = trades["title"].apply(categorise)
    if cats.empty:
        return {"dominant": "Unknown", "dominant_share": 0.0, "breakdown": {}}

    counts = cats.value_counts(normalize=True)
    return {
        "dominant": str(counts.index[0]),
        "dominant_share": float(counts.iloc[0]),
        "breakdown": {str(k): float(v) for k, v in counts.items()},
    }


# ─────────────────────────────────────────────
# 5. Per-market PnL reconstruction
# ─────────────────────────────────────────────
def reconstruct_market_pnl(
    activity: pd.DataFrame, days_open_cutoff: int = 7
) -> pd.DataFrame:
    """
    Returns one row per conditionId with reconstructed PnL.

    Columns:
        conditionId, title, outcome, category,
        money_in, sell_proceeds, redeem_amount, money_out, pnl,
        first_trade, last_trade, status

    status ∈ {"won", "exited", "lost", "open"}
    """
    if activity.empty or "conditionId" not in activity.columns:
        return pd.DataFrame()

    size_col = _size_col(activity)
    trades = _trades(activity)
    redeems = _redeems(activity)
    buys = _buys(trades)
    sells = _sells(trades)

    if buys.empty:
        return pd.DataFrame()

    agg = (
        buys.groupby("conditionId")
        .agg(
            title=("title", "first") if "title" in buys.columns else ("conditionId", "first"),
            outcome=("outcome", "first") if "outcome" in buys.columns else ("conditionId", "first"),
            money_in=(size_col, "sum"),
            avg_price=("price", "mean") if "price" in buys.columns else ("conditionId", "size"),
            first_trade=("timestamp", "min"),
            last_trade=("timestamp", "max"),
            num_buys=("conditionId", "count"),
        )
        .reset_index()
    )

    if not sells.empty:
        sell_proceeds = (
            sells.groupby("conditionId")[size_col]
            .sum()
            .reset_index()
            .rename(columns={size_col: "sell_proceeds"})
        )
    else:
        sell_proceeds = pd.DataFrame(columns=["conditionId", "sell_proceeds"])

    if not redeems.empty and "conditionId" in redeems.columns:
        redeem_amount = (
            redeems.groupby("conditionId")[size_col]
            .sum()
            .reset_index()
            .rename(columns={size_col: "redeem_amount"})
        )
    else:
        redeem_amount = pd.DataFrame(columns=["conditionId", "redeem_amount"])

    agg = agg.merge(sell_proceeds, on="conditionId", how="left")
    agg = agg.merge(redeem_amount, on="conditionId", how="left")
    agg["sell_proceeds"] = pd.to_numeric(agg["sell_proceeds"], errors="coerce").fillna(0.0)
    agg["redeem_amount"] = pd.to_numeric(agg["redeem_amount"], errors="coerce").fillna(0.0)
    agg["money_in"] = pd.to_numeric(agg["money_in"], errors="coerce").fillna(0.0)
    agg["money_out"] = agg["sell_proceeds"] + agg["redeem_amount"]
    agg["pnl"] = agg["money_out"] - agg["money_in"]
    agg["category"] = agg["title"].apply(categorise)

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_open_cutoff)
    has_redeem = agg["redeem_amount"] > 0
    has_sell = agg["sell_proceeds"] > 0
    is_recent = agg["last_trade"] >= cutoff

    agg["status"] = np.select(
        [has_redeem, ~has_redeem & has_sell, ~has_redeem & ~has_sell & ~is_recent],
        ["won",      "exited",                "lost"],
        default="open",
    )
    return agg


# ─────────────────────────────────────────────
# 6. Win-rate stats
# ─────────────────────────────────────────────
def win_rate_stats(market_pnl: pd.DataFrame) -> dict[str, float]:
    if market_pnl.empty:
        return {"win_rate": 0.0, "wins": 0, "losses": 0, "exited": 0, "open": 0,
                "avg_win": 0.0, "avg_loss": 0.0, "net_realised_pnl": 0.0}

    won = market_pnl[market_pnl["status"] == "won"]
    lost = market_pnl[market_pnl["status"] == "lost"]
    exited = market_pnl[market_pnl["status"] == "exited"]
    open_ = market_pnl[market_pnl["status"] == "open"]

    resolved = len(won) + len(lost)
    win_rate = float(len(won) / resolved) if resolved > 0 else 0.0

    closed = market_pnl[market_pnl["status"].isin(["won", "lost", "exited"])]

    return {
        "win_rate": win_rate,
        "wins": int(len(won)),
        "losses": int(len(lost)),
        "exited": int(len(exited)),
        "open": int(len(open_)),
        "avg_win": float(won["pnl"].mean()) if not won.empty else 0.0,
        "avg_loss": float(lost["pnl"].mean()) if not lost.empty else 0.0,
        "net_realised_pnl": float(closed["pnl"].sum()) if not closed.empty else 0.0,
    }


# ─────────────────────────────────────────────
# 7. PnL concentration (top 1 / top 3 share of winning PnL)
# ─────────────────────────────────────────────
def pnl_concentration(market_pnl: pd.DataFrame) -> dict[str, float]:
    if market_pnl.empty:
        return {"top1_share": 0.0, "top3_share": 0.0, "winning_markets": 0}

    won = market_pnl[market_pnl["status"] == "won"]
    if won.empty or won["pnl"].sum() <= 0:
        return {"top1_share": 0.0, "top3_share": 0.0, "winning_markets": int(len(won))}

    total = won["pnl"].sum()
    top1 = won.nlargest(1, "pnl")["pnl"].sum() / total
    top3 = won.nlargest(3, "pnl")["pnl"].sum() / total

    return {
        "top1_share": float(top1),
        "top3_share": float(top3),
        "winning_markets": int(len(won)),
    }


# ─────────────────────────────────────────────
# 8. Activity tempo — bets per day, account age
# ─────────────────────────────────────────────
def activity_tempo(activity: pd.DataFrame) -> dict[str, float]:
    trades = _trades(activity)
    if trades.empty or "timestamp" not in trades.columns:
        return {"trades_per_day": 0.0, "active_days": 0, "first_trade_iso": "",
                "last_trade_iso": "", "avg_bet_usd": 0.0, "max_bet_usd": 0.0,
                "total_buy_volume_usd": 0.0}

    size_col = _size_col(trades)
    buys = _buys(trades)

    first = trades["timestamp"].min()
    last = trades["timestamp"].max()
    span_days = max(1, (last - first).days)

    return {
        "trades_per_day": float(len(trades) / span_days),
        "active_days": int(span_days),
        "first_trade_iso": first.isoformat(),
        "last_trade_iso": last.isoformat(),
        "avg_bet_usd": float(buys[size_col].mean()) if not buys.empty else 0.0,
        "max_bet_usd": float(buys[size_col].max()) if not buys.empty else 0.0,
        "total_buy_volume_usd": float(buys[size_col].sum()) if not buys.empty else 0.0,
    }


# ─────────────────────────────────────────────
# 9. Open positions snapshot — what AI agents actually want
# ─────────────────────────────────────────────
def open_positions(
    market_pnl: pd.DataFrame, top_n: Optional[int] = 25
) -> list[dict[str, object]]:
    if market_pnl.empty:
        return []
    open_ = market_pnl[market_pnl["status"] == "open"].copy()
    if open_.empty:
        return []
    open_ = open_.sort_values("money_in", ascending=False)
    if top_n:
        open_ = open_.head(top_n)
    return [
        {
            "condition_id": str(r["conditionId"]),
            "title": str(r["title"]),
            "outcome": str(r.get("outcome", "")),
            "category": str(r["category"]),
            "money_in_usd": float(r["money_in"]),
            "avg_entry_price": float(r["avg_price"]) if pd.notna(r["avg_price"]) else 0.0,
            "first_trade_iso": r["first_trade"].isoformat() if pd.notna(r["first_trade"]) else "",
            "last_trade_iso": r["last_trade"].isoformat() if pd.notna(r["last_trade"]) else "",
            "num_buys": int(r["num_buys"]),
        }
        for _, r in open_.iterrows()
    ]
