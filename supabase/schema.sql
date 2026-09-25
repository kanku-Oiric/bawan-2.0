-- ════════════════════════════════════════════════════════════════════════════
--  BAWAN 2.0 — Skema Supabase (db.ekonomi.pusat)
--  Jalankan sekali di Supabase Dashboard → SQL Editor → New query → Run.
--  Aman dijalankan ulang (pakai IF NOT EXISTS).
--
--  RLS diaktifkan TANPA policy: hanya key service_role / secret yang bisa
--  baca-tulis. Jadi SUPABASE_KEY di .env WAJIB key service_role (legacy)
--  atau secret key (sb_secret_...), BUKAN anon / publishable.
-- ════════════════════════════════════════════════════════════════════════════

-- Satu baris per (server, user). Wallet dalam mata uang resmi server tsb.
create table if not exists public.players (
    guild_id       bigint           not null,
    user_id        bigint           not null,
    stamina        double precision not null default 100,
    pickaxe_key    text             not null default 'copper_starter',
    wallet         double precision not null default 0,
    xp             bigint           not null default 0,
    level          integer          not null default 1,
    voice_seconds  double precision not null default 0,
    ore_bag        jsonb            not null default '[]'::jsonb,
    crystal_bag    jsonb            not null default '[]'::jsonb,
    updated_at     timestamptz      not null default now(),
    primary key (guild_id, user_id)
);

-- Satu mata uang resmi per server (CurrencyManifest dari currency_engine.py).
create table if not exists public.currencies (
    guild_id                     bigint           primary key,
    currency_name                text             not null,
    ticker                       text             not null unique,
    genesis_market_cap           double precision not null,
    total_supply                 double precision not null,
    circulating_supply           double precision not null,
    reserve_supply               double precision not null,
    exchange_rate_to_ua          double precision not null,
    geological_backing_value_ua  double precision not null,
    policy                       jsonb            not null,
    genesis_timestamp            timestamptz      not null,
    founding_geology_score       double precision not null,
    is_active                    boolean          not null default true,
    updated_at                   timestamptz      not null default now()
);

-- Konfigurasi voice tracker per server (/voiceconfig).
create table if not exists public.voice_config (
    guild_id                 bigint           primary key,
    notify_channel_id        bigint,
    reward_interval_minutes  integer          not null default 5
                             check (reward_interval_minutes between 1 and 1440),
    reward_amount            double precision not null default 10
                             check (reward_amount >= 0),
    block_self_mute_deaf     boolean          not null default true,
    block_afk_channel        boolean          not null default true,
    block_alone              boolean          not null default true,
    updated_at               timestamptz      not null default now()
);

-- ── Migrasi (aman dijalankan ulang) ───────────────────────────────────────
-- v2: pendaftaran auto mine (/automine daftar)
alter table public.players add column if not exists automine boolean not null default false;

-- ════════════════════════════════════════════════════════════════════════════
-- v3: STAGE 0 — registrasi server & world nonce (drand quicknet)
--   Dua tabel INSERT-ONLY.  "pending" = belum ada baris di world_nonces;
--   status diturunkan dari data, tidak pernah di-UPDATE.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.server_registry (
    guild_id           bigint      primary key,
    algo_version       text        not null,
    randomness_source  text        not null,   -- mis. drand-quicknet:<chain_hash>
    registered_at      timestamptz not null default now()   -- jam server DB, bukan klien
);

create table if not exists public.world_nonces (
    guild_id         bigint      primary key references public.server_registry (guild_id),
    drand_round      bigint      not null check (drand_round > 0),
    world_nonce      text        not null check (world_nonce ~ '^[0-9a-f]{64}$'),
    drand_signature  text        not null check (drand_signature ~ '^[0-9a-f]{96}$'),
    fetched_at       timestamptz not null default now(),
    -- drand quicknet: randomness = SHA-256(signature)
    constraint world_nonce_matches_signature
        check (world_nonce = encode(sha256(decode(drand_signature, 'hex')), 'hex'))
);

