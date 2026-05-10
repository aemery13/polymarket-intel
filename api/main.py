"""
Polymarket Wallet Intelligence API.

Run locally:
    uvicorn api.main:app --reload --port 8000

Endpoints:
    GET  /                                       health
    GET  /wallet/{address}                       score blob (1h TTL, slow tier)
    GET  /wallet/{address}/positions             open positions (30s TTL, fast tier)
    GET  /wallet/{address}/history               score time series (DB)
    GET  /wallet/{address}/positions/history     position changes over time (DB)
    GET  /wallet/by-username/{username}          convenience lookup
    GET  /leaderboard                            raw Polymarket top traders
    GET  /leaderboard/verified                   filtered to scored humans
    GET  /leaderboard/historical                 leaderboard at a past date (DB)
    GET  /snapshots/latest                       most recent snapshot run summary
    GET  /docs                                   OpenAPI / Swagger UI

Tiering rationale:
    The score blob (classification, edge_score, signals) barely shifts from
    one minute to the next — those numbers are summarised over months of
    activity. Cache it for an hour, charge cheaply, debounce DB writes to
    once per wallet per hour.

    Open positions move with every trade. Cache 30s (Polygon block time
    is ~2s; <10s polling produces no new info, just rate-limits). No DB
    writes — the daily snapshot already covers history.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from core import PolymarketClient, score_wallet
from core.models import (
    LeaderboardEntry,
    Metadata,
    VerifiedLeaderboardResponse,
    WalletScoreResponse,
)
from db import get_repository

from .auth import auth_dependency
from .cache import cache
from mcp_server.server import mcp as mcp_server


# ─────────────────────────────────────────────
# Pre-build the MCP HTTP app so we can share its lifespan with FastAPI.
# FastAPI doesn't propagate lifespan to mounted sub-apps, so the MCP
# session manager's task group would never start without this — and
# every /mcp/* request would crash with "Task group is not initialized".
# ─────────────────────────────────────────────
mcp_server.settings.streamable_http_path = "/"
# Railway's edge proxy uses internal hostnames that FastMCP's DNS-rebinding
# protection rejects by default. Disable it — Railway already handles TLS
# termination and DDoS protection at the edge.
from mcp.server.transport_security import TransportSecuritySettings
mcp_server.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)
_mcp_http_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the MCP session manager alongside FastAPI's lifespan."""
    async with _mcp_http_app.router.lifespan_context(app):
        yield


# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
app = FastAPI(
    title="Polymarket Wallet Intelligence",
    description=(
        "Classifies Polymarket wallets as human or bot, scores their edge, "
        "and exposes their current open positions. Designed for AI agents "
        "that need a reliable signal layer when building Polymarket trading bots."
    ),
    version="1.0.0",
    contact={"name": "Polymarket Intel API"},
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

client = PolymarketClient()
repo = get_repository()

# Mount the pre-built MCP app at /mcp. The lifespan above ensures
# its session manager starts up alongside FastAPI's.
app.mount("/mcp", _mcp_http_app)

# ─────────────────────────────────────────────
# Cache TTLs and DB-write debounce
# ─────────────────────────────────────────────
TTL_WALLET_SCORE = 3600        # 1h — slow tier; score blob barely moves
TTL_WALLET_POSITIONS = 30      # 30s — fast tier; positions move with every trade
TTL_LEADERBOARD = 1800         # 30m
TTL_VERIFIED = 3600

# Only write a new score record to the DB if the last one for this wallet is
# older than this. Caps history-table growth from API traffic; the daily
# snapshot job still guarantees one row/wallet/day.
SCORE_PERSIST_DEBOUNCE_SECONDS = 3600


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# Smithery's well-known server card.
# ─────────────────────────────────────────────
@app.get("/.well-known/mcp/server-card.json")
def well_known_server_card() -> dict:
    return {
        "name": "polymarket-intel",
        "version": "1.0.0",
        "title": "Polymarket Wallet Intelligence",
        "description": (
            "Classify Polymarket wallets as human or bot, score their "
            "trading edge, and read their current open positions."
        ),
        "transport": {
            "type": "streamable-http",
            "url": "https://polymarket-intel-production.up.railway.app/mcp/",
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "tools": [
            {
                "name": "score_polymarket_wallet",
                "description": "Score a Polymarket wallet by address.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"wallet_address": {"type": "string"}},
                    "required": ["wallet_address"],
                },
            },
            {
                "name": "score_polymarket_user",
                "description": "Score a Polymarket user by username.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"username": {"type": "string"}},
                    "required": ["username"],
                },
            },
            {
                "name": "get_polymarket_leaderboard",
                "description": "Get the top wallets from the Polymarket leaderboard.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": [],
                },
            },
            {
                "name": "get_open_positions",
                "description": "Get a wallet's currently open positions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"wallet_address": {"type": "string"}},
                    "required": ["wallet_address"],
                },
            },
        ],
    }
