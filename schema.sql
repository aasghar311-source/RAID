-- RAID — Rapid AI Decision Engine — database schema
-- Run this once in the Supabase SQL Editor (anon key cannot issue DDL).
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.

create extension if not exists "pgcrypto";

create table if not exists trades (
    id uuid primary key default gen_random_uuid(),
    bot_name text,
    market text,
    symbol text,
    direction text,
    entry_price float,
    exit_price float,
    size_usd float,
    confidence float,
    pnl float default 0,
    status text default 'open',
    open_time timestamptz default now(),
    close_time timestamptz,
    close_reason text,
    paper_mode boolean default true,
    sl float,
    tp float
);

create table if not exists equity_snapshots (
    id uuid primary key default gen_random_uuid(),
    equity float,
    daily_pnl float default 0,
    timestamp timestamptz default now(),
    paper_mode boolean default true
);

create table if not exists signals (
    id uuid primary key default gen_random_uuid(),
    market text,
    symbol text,
    direction text,
    confidence float,
    news_sentiment text,
    technical_score float,
    ai_validated boolean default false,
    ai_decision text,
    rejected_reason text,
    entered_trade boolean default false,
    timestamp timestamptz default now()
);

create table if not exists brain_decisions (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid,
    prompt_tokens int,
    response_tokens int,
    cost_usd float,
    decision text,
    reasoning text,
    timestamp timestamptz default now()
);

create table if not exists daily_stats (
    id uuid primary key default gen_random_uuid(),
    date date unique,
    total_trades int default 0,
    wins int default 0,
    losses int default 0,
    pnl float default 0,
    win_rate float default 0,
    ai_spend float default 0,
    paper_mode boolean default true
);

create table if not exists kill_switch (
    id uuid primary key default gen_random_uuid(),
    active boolean default false,
    reason text,
    activated_at timestamptz,
    activated_by text
);

create table if not exists learning_adjustments (
    id uuid primary key default gen_random_uuid(),
    market text,
    signal_type text,
    old_weight float,
    new_weight float,
    win_rate float,
    sample_size int,
    applied_at timestamptz default now()
);
