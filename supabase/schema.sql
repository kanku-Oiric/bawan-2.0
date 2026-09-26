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

-- register_server() (insert-or-get atomik) didefinisikan di v6.

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

-- ════════════════════════════════════════════════════════════════════════════
-- v6: WORLDGEN_VERSION — versi pembangkit dunia, dikunci per server
--   Terpisah dari algo_version (cara seed dibuat).  Baris yang sudah ada
--   otomatis ditandai 'dev' — ADD COLUMN tidak memicu trigger UPDATE, jadi
--   aman untuk tabel insert-only.  Default langsung dicabut: registrasi baru
--   WAJIB menyebut versinya sendiri lewat register_server().
-- ════════════════════════════════════════════════════════════════════════════
alter table public.server_registry add column if not exists worldgen_version text not null default 'dev';
alter table public.server_registry alter column worldgen_version drop default;
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'server_registry_worldgen_version_format') then
        alter table public.server_registry add constraint server_registry_worldgen_version_format
            check (worldgen_version ~ '^(dev|v[1-9][0-9]*)$');
    end if;
end $$;

-- Insert-or-get atomik: unique index yang menyerialkan panggilan paralel,
-- bukan cek-lalu-insert di Python.  Baris yang sudah ada dikembalikan apa
-- adanya — worldgen_version server lama TIDAK ikut berubah.
drop function if exists public.register_server(bigint, text, text);
create or replace function public.register_server(
    p_guild_id bigint, p_algo_version text, p_source text, p_worldgen_version text
) returns setof public.server_registry
language sql volatile as $$
    insert into public.server_registry (guild_id, algo_version, randomness_source, worldgen_version)
    values (p_guild_id, p_algo_version, p_source, p_worldgen_version)
    on conflict (guild_id) do nothing;
    select * from public.server_registry where guild_id = p_guild_id;
$$;
revoke all on function public.register_server(bigint, text, text, text) from public, anon, authenticated;
grant execute on function public.register_server(bigint, text, text, text) to service_role;

-- ════════════════════════════════════════════════════════════════════════════
-- v7: SALDO OTORITATIF DI DB
--   • Setiap perubahan saldo = SATU fungsi = SATU transaksi + SATU baris
--     ledger (insert-only).  players.wallet hanya bisa berubah dari dalam
--     bawan_ledger_post() — trigger menolak jalur lain, termasuk service_role.
--   • Barang = baris `items` (insert-only); terjual = baris `item_disposals`
--     (PK item_id → satu barang hanya bisa dijual sekali).
--   • Setiap ayunan tercatat di `mining_results` (log per ayunan).
--   • Stamina & batas ayunan dibaca dari production_policy: angkanya bisa
--     diganti nanti TANPA mengubah skema.
--   URUTAN DEPLOY: matikan bot lama → jalankan file ini → start bot baru.
-- ════════════════════════════════════════════════════════════════════════════

-- 1. Uang eksak + stamina bertimestamp (jam DB)
do $$
begin
    if (select data_type from information_schema.columns
        where table_schema = 'public' and table_name = 'players' and column_name = 'wallet') <> 'numeric' then
        alter table public.players alter column wallet type numeric(24,4) using round(wallet::numeric, 4);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'players_wallet_nonnegative') then
        alter table public.players add constraint players_wallet_nonnegative check (wallet >= 0);
    end if;
end $$;
alter table public.players alter column wallet set default 0;
alter table public.players add column if not exists stamina_at timestamptz not null default now();

-- 2. Ledger
create table if not exists public.ledger (
    id             bigserial      primary key,
    ref            text           not null unique check (length(ref) between 3 and 256),
    guild_id       bigint         not null,
    user_id        bigint         not null,
    kind           text           not null check (kind ~ '^[a-z_]{1,32}$'),
    amount         numeric(24,4)  not null check (amount <> 0),
    balance_after  numeric(24,4)  not null check (balance_after >= 0),
    item_id        text,
    created_at     timestamptz    not null default now()
);
create index if not exists ledger_by_player on public.ledger (guild_id, user_id, id);

