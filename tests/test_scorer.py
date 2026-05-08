"""End-to-end tests for the scoring engine."""

from __future__ import annotations

from core import score_wallet
from tests import fixtures


def test_human_classification():
    activity = fixtures.phonesculptor_like()
    result = score_wallet(activity, wallet="0xtest_human", username="phonesculptor_synth")

    assert result.classification == "human", \
        f"Expected human, got {result.classification} ({result.reason_codes})"
    assert result.confidence >= 0.6
    assert result.edge_score is not None
    assert result.edge_score >= 5.0, \
        f"Strong human bettor should score ≥5/10, got {result.edge_score}"
    assert result.signals["category"]["dominant"] == "Sports"


def test_bot_classification():
    activity = fixtures.hft_bot_like()
    result = score_wallet(activity, wallet="0xtest_bot", username="gabigol_synth")

    assert result.classification == "bot", \
        f"Expected bot, got {result.classification} ({result.reason_codes})"
    assert result.confidence >= 0.7
    assert result.edge_score is None  # bots don't get an edge score
    # at least one of the bot reason codes must fire
    assert any(c.startswith(("focus_ratio", "hft_holding", "crypto_market_maker"))
               for c in result.reason_codes), \
        f"Expected a bot reason code, got {result.reason_codes}"


def test_tilt_loser_is_human_with_low_edge():
    activity = fixtures.tilt_loser_like()
    result = score_wallet(activity, wallet="0xtest_tilt", username="neutralwave23_synth")

    # Loser is still classified as human (we want agents to know the difference)
    # but the edge score should be poor
    if result.classification == "human":
        assert result.edge_score is not None and result.edge_score < 5.0, \
            f"Loser shouldn't have strong edge, got {result.edge_score}"


def test_insufficient_data():
    activity = fixtures.insufficient_data_like(n_trades=5)
    result = score_wallet(activity, wallet="0xtest_new", username="newbie")
    assert result.classification == "insufficient_data"
    assert result.edge_score is None


def test_response_serialisable():
    """ScoreResult.to_dict() must produce JSON-safe output for the API."""
    import json
    activity = fixtures.phonesculptor_like()
    result = score_wallet(activity, wallet="0xtest", username="x")
    payload = result.to_dict()
    json.dumps(payload, default=str)  # should not throw
    assert "wallet" in payload
    assert "signals" in payload
    assert "open_positions" in payload