@app.get("/")
def root() -> dict:
    return {
        "service": "polymarket-wallet-intel",
        "version": app.version,
        "endpoints": {
            "score (slow tier, 1h cache)": "/wallet/{address}",
            "positions (fast tier, 30s cache)": "/wallet/{address}/positions",
            "score history": "/wallet/{address}/history",
            "positions history": "/wallet/{address}/positions/history",
            "leaderboard": "/leaderboard",
            "verified leaderboard": "/leaderboard/verified",
            "historical leaderboard": "/leaderboard/historical",
            "by username": "/wallet/by-username/{username}",
            "docs": "/docs",
        },
        "cache_stats": cache.stats(),
    }


# ─────────────────────────────────────────────
# Wallet score — slow tier (1h cache, debounced DB write)
# ─────────────────────────────────────────────
@app.get("/wallet/{address}", response_model=WalletScoreResponse)
def wallet_score(
    address: str,
    refresh: bool = Query(False, description="Bypass cache and re-fetch."),
    _auth: dict = Depends(auth_dependency),
) -> WalletScoreResponse:
    """
    Score a wallet by its proxy address. **Slow tier — 1 hour cache.**

    Returns classification (human / bot / insufficient_data), confidence,
    edge score (humans only), all underlying signals, and performance stats.

    Does **not** include open positions — those move on every trade and
    belong on the fast tier. Call `/wallet/{address}/positions` for those.

    Side effect: persists the score to the wallet_scores history table,
    debounced to once per wallet per hour. Cache hits skip the write.
    """
    address = _validate_address(address)

    cache_key = f"wallet:{address}"
    if not refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            cached.metadata.cache_hit = True
            return cached

    try:
        activity = client.get_activity(address)
    except Exception as e:
        raise HTTPException(502, detail=f"Polymarket API error: {e}")

    score = score_wallet(activity, wallet=address)

    # Persist BEFORE stripping positions — we already computed them, so
    # writing them to history is free. The response still won't include
    # positions (use /positions for live data).
    _maybe_persist_score(score)
    score.open_positions = []

    payload = _to_response(score, cache_hit=False)
    cache.set(cache_key, payload, ttl_seconds=TTL_WALLET_SCORE)

    return payload


# ─────────────────────────────────────────────
# Wallet positions — fast tier (30s cache, no DB write)
# ─────────────────────────────────────────────
@app.get("/wallet/{address}/positions")
def wallet_positions(
    address: str,
    refresh: bool = Query(False, description="Bypass cache and re-fetch."),
    _auth: dict = Depends(auth_dependency),
) -> dict:
    """
    Return only the open positions for a wallet. **Fast tier — 30s cache.**

    Optimised for copy-trading agents that need near-real-time visibility
    into what a verified human is currently betting on. Does not include
    classification or edge_score — call `/wallet/{address}` for those
    (and cache the result; they barely change).

    No DB writes from this endpoint. Position history is captured by the
    daily snapshot job.
    """
    address = _validate_address(address)

    cache_key = f"positions:{address}"
    if not refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            cached["metadata"]["cache_hit"] = True
            return cached

    try:
        activity = client.get_activity(address)
    except Exception as e:
        raise HTTPException(502, detail=f"Polymarket API error: {e}")

    score = score_wallet(activity, wallet=address)

    payload = {
        "wallet": address,
        "classification": score.classification,
        "edge_score": score.edge_score,
        "open_positions_count": len(score.open_positions),
        "open_positions": score.open_positions,
        "metadata": {
            "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
            "cache_hit": False,
            "cache_ttl_seconds": TTL_WALLET_POSITIONS,
        },
    }
    cache.set(cache_key, payload, ttl_seconds=TTL_WALLET_POSITIONS)
    return payload