create or replace function public.forbid_mutation() returns trigger
language plpgsql as $$
begin
    raise exception 'Tabel % bersifat insert-only: % ditolak', TG_TABLE_NAME, TG_OP;
end $$;

drop trigger if exists server_registry_insert_only on public.server_registry;
create trigger server_registry_insert_only
    before update or delete on public.server_registry
    for each row execute function public.forbid_mutation();
drop trigger if exists server_registry_no_truncate on public.server_registry;
create trigger server_registry_no_truncate
    before truncate on public.server_registry
    for each statement execute function public.forbid_mutation();

drop trigger if exists world_nonces_insert_only on public.world_nonces;
create trigger world_nonces_insert_only
    before update or delete on public.world_nonces
    for each row execute function public.forbid_mutation();
drop trigger if exists world_nonces_no_truncate on public.world_nonces;
create trigger world_nonces_no_truncate
    before truncate on public.world_nonces
    for each statement execute function public.forbid_mutation();

-- Insert-or-get atomik: unique index yang menyerialkan panggilan paralel,
-- bukan cek-lalu-insert di Python.  Baris yang sudah ada dikembalikan apa adanya.
create or replace function public.register_server(
    p_guild_id bigint, p_algo_version text, p_source text
) returns setof public.server_registry
language sql volatile as $$
    insert into public.server_registry (guild_id, algo_version, randomness_source)
    values (p_guild_id, p_algo_version, p_source)
    on conflict (guild_id) do nothing;
    select * from public.server_registry where guild_id = p_guild_id;
$$;
revoke all on function public.register_server(bigint, text, text) from public, anon, authenticated;
grant execute on function public.register_server(bigint, text, text) to service_role;

alter table public.server_registry enable row level security;
alter table public.world_nonces    enable row level security;

-- ════════════════════════════════════════════════════════════════════════════
-- v4: COMMIT–REVEAL PEPPER, SAKSI EKSTERNAL, PENGERASAN HAK AKSES
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.world_commitments (
    algo_version       text        primary key check (algo_version ~ '^v[0-9]+$'),
    pepper_commitment  text        not null check (pepper_commitment ~ '^[0-9a-f]{64}$'),  -- SHA-256(pepper)
    committed_at       timestamptz not null default now()
);

-- Event yang SUDAH terkirim ke webhook saksi.  Pending = diturunkan dari
-- registry dikurangi tabel ini (lihat world_witness.py).
create table if not exists public.world_witness_log (
    event_key     text        primary key,       -- registered:<gid> | activated:<gid> | commitment:<v>
    event_id      text        not null check (event_id ~ '^[0-9a-f]{16}$'),
    delivered_at  timestamptz not null default now()
);

drop trigger if exists world_commitments_insert_only on public.world_commitments;
create trigger world_commitments_insert_only
    before update or delete on public.world_commitments
    for each row execute function public.forbid_mutation();
drop trigger if exists world_commitments_no_truncate on public.world_commitments;
create trigger world_commitments_no_truncate
    before truncate on public.world_commitments
    for each statement execute function public.forbid_mutation();

drop trigger if exists world_witness_log_insert_only on public.world_witness_log;
create trigger world_witness_log_insert_only
    before update or delete on public.world_witness_log
    for each row execute function public.forbid_mutation();
drop trigger if exists world_witness_log_no_truncate on public.world_witness_log;
create trigger world_witness_log_no_truncate
    before truncate on public.world_witness_log
    for each statement execute function public.forbid_mutation();

alter table public.world_commitments enable row level security;
alter table public.world_witness_log enable row level security;

-- Pertahanan berlapis: selain RLS tanpa policy, cabut SEMUA hak tabel dari
-- role publik Supabase.  Kalau suatu hari ada policy yang keliru dibuat,
-- anon/authenticated tetap tidak punya hak INSERT/UPDATE/DELETE/SELECT.
revoke all on table
    public.players, public.currencies, public.voice_config,
    public.server_registry, public.world_nonces,
    public.world_commitments, public.world_witness_log
