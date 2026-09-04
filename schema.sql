-- Bima Farm — Supabase schema.
-- Run this once in the Supabase SQL editor for a new project.
-- Hackathon-level RLS (open policies) — not production-grade, see README.

create table if not exists policies (
    id              bigint generated always as identity primary key,
    farmer_name     text not null,
    ward            int not null,
    municipality    text not null,
    district        text not null default 'Dhanusa',
    station         text not null,
    hazard_type     text not null,                 -- 'flood' | 'drought'
    triggers        jsonb not null,                 -- [{parameter, operator, threshold}, ...]
    land_kattha     numeric,                        -- insured land size (Terai unit); drives payout_amount
    payout_amount   numeric not null,
    premium         numeric not null,
    phone           text,
    active          boolean not null default true
);
-- If this table was already created before land_kattha existed, run:
--   alter table policies add column if not exists land_kattha numeric;

create table if not exists readings (
    id          bigint generated always as identity primary key,
    station     text not null,
    parameter   text not null,                      -- 'river_level' | 'rainfall_3day' | 'rainfall_30day'
    value       numeric not null,
    unit        text not null default 'm',
    trend       text,
    source      text not null default 'seeded',      -- 'seeded' | 'manual entry' | 'simulated_spike'
    "timestamp" timestamptz not null default now(),
    raw_text    text
);

create table if not exists claims (
    id                  bigint generated always as identity primary key,
    policy_id           bigint references policies(id),
    hazard_type         text not null,
    station             text not null,
    farmer_name         text not null,
    ward                int,
    municipality        text,
    phone               text,
    checked_parameters  jsonb not null,               -- [{name, value, threshold, operator, met}, ...]
    confidence          numeric not null,
    borderline          boolean not null default false,
    claim_text          text not null,
    recommended_amount  numeric not null,
    source              text not null default 'ai',   -- 'ai' | 'rule_based' | 'rule_based_fallback'
    run_id              text,
    status              text not null default 'Pending Review',
    sms_text            text,
    voice_file          text,
    voice_engine        text,
    dispatch_status     text,                          -- 'Sandbox Dispatch' once confirmed
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create table if not exists audit_log (
    id                bigint generated always as identity primary key,
    claim_id          bigint references claims(id),
    action            text not null, -- 'approve' | 'reject'
    officer_name      text not null,
    reason            text,
    ai_recommendation jsonb,
    "timestamp"       timestamptz not null default now()
);

create table if not exists trace_log (
    id         bigint generated always as identity primary key,
    run_id     text not null,
    ts         timestamptz not null default now(),
    step_type  text not null,
    message    text not null,
    line       text not null
);

create index if not exists idx_claims_policy_hazard on claims (policy_id, hazard_type, created_at);
create index if not exists idx_readings_station_param_ts on readings (station, parameter, "timestamp");
create index if not exists idx_trace_run on trace_log (run_id, ts);

-- Hackathon-only: open row-level security so the sandbox key can read/write
-- without per-user auth. Do NOT ship this as-is to production.
alter table policies enable row level security;
alter table readings enable row level security;
alter table claims enable row level security;
alter table audit_log enable row level security;
alter table trace_log enable row level security;

create policy "sandbox_open_policies" on policies for all using (true) with check (true);
create policy "sandbox_open_readings" on readings for all using (true) with check (true);
create policy "sandbox_open_claims" on claims for all using (true) with check (true);
create policy "sandbox_open_audit" on audit_log for all using (true) with check (true);
create policy "sandbox_open_trace" on trace_log for all using (true) with check (true);