# ─────────────────────────────────────────────
# Wallet score by username
# ─────────────────────────────────────────────
@app.get("/wallet/by-username/{username}", response_model=WalletScoreResponse)
def wallet_score_by_username(
    username: str,
    _auth: dict = Depends(auth_dependency),
) -> WalletScoreResponse:
    """
    Look up a wallet on the leaderboard by username, then score it.

    Same slow-tier semantics as /wallet/{address}: 1h cache, debounced
    DB write, no open positions in the response.
    """
    lb = _get_leaderboard()
    wallet, lb_pnl = client.find_wallet(username, lb)
    if not wallet:
        raise HTTPException(
            404,
            detail=f"Username '{username}' not in top-200 leaderboard. Pass the wallet address directly.",
        )

    cache_key = f"wallet:{wallet.lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        cached.username = username
        cached.metadata.cache_hit = True
        return cached

    activity = client.get_activity(wallet)
    score = score_wallet(activity, wallet=wallet.lower(), username=username, leaderboard_pnl=lb_pnl)
    _maybe_persist_score(score)
    score.open_positions = []  # slow tier never returns positions
    payload = _to_response(score, cache_hit=False)
    cache.set(cache_key, payload, ttl_seconds=TTL_WALLET_SCORE)
    return payload


# ─────────────────────────────────────────────
# Raw leaderboard
# ─────────────────────────────────────────────
@app.get("/leaderboard")
def leaderboard(
    limit: int = Query(50, ge=1, le=200),
    _auth: dict = Depends(auth_dependency),
) -> dict:
    lb = _get_leaderboard()
    head = lb.head(limit)
    return {
        "count": int(len(head)),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "entries": [
            {
                "username": str(row.get("userName", "")),
                "wallet": str(row.get("proxyWallet", "")),
                "pnl_usd": float(row.get("pnl", 0) or 0),
                "volume_usd": float(row.get("vol", 0) or 0) if row.get("vol") is not None else None,
            }
            for _, row in head.iterrows()
        ],
    }


# ─────────────────────────────────────────────
# Verified leaderboard — top scored humans
# ─────────────────────────────────────────────
@app.get("/leaderboard/verified", response_model=VerifiedLeaderboardResponse)
def leaderboard_verified(
    limit: int = Query(20, ge=1, le=50, description="How many top humans to return."),
    min_edge: float = Query(5.0, ge=0.0, le=10.0),
    sample_pool: int = Query(50, ge=10, le=200, description="How many top leaderboard wallets to score."),
    _auth: dict = Depends(auth_dependency),
) -> VerifiedLeaderboardResponse:
    """
    Score the top `sample_pool` leaderboard wallets, filter to humans with
    edge_score >= min_edge, and return the top `limit`.

    Cached for 1h. The first call is slow (scores N wallets sequentially);
    subsequent calls within TTL are instant.
    """
    cache_key = f"verified:{sample_pool}:{min_edge}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    lb = _get_leaderboard()
    scored: list[tuple[float, LeaderboardEntry]] = []

    for _, row in lb.head(sample_pool).iterrows():
        wallet = row.get("proxyWallet")
        username = row.get("userName")
        lb_pnl = float(row.get("pnl", 0) or 0)
        if not wallet:
            continue

        wkey = f"wallet:{wallet.lower()}"
        existing = cache.get(wkey)
        if existing is not None:
            score_payload = existing
        else:
            try:
                activity = client.get_activity(wallet)
            except Exception:
                continue
            score = score_wallet(activity, wallet=wallet.lower(), username=username, leaderboard_pnl=lb_pnl)
            _maybe_persist_score(score)
            score.open_positions = []  # slow tier — agent calls /positions for the live ones
            score_payload = _to_response(score, cache_hit=False)
            cache.set(wkey, score_payload, ttl_seconds=TTL_WALLET_SCORE)
            time.sleep(0.3)  # be nice to the upstream

        if score_payload.classification == "human" and (score_payload.edge_score or 0) >= min_edge:
            scored.append((
                score_payload.edge_score,
                LeaderboardEntry(
                    username=username or "",
                    wallet=wallet,
                    pnl_usd=lb_pnl,
                    volume_usd=float(row.get("vol", 0) or 0) if row.get("vol") is not None else None,
                ),
            ))

    scored.sort(key=lambda x: x[0], reverse=True)
    response = VerifiedLeaderboardResponse(
        count=len(scored[:limit]),
        fetched_at_iso=datetime.now(timezone.utc).isoformat(),
        entries=[entry for _, entry in scored[:limit]],
    )
    cache.set(cache_key, response, ttl_seconds=TTL_VERIFIED)
    return response


