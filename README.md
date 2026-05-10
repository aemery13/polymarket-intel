# Polymarket Wallet Intelligence

<!-- mcp-name: io.github.aemery13/polymarket-intel -->

**An MCP server and REST API that classifies Polymarket wallets as human or bot, scores their trading edge from 0–10, and streams their current open positions.** Built for AI agents on copy-trading and signal-following stacks.

```bash
# Use it from any MCP client (Claude Desktop, Cursor, etc.)
pip install polymarket-intel-mcp
polymarket-intel-mcp

# Or call the hosted REST API directly
curl https://polymarket-intel-production.up.railway.app/wallet/0xf1528f12e645462c344799b62b1b421a6a4c64aa
```
## How this fits with other Polymarket MCP servers

There are several MCP servers covering Polymarket, each at a different layer:

| Server | What it does | When to use it |
|---|---|---|
| **polymarket-intel** (this) | Wallet intelligence — classify human vs bot, score trading edge, read open positions | Deciding *whose* signals to follow |
| graph-polymarket-mcp | Market data via The Graph subgraphs (20 tools, 8 subgraphs) | Reading raw on-chain market data |
| whitmorelabs/polymarket-mcp | Slippage, liquidity, arbitrage, price feeds | Pricing your own trades |
| joinQuantish/polymarket | Self-hosted trading agent | Running an autonomous bot |

These complement each other. A copy-trading agent would use **polymarket-intel** to filter wallets worth following, then **graph-polymarket-mcp** to read the markets those wallets are betting on, then **whitmorelabs/polymarket-mcp** to size its own entries.

## What it answers

- **"Is this trader a human or a bot?"** — `score_polymarket_wallet(wallet_address)` → returns `classification ∈ {human, bot, insufficient_data}` plus a confidence score and reason codes.
- **"Do they actually have an edge?"** — `edge_score` from 0–10, gated on net realised PnL so distributed-but-losing wallets don't get false positives.
- **"What are they betting on right now?"** — `get_open_positions(wallet_address)` returns live positions sorted by size, refreshed every 30s.
- **"How has their edge changed over time?"** — `/wallet/{address}/history` returns the score time series from the daily snapshots.

## Why this exists

The Polymarket leaderboard is misleading. It includes unrealised PnL marked-to-current-price, so the names at the top are dominated by bots running structural arb plus a few wallets sitting on huge open positions that may never resolve in their favour. Agents that copy-trade naively from the leaderboard get burned.

This service runs every leaderboard wallet through behavioural fingerprinting (focus ratio, holding period, timing regularity, category concentration) plus PnL reconstruction from raw activity, and only surfaces traders that look like genuine humans with a real edge.

**The dataset grows more valuable over time** — every day the snapshot job runs, historical signals accumulate. Wallets that have been consistently above edge 7 for 90 days are a stronger signal than any single point-in-time score.

## Distributed as both a REST API and an MCP server

| Surface     | Use case                                    | Setup                               |
|-------------|---------------------------------------------|-------------------------------------|
| MCP server  | Agent that needs tool-style access          | `pip install polymarket-intel-mcp`  |
| REST API    | Custom HTTP integration, dashboards         | `curl https://polymarket-intel-production.up.railway.app/...` |
| Hosted MCP  | Agent on any MCP-compatible client          | Add `https://polymarket-intel-production.up.railway.app/mcp` to client config |

## Architecture

```
┌──────────────────────────────────────────────┐
│  core/                                       │
│    client.py    — Polymarket data API client │
│    signals.py   — pure signal calculators    │
│    scorer.py    — classifier + edge score    │
│    models.py    — Pydantic response schemas  │
├──────────────────────────────────────────────┤
│  db/                                         │
│    schema.sql   — Postgres tables + indexes  │
│    repository.py — Repository protocol +     │
│                    InMemoryRepository        │
│    supabase_repo.py — Supabase impl          │
│    converters.py — ScoreResult ↔ records     │
├──────────────────────────────────────────────┤
│  api/main.py    — FastAPI HTTP server        │
│  mcp_server/    — MCP server (stdio)         │
│  scripts/                                    │
│    analyze_wallet.py — CLI                   │
│    snapshot_job.py   — daily cron entry      │
│  tests/                                      │
└──────────────────────────────────────────────┘
```

Core has no idea persistence exists. The API and snapshot job depend on the `Repository` protocol — Supabase in production, in-memory in tests and when env vars are unset. This is what makes the suite run without a database and what lets you swap Supabase for Neon, RDS, or anything else later by adding one file.

## Quickstart

```bash
git clone <repo> && cd polymarket-intel
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                             # 19 tests, all green
```

### CLI

