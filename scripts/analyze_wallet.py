"""
analyze_wallet.py — terminal CLI wrapping the scorer.

Usage:
    python scripts/analyze_wallet.py 0xf1528f12e645462c344799b62b1b421a6a4c64aa
    python scripts/analyze_wallet.py phonesculptor
    python scripts/analyze_wallet.py phonesculptor --json

Replaces the older deep_dive_v3.py — same data, same Polymarket endpoints,
but scored through the unified engine and (optionally) printed pretty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the package importable when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import PolymarketClient, score_wallet


def looks_like_address(s: str) -> bool:
    return s.startswith("0x") and len(s) == 42


def render_pretty(score) -> None:
    print()
    print("=" * 72)
    print(f"  {score.username or score.wallet}")
    print(f"  {score.wallet}")
    print("=" * 72)

    cls_emoji = {"human": "👤", "bot": "🤖", "insufficient_data": "❓"}.get(score.classification, "❓")
    edge = f"{score.edge_score}/10" if score.edge_score is not None else "n/a"
    print(f"  {cls_emoji}  classification: {score.classification:18s}  "
          f"confidence: {score.confidence:.0%}  edge: {edge}")

    if score.reason_codes:
        print(f"\n  reason codes:")
        for c in score.reason_codes:
            print(f"    • {c}")

    if score.signals:
        s = score.signals
        print(f"\n  signals:")
        print(f"    focus_ratio:    {s['focus_ratio']:>7.2f}   (>12 = bot)")
        print(f"    holding median: {s['holding_period']['median_seconds']:>7.0f}s  "
              f"(<60s = HFT, >3600s = human)")
        print(f"    timing CV:      {s['timing_cv']:>7.2f}   (<0.3 = scheduled)")
        print(f"    dominant cat:   {s['category']['dominant']:>7s}   "
              f"({s['category']['dominant_share']:.0%})")
        print(f"    win rate:       {s['win_rate']:>7.1%}")
        print(f"    PnL top-1:      {s['pnl_top1_share']:>7.1%}   (>40% = lucky concentration)")

    if score.performance:
        p = score.performance
        print(f"\n  performance:")
        print(f"    net realised PnL:   ${p['net_realised_pnl_usd']:>12,.0f}")
        if p.get("leaderboard_pnl_usd"):
            print(f"    leaderboard PnL:    ${p['leaderboard_pnl_usd']:>12,.0f}  "
                  f"(diff = open positions)")
        print(f"    wins / losses:      {p['wins']:>5} / {p['losses']:<5}  "
              f"(exited early: {p['exited_early']}, open: {p['open_positions_count']})")
        print(f"    avg / max bet:      ${p['avg_bet_usd']:,.0f}  /  ${p['max_bet_usd']:,.0f}")
        print(f"    active days:        {p['active_days']}  ({p['trades_per_day']:.1f} trades/day)")
        print(f"    first trade:        {p['first_trade_iso'][:10]}")
        print(f"    last trade:         {p['last_trade_iso'][:10]}")

    if score.open_positions:
        print(f"\n  open positions ({len(score.open_positions)} shown):")
        for pos in score.open_positions[:10]:
            title = pos["title"][:50]
            print(f"    ${pos['money_in_usd']:>9,.0f}  @ {pos['avg_entry_price']:.2f}  "
                  f"[{pos['outcome']:>3s}]  {title}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a Polymarket wallet.")
    parser.add_argument("target", help="Wallet address (0x...) or Polymarket username.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    args = parser.parse_args()

    client = PolymarketClient()
    if looks_like_address(args.target):
        wallet = args.target.lower()
        username = None
        lb_pnl = None
    else:
        lb = client.get_leaderboard(limit=200)
        wallet, lb_pnl = client.find_wallet(args.target, lb)
        if not wallet:
            print(f"⚠️  '{args.target}' not in top-200 leaderboard. "
                  f"Pass the wallet address directly.", file=sys.stderr)
            sys.exit(1)
        username = args.target

    if not args.json:
        print(f"🔎 Pulling activity for {username or wallet}...")

    activity = client.get_activity(wallet)
    score = score_wallet(activity, wallet=wallet, username=username, leaderboard_pnl=lb_pnl)

    if args.json:
        print(json.dumps(score.to_dict(), indent=2, default=str))
    else:
        render_pretty(score)


if __name__ == "__main__":
    main()
