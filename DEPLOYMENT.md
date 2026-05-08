# Deployment Runbook

Step-by-step from zero to listed on three MCP marketplaces. Total time: **~2 hours active work** (most of it waiting for things to propagate).

The path:

```
GitHub repo
    ├── Railway deploy              (REST API + MCP HTTP transport)
    ├── Supabase setup              (persistence)
    ├── Daily snapshot cron         (the moat)
    ├── PyPI publish                (lets users install MCP server locally)
    ├── Official MCP Registry       (server.json → registry.modelcontextprotocol.io)
    ├── Smithery                    (smithery.yaml → smithery.ai)
    └── Glama, mcp.so, awesome-mcp  (auto-index from GitHub repo)
```

---

## Phase 1: Hosting (30 min)

### 1.1 Push to GitHub

```bash
gh repo create polymarket-intel --public --source=. --push
```

The repo must be public for Glama / awesome-mcp / the official registry to index it.

### 1.2 Set up Supabase

1. Sign up at supabase.com, create a project (free tier is enough to start).
2. **SQL Editor → New query → paste `db/schema.sql` → Run**.
3. Project Settings → API → copy:
   - `URL` → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_KEY` (use the service-role here; the snapshot job needs writes)

### 1.3 Deploy to Railway

1. Sign up at railway.com, "Deploy from GitHub repo" → pick polymarket-intel.
2. Railway auto-detects the `Dockerfile`. If it picks Nixpacks instead, just set the builder to Dockerfile in service settings.
3. **Variables tab** — add:
   - `SUPABASE_URL` = (from 1.2)
   - `SUPABASE_KEY` = (service-role from 1.2)
   - `API_KEYS` = (leave empty for now; add later when you start charging)
4. **Networking tab** → "Generate domain" → you get something like `polymarket-intel-production.up.railway.app`.
5. Wait ~2 min for first deploy. Visit the domain — you should see the JSON health page.

### 1.4 Verify the snapshot job works

Run it once manually before scheduling. From your laptop:

```bash
SUPABASE_URL=... SUPABASE_KEY=... python scripts/snapshot_job.py --top 10
```

Should print one row per wallet, end with "wallets_scored: 10". Confirm in Supabase dashboard → Table editor → `wallet_scores` has 10 rows.

### 1.5 Schedule the snapshot job

Two options:

**Option A — Railway cron** (simplest):
1. New service in the same project → Empty service → Source = same GitHub repo, same Dockerfile.
2. Settings → Custom Start Command: `python scripts/snapshot_job.py --top 50`
3. Settings → Cron Schedule: `0 8 * * *` (daily at 8am UTC)
4. Same env vars as the API service.

**Option B — GitHub Actions** (free, uses GitHub's runners):

Create `.github/workflows/snapshot.yml`:
```yaml
name: Daily snapshot
on:
  schedule: [{ cron: "0 8 * * *" }]
  workflow_dispatch:
jobs:
  snapshot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python scripts/snapshot_job.py --top 50
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
```

Add the two secrets in repo settings → Secrets and variables → Actions.

---

## Phase 2: Publish the local MCP package (45 min)

Even though the API is hosted, many users will want to run the MCP server locally with stdio (Claude Desktop, Cursor). For that, the package needs to be on PyPI so they can `pip install` it.

### 2.1 Create a `pyproject.toml`

(Created already in this repo; if you want to publish under a different name, edit there.)

### 2.2 Build and publish

```bash
pip install build twine
python -m build                  # creates dist/polymarket_intel_mcp-1.0.0-*.whl
twine upload dist/*              # prompts for PyPI credentials
```

You'll need a PyPI account first; get an API token at pypi.org/manage/account/.

### 2.3 Add the `mcp-name` marker

The official registry verifies that your PyPI package matches your `server.json` namespace. Add to your README (in the published package):

```html
<!-- mcp-name: io.github.YOUR_USERNAME/polymarket-intel -->
```

This can be in an HTML comment so it's invisible.

---

## Phase 3: Listing — official MCP Registry (15 min)

The canonical source of truth. Listing here gets you indexed by Claude Desktop, Cursor, and most other MCP clients.

### 3.1 Edit `server.json`

Replace `YOUR_GITHUB_USERNAME` everywhere. Update the `remotes[0].url` to your Railway domain. Bump `version` if you've shipped changes.

### 3.2 Install the publisher CLI

```bash
# macOS
brew install mcp-publisher

# or download from
# https://github.com/modelcontextprotocol/registry/releases
```

### 3.3 Authenticate + publish

```bash
mcp-publisher login github
# → opens a browser, GitHub OAuth flow
mcp-publisher publish --dry-run   # validate first
mcp-publisher publish
```

If successful: `Server io.github.YOUR_USERNAME/polymarket-intel version 1.0.0` is live.

Verify:
```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=polymarket-intel"
```

---

## Phase 4: Listing — Smithery (10 min)

### 4.1 Push smithery.yaml to GitHub root

(Already in the repo.)

### 4.2 Submit

Two paths:

**Web dashboard**: smithery.ai/dashboard → "New Server" → connect GitHub → pick repo → Smithery reads smithery.yaml automatically.

**CLI**:
```bash
npm install -g @smithery/cli
smithery mcp publish https://your-railway-domain/mcp -n YOUR_USERNAME/polymarket-intel
```

Smithery will run a quality probe — a few automated MCP calls to verify the server responds. Make sure the API is up and the `/mcp` endpoint responds before publishing.

---

## Phase 5: Auto-indexed marketplaces (5 min)

These crawl GitHub. You don't submit; they find you. But submitting accelerates discovery.

### 5.1 Glama

`glama.json` is already in the repo. Glama auto-indexes new MCP servers nightly. If you want to claim/manage your listing manually, sign up at glama.ai.

### 5.2 mcp.so

Visit mcp.so/submit, paste your repo URL. Manual review, usually live within a day.

### 5.3 awesome-mcp-servers

Open a PR on https://github.com/punkpeye/awesome-mcp-servers adding an entry under the right category (Finance / Data Analysis). Format:

```
- [polymarket-intel](https://github.com/YOUR_USERNAME/polymarket-intel) - Classify Polymarket wallets as human or bot, score their trading edge, and stream their current open positions.
```

### 5.4 PulseMCP

Visit pulsemcp.com/submit, similar to mcp.so.

---

## Phase 6: Verify discoverability (10 min)

Open Claude Desktop, edit `~/Library/Application Support/Claude/claude_desktop_config.json` to add the *remote* server:

```json
{
  "mcpServers": {
    "polymarket-intel": {
      "url": "https://your-railway-domain/mcp"
    }
  }
}
```

Restart Claude Desktop. Ask Claude: *"Score the Polymarket wallet 0xf1528f12e645462c344799b62b1b421a6a4c64aa"*. Claude should pick `score_polymarket_wallet` from the registered tools and call it.

If that works, the loop is closed end-to-end. Your service is discoverable, callable, persisting, and growing the dataset daily.

---

## Operational notes

### Adding paid customers later

When you're ready to charge:

1. Create a key generation script (5 lines: `f"pmi_{secrets.token_urlsafe(24)}"`).
2. Add the key to Railway's `API_KEYS` env var: `API_KEYS=pmi_abc...:starter,pmi_xyz...:pro`.
3. Send the customer their key out-of-band.
4. They include `X-API-Key: pmi_abc...` in requests. Higher rate limit applies automatically.

For self-service signup (Stripe checkout → key emailed), add a `/auth/keys` endpoint backed by a `api_keys` table in Supabase. Keep the env-var path for grandfathered keys.

### Monitoring

- Railway has a logs tab — check it daily for the first week.
- The `/snapshots/latest` endpoint is your "is the cron working" canary.
- Supabase's Database → Reports tab tracks query count and storage growth.

### When to alarm

Realistic v1 alarms:
- `/snapshots/latest` returns `null` or `> 26 hours old` → cron is broken
- 5xx rate > 1% on Railway logs → upstream Polymarket issue or your code
- Supabase storage > 80% of free tier (8GB) → time to partition `open_position_snapshots` by month

---

## Distribution playbook (after technical launch)

The artifact-shipping above is necessary but not sufficient. Real traction comes from:

1. **README that scores high in answer engines.** When Claude / GPT / Cursor searches for "Polymarket trader analysis", does our README rank? Use clear structured prose, not marketing fluff. Concrete claims, copy-pasteable code, screenshots of agent responses calling the tool.

2. **Founders who ship copy-trading bots.** They'll be early users. DM them when they post their bots — "your strategy needs a wallet quality filter; here's mine."

3. **Tweet thread per scored wallet.** "We scored the top 50 Polymarket wallets. 23 are bots. Here are the 5 humans actually beating the market." This kind of content drives both API traffic and inbound interest.

4. **Be the cited source for Polymarket research.** When someone writes a Substack or a paper about prediction-market behavior, we want to be the data layer they cite.

The build is done. The directory listings get you on the shelf. The above is what gets you off the shelf.