from anon, authenticated;

-- ════════════════════════════════════════════════════════════════════════════
-- v5: COUNTER PERCOBAAN MINING (roll = mining_roll(seed, guild, user, node, n))
--   n di-increment atomik SEBELUM roll dihitung.  node_id ada di pre-image
--   roll, bukan di key counter.  Counter hanya boleh naik — tidak bisa
--   dimundurkan/dihapus untuk mengulang nomor percobaan yang hasilnya bagus.
-- ════════════════════════════════════════════════════════════════════════════
create table if not exists public.mining_attempts (
    guild_id    bigint      not null,
    user_id     bigint      not null,
    attempts    bigint      not null check (attempts > 0),
    updated_at  timestamptz not null default now(),
    primary key (guild_id, user_id)
);

create or replace function public.forbid_counter_rewind() returns trigger
language plpgsql as $$
begin
    if TG_OP = 'DELETE' then
        raise exception 'mining_attempts: DELETE ditolak (counter tidak boleh direset)';
    end if;
    if new.guild_id <> old.guild_id or new.user_id <> old.user_id or new.attempts <= old.attempts then
        raise exception 'mining_attempts: counter hanya boleh naik (% → %)', old.attempts, new.attempts;
    end if;
    return new;
end $$;

drop trigger if exists mining_attempts_monotonic on public.mining_attempts;
create trigger mining_attempts_monotonic
    before update or delete on public.mining_attempts
    for each row execute function public.forbid_counter_rewind();
drop trigger if exists mining_attempts_no_truncate on public.mining_attempts;
create trigger mining_attempts_no_truncate
    before truncate on public.mining_attempts
    for each statement execute function public.forbid_mutation();

-- Satu statement → atomik; row lock menyerialkan ayunan paralel user yang sama.
create or replace function public.next_mining_attempt(p_guild_id bigint, p_user_id bigint)
returns bigint
language sql volatile as $$
    insert into public.mining_attempts as m (guild_id, user_id, attempts)
    values (p_guild_id, p_user_id, 1)
    on conflict (guild_id, user_id)
    do update set attempts = m.attempts + 1, updated_at = now()
    returning m.attempts;
$$;
revoke all on function public.next_mining_attempt(bigint, bigint) from public, anon, authenticated;
grant execute on function public.next_mining_attempt(bigint, bigint) to service_role;

alter table public.mining_attempts enable row level security;
revoke all on table public.mining_attempts from anon, authenticated;

-- Laporan keamanan read-only (untuk `python db_ekonomi_pusat.py --security`).
create or replace function public.security_report() returns json
language sql stable as $$
    select json_build_object(
        'caller_role',         current_user,
        'caller_bypasses_rls', (select rolbypassrls from pg_roles where rolname = current_user),
        'tables', (
            select json_agg(json_build_object(
                'table',       c.relname,
                'rls_enabled', c.relrowsecurity,
                'policies',    (select count(*) from pg_policies p
                                where p.schemaname = 'public' and p.tablename = c.relname),
                'anon_insert', has_table_privilege('anon', c.oid, 'INSERT'),
                'anon_select', has_table_privilege('anon', c.oid, 'SELECT'),
                'auth_insert', has_table_privilege('authenticated', c.oid, 'INSERT')
            ) order by c.relname)
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'r'
        ),
        'functions', (
            select json_agg(json_build_object(
                'function',     p.oid::regprocedure::text,
                'anon_execute', has_function_privilege('anon', p.oid, 'EXECUTE'),
                'auth_execute', has_function_privilege('authenticated', p.oid, 'EXECUTE')
            ) order by p.oid::regprocedure::text)
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public'
        )
    );
$$;
revoke all on function public.security_report() from public, anon, authenticated;
grant execute on function public.security_report() to service_role;

alter table public.players      enable row level security;
alter table public.currencies   enable row level security;
alter table public.voice_config enable row level security;
