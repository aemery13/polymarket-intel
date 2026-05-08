"""
Polymarket Wallet Intel — MCP server.

Run for local stdio use (Claude Desktop, etc.):
    python -m mcp.server polymarket-intel
or simply:
    python mcp/server.py

This exposes the wallet scoring tools as MCP tools so AI agents on
Claude Desktop, Cursor, Replit Agent, and other MCP-aware platforms
can call them directly.

For remote / paid distribution: deploy `api/main.py` and front it with
the MCP HTTP transport — same tool definitions, different transport.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Make the parent dir importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

from core import PolymarketClient, score_wallet


mcp = FastMCP("polymarket-intel")
client = PolymarketClient()


# ─────────────────────────────────────────────
# Tool 1 — score a wallet (slow tier — agent guidance: cache aggressively)
# ─────────────────────────────────────────────
@mcp.tool()
def score_polymarket_wallet(wallet_address: str) -> dict:
    """
    Classify a Polymarket wallet as human or bot and score its trading edge.

    Use this once when deciding whether to follow / copy a given trader.
    The result barely changes from minute to minute — agents should cache
    it on their side and not re-call more than ~hourly. Use
    `get_open_positions` for live bet visibility.

    Returns:
      - classification: "human" | "bot" | "insufficient_data"
      - confidence: 0.0–1.0
      - edge_score: 0.0–10.0 (humans only; null otherwise)
      - reason_codes: list of human-readable reasons for the classification
      - signals: focus_ratio, holding_period, timing_cv, category breakdown,
                 win_rate, pnl concentration
      - performance: net realised PnL, wins/losses, avg/max bet, active days

    Does NOT include open positions — call get_open_positions for that.

    Args:
        wallet_address: 0x-prefixed Ethereum proxy wallet address (42 chars).
    """
    addr = wallet_address.lower().strip()
    if not addr.startswith("0x") or len(addr) != 42:
        return {"error": "wallet_address must be a 0x-prefixed 42-character hex string"}

    activity = client.get_activity(addr)
    score = score_wallet(activity, wallet=addr)
    payload = score.to_dict()
    # Slow-tier contract: never include positions in the score blob
    payload["open_positions"] = []
    return payload


# ─────────────────────────────────────────────
# Tool 2 — score by username
# ─────────────────────────────────────────────
@mcp.tool()
def score_polymarket_user(username: str) -> dict:
    """
    Look up a Polymarket username on the top-200 leaderboard, then score it.

    Convenience wrapper for cases where the agent has a Polymarket
    display name but not a wallet address. Falls back with a clear error
    if the user has dropped off the leaderboard.

    Args:
        username: Polymarket display name, e.g. "phonesculptor".
    """
    lb = client.get_leaderboard(limit=200)
    wallet, lb_pnl = client.find_wallet(username, lb)
    if not wallet:
        return {
            "error": f"User '{username}' not in the top-200 leaderboard.",
            "hint": "Pass the wallet address directly via score_polymarket_wallet.",
        }

    activity = client.get_activity(wallet)
    score = score_wallet(activity, wallet=wallet.lower(), username=username, leaderboard_pnl=lb_pnl)
    payload = score.to_dict()
    payload["open_positions"] = []  # slow-tier contract
    return payload


# ─────────────────────────────────────────────
# Tool 3 — raw leaderboard
# ─────────────────────────────────────────────
@mcp.tool()
def get_polymarket_leaderboard(limit: int = 50) -> dict:
    """
    Fetch the current Polymarket all-time PnL leaderboard.

    Note: the leaderboard reflects realised + unrealised PnL marked to
    current price. Top entries are heavily skewed by open positions and
    are not a reliable signal of skill on their own. Use this as a pool
    to feed into score_polymarket_wallet.

    Args:
        limit: Number of entries to return (1–200).
    """
    limit = max(1, min(200, limit))
    df = client.get_leaderboard(limit=limit)
    return {
        "count": int(len(df)),
        "entries": [
            {
                "username": str(row.get("userName", "")),
                "wallet": str(row.get("proxyWallet", "")),
                "pnl_usd": float(row.get("pnl", 0) or 0),
                "volume_usd": float(row.get("vol", 0) or 0) if row.get("vol") is not None else None,
            }
            for _, row in df.iterrows()
        ],
    }


# ─────────────────────────────────────────────
# Tool 4 — open positions (fast tier — for live monitoring)
# ─────────────────────────────────────────────
@mcp.tool()
def get_open_positions(wallet_address: str) -> dict:
    """
    Return what a wallet currently has live money on, sorted by size.

    Use this for ongoing monitoring of a verified trader — what bets
    they're entering and exiting in near-real-time. Safe to poll every
    30+ seconds; below ~10s you'll just hit Polymarket's rate limit
    without getting fresher data (Polygon block time is ~2s and the
    activity index lags).

    Returns:
      - wallet, classification, edge_score (so agents can sanity-check
        they're following a verified human, without re-running the full
        score)
      - open_positions: list of {condition_id, title, outcome, category,
        money_in_usd, avg_entry_price, num_buys, first_trade_at,
        last_trade_at}

    Args:
        wallet_address: 0x-prefixed Ethereum proxy wallet address.
    """
    addr = wallet_address.lower().strip()
    if not addr.startswith("0x") or len(addr) != 42:
        return {"error": "wallet_address must be a 0x-prefixed 42-character hex string"}

    activity = client.get_activity(addr)
    score = score_wallet(activity, wallet=addr)
    return {
        "wallet": addr,
        "classification": score.classification,
        "edge_score": score.edge_score,
        "open_positions_count": len(score.open_positions),
        "open_positions": score.open_positions,
    }


def main() -> None:
    """
    Console-script entry point.

    Two transports supported:
      stdio (default)            — Claude Desktop, Cursor, Continue, local use
      streamable-http            — remote hosting (Smithery, official registry)

    Choose via MCP_TRANSPORT env var. The HTTP transport listens on PORT
    (default 8765) at the path "/mcp".
    """
    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        port = int(os.environ.get("PORT", "8765"))
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio (default)


if __name__ == "__main__":
    main()