# ─────────────────────────────────────────────
# History endpoints — backed by the repository
# ─────────────────────────────────────────────
@app.get("/wallet/{address}/history")
def wallet_history(
    address: str,
    days: int = Query(30, ge=1, le=365, description="Look-back window in days."),
    limit: int = Query(365, ge=1, le=1000),
    _auth: dict = Depends(auth_dependency),
) -> dict:
    """
    Return the score time series for a wallet.

    Each row is one snapshot from the daily job. Use this to track how
    a wallet's edge_score, win rate, and signals have changed over time
    — and to spot wallets that are improving vs deteriorating.
    """
    address = _validate_address(address)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = repo.get_score_history(wallet=address, since=since, limit=limit)
    return {
        "wallet": address,
        "window_days": days,
        "count": len(rows),
        "history": [
            {
                "scored_at": r.scored_at.isoformat(),
                "classification": r.classification,
                "confidence": r.confidence,
                "edge_score": r.edge_score,
                "reason_codes": r.reason_codes,
                "leaderboard_pnl_usd": r.leaderboard_pnl_usd,
                "win_rate": r.signals.get("win_rate") if r.signals else None,
                "net_realised_pnl_usd": r.performance.get("net_realised_pnl_usd") if r.performance else None,
                "open_positions_count": r.performance.get("open_positions_count") if r.performance else None,
            }
            for r in rows
        ],
    }


@app.get("/wallet/{address}/positions/history")
def wallet_positions_history(
    address: str,
    days: int = Query(7, ge=1, le=90),
    _auth: dict = Depends(auth_dependency),
) -> dict:
    """
    Return position snapshots for a wallet over the look-back window.

    By diffing across timestamps the consumer can compute:
      - which markets the wallet entered or exited each day
      - how their per-market exposure changed
    """
    address = _validate_address(address)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = repo.get_position_history(wallet=address, since=since)

    # Group by snapshot_at so the consumer can diff between days
    by_snapshot: dict[str, list[dict]] = {}
    for p in rows:
        key = p.snapshot_at.isoformat()
        by_snapshot.setdefault(key, []).append({
            "condition_id": p.condition_id,
            "title": p.title,
            "outcome": p.outcome,
            "category": p.category,
            "money_in_usd": p.money_in_usd,
            "avg_entry_price": p.avg_entry_price,
            "num_buys": p.num_buys,
            "first_trade_at": p.first_trade_at.isoformat() if p.first_trade_at else None,
            "last_trade_at": p.last_trade_at.isoformat() if p.last_trade_at else None,
        })

    snapshots = [
        {"snapshot_at": ts, "positions": positions}
        for ts, positions in sorted(by_snapshot.items(), reverse=True)
    ]
    return {
        "wallet": address,
        "window_days": days,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }


