"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         DB_EKONOMI_PUSAT.PY  —  Supabase Persistence Layer                   ║
║         "db.ekonomi.pusat" — satu-satunya file yang bicara dengan Supabase   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  TABEL (lihat supabase/schema.sql)                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  players       (guild_id, user_id) → wallet, stamina, xp, level, voice     ║
║  ledger / items / item_disposals / mining_results / voice_ticks            ║
║                → saldo otoritatif (v7): SEMUA perubahan lewat RPC          ║
║  currencies    guild_id → CurrencyManifest dari currency_engine.py         ║
║  voice_config  guild_id → VoiceConfig dari voice_engine.py                 ║
║  server_registry / world_nonces  → Stage 0 (insert-only, world_registry)   ║
║                                                                              ║
║  DESIGN                                                                      ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • Semua method SINKRON (supabase-py sync client).  main_core.py wajib     ║
║    memanggilnya lewat executor agar event loop Discord tidak tersendat.    ║
║  • Tidak tahu apa-apa soal PlayerProfile — player diterima/dikembalikan    ║
║    sebagai dict baris; konversinya ada di main_core.PlayerProfile.         ║
║  • Kolom eksplisit (bukan satu blob JSON) supaya dashboard web ekonomi     ║
║    nanti bisa query statistik langsung.                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from supabase import Client, create_client

from currency_engine import CurrencyManifest, CurrencyPolicy
from voice_engine import VoiceConfig


TABLE_PLAYERS: str = "players"
TABLE_CURRENCIES: str = "currencies"
TABLE_VOICE_CONFIG: str = "voice_config"
TABLE_SERVER_REGISTRY: str = "server_registry"
TABLE_WORLD_NONCES: str = "world_nonces"
TABLE_WORLD_COMMITMENTS: str = "world_commitments"
TABLE_WORLD_WITNESS_LOG: str = "world_witness_log"
TABLE_MINING_ATTEMPTS: str = "mining_attempts"
TABLE_LEDGER: str = "ledger"
TABLE_ITEMS: str = "items"
TABLE_ITEM_DISPOSALS: str = "item_disposals"
TABLE_MINING_RESULTS: str = "mining_results"
TABLE_VOICE_TICKS: str = "voice_ticks"
TABLE_PRODUCTION_POLICY: str = "production_policy"
TABLE_NODE_STATE: str = "node_state"

# Kolom yang WAJIB ada; dicek saat startup supaya migrasi yang terlewat
# langsung ketahuan, bukan gagal diam-diam di setiap flush.
_REQUIRED_COLUMNS = {
    TABLE_PLAYERS: "guild_id,user_id,stamina,stamina_at,pickaxe_key,wallet,xp,level,voice_seconds,automine",
    TABLE_CURRENCIES: (
        "guild_id,currency_name,ticker,genesis_market_cap,total_supply,circulating_supply,"
        "reserve_supply,exchange_rate_to_ua,geological_backing_value_ua,policy,"
        "genesis_timestamp,founding_geology_score,is_active"
    ),
    TABLE_VOICE_CONFIG: (
        "guild_id,notify_channel_id,reward_interval_minutes,reward_amount,"
        "block_self_mute_deaf,block_afk_channel,block_alone"
    ),
    TABLE_SERVER_REGISTRY: "guild_id,algo_version,randomness_source,registered_at,worldgen_version",
    TABLE_WORLD_NONCES: "guild_id,drand_round,world_nonce,drand_signature,fetched_at",
    TABLE_WORLD_COMMITMENTS: "algo_version,pepper_commitment,committed_at",
    TABLE_WORLD_WITNESS_LOG: "event_key,event_id,delivered_at",
    TABLE_MINING_ATTEMPTS: "guild_id,user_id,attempts,updated_at",
    TABLE_LEDGER: "id,ref,guild_id,user_id,kind,amount,balance_after,item_id,created_at",
    TABLE_ITEMS: "item_id,guild_id,owner_id,kind,item_uuid,payload,created_at",
    TABLE_ITEM_DISPOSALS: "item_id,kind,ledger_ref,created_at",
    TABLE_MINING_RESULTS: (
        "guild_id,user_id,attempt,node_id,success,critical_hit,amount_extracted,"
        "stamina_before,stamina_consumed,stamina_after,item_id,created_at"
    ),
    TABLE_VOICE_TICKS: "ref,guild_id,created_at",
    TABLE_PRODUCTION_POLICY: "scope,stamina_cap,stamina_regen_per_second,max_swings_per_minute,rest_enabled",
    TABLE_NODE_STATE: "guild_id,node_id,max_reserve,regen_per_second,reserve,reserve_at,registered_at",
}

# wallet dibaca sebagai teks → Decimal: numeric(24,4) tidak lewat float.
_PLAYER_COLUMNS: str = (
    "guild_id,user_id,stamina,stamina_at,pickaxe_key,wallet::text,xp,level,voice_seconds,automine"
)

MONEY_QUANTUM: Decimal = Decimal("0.0001")          # = numeric(24,4)


def money_str(value: object) -> str:
    """Jumlah uang → string 4 desimal untuk RPC.  Menolak negatif/NaN/inf."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite() or d < 0:
        raise ValueError(f"jumlah uang tidak sah: {value!r}")
    return str(d.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def parse_money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise NotSupabaseResponse(f"nilai uang bukan teks/angka: {type(value).__name__}. {_URL_HINT}")
    return Decimal(str(value))


_REJECT_CODE = re.compile(r"bawan:([a-z_]+)")


class LedgerRejected(Exception):
    """Aturan database menolak operasi (bukan gangguan jaringan).  `.code` = kode `bawan:<code>`."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code} ({detail})")
        self.code = code

# PostgREST membatasi 1000 baris per response secara default.
_PAGE_SIZE: int = 1000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_supabase_url(url: str) -> str:
    """
    supabase-py expects the bare project URL (https://<ref>.supabase.co) and
    appends /rest/v1 itself.  The dashboard also shows the REST URL ending in
    /rest/v1, which would produce /rest/v1/rest/v1/... → PGRST125.  Accept both.
    """
    clean = url.strip().rstrip("/")
    if clean.endswith("/rest/v1"):
        clean = clean[: -len("/rest/v1")]
    return clean


class NotSupabaseResponse(RuntimeError):
    """The endpoint answered, but not as the Supabase REST API (e.g. an HTML page)."""


_URL_HINT = ("Isi dengan 'Project URL' dari Settings → API (bentuknya https://<ref>.supabase.co), "
             "BUKAN URL dashboard di browser.")