-- 3. Barang & pelepasannya
create table if not exists public.items (
    item_id     text        primary key check (item_id ~ '^(swing|legacy):'),
    guild_id    bigint      not null,
    owner_id    bigint      not null,
    kind        text        not null check (kind in ('ore', 'crystal')),
    item_uuid   text        not null,     -- hash isi dari ore.py/crystal.py — BUKAN kunci
    payload     jsonb       not null,     -- item.to_dict(): fakta mentah
    created_at  timestamptz not null default now()
);
create index if not exists items_by_owner on public.items (guild_id, owner_id);

create table if not exists public.item_disposals (
    item_id     text        primary key references public.items (item_id),
    kind        text        not null check (kind ~ '^[a-z_]{1,32}$'),
    ledger_ref  text        not null unique references public.ledger (ref),
    created_at  timestamptz not null default now()
);

-- 4. Log per ayunan
create table if not exists public.mining_results (
    guild_id          bigint           not null,
    user_id           bigint           not null,
    attempt           bigint           not null check (attempt > 0),
    node_id           text             not null,
    success           boolean          not null,
    critical_hit      boolean          not null,
    amount_extracted  double precision not null check (amount_extracted >= 0),
    stamina_before    double precision not null,
    stamina_consumed  double precision not null check (stamina_consumed >= 0),
    stamina_after     double precision not null check (stamina_after >= 0),
    item_id           text             references public.items (item_id),
    created_at        timestamptz      not null default now(),
    primary key (guild_id, user_id, attempt)
);
create index if not exists mining_results_recent on public.mining_results (guild_id, user_id, created_at);

-- 5. Batch reward voice (idempotensi satu tick)
create table if not exists public.voice_ticks (
    ref         text        primary key check (length(ref) between 3 and 128),
    guild_id    bigint      not null,
    created_at  timestamptz not null default now()
);

-- 6. Kebijakan produksi — satu baris; angka diubah lewat UPDATE, skema tetap
create table if not exists public.production_policy (
    scope                     text             primary key check (scope = 'global'),
    stamina_cap               double precision not null default 100 check (stamina_cap > 0),
    stamina_regen_per_second  double precision not null default 0   check (stamina_regen_per_second >= 0),
    max_swings_per_minute     integer          check (max_swings_per_minute is null or max_swings_per_minute > 0),
    rest_enabled              boolean          not null default true,
    updated_at                timestamptz      not null default now()
);
insert into public.production_policy (scope) values ('global') on conflict (scope) do nothing;