@app.get("/leaderboard/historical")
def leaderboard_historical(
    date: Optional[str] = Query(
        None,
        description="ISO date or datetime (e.g. '2025-12-01' or '2025-12-01T08:00:00Z'). "
                    "Defaults to latest snapshot.",
    ),
    limit: int = Query(50, ge=1, le=200),
    _auth: dict = Depends(auth_dependency),
) -> dict:
    """
    Return the leaderboard as it stood at a past date.

    Useful for backtesting: 'who was top of the leaderboard 30 days ago,
    and how have they performed since?'
    """
    if date is None:
        rows = repo.get_latest_leaderboard()
        as_of_query = "latest"
    else:
        try:
            # URL-decoding turns `+00:00` into ` 00:00`. Accept both forms.
            normalized = date.replace(" ", "+").replace("Z", "+00:00")
            at = datetime.fromisoformat(normalized)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, detail=f"Invalid date format: {date!r}")
        rows = repo.get_leaderboard_at(at)
        as_of_query = at.isoformat()

    if not rows:
        return {
            "as_of_query": as_of_query,
            "snapshot_at": None,
            "count": 0,
            "entries": [],
            "note": "No leaderboard snapshots stored. Run scripts/snapshot_job.py first.",
        }

    return {
        "as_of_query": as_of_query,
        "snapshot_at": rows[0].snapshot_at.isoformat(),
        "count": min(len(rows), limit),
        "entries": [
            {
                "rank": e.rank,
                "username": e.username,
                "wallet": e.wallet,
                "pnl_usd": e.pnl_usd,
                "volume_usd": e.volume_usd,
            }
            for e in rows[:limit]
        ],
    }


@app.get("/snapshots/latest")
def snapshots_latest() -> dict:
    """Quick health probe — when did the daily job last run?"""
    lb = repo.get_latest_leaderboard()
    return {
        "latest_snapshot_at": lb[0].snapshot_at.isoformat() if lb else None,
        "leaderboard_size": len(lb),
    }


# ─────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────
def _validate_address(address: str) -> str:
    address = address.lower().strip()
    if not address.startswith("0x") or len(address) != 42:
        raise HTTPException(400, detail="Address must be a 0x-prefixed 42-char hex string.")
    return address


def _maybe_persist_score(score) -> None:
    """
    Write a score record + open-position snapshot, but only if the last
    record for this wallet is older than SCORE_PERSIST_DEBOUNCE_SECONDS.

    This caps the size of `wallet_scores` regardless of API traffic. The
    daily snapshot job still guarantees one row per scored wallet per day.

    Failures here are swallowed — persistence shouldn't block a response.
    """
    from db.converters import positions_to_snapshots, score_to_record  # local import to avoid cycle

    try:
        existing = repo.get_latest_score(score.wallet)
        if existing is not None:
            elapsed = (datetime.now(timezone.utc) - existing.scored_at).total_seconds()
            if elapsed < SCORE_PERSIST_DEBOUNCE_SECONDS:
                return  # too soon — skip

        scored_at = datetime.now(timezone.utc)
        repo.upsert_wallet(score.wallet, score.username, scored_at)
        repo.insert_score(score_to_record(score, scored_at=scored_at))
        if score.open_positions:
            repo.insert_position_snapshots(positions_to_snapshots(score, snapshot_at=scored_at))
    except Exception:
        # Persistence is best-effort. Log it in production; here we just swallow.
        pass


def _get_leaderboard():
    cached = cache.get("leaderboard:200")
    if cached is not None:
        return cached
    lb = client.get_leaderboard(limit=200)
    cache.set("leaderboard:200", lb, ttl_seconds=TTL_LEADERBOARD)
    return lb


def _to_response(score, cache_hit: bool) -> WalletScoreResponse:
    payload = score.to_dict()
    payload["metadata"] = {
        "records_analyzed": payload["metadata"]["records_analyzed"],
        "scoring_version": payload["metadata"].get("scoring_version", "1.0.0"),
        "cache_hit": cache_hit,
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
    }
    return WalletScoreResponse(**payload)
