-- ─────────────────────────────────────────────────────────────────────
-- Polymarket Wallet Intelligence — schema
-- Run once in Supabase SQL editor (or `psql -f db/schema.sql`).
--
-- Design notes
--   • numeric for money (no float rounding)
--   • jsonb for flexible signal blobs (the scoring engine evolves;
--     migrating typed columns every release is painful)
--   • indexes target the read paths actually used by the API:
--       - history for one wallet, ordered by time
--       - latest leaderboard snapshot
--       - "newly verified" / "fell off" diff queries
-- ─────────────────────────────────────────────────────────────────────

-- ── wallets: one row per wallet we have ever seen ─────────────────────
create table if not exists wallets (
    wallet            text primary key,
    username          text,
    first_seen_at     timestamptz not null default now(),
    last_scored_at    timestamptz,
    created_at        timestamptz not null default now()
);

create index if not exists idx_wallets_username     on wallets (lower(username));
create index if not exists idx_wallets_last_scored  on wallets (last_scored_at desc);


-- ── wallet_scores: append-only time series of scores ─────────────────
create table if not exists wallet_scores (
    id                   bigserial primary key,
    wallet               text not null references wallets(wallet) on delete cascade,
    scored_at            timestamptz not null default now(),
    classification       text not null,
    confidence           numeric(4,2) not null,
    edge_score           numeric(3,1),
    reason_codes         jsonb not null default '[]'::jsonb,
    signals              jsonb not null default '{}'::jsonb,
    performance          jsonb not null default '{}'::jsonb,
    leaderboard_pnl_usd  numeric(14,2),
    scoring_version      text not null
);

create index if not exists idx_scores_wallet_time   on wallet_scores (wallet, scored_at desc);
create index if not exists idx_scores_time          on wallet_scores (scored_at desc);
create index if not exists idx_scores_classification on wallet_scores (classification, scored_at desc);
create index if not exists idx_scores_edge          on wallet_scores (edge_score desc) where edge_score is not null;


-- ── open_position_snapshots: what each wallet held at each tick ──────
-- Volume here grows fast; partition by month later if it matters.
create table if not exists open_position_snapshots (
    id                bigserial primary key,
    wallet            text not null references wallets(wallet) on delete cascade,
    snapshot_at       timestamptz not null default now(),
    condition_id      text not null,
    title             text not null,
    outcome           text,
    category          text,
    money_in_usd      numeric(14,2) not null,
    avg_entry_price   numeric(6,4),
    num_buys          integer,
    first_trade_at    timestamptz,
    last_trade_at     timestamptz
);

create index if not exists idx_pos_wallet_time  on open_position_snapshots (wallet, snapshot_at desc);
create index if not exists idx_pos_condition    on open_position_snapshots (condition_id, snapshot_at desc);
create index if not exists idx_pos_time         on open_position_snapshots (snapshot_at desc);


-- ── leaderboard_snapshots: full leaderboard preserved daily ──────────
create table if not exists leaderboard_snapshots (
    id            bigserial primary key,
    snapshot_at   timestamptz not null default now(),
    rank          integer not null,
    wallet        text not null,
    username      text,
    pnl_usd       numeric(14,2) not null,
    volume_usd    numeric(14,2)
);

create index if not exists idx_lb_time         on leaderboard_snapshots (snapshot_at desc, rank);
create index if not exists idx_lb_wallet_time  on leaderboard_snapshots (wallet, snapshot_at desc);


-- ── snapshot_runs: audit trail for the daily job ─────────────────────
create table if not exists snapshot_runs (
    id              bigserial primary key,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    wallets_scored  integer default 0,
    errors          integer default 0,
    notes           text
);

create index if not exists idx_runs_time on snapshot_runs (started_at desc);


-- ── view: latest score per wallet (used by the verified leaderboard) ──
create or replace view latest_wallet_scores as
select distinct on (wallet)
    wallet, scored_at, classification, confidence, edge_score,
    reason_codes, signals, performance, leaderboard_pnl_usd, scoring_version
from wallet_scores
order by wallet, scored_at desc;


-- ── view: latest leaderboard snapshot ─────────────────────────────────
create or replace view latest_leaderboard as
select * from leaderboard_snapshots
where snapshot_at = (select max(snapshot_at) from leaderboard_snapshots)
order by rank;
