"""
Synthetic activity fixtures.

These mimic the three real wallet patterns from the brief so we can test
the classifier without hitting the live API.
"""

from __future__ import annotations

import random

import pandas as pd


def _ts(year: int = 2025, month: int = 10, day: int = 15, hour: int = 12, minute: int = 0):
    return pd.Timestamp(year=year, month=month, day=day, hour=hour, minute=minute, tz="UTC")


# ─────────────────────────────────────────────
# 1. phonesculptor pattern — human MLB sports bettor
#    1,055 trades, ~290 markets, holds 1+ hour, 72% wins, very irregular timing
# ─────────────────────────────────────────────
def phonesculptor_like(seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    base = _ts(2025, 10, 15)

    mlb_titles = [
        "Yankees vs Red Sox", "Dodgers vs Padres", "Astros vs Mariners",
        "Tigers vs Royals", "Mets vs Phillies", "Braves vs Marlins",
        "Cubs vs Cardinals", "Giants vs Rockies", "Rays vs Blue Jays",
        "Twins vs White Sox", "Brewers vs Reds", "Angels vs Athletics",
    ]

    # 290 unique markets, ~3.6 trades each on average
    for market_id in range(290):
        title = f"MLB: {rng.choice(mlb_titles)} game {market_id}"
        condition_id = f"0xmlb{market_id:04d}"
        outcome = rng.choice(["Yes", "No"])
        entry_price = round(rng.uniform(0.45, 0.75), 2)
        size_usd = round(rng.uniform(150, 1200), 0)
        # Bursty entry: a couple of trades within a few mins, then a fill the next day
        entry_time = base + pd.Timedelta(hours=market_id * rng.uniform(2, 8))

        # 1-3 BUY trades
        n_buys = rng.choice([1, 1, 2, 2, 3])
        for j in range(n_buys):
            rows.append({
                "type": "TRADE",
                "side": "BUY",
                "conditionId": condition_id,
                "title": title,
                "outcome": outcome,
                "price": entry_price + rng.uniform(-0.02, 0.02),
                "size": size_usd / n_buys,
                "usdcSize": size_usd / n_buys,
                "timestamp": entry_time + pd.Timedelta(minutes=j * rng.randint(2, 30)),
            })

        # 72% redeem (won), 18% lost (no exit), 10% sold early
        roll = rng.random()
        resolution_time = entry_time + pd.Timedelta(hours=rng.uniform(4, 36))

        if roll < 0.72:
            rows.append({
                "type": "REDEEM",
                "side": None,
                "conditionId": condition_id,
                "title": title,
                "outcome": outcome,
                "price": 1.0,
                "size": size_usd / entry_price,
                "usdcSize": size_usd / entry_price,
                "timestamp": resolution_time,
            })
        elif roll < 0.82:
            rows.append({
                "type": "TRADE",
                "side": "SELL",
                "conditionId": condition_id,
                "title": title,
                "outcome": outcome,
                "price": entry_price * rng.uniform(0.3, 1.4),
                "size": size_usd * rng.uniform(0.3, 1.4),
                "usdcSize": size_usd * rng.uniform(0.3, 1.4),
                "timestamp": resolution_time,
            })
        # else: lost — no exit recorded

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


# ─────────────────────────────────────────────
# 2. gabigol pattern — HFT crypto arbitrage bot
#    55,600 trades on ~280 markets, sub-second to ~5min holds,
#    grinds tiny PnL, regular timing
# ─────────────────────────────────────────────
def hft_bot_like(seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    base = _ts(2026, 1, 1)

    # 50 markets, but trades go out at a steady 30-second tick across all of them
    # in a cycling order — this matches gabigol's continuous arb pattern.
    n_markets = 50
    n_ticks = 1500  # 1500 trades total, alternating BUY/SELL
    for tick in range(n_ticks):
        market_id = tick % n_markets
        title = f"Bitcoin Up or Down — 5min — slot {market_id}"
        condition_id = f"0xbtc{market_id:04d}"
        outcome = "Yes"
        side = "BUY" if tick % 2 == 0 else "SELL"
        # Tight 30-second tick interval; tiny random jitter so deltas aren't literally identical
        ts = base + pd.Timedelta(seconds=30 * tick + rng.randint(-2, 2))

        # The price moves slightly per tick to give realistic SELL > BUY proceeds
        price = 0.50 + (0.005 if side == "SELL" else 0.0)
        size = 50

        rows.append({
            "type": "TRADE",
            "side": side,
            "conditionId": condition_id,
            "title": title,
            "outcome": outcome,
            "price": price,
            "size": size,
            "usdcSize": size,
            "timestamp": ts,
        })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


# ─────────────────────────────────────────────
# 3. neutralwave23 pattern — tilt/spray loser
#    Many tiny near-certain NO bets, plus chaotic crypto direction bets,
#    huge total volume, mostly losses, irregular but frequent
# ─────────────────────────────────────────────
def tilt_loser_like(seed: int = 99) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    base = _ts(2026, 1, 15)

    titles = [
        "Bitcoin above $100k by Friday", "Ethereum above $4000 today",
        "Will it rain in NYC tomorrow", "Trump tweet count today",
        "Knicks beat Heat", "Lakers cover spread",
        "Powell speech this week", "AI stock pump",
    ]

    # 400 markets, 1-3 trades each, mostly small bets
    for market_id in range(400):
        title = rng.choice(titles) + f" #{market_id}"
        condition_id = f"0xspy{market_id:04d}"
        outcome = rng.choice(["Yes", "No"])
        entry_price = round(rng.uniform(0.85, 0.97), 2)  # mostly NO bets at high prices
        size_usd = round(rng.uniform(5, 25), 0)
        # very dense — multiple trades per hour
        entry_time = base + pd.Timedelta(minutes=market_id * rng.uniform(8, 20))

        rows.append({
            "type": "TRADE",
            "side": "BUY",
            "conditionId": condition_id,
            "title": title,
            "outcome": outcome,
            "price": entry_price,
            "size": size_usd,
            "usdcSize": size_usd,
            "timestamp": entry_time,
        })

        # 20% of these resolve in their favor (tiny redeems)
        if rng.random() < 0.20:
            rows.append({
                "type": "REDEEM",
                "side": None,
                "conditionId": condition_id,
                "title": title,
                "outcome": outcome,
                "price": 1.0,
                "size": size_usd / entry_price,
                "usdcSize": size_usd / entry_price,
                "timestamp": entry_time + pd.Timedelta(hours=rng.uniform(2, 24)),
            })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


# ─────────────────────────────────────────────
# 4. Brand-new wallet — too few trades to score
# ─────────────────────────────────────────────
def insufficient_data_like(n_trades: int = 5) -> pd.DataFrame:
    rows = []
    base = _ts(2026, 5, 1)
    for i in range(n_trades):
        rows.append({
            "type": "TRADE",
            "side": "BUY",
            "conditionId": f"0xnew{i:03d}",
            "title": f"Some market {i}",
            "outcome": "Yes",
            "price": 0.5,
            "size": 10,
            "usdcSize": 10,
            "timestamp": base + pd.Timedelta(hours=i * 6),
        })
    return pd.DataFrame(rows)