def _rows(data: object, what: str) -> List[dict]:
    """A PostgREST table/RPC-setof response must be a list of objects — anything else is refused."""
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        raise NotSupabaseResponse(
            f"{what}: respons bukan dari API Supabase (dapat {type(data).__name__}"
            f"{', diawali ' + repr(data[:15]) if isinstance(data, str) else ''}). {_URL_HINT}"
        )
    return data


def validate_supabase_url(url: str, env_name: str) -> str:
    """Reject URLs that cannot be a Supabase API root (dashboard links, extra paths)."""
    parsed = urlparse(normalize_supabase_url(url))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.path not in ("", "/") \
            or host == "supabase.com" or host.endswith(".supabase.com"):
        raise RuntimeError(f"{env_name} tidak terlihat seperti URL API Supabase. {_URL_HINT}")
    return normalize_supabase_url(url)


def connect_test_database(env: Mapping[str, str]) -> Client:
    """
    Client ke project Supabase KHUSUS TES (SUPABASE_TEST_URL / SUPABASE_TEST_KEY).
    Menolak kalau belum diisi, atau kalau host-nya sama dengan SUPABASE_URL
    utama — tes live yang menulis baris permanen tidak boleh menyentuh produksi.
    """
    test_url = normalize_supabase_url(env.get("SUPABASE_TEST_URL", ""))
    test_key = env.get("SUPABASE_TEST_KEY", "").strip()
    if not test_url or not test_key:
        raise RuntimeError("SUPABASE_TEST_URL / SUPABASE_TEST_KEY belum diisi di .env — tes live menolak jalan.")
    test_host = (urlparse(test_url).hostname or "").lower()
    main_host = (urlparse(normalize_supabase_url(env.get("SUPABASE_URL", ""))).hostname or "").lower()
    if not test_host:
        raise RuntimeError("SUPABASE_TEST_URL tidak valid.")
    if test_host == main_host:
        raise RuntimeError("SUPABASE_TEST_URL menunjuk ke project yang SAMA dengan SUPABASE_URL utama — tes menolak jalan.")
    return create_client(validate_supabase_url(test_url, "SUPABASE_TEST_URL"), test_key)


# ─────────────────────────────────────────────────────────────────────────────
# KONVERSI BARIS ⇄ DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

def manifest_to_row(manifest: CurrencyManifest) -> dict:
    return {
        "guild_id":                    manifest.server_id,
        "currency_name":               manifest.currency_name,
        "ticker":                      manifest.ticker,
        "genesis_market_cap":          manifest.genesis_market_cap,
        "total_supply":                manifest.total_supply,
        "circulating_supply":          manifest.circulating_supply,
        "reserve_supply":              manifest.reserve_supply,
        "exchange_rate_to_ua":         manifest.exchange_rate_to_ua,
        "geological_backing_value_ua": manifest.geological_backing_value_ua,
        "policy":                      dataclasses.asdict(manifest.policy),
        "genesis_timestamp":           manifest.genesis_timestamp,
        "founding_geology_score":      manifest.founding_geology_score,
        "is_active":                   manifest.is_active,
    }


def row_to_manifest(row: dict) -> CurrencyManifest:
    # float() di mana-mana: PostgREST mengembalikan 1000.0 sebagai 1000 (int).
    p = row["policy"]
    return CurrencyManifest(
        server_id                   = int(row["guild_id"]),
        currency_name               = row["currency_name"],
        ticker                      = row["ticker"],
        genesis_market_cap          = float(row["genesis_market_cap"]),
        total_supply                = float(row["total_supply"]),
        circulating_supply          = float(row["circulating_supply"]),
        reserve_supply              = float(row["reserve_supply"]),
        exchange_rate_to_ua         = float(row["exchange_rate_to_ua"]),
        geological_backing_value_ua = float(row["geological_backing_value_ua"]),
        policy = CurrencyPolicy(
            hard_cap_supply             = float(p["hard_cap_supply"]),
            max_circulating_supply      = float(p["max_circulating_supply"]),
            reserve_ratio               = float(p["reserve_ratio"]),
            founding_geology_score      = float(p["founding_geology_score"]),
            geological_backing_value_ua = float(p["geological_backing_value_ua"]),
        ),
        genesis_timestamp           = row["genesis_timestamp"],
        founding_geology_score      = float(row["founding_geology_score"]),
        is_active                   = bool(row["is_active"]),
    )


def voice_config_to_row(config: VoiceConfig) -> dict:
    return dataclasses.asdict(config)