```bash
python scripts/analyze_wallet.py phonesculptor
python scripts/analyze_wallet.py 0xf1528f12e645462c344799b62b1b421a6a4c64aa --json
```

### REST API

```bash
uvicorn api.main:app --reload --port 8000
open http://localhost:8000/docs
```

The API is split into a **slow tier** (cached aggressively, cheap, ideal for one-off discovery) and a **fast tier** (short cache, ideal for live copy-trading agents). The split exists because the underlying data has different freshness needs — a wallet's classification doesn't change minute-to-minute, but their open positions do.

| Tier | Method | Path                                     | TTL  | Notes                                    |
|------|--------|------------------------------------------|------|------------------------------------------|
| slow | GET    | `/wallet/{address}`                      | 1h   | Score blob — classification, edge_score, signals. No positions. Persisted to history (debounced). |
| fast | GET    | `/wallet/{address}/positions`            | 30s  | Open positions only. No DB write per call. |
| —    | GET    | `/wallet/{address}/history`              | DB   | Score time series                        |
| —    | GET    | `/wallet/{address}/positions/history`    | DB   | Position changes over time               |
| —    | GET    | `/wallet/by-username/{username}`         | 1h   | Convenience lookup                       |
| —    | GET    | `/leaderboard?limit=50`                  | 30m  | Raw Polymarket top traders               |
| —    | GET    | `/leaderboard/verified?min_edge=5`       | 1h   | Filtered to scored humans                |
| —    | GET    | `/leaderboard/historical?date=…`         | DB   | Leaderboard at any past date             |
| —    | GET    | `/snapshots/latest`                      | DB   | When did the cron last run?              |

**Why 30s on positions and not faster?** Polygon block time is ~2s and Polymarket's activity index lags a few seconds. Polling below 10s gets you no fresher data, just rate-limit errors. 30s is the sweet spot for cost/freshness/upstream-friendliness.

**Why debounced DB writes?** A trading agent may hit `/wallet/{address}` thousands of times an hour. Writing a row per call would bloat history with near-duplicate snapshots. The score endpoint persists at most once per wallet per hour. The daily snapshot job guarantees coverage of the top 50 regardless of API traffic.

### MCP server (Claude Desktop, Cursor, Continue)

```bash
python mcp_server/server.py
```

Then drop `mcp_server/claude_desktop_config.example.json` into your Claude Desktop config and edit the absolute path.

The server exposes four tools:

- `score_polymarket_wallet(wallet_address)` — full score
- `score_polymarket_user(username)` — lookup by display name
- `get_polymarket_leaderboard(limit)` — raw leaderboard
- `get_open_positions(wallet_address)` — fast snapshot of live bets

## Scoring methodology

### Bot triggers (any one fires → bot)

| Signal              | Threshold     | Source                      |
|---------------------|---------------|-----------------------------|
| Focus ratio         | > 12          | Hubble Research, validated empirically |
| Median hold time    | < 60s         | HFT / MEV pattern           |
| Timing CV           | < 0.3 (n≥100) | Scheduled trading           |

Soft signals stack: crypto-market-maker pattern, > 200 trades/day, etc.

### Edge score (0–10) for humans

```
Hard gate: net realised PnL ≤ 0  →  capped at 2.0
Hard gate: < 10 winning markets  →  capped at 3.0

35%  PnL magnitude (log-scaled)
25%  win rate (capped at 70%)
15%  PnL distribution (penalises top-1 concentration)
15%  sample size (winning markets, capped at 50)
10%  win/loss ratio (capped at 3x)
```

Net PnL is the hard gate so wallets like neutralwave23 — many distributed tiny wins masking $375k of losses — are correctly flagged as poor.

### PnL reconstruction

```
money_in   = sum(BUY usdcSize per conditionId)
money_out  = sum(SELL usdcSize) + sum(REDEEM usdcSize)
pnl        = money_out - money_in

status:
  REDEEM exists                                    → won
  SELL exists, no REDEEM                           → exited
  no SELL, no REDEEM, last trade > 7 days old      → lost
  no SELL, no REDEEM, last trade within 7 days     → open
```

Why activity rather than the positions endpoint: positions vanish from the API after redeem, so any naive analysis using `/positions` undercounts wins. Always reconstruct from `/activity?type=TRADE` + `/activity?type=REDEEM` (separate calls — comma-joined types return 400).

## Persistence (Supabase)

The historical dataset is the moat. Every day the snapshot job pulls the leaderboard, scores the top N wallets, and persists three things: the score itself (`wallet_scores`), the wallet's open positions at that moment (`open_position_snapshots`), and the leaderboard as it stood (`leaderboard_snapshots`). After 90 days you can answer questions no one else can: "who has been consistently above edge 7 for the last quarter?", "which wallets just entered the top 50?", "show me everyone who held YES on this market three days before resolution."

