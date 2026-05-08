"""
snapshot_job.py — daily cron entry point.

Pulls the Polymarket leaderboard, scores the top N wallets, persists
both their scores and current open positions, and stores the leaderboard
itself as a snapshot.

Schedule on Railway / Render with a daily cron (or via Supabase pg_cron
calling an Edge Function that triggers this). Idempotent: running twice
in the same day just creates two snapshots, which is fine.

Usage:
    python scripts/snapshot_job.py                    # default: top 50
    python scripts/snapshot_job.py --top 100          # top 100
    python scripts/snapshot_job.py --top 50 --dry-run # don't persist

Env:
    SUPABASE_URL, SUPABASE_KEY  — if set, persists to Supabase.
                                   Otherwise persists to in-memory store
                                   (for dry-runs and local testing).
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make package importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import PolymarketClient, score_wallet
from db import (
    InMemoryRepository,
    Repository,
    get_repository,
)
from db.converters import (
    leaderboard_to_records,
    positions_to_snapshots,
    score_to_record,
)


def run_snapshot(
    repo: Repository,
    top_n: int = 50,
    inter_request_delay: float = 0.4,
    verbose: bool = True,
) -> dict:
    """
    Execute one snapshot pass. Returns a summary dict.

    Errors per wallet are caught and counted — one bad wallet doesn't
    abort the whole run.
    """
    started = datetime.now(timezone.utc)
    run_id = repo.start_snapshot_run(started)

    client = PolymarketClient()
    summary = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "top_n": top_n,
        "leaderboard_size": 0,
        "wallets_scored": 0,
        "errors": 0,
        "error_wallets": [],
    }

    try:
        # ── 1. Persist the full leaderboard
        if verbose:
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] fetching leaderboard…", flush=True)
        lb = client.get_leaderboard(limit=200)
        summary["leaderboard_size"] = int(len(lb))

        lb_records = leaderboard_to_records(lb, snapshot_at=started)
        repo.insert_leaderboard_snapshot(lb_records)
        if verbose:
            print(f"  → stored leaderboard: {len(lb_records)} entries", flush=True)

        # ── 2. Score top N wallets
        for rank, (_, row) in enumerate(lb.head(top_n).iterrows(), start=1):
            wallet = row.get("proxyWallet")
            username = row.get("userName")
            lb_pnl = float(row.get("pnl") or 0)
            if not wallet:
                continue

            try:
                if verbose:
                    print(f"  #{rank:3d}  {(username or wallet)[:20]:20s}  ", end="", flush=True)

                activity = client.get_activity(wallet)
                score = score_wallet(
                    activity,
                    wallet=str(wallet).lower(),
                    username=username,
                    leaderboard_pnl=lb_pnl,
                )

                # Reuse the same scored_at across all writes for this wallet
                # so they line up cleanly in time-series queries.
                scored_at = datetime.now(timezone.utc)

                repo.upsert_wallet(wallet=str(wallet).lower(), username=username, scored_at=scored_at)
                repo.insert_score(score_to_record(score, scored_at=scored_at))
                position_snapshots = positions_to_snapshots(score, snapshot_at=scored_at)
                if position_snapshots:
                    repo.insert_position_snapshots(position_snapshots)

                summary["wallets_scored"] += 1
                if verbose:
                    edge = f"{score.edge_score:>4.1f}" if score.edge_score is not None else " n/a"
                    print(
                        f"{score.classification:18s}  edge={edge}  "
                        f"opens={len(score.open_positions)}",
                        flush=True,
                    )

            except Exception as e:
                summary["errors"] += 1
                summary["error_wallets"].append({"wallet": wallet, "error": str(e)})
                if verbose:
                    print(f"ERROR — {e}", flush=True)

            time.sleep(inter_request_delay)

        notes = f"top_n={top_n}; lb_size={summary['leaderboard_size']}"
        repo.finish_snapshot_run(
            run_id=run_id,
            finished_at=datetime.now(timezone.utc),
            wallets_scored=summary["wallets_scored"],
            errors=summary["errors"],
            notes=notes,
        )

    except Exception as e:
        # Catastrophic failure — mark the run as failed and re-raise
        if verbose:
            print(f"\nFATAL: {e}", flush=True)
            traceback.print_exc()
        repo.finish_snapshot_run(
            run_id=run_id,
            finished_at=datetime.now(timezone.utc),
            wallets_scored=summary["wallets_scored"],
            errors=summary["errors"] + 1,
            notes=f"FATAL: {e}",
        )
        raise

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily Polymarket snapshot job.")
    parser.add_argument("--top", type=int, default=50, help="Score the top N leaderboard wallets.")
    parser.add_argument("--delay", type=float, default=0.4, help="Inter-request delay seconds.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use the in-memory repo regardless of env. Print summary at the end.")
    parser.add_argument("--quiet", action="store_true", help="Only print final summary.")
    args = parser.parse_args()

    repo = InMemoryRepository() if args.dry_run else get_repository()
    is_in_memory = isinstance(repo, InMemoryRepository)
    print(
        f"snapshot_job: backend={'in-memory (DRY RUN)' if is_in_memory else 'supabase'}, "
        f"top_n={args.top}",
        flush=True,
    )

    summary = run_snapshot(
        repo=repo,
        top_n=args.top,
        inter_request_delay=args.delay,
        verbose=not args.quiet,
    )

    print()
    print("─" * 60)
    print(f"started:        {summary['started_at']}")
    print(f"finished:       {summary['finished_at']}")
    print(f"wallets scored: {summary['wallets_scored']} / {args.top}")
    print(f"errors:         {summary['errors']}")
    if summary["errors"] > 0:
        for err in summary["error_wallets"][:5]:
            print(f"   {err['wallet']}: {err['error']}")
    print("─" * 60)

    sys.exit(0 if summary["errors"] < (args.top // 2) else 1)


if __name__ == "__main__":
    main()