def row_to_voice_config(row: dict) -> VoiceConfig:
    return VoiceConfig(
        guild_id                = int(row["guild_id"]),
        notify_channel_id       = int(row["notify_channel_id"]) if row["notify_channel_id"] is not None else None,
        reward_interval_minutes = int(row["reward_interval_minutes"]),
        reward_amount           = float(row["reward_amount"]),
        block_self_mute_deaf    = bool(row["block_self_mute_deaf"]),
        block_afk_channel       = bool(row["block_afk_channel"]),
        block_alone             = bool(row["block_alone"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE GATEWAY
# ─────────────────────────────────────────────────────────────────────────────

class EconomyDatabase:
    """Gateway sinkron ke Supabase untuk semua state ekonomi yang persisten."""

    def __init__(self, client: Client) -> None:
        self._db = client

    # ── Internal ──────────────────────────────────────────────────────────────

    def _select_all(self, table: str, order_by: Sequence[str], columns: str = "*") -> List[dict]:
        rows: List[dict] = []
        start = 0
        while True:
            query = self._db.table(table).select(columns)
            for col in order_by:
                query = query.order(col)
            # _rows() also stops a non-API endpoint from looping forever here
            # (an HTML string "looks like" thousands of rows to len()).
            page = _rows(query.range(start, start + _PAGE_SIZE - 1).execute().data, f"select {table}")
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                return rows
            start += _PAGE_SIZE

    def _rpc(self, name: str, params: dict) -> object:
        """RPC; a rule violation raised by the DB (`bawan:<code>`) becomes LedgerRejected."""
        try:
            return self._db.rpc(name, params).execute().data
        except NotSupabaseResponse:
            raise
        except Exception as exc:
            match = _REJECT_CODE.search(str(exc))
            if match:
                raise LedgerRejected(match.group(1), str(exc)[:300]) from exc
            raise

    def _rpc_row(self, name: str, params: dict) -> dict:
        rows = _rows(self._rpc(name, params), f"rpc {name}")
        if len(rows) != 1:
            raise NotSupabaseResponse(f"rpc {name}: harus 1 baris, dapat {len(rows)}. {_URL_HINT}")
        return rows[0]

    # ── Startup check ─────────────────────────────────────────────────────────

    def verify_schema(self) -> None:
        """Gagal cepat dengan pesan jelas kalau tabel belum dibuat / key salah."""
        for table, columns in _REQUIRED_COLUMNS.items():
            try:
                _rows(self._db.table(table).select(columns).limit(1).execute().data, f"tabel {table}")
            except NotSupabaseResponse:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"Tidak bisa membaca tabel Supabase '{table}': {exc}\n"
                    f"→ Jalankan ulang SELURUH supabase/schema.sql di SQL Editor (aman diulang; "
                    f"berisi migrasi kolom baru), pastikan SUPABASE_URL benar, dan SUPABASE_KEY "
                    f"adalah key service_role / secret."
                ) from exc

    # ── Players (read-only here: every write goes through an RPC below) ───────

    def load_player_rows(self) -> List[dict]:
        rows = self._select_all(TABLE_PLAYERS, ("guild_id", "user_id"), columns=_PLAYER_COLUMNS)
        for row in rows:
            row["wallet"] = parse_money(row["wallet"])
        return rows

    def load_held_items(self) -> List[dict]:
        """Barang yang belum dilepas (items − item_disposals), urut waktu dibuat."""
        disposed = {r["item_id"] for r in self._select_all(TABLE_ITEM_DISPOSALS, ("item_id",), columns="item_id")}
        return [r for r in self._select_all(TABLE_ITEMS, ("created_at", "item_id")) if r["item_id"] not in disposed]

    # ── Saldo otoritatif (schema v7) — satu RPC = satu transaksi ─────────────

    def begin_swing(self, guild_id: int, user_id: int, node_id: str,
                    stamina_required: float) -> Tuple[int, float, float]:
        """
        Rate limit → stamina cukup? → counter n naik atomik.
        Returns (n, stamina saat ini, cadangan node saat ini) — keduanya menurut jam DB.
        """
        row = self._rpc_row("begin_swing", {"p_guild_id": guild_id, "p_user_id": user_id,
                                            "p_node_id": node_id, "p_stamina_required": float(stamina_required)})
        attempt, stamina, reserve = row.get("attempt"), row.get("stamina"), row.get("node_reserve")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in (stamina, reserve)):
            raise NotSupabaseResponse(f"rpc begin_swing: respons tidak valid {row!r}. {_URL_HINT}")
        return attempt, float(stamina), float(reserve)

    def record_swing(
        self, guild_id: int, user_id: int, attempt: int, *, node_id: str, success: bool,
        critical_hit: bool, amount_extracted: float, stamina_consumed: float,
        item_kind: Optional[str], item_uuid: Optional[str], item_payload: Optional[dict],
    ) -> Tuple[float, Optional[str], bool, Optional[float]]:
        """Catat hasil ayunan n sekali.  Returns (stamina_after, item_id|None, already, cadangan node sesudahnya)."""
        row = self._rpc_row("record_swing", {
            "p_guild_id": guild_id, "p_user_id": user_id, "p_attempt": attempt, "p_node_id": node_id,
            "p_success": success, "p_critical_hit": critical_hit,
            "p_amount_extracted": float(amount_extracted), "p_stamina_consumed": float(stamina_consumed),
            "p_item_kind": item_kind, "p_item_uuid": item_uuid, "p_item_payload": item_payload,
        })
        reserve = row.get("node_reserve")
        return (float(row["stamina_after"]), row["item_id"], bool(row["already"]),
                None if reserve is None else float(reserve))

    def register_nodes(self, guild_id: int, nodes: Sequence[dict]) -> List[dict]:
        """
        Daftarkan node (insert-or-ignore: max_reserve/regen yang sudah tersimpan TIDAK ditimpa),
        lalu kembalikan semua node guild itu dengan cadangan saat ini.  nodes=[] → hanya membaca.
        """
        payload = [{"node_id": n["node_id"], "max_reserve": float(n["max_reserve"]),
                    "regen_per_second": float(n["regen_per_second"])} for n in nodes]
        return _rows(self._rpc("register_nodes", {"p_guild_id": guild_id, "p_nodes": payload}), "rpc register_nodes")

    def load_production_policy(self) -> dict:
        rows = _rows(self._db.table(TABLE_PRODUCTION_POLICY).select("*").eq("scope", "global").execute().data,
                     f"select {TABLE_PRODUCTION_POLICY}")
        if len(rows) != 1:
            raise RuntimeError("production_policy 'global' tidak ada — jalankan ulang supabase/schema.sql")
        return rows[0]

    def sell_item(self, guild_id: int, user_id: int, item_id: str, payout: object, kind: str) -> Tuple[Decimal, bool]:
        """Jual satu barang (sekali saja).  Returns (saldo baru, already=penjualan yang sama di-retry)."""
        row = self._rpc_row("sell_item", {
            "p_guild_id": guild_id, "p_user_id": user_id, "p_item_id": item_id,
            "p_payout": money_str(payout), "p_kind": kind,
        })
        return parse_money(row["balance"]), bool(row["already"])

    def apply_voice_tick(self, guild_id: int, ref: str, entries: Sequence[dict]) -> List[dict]:
        """entries: [{user_id, coin, xp, voice_seconds}] → baris terbaru tiap user (+applied)."""
        payload = [{
            "user_id": int(e["user_id"]), "coin": money_str(e.get("coin", 0)),
            "xp": int(e.get("xp", 0)), "voice_seconds": float(e.get("voice_seconds", 0.0)),
        } for e in entries]
        rows = _rows(self._rpc("apply_voice_tick", {"p_guild_id": guild_id, "p_ref": ref, "p_entries": payload}),
                     "rpc apply_voice_tick")
        for row in rows:
            row["wallet"] = parse_money(row["wallet"])
        return rows

    def set_player_prefs(self, guild_id: int, user_id: int, pickaxe_key: Optional[str] = None,
                         automine: Optional[bool] = None) -> Tuple[str, bool]:
        row = self._rpc_row("set_player_prefs", {
            "p_guild_id": guild_id, "p_user_id": user_id, "p_pickaxe_key": pickaxe_key, "p_automine": automine,
        })
        return row["pickaxe_key"], bool(row["automine"])

    def money_supply(self, guild_id: int) -> Decimal:
        """Uang beredar = jumlah saldo di DB.  Satu-satunya sumber M."""
        return parse_money(self._rpc("money_supply", {"p_guild_id": guild_id}))

    def ledger_audit(self) -> dict:
        data = self._rpc("ledger_audit", {})
        if not isinstance(data, dict) or "wallet_mismatch" not in data:
            raise NotSupabaseResponse(f"rpc ledger_audit: respons bukan dari API Supabase. {_URL_HINT}")
        return data

    # ── Currencies ────────────────────────────────────────────────────────────

    def load_currencies(self) -> Dict[int, CurrencyManifest]:
        return {
            int(row["guild_id"]): row_to_manifest(row)
            for row in self._select_all(TABLE_CURRENCIES, ("guild_id",))
        }

    def upsert_currency(self, manifest: CurrencyManifest) -> None:
        row = {**manifest_to_row(manifest), "updated_at": _utc_now_iso()}
        self._db.table(TABLE_CURRENCIES).upsert(row, on_conflict="guild_id").execute()

    # ── Voice config ──────────────────────────────────────────────────────────

    def load_voice_configs(self) -> Dict[int, VoiceConfig]:
        return {
            int(row["guild_id"]): row_to_voice_config(row)
            for row in self._select_all(TABLE_VOICE_CONFIG, ("guild_id",))
        }

    def upsert_voice_config(self, config: VoiceConfig) -> None:
        row = {**voice_config_to_row(config), "updated_at": _utc_now_iso()}
        self._db.table(TABLE_VOICE_CONFIG).upsert(row, on_conflict="guild_id").execute()

    # ── Stage 0: server registry (insert-only; see world_registry.py) ─────────

    def register_server_row(self, guild_id: int, algo_version: str, source_id: str,
                            worldgen_version: str) -> dict:
        """Atomic insert-or-get via the register_server() SQL function (schema v6)."""
        rows = _rows(self._db.rpc("register_server", {
            "p_guild_id": guild_id, "p_algo_version": algo_version, "p_source": source_id,
            "p_worldgen_version": worldgen_version,
        }).execute().data, "rpc register_server")
        if not rows:
            raise RuntimeError(f"register_server tidak mengembalikan baris untuk guild {guild_id}")
        return rows[0]

    def get_world_nonce_row(self, guild_id: int) -> Optional[dict]:
        rows = _rows(self._db.table(TABLE_WORLD_NONCES).select("*").eq("guild_id", guild_id).execute().data,
                     f"select {TABLE_WORLD_NONCES}")
        return rows[0] if rows else None

    def insert_world_nonce_row(self, guild_id: int, drand_round: int, world_nonce: str, signature: str) -> None:
        # ON CONFLICT DO NOTHING: two workers storing the same beacon is harmless;
        # the first stored row always wins and is what callers read back.
        self._db.table(TABLE_WORLD_NONCES).upsert({
            "guild_id": guild_id, "drand_round": drand_round,
            "world_nonce": world_nonce, "drand_signature": signature,
        }, on_conflict="guild_id", ignore_duplicates=True).execute()

    def load_registry_rows(self) -> List[dict]:
        return self._select_all(TABLE_SERVER_REGISTRY, ("guild_id",))

    def load_world_nonce_rows(self) -> List[dict]:
        return self._select_all(TABLE_WORLD_NONCES, ("guild_id",))

    # ── Stage 1: pepper commitments (insert-only; see world_seed.py) ──────────

    def get_commitment_row(self, algo_version: str) -> Optional[dict]:
        rows = _rows(self._db.table(TABLE_WORLD_COMMITMENTS).select("*").eq("algo_version", algo_version)
                     .execute().data, f"select {TABLE_WORLD_COMMITMENTS}")
        return rows[0] if rows else None

    def insert_commitment_row(self, algo_version: str, commitment: str) -> None:
        self._db.table(TABLE_WORLD_COMMITMENTS).upsert(
            {"algo_version": algo_version, "pepper_commitment": commitment},
            on_conflict="algo_version", ignore_duplicates=True,
        ).execute()

    def load_commitment_rows(self) -> List[dict]:
        return self._select_all(TABLE_WORLD_COMMITMENTS, ("algo_version",))

    # ── External witness outbox (insert-only; see world_witness.py) ───────────

    def load_witness_keys(self) -> Set[str]:
        return {row["event_key"] for row in self._select_all(TABLE_WORLD_WITNESS_LOG, ("event_key",))}

    def insert_witness_row(self, event_key: str, event_id: str) -> None:
        self._db.table(TABLE_WORLD_WITNESS_LOG).upsert(
            {"event_key": event_key, "event_id": event_id},
            on_conflict="event_key", ignore_duplicates=True,
        ).execute()

    # ── Mining attempt counter (monotonic; see mining_swing.py) ───────────────

    def next_mining_attempt(self, guild_id: int, user_id: int) -> int:
        """Atomically increment and return n for (guild, user).  Raises on ANY doubt."""
        data = self._db.rpc("next_mining_attempt", {"p_guild_id": guild_id, "p_user_id": user_id}).execute().data
        if not isinstance(data, int) or isinstance(data, bool) or data < 1:
            raise NotSupabaseResponse(f"rpc next_mining_attempt: respons tidak valid ({type(data).__name__}). {_URL_HINT}")
        return data

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def security_report(self) -> dict:
        data = self._db.rpc("security_report", {}).execute().data
        if not isinstance(data, dict) or "caller_role" not in data:
            raise NotSupabaseResponse(f"rpc security_report: respons bukan dari API Supabase. {_URL_HINT}")
        return data


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST
#   python db_ekonomi_pusat.py             → tes konversi offline (tanpa network)
#   python db_ekonomi_pusat.py --live      → + tulis/baca/hapus di project TES
#                                            (SUPABASE_TEST_*), guild_id 0
#   python db_ekonomi_pusat.py --security  → laporan RLS/hak akses project utama
#                                            (read-only) + uji anon di project TES
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import os
    import sys
    from types import SimpleNamespace

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))

    def over_the_wire(row: dict) -> dict:
        # Simulasikan JSON transport PostgREST: float bulat kembali sebagai int.
        text = json.dumps(row).replace(".0,", ",").replace(".0}", "}")
        return json.loads(text)

    TEST_GUILD = 0
    policy = CurrencyPolicy(2000.0, 800.0, 0.2, 900.0, 675.0)
    manifest = CurrencyManifest(
        server_id=TEST_GUILD, currency_name="Uji Dollar", ticker="ZZUJI",
        genesis_market_cap=600.0, total_supply=1000.0, circulating_supply=800.0,
        reserve_supply=200.0, exchange_rate_to_ua=0.75, geological_backing_value_ua=675.0,
        policy=policy, genesis_timestamp="2026-01-01T00:00:00+00:00",
        founding_geology_score=900.0, is_active=True,
    )
    vcfg = VoiceConfig(guild_id=TEST_GUILD, notify_channel_id=1234567890123456789,
                       reward_interval_minutes=3, reward_amount=12.5, block_alone=False)

    print("\n[offline] Konversi baris ⇄ dataclass")
    check("CurrencyManifest round-trip", row_to_manifest(over_the_wire(manifest_to_row(manifest))), manifest)
    check("VoiceConfig round-trip", row_to_voice_config(over_the_wire(voice_config_to_row(vcfg))), vcfg)
    no_channel = dataclasses.replace(vcfg, notify_channel_id=None)
    check("VoiceConfig tanpa channel", row_to_voice_config(over_the_wire(voice_config_to_row(no_channel))), no_channel)

    print("\n[offline] Penjaga DB tes (SUPABASE_TEST_*)")
    PROD = "https://prodref.supabase.co"
    for label, env in [
        ("belum diisi → tolak", {"SUPABASE_URL": PROD}),
        ("sama persis → tolak", {"SUPABASE_URL": PROD, "SUPABASE_TEST_URL": PROD, "SUPABASE_TEST_KEY": "k"}),
        ("sama tapi pakai /rest/v1/ & huruf besar → tolak",
         {"SUPABASE_URL": PROD, "SUPABASE_TEST_URL": "https://PRODREF.supabase.co/rest/v1/", "SUPABASE_TEST_KEY": "k"}),
    ]:
        try:
            connect_test_database(env)
            check(label, "diterima", "RuntimeError")
        except RuntimeError:
            check(label, "RuntimeError", "RuntimeError")
    try:
        connect_test_database({"SUPABASE_URL": PROD, "SUPABASE_TEST_KEY": "k",
                               "SUPABASE_TEST_URL": "https://supabase.com/dashboard/project/testref"})
        check("URL dashboard → tolak", "diterima", "RuntimeError")
    except RuntimeError:
        check("URL dashboard → tolak", "RuntimeError", "RuntimeError")

    class _HtmlEndpoint:
        """Mimics supabase-py against a web page: every call 'succeeds' with an HTML string."""
        calls = 0
        def __getattr__(self, _name):
            return lambda *a, **k: self
        def execute(self):
            _HtmlEndpoint.calls += 1
            if _HtmlEndpoint.calls > 5:
                raise AssertionError("loop tanpa henti")
            return SimpleNamespace(data="<!DOCTYPE html>" + "x" * 5000)

    html_gw = EconomyDatabase(_HtmlEndpoint())
    for label, fn in [("verify_schema", html_gw.verify_schema), ("load_player_rows", html_gw.load_player_rows),
                      ("register_server_row", lambda: html_gw.register_server_row(1, "v1", "s", "dev"))]:
        _HtmlEndpoint.calls = 0
        try:
            fn()
            check(f"endpoint HTML → {label} menolak", "lolos", "NotSupabaseResponse")
        except NotSupabaseResponse:
            check(f"endpoint HTML → {label} menolak (tanpa loop)", "NotSupabaseResponse", "NotSupabaseResponse")
        except AssertionError as exc:
            check(f"endpoint HTML → {label} menolak", str(exc), "NotSupabaseResponse")

    check("project beda → diterima",
          connect_test_database({"SUPABASE_URL": PROD, "SUPABASE_TEST_URL": "https://testref.supabase.co",
                                 "SUPABASE_TEST_KEY": "sb_secret_dummy"}) is not None, True)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    def fail(message: str) -> None:
        print(f"\n✗ {message}\n")
        raise SystemExit(1)

    if "--live" in sys.argv:
        try:
            gw = EconomyDatabase(connect_test_database(os.environ))   # never the production project
        except RuntimeError as exc:
            fail(str(exc))
        raw = gw._db

        print("\n[live] Supabase TES (currency/voice_config guild_id=0, dihapus di akhir)")
        try:
            gw.verify_schema()
            check("verify_schema", True, True)

            gw.upsert_currency(manifest)
            check("currency tersimpan & terbaca", gw.load_currencies().get(TEST_GUILD, None) is not None, True)
            loaded = gw.load_currencies()[TEST_GUILD]
            check("currency sama persis (kecuali format timestamp)",
                  dataclasses.replace(loaded, genesis_timestamp=manifest.genesis_timestamp), manifest)

            gw.upsert_voice_config(vcfg)
            check("voice_config round-trip", gw.load_voice_configs().get(TEST_GUILD), vcfg)

            print("\n[live] Saldo otoritatif (v7) — sentinel guild -2, PERMANEN (ledger insert-only)")
            import time as _time
            from concurrent.futures import ThreadPoolExecutor
            from voice_engine import level_for_xp

            LG = -2
            run = _time.time_ns() // 1000 % 10**12          # user baru tiap run → state bersih
            U, U2, U3 = run, run + 1, run + 2

            def rejected(label: str, code: str, fn) -> None:
                try:
                    fn()
                    check(label, "diterima", code)
                except LedgerRejected as exc:
                    check(label, exc.code, code)
                except Exception as exc:
                    check(label, f"error lain: {str(exc)[:160]}", code)

            def refused(label: str, needle: str, fn) -> None:
                try:
                    fn()
                    check(label, "diterima", "ditolak")
                except Exception as exc:
                    check(label, "ditolak" if needle in str(exc) else f"error lain: {str(exc)[:160]}", "ditolak")

            payload = {"item_uuid": "ab" * 32, "element_symbol": "Fe", "purity": "Crude", "weight_tonnes": 1.5}
            NODE = f"test:{run}:Fe:0"
            policy = gw.load_production_policy()
            check("kebijakan produksi v8: cap 100, regen 50/jam, 6 ayunan/menit, /rest mati",
                  (policy["stamina_cap"], round(policy["stamina_regen_per_second"] * 3600, 6),
                   policy["max_swings_per_minute"], policy["rest_enabled"]), (100, 50.0, 6, False))
            rejected("begin_swing di node yang belum terdaftar", "unknown_node",
                     lambda: gw.begin_swing(LG, U, NODE, 10.0))
            reg = {r["node_id"]: r for r in gw.register_nodes(LG, [{"node_id": NODE, "max_reserve": 100.0,
                                                                     "regen_per_second": 0.5}])}
            check("register_nodes: node baru mulai penuh", reg[NODE]["reserve"], 100.0)
            again_reg = {r["node_id"]: r for r in gw.register_nodes(LG, [{"node_id": NODE, "max_reserve": 9999.0,
                                                                           "regen_per_second": 99.0}])}
            check("register_nodes ulang TIDAK menimpa parameter tersimpan",
                  (again_reg[NODE]["max_reserve"], again_reg[NODE]["regen_per_second"]), (100.0, 0.5))
            n1, st1, res1 = gw.begin_swing(LG, U, NODE, 10.0)
            check("begin_swing: n pertama = 1, stamina = cap 100, node penuh", (n1, st1, res1), (1, 100.0, 100.0))
            after, item_id, again, node_after = gw.record_swing(
                LG, U, n1, node_id=NODE, success=True, critical_hit=False, amount_extracted=30.0,
                stamina_consumed=10.0, item_kind="ore", item_uuid=payload["item_uuid"], item_payload=payload)
            check("record_swing: stamina & cadangan node berkurang di transaksi yang sama, barang tercatat",
                  (round(after, 3), item_id, again, round(node_after, 3)), (90.0, f"swing:{LG}:{U}:{n1}", False, 70.0))
            retry = gw.record_swing(LG, U, n1, node_id=NODE, success=True, critical_hit=False,
                                    amount_extracted=30.0, stamina_consumed=10.0, item_kind="ore",
                                    item_uuid=payload["item_uuid"], item_payload=payload)
            check("record_swing di-retry → hasil yang sama, tidak dobel (node tidak berkurang lagi)",
                  (round(retry[0], 3), retry[1], retry[2], retry[3] < 100.0 and retry[3] >= 70.0),
                  (90.0, item_id, True, True))
            _time.sleep(2.0)
            _, st_regen, res_regen = gw.begin_swing(LG, U, NODE, 0.0)
            check("stamina pulih dari waktu (jam DB): naik tapi ≤ 50/jam", 90.0 < st_regen <= 90.0 + 50 / 3600 * 10,
                  True)
            check("cadangan node pulih dari waktu: 70 + 0.5/detik", 70.0 + 0.5 < res_regen <= 70.0 + 0.5 * 10, True)
            rejected("record_swing untuk n yang belum dikeluarkan counter", "attempt_not_issued",
                     lambda: gw.record_swing(LG, U, n1 + 5, node_id="x", success=False, critical_hit=False,
                                             amount_extracted=0, stamina_consumed=0, item_kind=None,
                                             item_uuid=None, item_payload=None))
            n_before = gw.begin_swing(LG, U, NODE, 0.0)[0]
            rejected("begin_swing dengan stamina kurang → ditolak SEBELUM counter naik", "insufficient_stamina",
                     lambda: gw.begin_swing(LG, U, NODE, 1000.0))
            check("… counter tidak terbuang", gw.begin_swing(LG, U, NODE, 0.0)[0], n_before + 1)
            rejected("record_swing dengan stamina tidak cukup", "insufficient_stamina",
                     lambda: gw.record_swing(LG, U, n_before, node_id=NODE, success=True, critical_hit=False,
                                             amount_extracted=0, stamina_consumed=1000, item_kind=None,
                                             item_uuid=None, item_payload=None))
            rejected("record_swing mengambil lebih dari cadangan node", "node_depleted",
                     lambda: gw.record_swing(LG, U, n_before, node_id=NODE, success=True, critical_hit=False,
                                             amount_extracted=10_000, stamina_consumed=0, item_kind=None,
                                             item_uuid=None, item_payload=None))
            EDGE = f"test:{run}:edge"
            gw.register_nodes(LG, [{"node_id": EDGE, "max_reserve": 10.00008, "regen_per_second": 0.0}])
            n_edge, _, _ = gw.begin_swing(LG, U, EDGE, 0.0)
            edge = gw.record_swing(LG, U, n_edge, node_id=EDGE, success=True, critical_hit=False,
                                   amount_extracted=round(10.00008, 4), stamina_consumed=0, item_kind=None,
                                   item_uuid=None, item_payload=None)
            check("ambil seluruh sisa node, hasil dibulatkan NAIK ke 4 desimal (10.0001 > 10.00008) → diterima, node 0",
                  (round(10.00008, 4) > 10.00008, edge[2], edge[3]), (True, False, 0.0))
            refused("UPDATE cadangan node langsung (service_role) ditolak", "node_outside_swing",
                    lambda: raw.table(TABLE_NODE_STATE).update({"reserve": 100}).eq("guild_id", LG)
                               .eq("node_id", NODE).execute())
            refused("DELETE node ditolak", "node_delete_forbidden",
                    lambda: raw.table(TABLE_NODE_STATE).delete().eq("guild_id", LG).eq("node_id", NODE).execute())
            try:
                raw.rpc("rest_player", {"p_guild_id": LG, "p_user_id": U}).execute()
                check("rest_player sudah dihapus", "masih ada", "hilang")
            except Exception:
                check("rest_player sudah dihapus", "hilang", "hilang")
            RL = run + 3                                      # pemain baru khusus uji batas ayunan
            recorded = 0
            for _ in range(6):
                n_rl, _, _ = gw.begin_swing(LG, RL, NODE, 0.0)
                gw.record_swing(LG, RL, n_rl, node_id=NODE, success=False, critical_hit=False,
                                amount_extracted=0, stamina_consumed=0, item_kind=None, item_uuid=None, item_payload=None)
                recorded += 1
            rejected(f"ayunan ke-7 dalam 1 menit ditolak (batas 6)", "rate_limited",
                     lambda: gw.begin_swing(LG, RL, NODE, 0.0))
            check("set_player_prefs tersimpan", gw.set_player_prefs(LG, U, pickaxe_key="iron_standard", automine=True),
                  ("iron_standard", True))

            def sell_on_own_connection(_):
                own = EconomyDatabase(connect_test_database(os.environ))
                return own.sell_item(LG, U, item_id, "12.3456", "sell_ore")

            with ThreadPoolExecutor(max_workers=50) as pool:
                sales = list(pool.map(sell_on_own_connection, range(50)))
            check("50 penjualan paralel barang yang sama → tepat 1 yang berhasil",
                  sum(1 for _, already in sales if not already), 1)
            check("… dan semua 50 melihat saldo akhir yang sama (12.3456)",
                  {str(balance) for balance, _ in sales}, {"12.3456"})
            ledger_rows = _rows(raw.table(TABLE_LEDGER).select("ref").eq("ref", f"sell:{item_id}").execute().data, "ledger")
            check("… dan tepat 1 baris ledger untuk penjualan itu", len(ledger_rows), 1)
            rejected("jual barang milik orang lain", "item_not_owned",
                     lambda: gw.sell_item(LG, U2, item_id, "1", "sell_ore"))

            ref = f"voice:test:{run}"
            first = gw.apply_voice_tick(LG, ref, [{"user_id": U, "coin": "2.5", "xp": 150, "voice_seconds": 61.0}])
            second = gw.apply_voice_tick(LG, ref, [{"user_id": U, "coin": "2.5", "xp": 150, "voice_seconds": 61.0}])
            check("voice tick: diterapkan sekali, retry ref sama tidak dobel",
                  ([r["applied"] for r in first], [r["applied"] for r in second]), ([True], [False]))
            check("voice tick: wallet/xp/level/detik ditambah tepat sekali",
                  (str(second[0]["wallet"]), second[0]["xp"], second[0]["level"], second[0]["voice_seconds"]),
                  ("14.8456", 150, 2, 61.0))
            levels_ok = True
            cumulative = 0
            for i, delta in enumerate((99, 1, 299, 1, 999_600, 1, 7)):
                cumulative += delta
                row = gw.apply_voice_tick(LG, f"voice:level:{run}:{i}",
                                          [{"user_id": U3, "coin": "0", "xp": delta, "voice_seconds": 0}])[0]
                levels_ok &= (row["xp"], row["level"]) == (cumulative, level_for_xp(cumulative))
            check("level di SQL = voice_engine.level_for_xp (batas 99/100/399/400/10⁶)", levels_ok, True)

            refused("UPDATE wallet langsung (service_role) ditolak", "wallet_outside_ledger",
                    lambda: raw.table(TABLE_PLAYERS).update({"wallet": "999"}).eq("guild_id", LG).eq("user_id", U).execute())
            refused("INSERT pemain dengan saldo langsung ditolak", "wallet_outside_ledger",
                    lambda: raw.table(TABLE_PLAYERS).insert({"guild_id": LG, "user_id": run + 9, "wallet": "5"}).execute())
            refused("DELETE pemain ditolak", "players_delete_forbidden",
                    lambda: raw.table(TABLE_PLAYERS).delete().eq("guild_id", LG).eq("user_id", U).execute())
            refused("UPDATE ledger ditolak", "insert-only",
                    lambda: raw.table(TABLE_LEDGER).update({"amount": "1000"}).eq("ref", f"sell:{item_id}").execute())
            refused("DELETE ledger ditolak", "insert-only",
                    lambda: raw.table(TABLE_LEDGER).delete().eq("ref", f"sell:{item_id}").execute())
            refused("DELETE item_disposals ditolak (barang tidak bisa 'dibatalkan jual')", "insert-only",
                    lambda: raw.table(TABLE_ITEM_DISPOSALS).delete().eq("item_id", item_id).execute())
            rejected("payout negatif ditolak di DB", "bad_amount",
                     lambda: gw._rpc("sell_item", {"p_guild_id": LG, "p_user_id": U, "p_item_id": item_id,
                                                   "p_payout": "-1", "p_kind": "sell_ore"}))
            rejected("koin voice negatif ditolak di DB", "bad_amount",
                     lambda: gw._rpc("apply_voice_tick", {"p_guild_id": LG, "p_ref": f"voice:neg:{run}",
                                                          "p_entries": [{"user_id": U, "coin": "-1", "xp": 0,
                                                                         "voice_seconds": 0}]}))
            for helper in ("ledger_post", "bawan_ledger_post"):
                try:
                    raw.rpc(helper, {"p_ref": f"hack:{run}", "p_guild_id": LG, "p_user_id": U, "p_kind": "hack",
                                     "p_amount": "1000", "p_item_id": None}).execute()
                    check(f"helper {helper} TIDAK bisa dipanggil lewat REST", "terpanggil", "tidak ada")
                except Exception:
                    check(f"helper {helper} TIDAK bisa dipanggil lewat REST", "tidak ada", "tidak ada")

            fresh = EconomyDatabase(connect_test_database(os.environ))      # "restart": cache kosong, baca DB
            mine = {r["user_id"]: r for r in fresh.load_player_rows() if r["guild_id"] == LG}
            check("setelah restart: saldo = saldo terakhir dari RPC", str(mine[U]["wallet"]), "14.8456")
            check("setelah restart: barang terjual tidak kembali ke tas",
                  item_id in {r["item_id"] for r in fresh.load_held_items()}, False)
            check("uang beredar = jumlah saldo di DB",
                  gw.money_supply(LG), sum((r["wallet"] for r in mine.values()), Decimal(0)))
            audit = gw.ledger_audit()
            check("audit: wallet = Σ ledger untuk SEMUA pemain", audit["wallet_mismatch"], [])
            check("audit: rantai balance_after utuh, tiap penjualan punya pelepasan, n ≤ counter",
                  (audit["ledger_without_player"], audit["balance_chain_broken"],
                   audit["sale_without_disposal"], audit["results_beyond_counter"]), (0, 0, 0, 0))

            # Counter rows are monotonic by design (cannot be deleted) → the
            # sentinel (guild -1, user -1) stays in the TEST project for good.

            def increment_on_own_connection(_):
                # Own client per worker = own Postgres session (a shared HTTP/2
                # client is not thread-safe); models parallel bot instances.
                return EconomyDatabase(connect_test_database(os.environ)).next_mining_attempt(-1, -1)

            with ThreadPoolExecutor(max_workers=50) as pool:
                ns = list(pool.map(increment_on_own_connection, range(50)))
            check("mining (b): 50 increment paralel → 50 n unik", len(set(ns)), 50)
            check("mining (b): n berurutan tanpa celah", sorted(ns), list(range(min(ns), min(ns) + 50)))
            restarted = EconomyDatabase(connect_test_database(os.environ))      # fresh client = "restart"
            check("mining (c): setelah restart n lanjut, tidak mengulang", restarted.next_mining_attempt(-1, -1), max(ns) + 1)
            for label, action in [
                ("mining: counter dimundurkan → ditolak",
                 lambda: raw.table(TABLE_MINING_ATTEMPTS).update({"attempts": 1})
                            .eq("guild_id", -1).eq("user_id", -1).execute()),
                ("mining: counter dihapus → ditolak",
                 lambda: raw.table(TABLE_MINING_ATTEMPTS).delete().eq("guild_id", -1).eq("user_id", -1).execute()),
            ]:
                try:
                    action()
                    check(label, "diterima", "ditolak")
                except Exception as exc:
                    check(label, "ditolak" if "mining_attempts" in str(exc) else f"error lain: {exc}", "ditolak")
        finally:
            # Jangan sampai error cleanup menutupi error aslinya.
            try:
                for table in (TABLE_CURRENCIES, TABLE_VOICE_CONFIG):
                    raw.table(table).delete().eq("guild_id", TEST_GUILD).execute()
                print("  ·  baris uji guild_id=0 dihapus (currency, voice_config)")
            except Exception:
                print("  ·  cleanup dilewati (tabel tidak bisa diakses)")

    if "--security" in sys.argv:
        # Read-only report from the PRODUCTION project + behavioural anon checks
        # against the TEST project (a failed check there cannot pollute prod).
        BOT_TABLES = {TABLE_PLAYERS, TABLE_CURRENCIES, TABLE_VOICE_CONFIG, TABLE_SERVER_REGISTRY,
                      TABLE_WORLD_NONCES, TABLE_WORLD_COMMITMENTS, TABLE_WORLD_WITNESS_LOG,
                      TABLE_MINING_ATTEMPTS, TABLE_LEDGER, TABLE_ITEMS, TABLE_ITEM_DISPOSALS,
                      TABLE_MINING_RESULTS, TABLE_VOICE_TICKS, TABLE_PRODUCTION_POLICY, TABLE_NODE_STATE}
        BOT_FUNCTIONS = {"register_server(bigint,text,text,text)", "security_report()",
                         "next_mining_attempt(bigint,bigint)", "begin_swing(bigint,bigint,text,double precision)",
                         "record_swing(bigint,bigint,bigint,text,boolean,boolean,double precision,"
                         "double precision,text,text,jsonb)",
                         "sell_item(bigint,bigint,text,numeric,text)", "apply_voice_tick(bigint,text,jsonb)",
                         "set_player_prefs(bigint,bigint,text,boolean)", "register_nodes(bigint,jsonb)",
                         "money_supply(bigint)", "ledger_audit()"}
        RETIRED_FUNCTIONS = {"register_server(bigint,text,text)",      # v6: registrasi tanpa worldgen_version
                             "begin_swing(bigint,bigint)",              # v8: tanpa node & stamina minimum
                             "rest_player(bigint,bigint)"}              # v8: /rest dihapus

        prod = EconomyDatabase(create_client(normalize_supabase_url(os.getenv("SUPABASE_URL", "")),
                                             os.getenv("SUPABASE_KEY", "")))
        try:
            report = prod.security_report()
        except Exception as exc:
            fail("security_report() belum ada di project UTAMA → jalankan ulang SELURUH supabase/schema.sql "
                 f"(v4) di SQL Editor project utama.\n  detail: {exc}")
        print("\n[security] Project UTAMA (read-only)")
        check("key bot berjalan sebagai role", report["caller_role"], "service_role")
        check("role itu bypass RLS", report["caller_bypasses_rls"], True)
        for t in sorted((r for r in report["tables"] if r["table"] in BOT_TABLES), key=lambda r: r["table"]):
            check(f"{t['table']:<18} RLS aktif, 0 policy, anon/auth tanpa hak",
                  (t["rls_enabled"], t["policies"], t["anon_insert"], t["anon_select"], t["auth_insert"]),
                  (True, 0, False, False, False))
        check("semua tabel bot ada di laporan", BOT_TABLES <= {r["table"] for r in report["tables"]}, True)
        for f in (r for r in report["functions"] if r["function"] in BOT_FUNCTIONS):
            check(f"{f['function'][:48]:<48} tidak bisa dipanggil anon/auth",
                  (f["anon_execute"], f["auth_execute"]), (False, False))
        present = {r["function"] for r in report["functions"]}
        check("semua fungsi bot ada di laporan", sorted(BOT_FUNCTIONS - present), [])
        check("fungsi lama sudah dihapus (register_server 3 arg, begin_swing 2 arg, rest_player)",
              sorted(RETIRED_FUNCTIONS & present), [])

        anon_key = os.getenv("SUPABASE_TEST_ANON_KEY", "").strip()
        if not anon_key:
            print("  ·  SUPABASE_TEST_ANON_KEY kosong → uji perilaku anon dilewati")
        else:
            try:
                connect_test_database(os.environ)      # same guard: must not be the prod host
            except RuntimeError as exc:
                fail(str(exc))
            anon = create_client(normalize_supabase_url(os.environ["SUPABASE_TEST_URL"]), anon_key)
            print("\n[security] Uji perilaku dengan key ANON di project TES")
            for label, action in [
                ("anon INSERT server_registry",
                 lambda: anon.table(TABLE_SERVER_REGISTRY).insert(
                     {"guild_id": -999, "algo_version": "v1", "randomness_source": "x"}).execute()),
                ("anon INSERT world_nonces",
                 lambda: anon.table(TABLE_WORLD_NONCES).insert(
                     {"guild_id": -999, "drand_round": 1, "world_nonce": "0" * 64, "drand_signature": "0" * 96}).execute()),
                ("anon RPC register_server",
                 lambda: anon.rpc("register_server", {"p_guild_id": -999, "p_algo_version": "v1", "p_source": "x",
                                                      "p_worldgen_version": "dev"}).execute()),
                ("anon SELECT world_nonces",
                 lambda: anon.table(TABLE_WORLD_NONCES).select("*").limit(1).execute()),
                ("anon RPC next_mining_attempt",
                 lambda: anon.rpc("next_mining_attempt", {"p_guild_id": -999, "p_user_id": -999}).execute()),
                ("anon RPC sell_item",
                 lambda: anon.rpc("sell_item", {"p_guild_id": -999, "p_user_id": -999, "p_item_id": "swing:x",
                                                "p_payout": "1", "p_kind": "sell_ore"}).execute()),
                ("anon RPC apply_voice_tick",
                 lambda: anon.rpc("apply_voice_tick", {"p_guild_id": -999, "p_ref": "voice:anon",
                                                       "p_entries": []}).execute()),
                ("anon SELECT ledger",
                 lambda: anon.table(TABLE_LEDGER).select("*").limit(1).execute()),
                ("anon RPC register_nodes",
                 lambda: anon.rpc("register_nodes", {"p_guild_id": -999, "p_nodes": []}).execute()),
                ("anon UPDATE production_policy",
                 lambda: anon.table(TABLE_PRODUCTION_POLICY).update({"rest_enabled": True}).eq("scope", "global").execute()),
            ]:
                try:
                    data = action().data
                except Exception:
                    check(label, "ditolak", "ditolak")
                    continue
                # No exception: only a real PostgREST payload counts as "accepted".
                is_api = isinstance(data, (list, dict))
                check(label, "DITERIMA" if is_api else f"respons bukan API Supabase — {_URL_HINT}", "ditolak")

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