### Setup

```bash
# 1. Create a Supabase project, get the URL and service_role key
cp .env.example .env  # fill in SUPABASE_URL and SUPABASE_KEY

# 2. Apply the schema (Supabase dashboard → SQL editor → paste db/schema.sql → run)
#    Or via psql:
#    psql "$DATABASE_URL" -f db/schema.sql

# 3. Run the snapshot job once to verify it writes:
python scripts/snapshot_job.py --top 10
```

If `SUPABASE_URL` and `SUPABASE_KEY` are unset, both the API and the snapshot job fall back to an in-memory repository — the suite still passes, the API still serves live scoring, but history endpoints will be empty until you wire up Supabase.

### Daily snapshot job

Schedule `python scripts/snapshot_job.py --top 50` daily (Railway cron, GitHub Actions, or Supabase pg_cron triggering an edge function — your call). The job is idempotent: running twice creates two snapshots, which is fine — history queries pick the closest one.

```bash
python scripts/snapshot_job.py --top 50              # production
python scripts/snapshot_job.py --top 5  --dry-run    # local testing, no writes
```

Each run records an audit row in `snapshot_runs` with start/finish times, wallets scored, and error count.

### Schema overview

| Table                       | Purpose                                          |
|-----------------------------|--------------------------------------------------|
| `wallets`                   | One row per wallet ever seen                     |
| `wallet_scores`             | Append-only score time series                    |
| `open_position_snapshots`   | What each wallet held at each tick               |
| `leaderboard_snapshots`     | Full leaderboard, preserved daily                |
| `snapshot_runs`             | Audit trail for the cron job                     |

Two views (`latest_wallet_scores`, `latest_leaderboard`) make the common "what's current" queries cheap.

### Repository pattern

`db/repository.py` defines a `Repository` protocol. Two implementations:

- `InMemoryRepository` — thread-safe, lossy across restarts. Used in tests and as the dev-mode fallback.
- `SupabaseRepository` — production. Wraps the supabase-py client.

The API and snapshot job depend only on the protocol. To swap Supabase for Neon or self-hosted Postgres, write one new class implementing the same six method signatures.

## Pricing dimensions

The endpoint split was designed so each tier maps cleanly to a billing model. Suggested ranges:

| Tier            | Endpoints                              | Suggested price          | Why                              |
|-----------------|----------------------------------------|--------------------------|----------------------------------|
| Discovery       | `/wallet/{address}`, `/leaderboard/*`  | $0.001–$0.01 / call      | Slow cache, mostly DB reads      |
| Monitoring      | `/wallet/{address}/positions`          | $0.01–$0.05 / call       | Fresh data, hits Polymarket each time |
| Streaming (v2)  | SSE feed of position changes           | $20–$100 / month flat    | Continuous fetch on our side     |
| History         | `/wallet/{address}/history` etc.       | $0.005 / call            | Pure DB read, value grows over time |

The streaming endpoint is the one serious copy-trading bots will actually pay for, but it requires a continuous-fetch worker on our side — leaving it for v2 once we have signal that the per-call business works.

## Deploy

### Railway

Push the repo, point at it. `railway.toml` handles the rest.

### Render / Heroku-style

`Procfile` is in place.

### Caching

`api/cache.py` is a thread-safe in-memory TTL cache with the same interface as a Redis client. For multi-worker production, swap the singleton for `redis.Redis()` in one file. TTLs:

- wallet score: 1h
- open positions only: 5m
- leaderboard: 30m
- verified leaderboard: 1h

## Testing

```bash
pytest -v
```

Synthetic fixtures in `tests/fixtures.py` mimic the three real wallet patterns from the research phase (phonesculptor MLB human, gabigol HFT bot, neutralwave23 tilt loser) plus a low-data newbie. Tests run against fixtures only — no live API calls — so the suite is deterministic and CI-safe.

## Distribution roadmap

1. **Now** — REST API on Railway, Supabase for daily snapshot persistence
2. **Next** — Publish to MCP Hub, Replit Agent Market, awesome-mcp-servers
3. **Later** — x402 micropayments per call (USDC), historical query endpoints (the moat: every day we run, the dataset grows)

## Verified wallet examples

These are the personas the test fixtures target. Live numbers will differ as activity changes:

| Wallet                                        | Score | Notes                                         |
|-----------------------------------------------|-------|-----------------------------------------------|
| `phonesculptor`                               | ~9/10 | MLB-focused human, distributed wins, real edge|
| `gabigol`                                     | bot   | Crypto 5-min Up/Down arb (edge largely dead post-Feb 2026) |
| `neutralwave23`                               | ~1/10 | Distributed tiny wins masking large net loss  |
