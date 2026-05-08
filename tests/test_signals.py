"""Unit tests for individual signal calculators."""

from __future__ import annotations

import pytest

from core import signals
from tests import fixtures


# ─────────────────────────────────────────────
# Focus ratio
# ─────────────────────────────────────────────
def test_focus_ratio_human():
    activity = fixtures.phonesculptor_like()
    ratio = signals.focus_ratio(activity)
    assert 1.0 <= ratio <= 5.0, f"Human ratio should be 1-5, got {ratio:.2f}"


def test_focus_ratio_hft_bot():
    activity = fixtures.hft_bot_like()
    ratio = signals.focus_ratio(activity)
    assert ratio > 12.0, f"Bot ratio should be >12, got {ratio:.2f}"


def test_focus_ratio_empty():
    import pandas as pd
    assert signals.focus_ratio(pd.DataFrame()) == 0.0


# ─────────────────────────────────────────────
# Holding period
# ─────────────────────────────────────────────
def test_holding_period_human_long():
    activity = fixtures.phonesculptor_like()
    stats = signals.holding_period_stats(activity)
    assert stats["sample_size"] > 50
    # phonesculptor synthetic: 4-36h holds
    assert stats["median_seconds"] >= 3600, f"Median holds expected ≥1h, got {stats['median_seconds']}s"


def test_holding_period_hft_short():
    activity = fixtures.hft_bot_like()
    stats = signals.holding_period_stats(activity)
    # Synthetic HFT: 15-45s holds
    assert stats["median_seconds"] < 60, f"HFT median should be <60s, got {stats['median_seconds']}s"


# ─────────────────────────────────────────────
# Timing regularity
# ─────────────────────────────────────────────
def test_timing_human_irregular():
    activity = fixtures.phonesculptor_like()
    stats = signals.timing_regularity(activity)
    assert stats["cv"] > 0.5, f"Human CV expected >0.5, got {stats['cv']:.2f}"


def test_timing_bot_regular():
    activity = fixtures.hft_bot_like()
    stats = signals.timing_regularity(activity)
    # 5-min fixed intervals → very low CV
    assert stats["cv"] < 1.0, f"Bot CV expected <1.0, got {stats['cv']:.2f}"


# ─────────────────────────────────────────────
# Category
# ─────────────────────────────────────────────
def test_category_human_sports():
    activity = fixtures.phonesculptor_like()
    cat = signals.category_concentration(activity)
    assert cat["dominant"] == "Sports"
    assert cat["dominant_share"] > 0.9


def test_category_bot_crypto():
    activity = fixtures.hft_bot_like()
    cat = signals.category_concentration(activity)
    assert cat["dominant"] == "Crypto"
    assert cat["dominant_share"] > 0.95


# ─────────────────────────────────────────────
# Win rate / PnL reconstruction
# ─────────────────────────────────────────────
def test_winloss_human_high_winrate():
    activity = fixtures.phonesculptor_like()
    pnl = signals.reconstruct_market_pnl(activity)
    wl = signals.win_rate_stats(pnl)
    assert wl["wins"] > 100
    assert wl["win_rate"] > 0.6, f"Synthetic phonesculptor expected ~72% WR, got {wl['win_rate']:.2f}"


def test_winloss_loser_low_winrate():
    activity = fixtures.tilt_loser_like()
    pnl = signals.reconstruct_market_pnl(activity)
    wl = signals.win_rate_stats(pnl)
    assert wl["win_rate"] < 0.3, f"Tilt loser expected <30% WR, got {wl['win_rate']:.2f}"
    assert wl["net_realised_pnl"] < 0, "Tilt loser should be net negative"


# ─────────────────────────────────────────────
# PnL concentration
# ─────────────────────────────────────────────
def test_pnl_concentration_distributed():
    activity = fixtures.phonesculptor_like()
    pnl = signals.reconstruct_market_pnl(activity)
    conc = signals.pnl_concentration(pnl)
    # Many small wins, no single bet dominating
    assert conc["top1_share"] < 0.30, f"Distributed expected <30%, got {conc['top1_share']:.2f}"


# ─────────────────────────────────────────────
# Activity tempo
# ─────────────────────────────────────────────
def test_tempo_human():
    activity = fixtures.phonesculptor_like()
    tempo = signals.activity_tempo(activity)
    assert tempo["trades_per_day"] < 100, "Human shouldn't trade 100+/day"
    assert tempo["avg_bet_usd"] > 50, "phonesculptor avg bet ≥$50"


def test_tempo_bot_high_velocity():
    activity = fixtures.hft_bot_like()
    tempo = signals.activity_tempo(activity)
    # 3000 trades over ~10 days → 300+/day
    assert tempo["trades_per_day"] > 100