-- Insert-only: ledger, items, item_disposals, mining_results, voice_ticks.
-- production_policy: UPDATE boleh (angka kebijakan), DELETE/TRUNCATE tidak.
do $$
declare t text;
begin
    foreach t in array array['ledger', 'items', 'item_disposals', 'mining_results', 'voice_ticks'] loop
        execute format('drop trigger if exists %I on public.%I', t || '_insert_only', t);
        execute format('create trigger %I before update or delete on public.%I
                        for each row execute function public.forbid_mutation()', t || '_insert_only', t);
    end loop;
    foreach t in array array['ledger', 'items', 'item_disposals', 'mining_results', 'voice_ticks',
                             'production_policy', 'players'] loop
        execute format('drop trigger if exists %I on public.%I', t || '_no_truncate', t);
        execute format('create trigger %I before truncate on public.%I
                        for each statement execute function public.forbid_mutation()', t || '_no_truncate', t);
    end loop;
end $$;
drop trigger if exists production_policy_no_delete on public.production_policy;
create trigger production_policy_no_delete before delete on public.production_policy
    for each row execute function public.forbid_mutation();

-- 7. Migrasi data lama (aman diulang)
--    Saldo lama → satu baris 'opening' — HANYA untuk pemain yang belum punya
--    baris ledger sama sekali (pemain baru setelah v7 selalu punya ledger).
insert into public.ledger (ref, guild_id, user_id, kind, amount, balance_after)
select 'opening:' || p.guild_id || ':' || p.user_id, p.guild_id, p.user_id, 'opening', p.wallet, p.wallet
from public.players p
where p.wallet <> 0
  and not exists (select 1 from public.ledger l where l.guild_id = p.guild_id and l.user_id = p.user_id)
on conflict (ref) do nothing;
--    Isi tas JSON lama → items.  Kolom JSON dibiarkan (tidak dihapus), tidak ditulis lagi.
insert into public.items (item_id, guild_id, owner_id, kind, item_uuid, payload)
select 'legacy:' || p.guild_id || ':' || p.user_id || ':ore:' || e.ord, p.guild_id, p.user_id, 'ore',
       coalesce(e.v ->> 'item_uuid', ''), e.v
from public.players p, jsonb_array_elements(p.ore_bag) with ordinality as e(v, ord)
on conflict (item_id) do nothing;
insert into public.items (item_id, guild_id, owner_id, kind, item_uuid, payload)
select 'legacy:' || p.guild_id || ':' || p.user_id || ':crystal:' || e.ord, p.guild_id, p.user_id, 'crystal',
       coalesce(e.v ->> 'item_uuid', ''), e.v
from public.players p, jsonb_array_elements(p.crystal_bag) with ordinality as e(v, ord)
on conflict (item_id) do nothing;

-- 8. Helper internal di skema TERPISAH yang tidak diekspos PostgREST: tidak
--    bisa dipanggil lewat REST (termasuk dengan service_role), hanya dari
--    dalam fungsi RPC di bawah.
create schema if not exists bawan_private;
revoke all on schema bawan_private from public, anon, authenticated;
grant usage on schema bawan_private to service_role;

-- Penjaga players: wallet hanya lewat ledger; baris pemain tidak bisa dihapus
create or replace function bawan_private.guard_player() returns trigger
language plpgsql as $$
begin
    if TG_OP = 'DELETE' then
        raise exception 'bawan:players_delete_forbidden';
    end if;
    if coalesce(current_setting('bawan.ledger', true), '') <> 'on' then
        if TG_OP = 'INSERT' and new.wallet <> 0 then
            raise exception 'bawan:wallet_outside_ledger';
        elsif TG_OP = 'UPDATE' and new.wallet is distinct from old.wallet then
            raise exception 'bawan:wallet_outside_ledger';
        end if;
    end if;
    return new;
end $$;
drop trigger if exists players_guard on public.players;
create trigger players_guard before insert or update or delete on public.players
    for each row execute function bawan_private.guard_player();

create or replace function bawan_private.isqrt(n bigint) returns bigint
language plpgsql immutable strict as $$
declare r bigint;
begin
    if n < 0 then raise exception 'bawan:isqrt_negative'; end if;
    r := floor(sqrt(n::numeric))::bigint;
    while r * r > n loop r := r - 1; end loop;
    while (r + 1) * (r + 1) <= n loop r := r + 1; end loop;
    return r;
end $$;

-- = voice_engine.level_for_xp (LEVEL_XP_BASE = 100)
create or replace function bawan_private.level_for_xp(p_xp bigint) returns integer
language sql immutable strict as $$
    select (bawan_private.isqrt(greatest(p_xp, 0) / 100) + 1)::integer
$$;

-- stamina = min(cap, tersimpan + rate × Δt), Δt dari jam DB
create or replace function bawan_private.stamina_now(p_stamina double precision, p_at timestamptz)
returns double precision language sql stable as $$
    select least(pp.stamina_cap,
                 p_stamina + pp.stamina_regen_per_second
                             * greatest(0, extract(epoch from (now() - p_at)))::double precision)
    from public.production_policy pp where pp.scope = 'global'
$$;

create or replace function bawan_private.ensure_player(p_guild_id bigint, p_user_id bigint) returns void
language sql volatile as $$
    insert into public.players (guild_id, user_id) values (p_guild_id, p_user_id)
    on conflict (guild_id, user_id) do nothing;
$$;

create or replace function bawan_private.swings_last_minute(p_guild_id bigint, p_user_id bigint) returns bigint
language sql stable as $$
    select count(*) from public.mining_results r
    where r.guild_id = p_guild_id and r.user_id = p_user_id and r.created_at > now() - interval '1 minute'
$$;

-- Satu-satunya jalan wallet berubah.  Dipanggil HANYA dari fungsi di bawah.
create or replace function bawan_private.ledger_post(
    p_ref text, p_guild_id bigint, p_user_id bigint, p_kind text, p_amount numeric, p_item_id text
) returns numeric language plpgsql volatile as $$
declare v_balance numeric(24,4);
begin
    if p_amount is null or p_amount = 0 or p_amount <> round(p_amount, 4) then
        raise exception 'bawan:bad_amount';
    end if;
    perform bawan_private.ensure_player(p_guild_id, p_user_id);
    perform set_config('bawan.ledger', 'on', true);
    update public.players set wallet = wallet + p_amount, updated_at = now()
        where guild_id = p_guild_id and user_id = p_user_id
        returning wallet into v_balance;               -- CHECK wallet >= 0 menolak saldo negatif
    perform set_config('bawan.ledger', 'off', true);
    insert into public.ledger (ref, guild_id, user_id, kind, amount, balance_after, item_id)
        values (p_ref, p_guild_id, p_user_id, p_kind, p_amount, v_balance, p_item_id);
    return v_balance;
end $$;

-- 9. RPC untuk bot
-- Ayunan tahap 1: batas ayunan → counter n naik (atomik) → stamina saat ini.
create or replace function public.begin_swing(p_guild_id bigint, p_user_id bigint)
returns table (attempt bigint, stamina double precision)
language plpgsql volatile as $$
#variable_conflict use_column
declare v_policy public.production_policy%rowtype; v_player public.players%rowtype; v_n bigint;
begin
    select * into strict v_policy from public.production_policy where scope = 'global';
    perform bawan_private.ensure_player(p_guild_id, p_user_id);
    select * into strict v_player from public.players
        where guild_id = p_guild_id and user_id = p_user_id for update;       -- serialkan per pemain
    if v_policy.max_swings_per_minute is not null
       and bawan_private.swings_last_minute(p_guild_id, p_user_id) >= v_policy.max_swings_per_minute then
        raise exception 'bawan:rate_limited';
    end if;
    v_n := public.next_mining_attempt(p_guild_id, p_user_id);
    return query select v_n, bawan_private.stamina_now(v_player.stamina, v_player.stamina_at);
end $$;

-- Ayunan tahap 2: catat hasil (sekali per n) + stamina + barang, satu transaksi.
create or replace function public.record_swing(
    p_guild_id bigint, p_user_id bigint, p_attempt bigint, p_node_id text,
    p_success boolean, p_critical_hit boolean, p_amount_extracted double precision,
    p_stamina_consumed double precision,
    p_item_kind text, p_item_uuid text, p_item_payload jsonb
) returns table (stamina_after double precision, item_id text, already boolean)
language plpgsql volatile as $$
#variable_conflict use_column
declare
    v_policy public.production_policy%rowtype; v_player public.players%rowtype;
    v_prev public.mining_results%rowtype; v_counter bigint;
    v_before double precision; v_after double precision; v_item text;
begin
    select * into v_prev from public.mining_results r
        where r.guild_id = p_guild_id and r.user_id = p_user_id and r.attempt = p_attempt;
    if found then                                            -- retry setelah timeout: kembalikan yang tercatat
        return query select v_prev.stamina_after, v_prev.item_id, true;
        return;
    end if;
    select m.attempts into v_counter from public.mining_attempts m
        where m.guild_id = p_guild_id and m.user_id = p_user_id;
    if v_counter is null or p_attempt < 1 or p_attempt > v_counter then
        raise exception 'bawan:attempt_not_issued';
    end if;
    if p_stamina_consumed is null or p_stamina_consumed < 0 or p_amount_extracted is null or p_amount_extracted < 0 then
        raise exception 'bawan:bad_amount';
    end if;
    select * into strict v_policy from public.production_policy where scope = 'global';
    select * into strict v_player from public.players
        where guild_id = p_guild_id and user_id = p_user_id for update;
    if v_policy.max_swings_per_minute is not null
       and bawan_private.swings_last_minute(p_guild_id, p_user_id) >= v_policy.max_swings_per_minute then
        raise exception 'bawan:rate_limited';
    end if;
    v_before := bawan_private.stamina_now(v_player.stamina, v_player.stamina_at);
    if p_stamina_consumed > v_before + 1e-9 then
        raise exception 'bawan:insufficient_stamina';
    end if;
    v_after := greatest(0, v_before - p_stamina_consumed);
    update public.players set stamina = v_after, stamina_at = now(), updated_at = now()
        where guild_id = p_guild_id and user_id = p_user_id;
    if p_item_payload is not null then
        if p_item_kind not in ('ore', 'crystal') or p_item_uuid is null then
            raise exception 'bawan:bad_item';
        end if;
        v_item := 'swing:' || p_guild_id || ':' || p_user_id || ':' || p_attempt;
        insert into public.items (item_id, guild_id, owner_id, kind, item_uuid, payload)
            values (v_item, p_guild_id, p_user_id, p_item_kind, p_item_uuid, p_item_payload);
    end if;
    insert into public.mining_results (guild_id, user_id, attempt, node_id, success, critical_hit,
                                       amount_extracted, stamina_before, stamina_consumed, stamina_after, item_id)
        values (p_guild_id, p_user_id, p_attempt, p_node_id, p_success, p_critical_hit,
                p_amount_extracted, v_before, p_stamina_consumed, v_after, v_item);
    return query select v_after, v_item, false;
end $$;

-- Jual barang ke NPC: kunci barang → pelepasan + ledger + wallet, satu transaksi.
create or replace function public.sell_item(
    p_guild_id bigint, p_user_id bigint, p_item_id text, p_payout numeric, p_kind text
) returns table (ledger_ref text, balance text, already boolean)
language plpgsql volatile as $$
#variable_conflict use_column
declare v_ref text := 'sell:' || p_item_id; v_item public.items%rowtype; v_balance numeric;
begin
    if p_kind not in ('sell_ore', 'sell_crystal') then raise exception 'bawan:bad_kind'; end if;
    if p_payout is null or p_payout <= 0 then raise exception 'bawan:bad_amount'; end if;
    select * into v_item from public.items i where i.item_id = p_item_id for update;   -- serialkan per barang
    if not found or v_item.guild_id <> p_guild_id or v_item.owner_id <> p_user_id then
        raise exception 'bawan:item_not_owned';
    end if;
    if (p_kind = 'sell_ore') <> (v_item.kind = 'ore') then raise exception 'bawan:bad_kind'; end if;
    if exists (select 1 from public.item_disposals d where d.item_id = p_item_id) then
        if exists (select 1 from public.ledger l where l.ref = v_ref) then      -- penjualan yang sama, di-retry
            return query select v_ref, p.wallet::text, true from public.players p
                where p.guild_id = p_guild_id and p.user_id = p_user_id;
            return;
        end if;
        raise exception 'bawan:item_already_disposed';
    end if;
    v_balance := bawan_private.ledger_post(v_ref, p_guild_id, p_user_id, p_kind, p_payout, p_item_id);
    insert into public.item_disposals (item_id, kind, ledger_ref) values (p_item_id, 'sold', v_ref);
    return query select v_ref, v_balance::text, false;
end $$;

-- Reward voice satu tick: semua pemain dalam SATU transaksi; p_ref membuat retry aman.
-- p_entries = [{"user_id":..,"coin":"0.0000","xp":..,"voice_seconds":..}, ...]
create or replace function public.apply_voice_tick(p_guild_id bigint, p_ref text, p_entries jsonb)
returns table (user_id bigint, wallet text, xp bigint, level integer, voice_seconds double precision, applied boolean)
language plpgsql volatile as $$
#variable_conflict use_column
declare v_rows integer; v_applied boolean; e jsonb; v_uid bigint; v_coin numeric; v_xp bigint; v_secs double precision;
begin
    if jsonb_typeof(p_entries) <> 'array' then raise exception 'bawan:bad_entries'; end if;
    insert into public.voice_ticks (ref, guild_id) values (p_ref, p_guild_id) on conflict (ref) do nothing;
    get diagnostics v_rows = row_count;
    v_applied := v_rows = 1;
    if v_applied then
        for e in select value from jsonb_array_elements(p_entries) loop
            v_uid  := (e ->> 'user_id')::bigint;
            v_coin := coalesce((e ->> 'coin')::numeric, 0);
            v_xp   := coalesce((e ->> 'xp')::bigint, 0);
            v_secs := coalesce((e ->> 'voice_seconds')::double precision, 0);
            if v_coin < 0 or v_xp < 0 or v_secs < 0 then raise exception 'bawan:bad_amount'; end if;
            perform bawan_private.ensure_player(p_guild_id, v_uid);
            update public.players p
                set voice_seconds = p.voice_seconds + v_secs,
                    xp            = p.xp + v_xp,
                    level         = bawan_private.level_for_xp(p.xp + v_xp),
                    updated_at    = now()
                where p.guild_id = p_guild_id and p.user_id = v_uid;
            if v_coin > 0 then
                perform bawan_private.ledger_post(p_ref || ':' || v_uid, p_guild_id, v_uid, 'voice_reward', v_coin, null);
            end if;
        end loop;
    end if;
    return query
        select p.user_id, p.wallet::text, p.xp, p.level, p.voice_seconds, v_applied
        from public.players p
        where p.guild_id = p_guild_id
          and p.user_id in (select (x ->> 'user_id')::bigint from jsonb_array_elements(p_entries) x);
end $$;

-- /rest (selama production_policy.rest_enabled): stamina = cap
create or replace function public.rest_player(p_guild_id bigint, p_user_id bigint)
returns table (stamina_before double precision, stamina double precision)
language plpgsql volatile as $$
#variable_conflict use_column
declare v_policy public.production_policy%rowtype; v_player public.players%rowtype;
begin
    select * into strict v_policy from public.production_policy where scope = 'global';
    if not v_policy.rest_enabled then raise exception 'bawan:rest_disabled'; end if;
    perform bawan_private.ensure_player(p_guild_id, p_user_id);
    select * into strict v_player from public.players
        where guild_id = p_guild_id and user_id = p_user_id for update;
    update public.players set stamina = v_policy.stamina_cap, stamina_at = now(), updated_at = now()
        where guild_id = p_guild_id and user_id = p_user_id;
    return query select bawan_private.stamina_now(v_player.stamina, v_player.stamina_at), v_policy.stamina_cap;
end $$;

-- Preferensi (bukan uang): pickaxe terakhir, status auto mine.  NULL = tidak diubah.
create or replace function public.set_player_prefs(
    p_guild_id bigint, p_user_id bigint, p_pickaxe_key text, p_automine boolean
) returns table (pickaxe_key text, automine boolean)
language plpgsql volatile as $$
#variable_conflict use_column
begin
    perform bawan_private.ensure_player(p_guild_id, p_user_id);
    update public.players p
        set pickaxe_key = coalesce(p_pickaxe_key, p.pickaxe_key),
            automine    = coalesce(p_automine, p.automine),
            updated_at  = now()
        where p.guild_id = p_guild_id and p.user_id = p_user_id;
    return query select p.pickaxe_key, p.automine from public.players p
        where p.guild_id = p_guild_id and p.user_id = p_user_id;
end $$;

-- Uang beredar = jumlah saldo di DB (satu sumber).
create or replace function public.money_supply(p_guild_id bigint) returns text
language sql stable as $$
    select coalesce(sum(wallet), 0)::numeric(24,4)::text from public.players where guild_id = p_guild_id
$$;

-- Audit read-only: semua angka ini harus 0 / kosong.
create or replace function public.ledger_audit() returns json
language sql stable as $$
    select json_build_object(
        'players',  (select count(*) from public.players),
        'ledger_rows', (select count(*) from public.ledger),
        'wallet_mismatch', (
            select coalesce(json_agg(json_build_object(
                       'guild_id', p.guild_id, 'user_id', p.user_id,
                       'wallet', p.wallet::text, 'ledger_sum', coalesce(l.s, 0)::text)), '[]'::json)
            from public.players p
            left join (select guild_id, user_id, sum(amount) s from public.ledger group by 1, 2) l
                   using (guild_id, user_id)
            where p.wallet <> coalesce(l.s, 0)),
        'ledger_without_player', (
            select count(*) from (select distinct guild_id, user_id from public.ledger) l
            where not exists (select 1 from public.players p
                              where p.guild_id = l.guild_id and p.user_id = l.user_id)),
        'balance_chain_broken', (
            select count(*) from (
                select balance_after, amount,
                       lag(balance_after) over (partition by guild_id, user_id order by id) as prev
                from public.ledger) c
            where c.balance_after <> coalesce(c.prev, 0) + c.amount),
        'sale_without_disposal', (
            select count(*) from public.ledger l
            where l.kind in ('sell_ore', 'sell_crystal')
              and not exists (select 1 from public.item_disposals d where d.ledger_ref = l.ref)),
        'results_beyond_counter', (
            select count(*) from public.mining_results r
            join public.mining_attempts m using (guild_id, user_id)
            where r.attempt > m.attempts)
    );
$$;

-- 10. Hak akses: RLS tanpa policy + cabut semua dari role publik
alter table public.ledger            enable row level security;
alter table public.items             enable row level security;
alter table public.item_disposals    enable row level security;
alter table public.mining_results    enable row level security;
alter table public.voice_ticks       enable row level security;
alter table public.production_policy enable row level security;
revoke all on table public.ledger, public.items, public.item_disposals, public.mining_results,
                    public.voice_ticks, public.production_policy
    from anon, authenticated;
revoke all on sequence public.ledger_id_seq from anon, authenticated;

do $$
declare f text;
begin
    foreach f in array array[
        'bawan_private.guard_player()',
        'bawan_private.isqrt(bigint)',
        'bawan_private.level_for_xp(bigint)',
        'bawan_private.stamina_now(double precision, timestamptz)',
        'bawan_private.ensure_player(bigint, bigint)',
        'bawan_private.swings_last_minute(bigint, bigint)',
        'bawan_private.ledger_post(text, bigint, bigint, text, numeric, text)',
        'public.begin_swing(bigint, bigint)',
        'public.record_swing(bigint, bigint, bigint, text, boolean, boolean, double precision, double precision, text, text, jsonb)',
        'public.sell_item(bigint, bigint, text, numeric, text)',
        'public.apply_voice_tick(bigint, text, jsonb)',
        'public.rest_player(bigint, bigint)',
        'public.set_player_prefs(bigint, bigint, text, boolean)',
        'public.money_supply(bigint)',
        'public.ledger_audit()'
    ] loop
        execute format('revoke all on function %s from public, anon, authenticated', f);
        execute format('grant execute on function %s to service_role', f);
    end loop;
end $$;

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
