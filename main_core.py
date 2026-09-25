"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       MAIN_CORE.PY  —  The Discord Interface Core                          ║
║       Orchestration Layer of the Procedural World System                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM IMPORTS (full dependency chain)                                   ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  identitas_genetik  →  GeneticEngine, ServerGeneticProfile                 ║
║  material_gen       →  MaterialEngine, ServerMaterialCatalog               ║
║  resource_spawner   →  ResourceSpawner, ServerSpawnState,                  ║
║                         ActiveOreNode, ActiveCrystalNode                   ║
║  mining_engine      →  MiningEngine, MiningResult, Pickaxe, PICKAXES       ║
║  ore                →  OreFactory, OreItem                                 ║
║  crystal            →  CrystalFactory, CrystalItem                         ║
║  voice_engine       →  VoiceSessionTracker, VoiceConfig, quote_reward      ║
║  db_ekonomi_pusat   →  EconomyDatabase (Supabase persistence)              ║
║  automine_engine    →  AutoMineEngine (1 swing per voice interval)         ║
║  world_registry     →  Stage 0: WorldRegistry + drand quicknet nonce       ║
║  world_seed         →  Stage 1: HMAC seed + pepper commit–reveal           ║
║  world_witness      →  External witness (public Discord webhook)           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • No `random` Module    — all game logic flows through deterministic       ║
║      engines.  This file contains zero stochastic calls.                   ║
║  • Write-Through Cache   — players, currencies and voice config are        ║
║      loaded from Supabase at startup and cached in the global dicts.       ║
║      Players are flushed every voice tick and on shutdown; currencies and  ║
║      voice config are written immediately.  World/spawn state is still     ║
║      in-memory only (regenerated deterministically from server DNA).       ║
║  • Persistent View IDs   — every UI component uses a stable custom_id     ║
║      format so callback resolution survives bot restarts cleanly.          ║
║  • Lean Component Budget — one Select for nodes, one Select for tools,     ║
║      one rest button.  Never approaches the 25-component API limit.        ║
║  • Single Interaction Per Node  — downstream extractions are committed     ║
║      in one atomic sequence: select node → select tool → execute.         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  IN-MEMORY STATE SCHEMA                                                      ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  _GLOBAL_SPAWN_REGISTRY: Dict[int, ServerSpawnState]                        ║
║      guild_id → ServerSpawnState (ore + crystal nodes, live reserves)      ║
║                                                                              ║
║  _GLOBAL_CATALOG_REGISTRY: Dict[int, ServerMaterialCatalog]                 ║
║      guild_id → frozen geological catalog (needed by OreFactory)           ║
║                                                                              ║
║  _GLOBAL_PLAYER_REGISTRY: Dict[(int, int), PlayerProfile]                   ║
║      (guild_id, user_id) → PlayerProfile(stamina, wallet, xp, bags, ...)   ║
║                                                                              ║
║  _GLOBAL_VOICE_CONFIG_REGISTRY: Dict[int, VoiceConfig]                      ║
║      guild_id → voice tracker settings (/voiceconfig)                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  SETUP (one-time)                                                            ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1. Copy .env.example → .env and fill DISCORD_TOKEN, SUPABASE_URL,          ║
║     SUPABASE_KEY (service_role / secret key).                               ║
║  2. Run supabase/schema.sql once in the Supabase SQL Editor.                ║
║  3. Install deps:                                                            ║
║         pip install -r requirements.txt                                     ║
║  4. Run:                                                                     ║
║         python main_core.py                                                 ║
║                                                                              ║
║  SLASH COMMAND SYNC                                                          ║
║  on_ready() calls bot.tree.sync() globally.  First sync may take up to     ║
║  one hour to propagate to all guilds via Discord's CDN.  For instant       ║
║  local testing, replace with:                                               ║
║      MY_GUILD = discord.Object(id=YOUR_GUILD_ID)                           ║
║      await bot.tree.sync(guild=MY_GUILD)                                   ║
║  and register commands with @bot.tree.command(guild=MY_GUILD).             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import os
import sys
import math
import signal
import time
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ── Upstream module resolution ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from identitas_genetik import GeneticEngine, ServerGeneticProfile
from material_gen import MaterialEngine, ServerMaterialCatalog
from resource_spawner import (
    ResourceSpawner,
    ServerSpawnState,
    ActiveOreNode,
    ActiveCrystalNode,
    ACCESS_RESTRICTED,
)
from mining_engine import MiningEngine, MiningResult, Pickaxe, PICKAXES
from ore import OreFactory, OreItem
from crystal import CrystalFactory, CrystalItem, VARIANT_GEM, VARIANT_SPLINTER
from economy import EconomyOracle
from economy_engine import EconomyEngine
from economy_engine import EconomyEngine
from currency_engine import CurrencyEngine, preview_manifest, CurrencyManifest
from mint_cap import CentralBankEngine
from voice_engine import (
    VoiceSessionTracker,
    VoiceConfig,
    MemberVoiceSnapshot,
    quote_reward,
    level_for_xp,
    xp_for_level,
    TRACKER_TICK_SECONDS,
    XP_PER_INTERVAL,
    MIN_REWARD_INTERVAL_MINUTES,
    MAX_REWARD_INTERVAL_MINUTES,
    BLOCK_AFK_CHANNEL,
    BLOCK_MUTE_DEAF,
    BLOCK_ALONE,
)
from db_ekonomi_pusat import EconomyDatabase, normalize_supabase_url
from automine_engine import AutoMineEngine, AutoSwing, choose_best_node, node_label
from world_registry import (
    WorldRegistry,
    WorldRecord,
    DrandQuicknet,
    BeaconUnavailable,
    BeaconNotYet,
    STATUS_ACTIVE,
)
from world_seed import SeedService, load_peppers, reconcile_commitments, PepperError
from world_witness import WebhookWitness, pending_events
import dataclasses
from supabase import create_client, Client

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
SUPABASE_URL: str = normalize_supabase_url(os.getenv("SUPABASE_URL", ""))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL atau SUPABASE_KEY tidak ditemukan di .env!")

# Stage 1 pepper.  Refusing to start is deliberate: an empty or weak pepper
# would silently make every world seed guessable.
try:
    _PEPPERS = load_peppers(os.environ)
except PepperError as exc:
    raise SystemExit(f"[main_core] Bot menolak start.\n{exc}") from None

WORLD_LOG_WEBHOOK: str = os.getenv("WORLD_LOG_WEBHOOK", "")

db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
_economy_db = EconomyDatabase(db)
_drand = DrandQuicknet()
_world_registry = WorldRegistry(_economy_db, [_drand])
_seed_service = SeedService(_PEPPERS)
_witness = WebhookWitness(WORLD_LOG_WEBHOOK)

# supabase-py is synchronous.  Every DB call goes through this single worker
# thread so the Discord event loop never blocks and calls never overlap.
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="supabase")


async def _run_db(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(_DB_EXECUTOR, fn, *args)

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  [%(levelname)s]  %(name)s: %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main_core")
# httpx logs every Supabase request at INFO; the player flush runs every tick.
logging.getLogger("httpx").setLevel(logging.WARNING)

if not DISCORD_TOKEN:
    log.error("DISCORD_TOKEN not found in environment. Create a .env file with:")
    log.error("    DISCORD_TOKEN=your_bot_token_here")
    # Don't sys.exit here — allow import for testing without a live token.


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — STATELESS SERVICE SINGLETONS
# ─────────────────────────────────────────────────────────────────────────────
# All engines are stateless — safe to instantiate once at module level.

_genetic_engine  = GeneticEngine()
_material_engine = MaterialEngine()
_spawner         = ResourceSpawner()
_miner           = MiningEngine()
_ore_factory     = OreFactory()
_crystal_factory = CrystalFactory()
_auto_miner      = AutoMineEngine(_miner, _crystal_factory)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — PERSISTENCE LAYER (write-through cache over Supabase)
# ─────────────────────────────────────────────────────────────────────────────
# Players, currencies and voice config are loaded from Supabase in
# BawanBot.setup_hook() and written back via db_ekonomi_pusat.  World state
# (spawn/catalog/profile) stays in-memory and is rebuilt deterministically.

PlayerKey = Tuple[int, int]   # (guild_id, user_id)

_GLOBAL_SPAWN_REGISTRY:   Dict[int, ServerSpawnState]       = {}
_GLOBAL_CATALOG_REGISTRY: Dict[int, ServerMaterialCatalog]  = {}
_GLOBAL_PROFILE_REGISTRY: Dict[int, ServerGeneticProfile]   = {}
_GLOBAL_PLAYER_REGISTRY:  Dict[PlayerKey, "PlayerProfile"]  = {}
_GLOBAL_CURRENCY_REGISTRY: Dict[int, "CurrencyManifest"] = {}
_GLOBAL_VOICE_CONFIG_REGISTRY: Dict[int, VoiceConfig]      = {}
_GLOBAL_WORLD_RECORDS: Dict[int, WorldRecord]              = {}   # Stage 0 cache
_WORLD_COMMITMENT_ROWS: List[dict]                         = []   # Stage 1 (published)
_WITNESS_DELIVERED: Set[str]                               = set()

# Last row successfully written to Supabase per player; used to flush only
# profiles that actually changed.
_PLAYER_SAVED_ROWS: Dict[PlayerKey, dict] = {}

# Constant for default player stamina.
_DEFAULT_STAMINA: float = 100.0


@dataclass
class PlayerProfile:
    """
    Runtime profile for one Discord user INSIDE one server.
    Persisted as one row of the Supabase `players` table.

    stamina        : Current stamina (0.0 – 100.0).  Decays per mining swing.
    pickaxe_key    : Key into mining_engine.PICKAXES; last-used tool.
    ore_bag        : List of OreItem frozen records harvested.
    crystal_bag    : List of CrystalItem frozen records harvested.
    wallet         : Balance in this server's official currency.
    xp / level     : Progression (level derived via voice_engine.level_for_xp).
    voice_seconds  : Total time spent in non-AFK voice channels.
    automine       : Registered for auto mining (/automine daftar).
    """
    stamina:      float            = _DEFAULT_STAMINA
    pickaxe_key:  str              = "copper_starter"
    ore_bag:      List[OreItem]    = field(default_factory=list)
    crystal_bag:  List[CrystalItem]= field(default_factory=list)
    wallet:       float            = 0.0
    xp:           int              = 0
    level:        int              = 1
    voice_seconds: float           = 0.0
    automine:     bool             = False

    def to_row(self, guild_id: int, user_id: int) -> dict:
        return {
            "guild_id":      guild_id,
            "user_id":       user_id,
            "stamina":       self.stamina,
            "pickaxe_key":   self.pickaxe_key,
            "wallet":        self.wallet,
            "xp":            self.xp,
            "level":         self.level,
            "voice_seconds": round(self.voice_seconds, 3),
            "ore_bag":       [item.to_dict() for item in self.ore_bag],
            "crystal_bag":   [item.to_dict() for item in self.crystal_bag],
            "automine":      self.automine,
        }

    @classmethod
    def from_row(cls, row: dict) -> "PlayerProfile":
        return cls(
            stamina       = float(row["stamina"]),
            pickaxe_key   = row["pickaxe_key"],
            ore_bag       = [OreItem(**d) for d in row["ore_bag"]],
            crystal_bag   = [CrystalItem(**d) for d in row["crystal_bag"]],
            wallet        = float(row["wallet"]),
            xp            = int(row["xp"]),
            level         = int(row["level"]),
            voice_seconds = float(row["voice_seconds"]),
            automine      = bool(row["automine"]),
        )


def _get_player(guild_id: int, user_id: int) -> PlayerProfile:
    """Return existing PlayerProfile or create a fresh one."""
    key = (guild_id, user_id)
    if key not in _GLOBAL_PLAYER_REGISTRY:
        _GLOBAL_PLAYER_REGISTRY[key] = PlayerProfile()
    return _GLOBAL_PLAYER_REGISTRY[key]

def _get_server_circulation(guild_id: int) -> float:
    """Menghitung total uang fiat yang sedang beredar di tangan semua player server ini."""
    total = 0.0
    for (g_id, _), profile in _GLOBAL_PLAYER_REGISTRY.items():
        if g_id == guild_id:
            total += profile.wallet
    return total


def _get_voice_config(guild_id: int) -> VoiceConfig:
    return _GLOBAL_VOICE_CONFIG_REGISTRY.get(guild_id) or VoiceConfig(guild_id=guild_id)


async def _load_persistent_state() -> None:
    """Hydrate the caches from Supabase.  Raises if the schema is missing."""
    await _run_db(_economy_db.verify_schema)

    # Stage 1: every pepper must match its published commitment.  A mismatch
    # raises CommitmentMismatch and stops the bot — changing the pepper would
    # silently re-roll every world of that algo_version.
    newly = await _run_db(reconcile_commitments, _economy_db, _PEPPERS)
    _WORLD_COMMITMENT_ROWS[:] = await _run_db(_economy_db.load_commitment_rows)
    for version, commitment in _seed_service.commitments().items():
        log.info("Pepper commitment %s = SHA-256(pepper) = %s%s",
                 version, commitment, "  ← BARU di-commit, salin ke README" if version in newly else "")
    _WITNESS_DELIVERED.update(await _run_db(_economy_db.load_witness_keys))
    if not _witness.enabled:
        log.warning("WORLD_LOG_WEBHOOK kosong — saksi eksternal nonaktif; event menunggu di antrean sampai diisi.")

    for row in await _run_db(_economy_db.load_player_rows):
        key = (int(row["guild_id"]), int(row["user_id"]))
        profile = PlayerProfile.from_row(row)
        _GLOBAL_PLAYER_REGISTRY[key] = profile
        _PLAYER_SAVED_ROWS[key] = profile.to_row(*key)

    _GLOBAL_CURRENCY_REGISTRY.update(await _run_db(_economy_db.load_currencies))
    _GLOBAL_VOICE_CONFIG_REGISTRY.update(await _run_db(_economy_db.load_voice_configs))
    # RegistryIntegrityError propagates on purpose: a tampered world row must
    # stop the bot loudly instead of being silently used.
    _GLOBAL_WORLD_RECORDS.update(await _run_db(_world_registry.load_all))
    log.info(
        "Supabase loaded: %d player(s), %d currency(ies), %d voice config(s), %d world(s) (%d pending).",
        len(_GLOBAL_PLAYER_REGISTRY), len(_GLOBAL_CURRENCY_REGISTRY), len(_GLOBAL_VOICE_CONFIG_REGISTRY),
        len(_GLOBAL_WORLD_RECORDS),
        sum(1 for r in _GLOBAL_WORLD_RECORDS.values() if r.status != STATUS_ACTIVE),
    )


async def _flush_players() -> int:
    """Upsert every profile whose row differs from the last saved one."""
    dirty = []
    for key, profile in list(_GLOBAL_PLAYER_REGISTRY.items()):
        row = profile.to_row(*key)
        if _PLAYER_SAVED_ROWS.get(key) != row:
            dirty.append(row)
    if not dirty:
        return 0
    await _run_db(_economy_db.upsert_player_rows, dirty)
    for row in dirty:
        _PLAYER_SAVED_ROWS[(row["guild_id"], row["user_id"])] = row
    return len(dirty)

class WorldPending(Exception):
    """The guild has no active Stage-0 world nonce yet (drand round pending)."""


_WORLD_PENDING_TEXT: str = (
    "🌱 Dunia server ini sedang dibentuk dari beacon publik drand. "
    "Coba lagi beberapa detik lagi — cek status di `/worldproof`."
)


def _hydrate_server(guild: discord.Guild) -> tuple[
    ServerGeneticProfile, ServerMaterialCatalog, ServerSpawnState
]:
    """
    Ensure the server has a fully initialised world state.

    If the guild is already in the registry → return cached state (SKIP).
    If not → run the full cascade and cache the results.

    Entropy: Stage 0 nonce (drand) → Stage 1 seed (HMAC with pepper, guild_id
    in the pre-image) → Stage 2 per-domain streams inside each engine.  The
    guild's creation time and name never feed the world.

    Raises WorldPending while the guild has no active Stage-0 nonce — there
    is deliberately no fallback world.

    Returns (profile, catalog, spawn_state) — all frozen/cached objects.
    """
    gid = guild.id

    if gid in _GLOBAL_SPAWN_REGISTRY:
        return (
            _GLOBAL_PROFILE_REGISTRY[gid],
            _GLOBAL_CATALOG_REGISTRY[gid],
            _GLOBAL_SPAWN_REGISTRY[gid],
        )

    record = _GLOBAL_WORLD_RECORDS.get(gid)
    if record is None or record.status != STATUS_ACTIVE:
        raise WorldPending(gid)

    log.info("Hydrating new server: %s (id=%d, %s)", guild.name, gid, record.algo_version)

    seed = _world_seed(record)
    created_at: int = int(guild.created_at.timestamp())   # informational only

    profile  = _genetic_engine.generate_profile(server_id=gid, seed=seed, created_at=created_at)
    catalog  = _material_engine.generate_geology(profile, seed)
    state    = _spawner.initialise(profile, catalog, seed)

    _GLOBAL_PROFILE_REGISTRY[gid]  = profile
    _GLOBAL_CATALOG_REGISTRY[gid]  = catalog
    _GLOBAL_SPAWN_REGISTRY[gid]    = state

    log.info(
        "Server %s hydrated: %d ore nodes, %d crystal nodes | biomes: %s",
        guild.name,
        len(state.active_ores),
        len(state.active_crystals),
        " | ".join(profile.biome_affinity),
    )
    return profile, catalog, state


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — EMBED BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

_DEPTH_ICONS: Dict[str, str] = {
    "SURFACE": "🌿",
    "SHALLOW": "⛏",
    "DEEP":    "🔩",
    "ABYSS":   "💀",
}

_AFFINITY_ICONS: Dict[str, str] = {
    "POWER":    "⚡",
    "MANA":     "✨",
    "MUTATION": "☢",
    "UTILITY":  "🔧",
    "DEFENSE":  "🛡",
}

_QUALITY_ICONS: Dict[str, str] = {
    "Flawed":    "🔹",
    "Prismatic": "🔷",
    "Ethereal":  "💎",
}


def _reserve_bar(fraction: float, width: int = 10) -> str:
    """Render a simple ASCII progress bar for node reserve level."""
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _build_geology_embed(
    guild:   discord.Guild,
    profile: ServerGeneticProfile,
    catalog: ServerMaterialCatalog,
    state:   ServerSpawnState,
    is_cached: bool = False,
) -> discord.Embed:
    """
    Build the main geology status embed for /explore_mines.

    Shows macro world DNA, tectonic status, active node counts, and a
    resource score summary.  Node details live in the Select dropdown.
    """
    tectonic_bar = _reserve_bar(catalog.tectonic_activity)
    pressure_bar = _reserve_bar(catalog.pressure_index)

    # Colour: hot orange-red for high tectonic worlds, deep blue for stable
    tectonic_val = catalog.tectonic_activity
    if tectonic_val >= 0.75:
        colour = discord.Colour.from_rgb(230, 80, 20)   # lava orange
    elif tectonic_val >= 0.45:
        colour = discord.Colour.from_rgb(180, 120, 40)  # amber
    else:
        colour = discord.Colour.from_rgb(40, 80, 180)   # stable deep blue

    em = discord.Embed(
        title       = f"🌍  {guild.name}  —  Geological Survey",
        description = (
            f"*{'World state loaded from cache.' if is_cached else 'World state initialised for this server.'}*\n"
            f"Biomes: **{'** | **'.join(profile.biome_affinity)}**"
        ),
        colour      = colour,
    )

    # ── Macro DNA block ───────────────────────────────────────────────────────
    em.add_field(
        name  = "🧬  World DNA",
        value = (
            f"`Age      ` **{profile.world_age}**\n"
            f"`Stability` **{profile.base_world_stability:.3f}**\n"
            f"`Mutation ` **{profile.base_mutation_index:.3f}**\n"
            f"`Density  ` **{profile.base_resource_density:.3f}**"
        ),
        inline = True,
    )

    # ── Geology readings ──────────────────────────────────────────────────────
    em.add_field(
        name  = "⚙️  Geology",
        value = (
            f"`Rating   ` **{catalog.geological_rating}**\n"
            f"`Tectonic ` {tectonic_bar} {catalog.tectonic_activity:.2f}\n"
            f"`Pressure ` {pressure_bar} {catalog.pressure_index:.2f}\n"
            f"`Dominance` **{catalog.dominance_ratio:.2f}**"
        ),
        inline = True,
    )

    # ── Resource scores (bridge to economy.py) ────────────────────────────────
    em.add_field(
        name  = "📊  Resource Scores",
        value = (
            f"`Industrial` **{catalog.industrial_resource_score:.2f}**\n"
            f"`Strategic ` **{catalog.strategic_resource_score:.2f}**\n"
            f"`Luxury    ` **{catalog.luxury_resource_score:.2f}**\n"
            f"`Total     ` **{catalog.total_resource_score:.2f}**"
        ),
        inline = True,
    )

    # ── Active node summary ───────────────────────────────────────────────────
    active_ores      = sum(1 for n in state.active_ores.values()     if not n.is_depleted)
    active_crystals  = sum(1 for n in state.active_crystals.values() if not n.is_depleted)
    depleted_ores    = len(state.active_ores)     - active_ores
    depleted_crysts  = len(state.active_crystals) - active_crystals

    em.add_field(
        name  = "🗺️  Active Nodes",
        value = (
            f"⛏ Ore nodes    : **{active_ores}** active  ({depleted_ores} depleted)\n"
            f"💎 Crystal nodes: **{active_crystals}** active  ({depleted_crysts} depleted)\n"
            f"Use the dropdown below to select a node."
        ),
        inline = False,
    )

    # ── World flavour tags ────────────────────────────────────────────────────
    if profile.world_flavour_tags:
        em.add_field(
            name  = "🏷️  World Tags",
            value = "  ".join(f"`{t}`" for t in profile.world_flavour_tags),
            inline = False,
        )

    em.set_footer(text=(
        f"Genetic signature: {profile.genetic_signature[:16]}…  "
        f"| Ore nodes: {len(state.active_ores)}  "
        f"| Crystal nodes: {len(state.active_crystals)}"
    ))
    return em


def _build_result_embed(
    result:  MiningResult,
    item:    Union[OreItem, CrystalItem],
    player:  PlayerProfile,
) -> discord.Embed:
    """
    Build the extraction result embed shown after a successful mining attempt.
    Includes the flavour message, item stats, and updated stamina bar.
    """
    is_crystal = isinstance(item, CrystalItem)

    if result.critical_hit:
        colour = discord.Colour.gold()
        title  = "✨  Critical Strike!"
    elif result.resource_type == VARIANT_SPLINTER:
        colour = discord.Colour.orange()
        title  = "💥  Crystal Fracture!"
    elif is_crystal:
        colour = discord.Colour.from_rgb(100, 180, 255)
        title  = "💎  Crystal Harvested"
    else:
        colour = discord.Colour.from_rgb(140, 100, 60)
        title  = "⛏  Ore Extracted"

    em = discord.Embed(title=title, colour=colour)

    # Flavour message as the main body
    em.description = f"*{result.flavour_message}*"

    # Item stats
    if is_crystal:
        c: CrystalItem = item  # type: ignore[assignment]
        em.add_field(
            name  = "📦  Item Acquired",
            value = (
                f"`Name      ` **{c.display_name}**\n"
                f"`Type      ` {c.crystal_type}  ({c.origin_variant})\n"
                f"`Affinity  ` {_AFFINITY_ICONS.get(c.crystal_affinity, '')} {c.crystal_affinity}\n"
                f"`Quality   ` {_QUALITY_ICONS.get(c.quality, '')} {c.quality}\n"
                f"`Mass      ` **{c.weight_tonnes:.4f}** t\n"
                f"`Base Value` **{c.base_market_value:.4f}**"
            ),
            inline = True,
        )
    else:
        o: OreItem = item  # type: ignore[assignment]
        em.add_field(
            name  = "📦  Item Acquired",
            value = (
                f"`Name      ` **{o.display_name}**\n"
                f"`Origin    ` {o.origin_type}\n"
                f"`Purity    ` **{o.purity}**\n"
                f"`Mass      ` **{o.weight_tonnes:.4f}** t\n"
                f"`Base Value` **{o.base_market_value:.4f}**"
            ),
            inline = True,
        )

    # Player stamina
    stamina_bar = _reserve_bar(player.stamina / _DEFAULT_STAMINA)
    em.add_field(
        name  = "💪  Stamina",
        value = f"{stamina_bar}  **{player.stamina:.1f}** / {_DEFAULT_STAMINA:.0f}",
        inline = True,
    )

    em.set_footer(text=(
        f"Node: {result.node_id}  "
        f"| Depleted: {'Yes 🔴' if result.is_node_depleted else 'No 🟢'}"
        f"{'  | CRIT! ✨' if result.critical_hit else ''}"
    ))
    return em


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — CUSTOM_ID HELPERS
# ─────────────────────────────────────────────────────────────────────────────
# custom_id format must be stable across restarts for persistent view routing.
# We encode enough context into the ID so the callback never needs to re-query.

def _node_select_custom_id(guild_id: int) -> str:
    return f"node_select:{guild_id}"

def _tool_select_custom_id(guild_id: int, node_id: str) -> str:
    # node_id can contain colons; Discord custom_id max length is 100 chars.
    # We hash the node_id to keep the ID short and safe.
    import hashlib
    node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
    return f"tool_select:{guild_id}:{node_hash}"

def _rest_button_custom_id(guild_id: int) -> str:
    return f"rest:{guild_id}"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — UI VIEWS
# ─────────────────────────────────────────────────────────────────────────────

class NodeSelectView(discord.ui.View):
    """
    The primary view attached to the /explore_mines embed.

    Contains:
      • A Select menu listing all non-depleted ore and crystal nodes (up to 25).
      • A /rest button to restore stamina to 100.

    On node selection → swaps itself out for ToolSelectView on the same message.
    """

    def __init__(self, guild_id: int) -> None:
        # timeout=None makes the view persistent (survives restarts when
        # re-registered in on_ready, but for mock-state it means no auto-expiry).
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self._build_node_select()
        self._build_rest_button()

    def _build_node_select(self) -> None:
        state   = _GLOBAL_SPAWN_REGISTRY.get(self.guild_id)
        if state is None:
            return

        options: list[discord.SelectOption] = []

        # ── Ore nodes (non-depleted first) ────────────────────────────────────
        for node_id, node in state.active_ores.items():
            if node.is_depleted:
                continue
            depth_icon  = _DEPTH_ICONS.get(node.depth_layer, "⛏")
            bar         = _reserve_bar(node.reserve_fraction(), width=6)
            lock        = "🔒 " if node.access_tier == ACCESS_RESTRICTED else ""
            label       = f"{lock}[{node.depth_layer}] {node.ore_name} ({node.purity})"
            description = f"{depth_icon} {bar} {node.current_reserve:.1f}/{node.max_reserve:.1f} t"
            options.append(discord.SelectOption(
                label       = label[:100],          # Discord limit
                value       = node_id[:100],
                description = description[:100],
                emoji       = "⛏",
            ))
            if len(options) >= 20:   # Leave room for crystals
                break

        # ── Crystal nodes (non-depleted) ──────────────────────────────────────
        for node_id, node in state.active_crystals.items():
            if node.is_depleted:
                continue
            aff_icon    = _AFFINITY_ICONS.get(node.crystal_affinity, "💎")
            q_icon      = _QUALITY_ICONS.get(node.quality, "🔹")
            bar         = _reserve_bar(node.reserve_fraction(), width=6)
            lock        = "🔒 " if node.access_tier == ACCESS_RESTRICTED else ""
            label       = f"{lock}[{node.depth_layer}] {node.name}"
            description = (
                f"{q_icon} {node.quality}  {aff_icon} {node.crystal_affinity}  "
                f"{bar} {node.current_reserve:.1f}/{node.max_reserve:.1f}"
            )
            options.append(discord.SelectOption(
                label       = label[:100],
                value       = node_id[:100],
                description = description[:100],
                emoji       = "💎",
            ))
            if len(options) >= 25:
                break

        if not options:
            options.append(discord.SelectOption(
                label       = "No active nodes",
                value       = "__none__",
                description = "All nodes are depleted. Wait for regeneration.",
                emoji       = "🔴",
            ))

        select = discord.ui.Select(
            custom_id   = _node_select_custom_id(self.guild_id),
            placeholder = "🗺️  Select a mining node...",
            min_values  = 1,
            max_values  = 1,
            options     = options,
        )
        select.callback = self._on_node_selected
        self.add_item(select)

    def _build_rest_button(self) -> None:
        btn = discord.ui.Button(
            label     = "💤  Rest (restore stamina)",
            style     = discord.ButtonStyle.secondary,
            custom_id = _rest_button_custom_id(self.guild_id),
        )
        btn.callback = self._on_rest
        self.add_item(btn)

    async def _on_node_selected(self, interaction: discord.Interaction) -> None:
        node_id: str = interaction.data["values"][0]  # type: ignore[index]

        if node_id == "__none__":
            await interaction.response.send_message(
                "⚠️ All nodes are depleted. Come back later or wait for geological regeneration.",
                ephemeral=True,
            )
            return

        state = _GLOBAL_SPAWN_REGISTRY.get(self.guild_id)
        if state is None:
            await interaction.response.send_message("State not found. Re-run `/explore_mines`.", ephemeral=True)
            return

        # Verify node still active
        node = state.active_ores.get(node_id) or state.active_crystals.get(node_id)
        if node is None or node.is_depleted:
            await interaction.response.send_message(
                f"🔴 Node `{node_id[:40]}` is depleted or not found.", ephemeral=True
            )
            return

        # Swap to the tool selection view (ephemeral to avoid chat clutter)
        tool_view = ToolSelectView(
            guild_id = self.guild_id,
            node_id  = node_id,
            user_id  = interaction.user.id,
        )
        node_label = (
            node.ore_name if isinstance(node, ActiveOreNode) else node.name  # type: ignore
        )
        await interaction.response.send_message(
            content   = f"⛏  **{node_label}** selected.\nChoose your mining tool:",
            view      = tool_view,
            ephemeral = True,
        )

    async def _on_rest(self, interaction: discord.Interaction) -> None:
        player = _get_player(self.guild_id, interaction.user.id)
        old_stamina = player.stamina
        player.stamina = _DEFAULT_STAMINA
        await interaction.response.send_message(
            content   = (
                f"💤  **{interaction.user.display_name}** rested.\n"
                f"Stamina restored: **{old_stamina:.1f}** → **{_DEFAULT_STAMINA:.0f}**"
            ),
            ephemeral = True,
        )


class ToolSelectView(discord.ui.View):
    """
    Ephemeral view (sent as ephemeral=True) for tool selection.

    Presents the PICKAXES catalogue as a Select menu.
    On tool selection → executes MiningEngine → posts result embed.
    """

    def __init__(self, guild_id: int, node_id: str, user_id: int) -> None:
        super().__init__(timeout=120)   # Ephemeral — 2 min timeout is fine
        self.guild_id = guild_id
        self.node_id  = node_id
        self.user_id  = user_id
        self._build_tool_select()

    def _build_tool_select(self) -> None:
        player   = _get_player(self.guild_id, self.user_id)
        options  = []

        tool_display_order = [
            "copper_starter", "iron_standard", "titanium_drill", "abyss_resonator",
        ]
        for key in tool_display_order:
            pick = PICKAXES.get(key)
            if pick is None:
                continue
            default = (key == player.pickaxe_key)
            label   = pick.name
            desc    = (
                f"Power: {pick.power:.0f}  |  Efficiency: {pick.efficiency:.2f}  |  "
                f"Durability: {pick.durability}"
            )
            options.append(discord.SelectOption(
                label       = label[:100],
                value       = key,
                description = desc[:100],
                default     = default,
                emoji       = "🔨",
            ))

        select = discord.ui.Select(
            custom_id   = _tool_select_custom_id(self.guild_id, self.node_id),
            placeholder = "🔨  Choose your mining tool...",
            min_values  = 1,
            max_values  = 1,
            options     = options,
        )
        select.callback = self._on_tool_selected
        self.add_item(select)

    async def _on_tool_selected(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        pickaxe_key: str = interaction.data["values"][0]  # type: ignore[index]
        pickaxe = PICKAXES.get(pickaxe_key)
        if pickaxe is None:
            await interaction.followup.send("⚠️ Unknown tool selected.", ephemeral=True)
            return

        guild_id = self.guild_id
        user_id  = interaction.user.id

        state   = _GLOBAL_SPAWN_REGISTRY.get(guild_id)
        catalog = _GLOBAL_CATALOG_REGISTRY.get(guild_id)
        if state is None or catalog is None:
            await interaction.followup.send("Server state missing. Re-run `/explore_mines`.", ephemeral=True)
            return

        player = _get_player(guild_id, user_id)
        player.pickaxe_key = pickaxe_key

        # ── Stamina check ─────────────────────────────────────────────────────
        if player.stamina <= 0.0:
            await interaction.followup.send(
                "💨 You're exhausted! Use the **Rest** button to recover stamina.",
                ephemeral=True,
            )
            return

        # ── Execute mining attempt ────────────────────────────────────────────
        try:
            result: MiningResult = _miner.execute_mining_attempt(
                player_pickaxe = pickaxe,
                player_stamina = player.stamina,
                state          = state,
                node_id        = self.node_id,
                catalog        = catalog,
            )
        except (KeyError, ValueError) as exc:
            log.warning("Mining attempt error: %s", exc)
            await interaction.followup.send(f"⚠️ Mining error: {exc}", ephemeral=True)
            return

        # ── Deduct stamina ────────────────────────────────────────────────────
        player.stamina = max(0.0, player.stamina - result.stamina_consumed)

        # ── Pack item into inventory ──────────────────────────────────────────
        item: Optional[Union[OreItem, CrystalItem]] = None
        if result.success and result.amount_extracted > 0.0:
            try:
                if result.resource_type == "ORE":
                    item = OreFactory.create_item_from_mining(
                        mining_result = result,
                        catalog       = catalog,
                        owner_id      = user_id,
                        server_id     = guild_id,
                    )
                    player.ore_bag.append(item)

                elif result.resource_type in {VARIANT_GEM, VARIANT_SPLINTER}:
                    # Get the ActiveCrystalNode for CrystalFactory
                    crystal_node = state.active_crystals.get(self.node_id)
                    if crystal_node is not None:
                        item = _crystal_factory.create_item_from_mining(
                            mining_result = result,
                            active_node   = crystal_node,
                            owner_id      = user_id,
                        )
                        player.crystal_bag.append(item)

            except (ValueError, KeyError) as exc:
                log.warning("Item factory error: %s", exc)
                # Mining result still valid — just log and skip inventory commit

        # ── Build response ────────────────────────────────────────────────────
        if not result.success:
            await interaction.followup.send(
                content   = f"❌ {result.flavour_message}",
                ephemeral = True,
            )
            return

        if item is not None:
            result_embed = _build_result_embed(result, item, player)
            await interaction.followup.send(embed=result_embed, ephemeral=True)
        else:
            # success=True but zero yield (edge case: node depleted mid-swing)
            await interaction.followup.send(
                content   = f"⚠️ {result.flavour_message}\n*(No item produced — zero yield.)*",
                ephemeral = True,
            )

        # ── Refresh the public geology embed ─────────────────────────────────
        # Edit the original /explore_mines message to show updated reserves.
        # We do this via the original message reference stored in the view.
        # NodeSelectView is attached to the original message — we rebuild it
        # with fresh node data to reflect the depletion.
        try:
            profile  = _GLOBAL_PROFILE_REGISTRY[guild_id]
            new_view = NodeSelectView(guild_id)
            new_embed = _build_geology_embed(
                guild      = interaction.guild,  # type: ignore[arg-type]
                profile    = profile,
                catalog    = catalog,
                state      = state,
                is_cached  = True,
            )
            # We can only edit the original message if we have a reference.
            # The interaction's message is the ephemeral tool-picker, not the
            # original embed.  We store the original message on the view below.
            if hasattr(self, "_original_message") and self._original_message is not None:
                await self._original_message.edit(embed=new_embed, view=new_view)
        except Exception as exc:
            log.debug("Could not refresh geology embed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────

class BawanBot(commands.Bot):
    """commands.Bot + Supabase load on startup and a final flush on shutdown."""

    async def setup_hook(self) -> None:
        await _load_persistent_state()
        _voice_tick.start()
        _resolve_pending_worlds.start()
        _witness_tick.start()

    async def close(self) -> None:
        for loop in (_voice_tick, _resolve_pending_worlds, _witness_tick):
            if loop.is_running():
                loop.cancel()
        try:
            saved = await _flush_players()
            log.info("Shutdown flush: %d player(s) saved to Supabase.", saved)
        except Exception:
            log.exception("Shutdown flush to Supabase FAILED — recent changes may be lost.")
        await super().close()


# Intents.default() already includes voice_states, which the voice tracker needs.
intents = discord.Intents.default()
bot     = BawanBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    """
    Called when the bot is connected and ready.

    Syncs the application command tree globally.

    ── IMPORTANT NOTE ON SYNC SPEED ──────────────────────────────────────────
    Global sync propagates to all guilds within ~1 hour via Discord's CDN.
    For instant testing in your development server, use guild-scoped sync:

        MY_GUILD = discord.Object(id=YOUR_GUILD_ID_HERE)
        await bot.tree.sync(guild=MY_GUILD)

    and register commands with:

        @bot.tree.command(guild=MY_GUILD)

    Replace both occurrences (the decorator and this sync call) with the
    guild-scoped versions while developing, then remove guild= for production.
    ──────────────────────────────────────────────────────────────────────────
    """
    assert bot.user is not None
    log.info("Logged in as %s (id=%d)", bot.user.name, bot.user.id)

    synced = await bot.tree.sync()
    log.info("Synced %d application command(s) globally.", len(synced))

    # Guilds joined while the bot was offline get registered now (idempotent).
    for guild in bot.guilds:
        if guild.id not in _GLOBAL_WORLD_RECORDS:
            try:
                await _ensure_registered(guild, wait_for_round=False)
            except Exception:
                log.exception("Stage 0 registration failed for guild %s", guild.id)

    log.info("main_core.py is live.  Registered commands: %s",
             [c.name for c in bot.tree.get_commands()])


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — SLASH COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(
    name        = "explore_mines",
    description = "Survey your server's geological formations and start mining.",
)
async def explore_mines(interaction: discord.Interaction) -> None:
    """
    The genesis trigger.

    1. Hydrates the server's world state (or loads from cache).
    2. Sends a rich geology embed with a node selector and rest button.
    """
    await interaction.response.defer()

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "⚠️ This command can only be used inside a server.", ephemeral=True
        )
        return

    is_cached = guild.id in _GLOBAL_SPAWN_REGISTRY
    try:
        await _ensure_registered(guild, wait_for_round=True)
        profile, catalog, state = _hydrate_server(guild)
    except WorldPending:
        await interaction.followup.send(_WORLD_PENDING_TEXT, ephemeral=True)
        return

    embed = _build_geology_embed(
        guild     = guild,
        profile   = profile,
        catalog   = catalog,
        state     = state,
        is_cached = is_cached,
    )
    view = NodeSelectView(guild_id=guild.id)

    msg = await interaction.followup.send(embed=embed, view=view)

    # Attach the sent message to all ToolSelectViews that might be created
    # from this NodeSelectView, so they can refresh the embed after extraction.
    # We inject this reference into the view for downstream use.
    view._original_message = msg  # type: ignore[attr-defined]


@bot.tree.command(
    name        = "inventory",
    description = "Check your current stamina and item bag.",
)
@app_commands.guild_only()
async def inventory(interaction: discord.Interaction) -> None:
    """Show the calling player's current inventory and stamina in this server."""
    player = _get_player(interaction.guild_id, interaction.user.id)

    stamina_bar = _reserve_bar(player.stamina / _DEFAULT_STAMINA)
    em = discord.Embed(
        title  = f"🎒  {interaction.user.display_name}'s Inventory",
        colour = discord.Colour.blurple(),
    )
    em.add_field(
        name  = "💪  Stamina",
        value = f"{stamina_bar}  **{player.stamina:.1f}** / {_DEFAULT_STAMINA:.0f}",
        inline = False,
    )
    em.add_field(
        name  = "🔨  Equipped Tool",
        value = f"`{PICKAXES[player.pickaxe_key].name}`" if player.pickaxe_key in PICKAXES else "`None`",
        inline = True,
    )

    # Ore bag summary
    ore_count   = len(player.ore_bag)
    ore_value   = sum(i.base_market_value for i in player.ore_bag)
    em.add_field(
        name  = "⛏  Ore Bag",
        value = f"**{ore_count}** items  |  Total value: **{ore_value:.2f}**",
        inline = True,
    )

    # Crystal bag summary
    cryst_count = len(player.crystal_bag)
    cryst_value = sum(i.base_market_value for i in player.crystal_bag)
    em.add_field(
        name  = "💎  Crystal Bag",
        value = f"**{cryst_count}** items  |  Total value: **{cryst_value:.2f}**",
        inline = True,
    )

    # Last 5 ore items
    if player.ore_bag:
        recent_ores = player.ore_bag[-5:]
        ore_lines   = "\n".join(
            f"• {o.display_name} ({o.purity}) — {o.weight_tonnes:.3f}t  [{o.base_market_value:.2f}]"
            for o in reversed(recent_ores)
        )
        em.add_field(
            name  = "Recent Ores (last 5)",
            value = ore_lines[:1024],
            inline = False,
        )

    # Last 5 crystal items
    if player.crystal_bag:
        recent_crs  = player.crystal_bag[-5:]
        cr_lines    = "\n".join(
            f"• {c.display_name} ({c.quality}) — {c.weight_tonnes:.3f}t  [{c.base_market_value:.2f}]"
            for c in reversed(recent_crs)
        )
        em.add_field(
            name  = "Recent Crystals (last 5)",
            value = cr_lines[:1024],
            inline = False,
        )

    em.set_footer(text="Use /explore_mines to mine | /rest to restore stamina")
    await interaction.response.send_message(embed=em, ephemeral=True)


@bot.tree.command(
    name        = "rest",
    description = "Restore your stamina to full.",
)
@app_commands.guild_only()
async def rest(interaction: discord.Interaction) -> None:
    """Manual stamina restore command. Equivalent to the Rest button on the embed."""
    player      = _get_player(interaction.guild_id, interaction.user.id)
    old_stamina = player.stamina
    player.stamina = _DEFAULT_STAMINA
    await interaction.response.send_message(
        content   = (
            f"💤  **{interaction.user.display_name}** rested.\n"
            f"Stamina restored: **{old_stamina:.1f}** → **{_DEFAULT_STAMINA:.0f}**"
        ),
        ephemeral = True,
    )


@bot.tree.command(
    name        = "server_dna",
    description = "View your server's full genetic profile and biome breakdown.",
)
async def server_dna(interaction: discord.Interaction) -> None:
    """
    Shows the server's full ServerGeneticProfile — world age, elements, biomes,
    stability, mutation index.  Hydrates the server if not yet initialised.
    """
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Server-only command.", ephemeral=True)
        return

    try:
        await _ensure_registered(guild, wait_for_round=True)
        profile, catalog, _ = _hydrate_server(guild)
    except WorldPending:
        await interaction.followup.send(_WORLD_PENDING_TEXT, ephemeral=True)
        return

    em = discord.Embed(
        title  = f"🧬  {guild.name}  —  Genetic Profile",
        colour = discord.Colour.dark_green(),
    )
    em.add_field(
        name  = "🔑  Signature",
        value = f"`{profile.genetic_signature[:32]}…`",
        inline = False,
    )
    em.add_field(
        name  = "⚗️  Dominant Elements",
        value = (
            f"Metal    : **{profile.dominant_metal_element.name}** "
            f"[{profile.dominant_metal_element.symbol}]  "
            f"(rw={profile.dominant_metal_element.rarity_weight})\n"
            f"NonMetal : **{profile.dominant_nonmetal_element.name}** "
            f"[{profile.dominant_nonmetal_element.symbol}]"
        ),
        inline = False,
    )
    em.add_field(
        name  = "🌍  World Macro",
        value = (
            f"`Age      ` **{profile.world_age}**\n"
            f"`Stability` **{profile.base_world_stability:.6f}**\n"
            f"`Mutation ` **{profile.base_mutation_index:.6f}**\n"
            f"`Density  ` **{profile.base_resource_density:.6f}**"
        ),
        inline = True,
    )
    em.add_field(
        name  = "🗺️  Biomes",
        value = "\n".join(f"• `{b}`" for b in profile.biome_affinity),
        inline = True,
    )
    if profile.world_flavour_tags:
        em.add_field(
            name  = "🏷️  Flavour Tags",
            value = "  ".join(f"`{t}`" for t in profile.world_flavour_tags),
            inline = False,
        )

    await interaction.followup.send(embed=em, ephemeral=True)

# ── COMMAND MARKET STATUS (TERBARU) ──────────────────────────────────────────
@bot.tree.command(name="market_status", description="Cek kesehatan ekonomi dan kelayakan bank sentral lokal")
async def market_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id

    if guild_id not in _GLOBAL_SPAWN_REGISTRY:
        await interaction.followup.send("❌ Server ini belum di-survei. Ketik `/explore_mines` dulu, Bung!", ephemeral=True)
        return

    catalog = _GLOBAL_CATALOG_REGISTRY[guild_id]
    current_circulation = _get_server_circulation(guild_id)

    # 1. Panggil Arsitektur Mesin Terbaru (Bukan audit_monetary_health lagi!)
    report = EconomyEngine.evaluate_server_capability(catalog, current_circulation)

    # 2. Kalkulasi ulang inflation rate secara mandiri untuk keperluan display visual
    inflation_rate = current_circulation / report.max_allowed_circulation if report.max_allowed_circulation > 0 else 0.0

    # 3. Derivasi status UI berdasarkan hasil audit
    if not report.is_eligible:
        color = discord.Color.dark_grey()
        status_emoji = "❌ BELUM LAYAK CETAK UANG"
        keterangan = report.reasons[0] if report.reasons else "Tidak memenuhi syarat minimum Sovereign."
    elif inflation_rate >= 1.0:
        color = discord.Color.red()
        status_emoji = "🔴 HYPERINFLATION CRISIS"
        keterangan = report.reasons[0]
    elif inflation_rate >= 0.75:
        color = discord.Color.orange()
        status_emoji = "🟡 INFLATION WARNING"
        keterangan = report.reasons[0]
    else:
        color = discord.Color.green()
        status_emoji = "🟢 SECURE & HEALTHY"
        keterangan = report.reasons[0]

    em = discord.Embed(
        title = f"🏛️ Central Bank Monitor — {interaction.guild.name}",
        description = f"Status Moneter: **{status_emoji}**\n\n*Keterangan Bank Sentral:*\n> {keterangan}",
        color = color
    )

    inflation_bar = _reserve_bar(inflation_rate, width=12)

    em.add_field(name="📈 Indeks Inflasi", value=f"{inflation_bar} `{inflation_rate * 100:.2f}%`", inline=False)
    em.add_field(name="💰 Sirkulasi Fiat Lokal (M2)", value=f"`{current_circulation:.2f}` / `{report.max_allowed_circulation:.2f} Fiat`", inline=True)
    em.add_field(name="📊 Kapasitas Geologi", value=f"Score: **{report.geology_score:.2f}**", inline=True)
    
    multiplier_pct = report.seigniorage_modifier * 100
    em.add_field(
        name  = "💸 NPC Purchase Rate Modifier",
        value = f"Harga Beli Pedagang: **{multiplier_pct:.0f}%** dari nilai dasar Oracle.",
        inline = False
    )
    
    em.set_footer(text="Data agregat kuantitatif terpusat | Backed by UA standard")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── COMMAND SELL ORE (TERBARU) ───────────────────────────────────────────────
@bot.tree.command(name="sell_ore", description="Jual ore hasil tambang lu ke pasar NPC lokal")
@app_commands.describe(element_symbol="Simbol elemen atau nama ore (misal: Fe, Cu, Au, UA, Mineral Vein)")
async def sell_ore(interaction: discord.Interaction, element_symbol: str) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id
    user_id = interaction.user.id

    if guild_id not in _GLOBAL_SPAWN_REGISTRY:
        await interaction.followup.send("❌ Server ini belum di-survei. Ketik `/explore_mines` dulu, Bung!", ephemeral=True)
        return

    catalog = _GLOBAL_CATALOG_REGISTRY[guild_id]
    player = _get_player(guild_id, user_id)

    target_item = None
    search_query = element_symbol.strip().lower()
    
    for item in player.ore_bag:
        if (item.element_symbol.lower() == search_query or 
            item.display_name.lower() == search_query or 
            item.ore_name.lower() == search_query):
            target_item = item
            break

    if not target_item:
        await interaction.followup.send(f"❌ Di tas lu gak ada Ore dengan simbol atau nama `[{element_symbol}]`, Amerta!", ephemeral=True)
        return

    quote = EconomyOracle.calculate_ore_price(target_item, catalog)

    # Integrasi Layer 2
    current_circulation = _get_server_circulation(guild_id)
    audit = EconomyEngine.evaluate_server_capability(catalog, current_circulation)
    
    # Blokir transaksi jika server belum merdeka
    if not audit.is_eligible:
        alasan = "\n".join(audit.reasons)
        await interaction.followup.send(f"❌ **TRANSAKSI DITOLAK BANK SENTRAL**\nServer belum berdaulat.\n```text\n{alasan}\n```", ephemeral=True)
        return

    fiat_payout = round(quote.total_value * audit.seigniorage_modifier, 4)

    player.ore_bag.remove(target_item)
    player.wallet += fiat_payout

    em = discord.Embed(title="💰 NPC MARKET TRANSACTION SUCCESS", color=discord.Color.green())
    em.add_field(name="📦 Komoditas", value=f"`{target_item.display_name}`", inline=True)
    em.add_field(name="⚖️ Berat Bersih", value=f"`{target_item.weight_tonnes:.4f} Tonnes`", inline=True)
    em.add_field(name="💎 Kemurnian", value=f"`{target_item.purity}`", inline=True)
    em.add_field(name="📈 Scarcity Mult", value=f"`{quote.scarcity_multiplier}x`", inline=True)
    em.add_field(name="🏛️ Bank Modifier", value=f"`{audit.seigniorage_modifier}x`", inline=True)
    em.add_field(name="💸 Hasil Wallet", value=f"**+ {fiat_payout:.2f} Fiat**\nSaldo: **{player.wallet:.2f} Fiat**", inline=False)
    em.set_footer(text=f"Tx ID: {target_item.item_uuid[:12]}... | Backed by UA standard")

    await interaction.followup.send(embed=em, ephemeral=True)


# ── COMMAND SELL CRYSTAL (TERBARU) ───────────────────────────────────────────
@bot.tree.command(name="sell_crystal", description="Jual kristal hasil tambang lu ke pasar NPC lokal")
@app_commands.describe(crystal_name="Nama lengkap kristal yang mau dijual (misal: Abyssal Calcite, Pyro Quartz)")
async def sell_crystal(interaction: discord.Interaction, crystal_name: str) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id
    user_id = interaction.user.id

    if guild_id not in _GLOBAL_SPAWN_REGISTRY:
        await interaction.followup.send("❌ Server ini belum di-survei. Ketik `/explore_mines` dulu, Bung!", ephemeral=True)
        return

    catalog = _GLOBAL_CATALOG_REGISTRY[guild_id]
    player = _get_player(guild_id, user_id)

    target_item = None
    for item in player.crystal_bag:
        if item.display_name.strip().lower() == crystal_name.strip().lower():
            target_item = item
            break

    if not target_item:
        await interaction.followup.send(f"❌ Di tas kristal lu gak ada kristal bernama `[{crystal_name}]`, Amerta!", ephemeral=True)
        return

    purity_map = {"Flawed": 1.0, "Prismatic": 1.6, "Ethereal": 2.5}
    purity_mod = purity_map.get(target_item.quality, 1.0)
    
    if target_item.crystal_affinity in ["POWER", "MANA", "MUTATION"]:
        base_multiplier = max(30.0, catalog.strategic_resource_score * 0.25)
    else:
        base_multiplier = max(20.0, catalog.luxury_resource_score * 0.15)
        
    scarcity_mult = max(0.5, 2.0 - catalog.dominance_ratio)
    price_per_unit = base_multiplier * scarcity_mult * purity_mod
    raw_value = round(price_per_unit * (target_item.weight_tonnes * 0.1), 4)

    # Integrasi Layer 2
    current_circulation = _get_server_circulation(guild_id)
    audit = EconomyEngine.evaluate_server_capability(catalog, current_circulation)
    
    # Blokir transaksi jika server belum merdeka
    if not audit.is_eligible:
        alasan = "\n".join(audit.reasons)
        await interaction.followup.send(f"❌ **TRANSAKSI DITOLAK BANK SENTRAL**\nServer belum berdaulat.\n```text\n{alasan}\n```", ephemeral=True)
        return

    fiat_payout = round(raw_value * audit.seigniorage_modifier, 4)

    player.crystal_bag.remove(target_item)
    player.wallet += fiat_payout

    em = discord.Embed(title="🔮 NPC CRYSTAL MARKET TRANSACTION SUCCESS", color=discord.Color.blue())
    em.add_field(name="📦 Komoditas", value=f"`{target_item.display_name}`", inline=True)
    em.add_field(name="🛡️ Afinitas Magis", value=f"`{target_item.crystal_affinity}`", inline=True)
    em.add_field(name="🔷 Kualitas", value=f"`{target_item.quality}`", inline=True)
    em.add_field(name="📈 Scarcity Mult", value=f"`{round(scarcity_mult, 4)}x`", inline=True)
    em.add_field(name="🏛️ Bank Modifier", value=f"`{audit.seigniorage_modifier}x`", inline=True)
    em.add_field(name="💸 Hasil Wallet", value=f"**+ {fiat_payout:.2f} Fiat**\nSaldo: **{player.wallet:.2f} Fiat**", inline=False)
    em.set_footer(text=f"Tx ID: {target_item.item_uuid[:12]}... | Standard Jangkar UA")

    await interaction.followup.send(embed=em, ephemeral=True)

@bot.tree.command(name="found_currency", description="[ADMIN ONLY] Terbitkan mata uang fiat resmi server ini!")
@app_commands.describe(
    currency_name="Nama mata uang (misal: Amerta Dollar)",
    ticker="Kode ticker 2-5 huruf (misal: AMD)"
)
async def found_currency(interaction: discord.Interaction, currency_name: str, ticker: str) -> None:
    await interaction.response.defer(ephemeral=False) # Biar se-server bisa lihat pengumumannya!
    guild_id = interaction.guild_id

    # 1. Pastikan server sudah disurvei
    if guild_id not in _GLOBAL_SPAWN_REGISTRY:
        await interaction.followup.send("❌ Server belum di-survei. Jalankan `/explore_mines` dulu.")
        return

    # 2. Pastikan server belum punya mata uang
    if guild_id in _GLOBAL_CURRENCY_REGISTRY:
        existing = _GLOBAL_CURRENCY_REGISTRY[guild_id]
        await interaction.followup.send(f"❌ Server ini sudah meresmikan mata uang: **{existing.currency_name} ({existing.ticker})**!")
        return

    catalog = _GLOBAL_CATALOG_REGISTRY[guild_id]
    current_circulation = _get_server_circulation(guild_id)

    # 3. Audit Bank Sentral (Layer 2)
    audit = EconomyEngine.evaluate_server_capability(catalog, current_circulation)

    # 4. Coba Eksekusi Genesis (Layer 3)
    try:
        # Semua mata uang dimuat dari Supabase saat startup, jadi registry ini
        # berisi ticker seluruh ekosistem (UNIQUE constraint di DB jadi backstop).
        manifest = CurrencyEngine.establish_sovereign_currency(
            catalog=catalog,
            audit=audit,
            currency_name=currency_name,
            ticker=ticker,
            existing_tickers={m.ticker for m in _GLOBAL_CURRENCY_REGISTRY.values()},
        )

        # Tulis ke Supabase dulu; memori hanya diupdate kalau DB sukses.
        try:
            await _run_db(_economy_db.upsert_currency, manifest)
        except Exception:
            log.exception("Gagal menyimpan mata uang guild %s ke Supabase", guild_id)
            await interaction.followup.send("❌ **GENESIS FAILED**\nGagal menyimpan ke database. Coba lagi nanti.", ephemeral=True)
            return
        _GLOBAL_CURRENCY_REGISTRY[guild_id] = manifest

        # Render Output Estetik pake fungsi helper dari Claude
        report_text = preview_manifest(manifest)
        
        em = discord.Embed(
            title="🎉 SOVEREIGN FIAT GENESIS SUCCESS! 🎉",
            description=f"Server **{interaction.guild.name}** resmi mendeklarasikan kemerdekaan ekonomi!",
            color=discord.Color.gold()
        )
        em.add_field(name="📜 Currency Manifest", value=f"```text\n{report_text}\n```", inline=False)
        em.set_footer(text="Dicetak dan dijamin oleh Central Bank of Bawan | Layer 3 Consensus")

        await interaction.followup.send(embed=em)

    except ValueError as e:
        # Nangkep error dari validasi regex ticker/nama atau audit gagal
        await interaction.followup.send(f"❌ **GENESIS FAILED**\n{str(e)}", ephemeral=True)

@bot.tree.command(name="mint_fiat", description="[ADMIN ONLY] Cetak uang fiat lokal tambahan (Quantitative Easing)")
@app_commands.describe(amount="Jumlah uang yang mau dicetak (misal: 50000)")
async def mint_fiat(interaction: discord.Interaction, amount: float) -> None:
    await interaction.response.defer(ephemeral=False) # Biar se-server liat inflasi nambah wkwk
    guild_id = interaction.guild_id

    if guild_id not in _GLOBAL_SPAWN_REGISTRY:
        await interaction.followup.send("❌ Server belum di-survei. Ketik `/explore_mines` dulu.")
        return

    if guild_id not in _GLOBAL_CURRENCY_REGISTRY:
        await interaction.followup.send("❌ Server ini belum meresmikan mata uang. Pakai `/found_currency` dulu!")
        return

    catalog = _GLOBAL_CATALOG_REGISTRY[guild_id]
    manifest = _GLOBAL_CURRENCY_REGISTRY[guild_id]
    
    # 1. Lempar request ke Layer 4 (Operasional Bank Sentral)
    report = CentralBankEngine.evaluate_minting_request(
        manifest=manifest,
        current_geology_score=catalog.total_resource_score,
        mint_amount=amount
    )

    # 2. Jika Bank Sentral MENOLAK (Hard cap jebol / geologi hancur)
    if not report.success:
        em_fail = discord.Embed(
            title="⛔ PENCETAKAN UANG DITOLAK", 
            description=f"**Alasan:** {report.reason}", 
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=em_fail)
        return

    # 3. Jika Bank Sentral MENYETUJUI, update state manifest yang Frozen
    new_manifest = dataclasses.replace(
        manifest,
        total_supply=report.new_total_supply,
        circulating_supply=manifest.circulating_supply + report.amount_to_circulate,
        reserve_supply=manifest.reserve_supply + report.amount_to_reserve
    )
    
    # Simpan ke Supabase dulu, baru timpa state lama di memori
    try:
        await _run_db(_economy_db.upsert_currency, new_manifest)
    except Exception:
        log.exception("Gagal menyimpan hasil mint guild %s ke Supabase", guild_id)
        await interaction.followup.send("⛔ Pencetakan dibatalkan: gagal menyimpan ke database.")
        return
    _GLOBAL_CURRENCY_REGISTRY[guild_id] = new_manifest

    # 4. Render Output Estetik
    em = discord.Embed(
        title="🖨️ QUANTITATIVE EASING SUCCESS",
        description=f"Bank Sentral **{interaction.guild.name}** resmi mencetak uang baru!",
        color=discord.Color.green()
    )
    em.add_field(name="💵 Jumlah Dicetak", value=f"`+ {amount:,.2f} {manifest.ticker}`", inline=False)
    em.add_field(name="🔄 Masuk Sirkulasi (Pasar)", value=f"`+ {report.amount_to_circulate:,.2f}`", inline=True)
    em.add_field(name="🏦 Masuk Brankas (Reserve)", value=f"`+ {report.amount_to_reserve:,.2f}`", inline=True)
    em.add_field(name="📈 Total Supply Terkini", value=f"`{new_manifest.total_supply:,.2f} / {manifest.policy.hard_cap_supply:,.2f}`", inline=False)
    em.set_footer(text=report.reason)

    await interaction.followup.send(embed=em)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — VOICE ACTIVITY TRACKER
# ─────────────────────────────────────────────────────────────────────────────
# Pure rules live in voice_engine.py.  This section only turns Discord state
# into snapshots, applies rewards to PlayerProfile, and renders embeds.

_voice_tracker = VoiceSessionTracker()

_BLOCK_REASON_TEXT: Dict[str, str] = {
    BLOCK_ALONE:       "sendirian di VC",
    BLOCK_MUTE_DEAF:   "self-mute + deafen",
    BLOCK_AFK_CHANNEL: "di AFK channel",
}


def _format_duration(seconds: float) -> str:
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours}j {minutes}m" if hours else f"{minutes}m"


def _toggle(flag: bool) -> str:
    return "✅" if flag else "❌"


def _voice_snapshots(guild: discord.Guild) -> List[MemberVoiceSnapshot]:
    """Snapshot every human in every voice/stage channel of the guild."""
    afk_id = guild.afk_channel.id if guild.afk_channel else None
    snapshots: List[MemberVoiceSnapshot] = []
    for channel in [*guild.voice_channels, *guild.stage_channels]:
        humans = [m for m in channel.members if not m.bot]
        for member in humans:
            vs = member.voice
            snapshots.append(MemberVoiceSnapshot(
                user_id        = member.id,
                channel_id     = channel.id,
                is_afk_channel = channel.id == afk_id,
                self_mute      = bool(vs and vs.self_mute),
                self_deaf      = bool(vs and vs.self_deaf),
                human_count    = len(humans),
            ))
    return snapshots


def _sync_voice_guild(guild: discord.Guild) -> None:
    """Checkpoint the tracker for this guild and add elapsed VC time to profiles."""
    deltas = _voice_tracker.sync_guild(
        guild.id, _voice_snapshots(guild), _get_voice_config(guild.id), time.monotonic()
    )
    for user_id, seconds in deltas.items():
        _get_player(guild.id, user_id).voice_seconds += seconds


def _pay_voice_rewards(guild: discord.Guild) -> None:
    """Pay every completed interval in this guild's official currency + XP."""
    config  = _get_voice_config(guild.id)
    payouts = _voice_tracker.collect_payouts(guild.id, config)
    if not payouts:
        return

    manifest     = _GLOBAL_CURRENCY_REGISTRY.get(guild.id)
    has_currency = manifest is not None and manifest.is_active
    audit        = None
    if has_currency:
        # Same Layer-2 audit /sell_ore uses; hydration is deterministic + cached.
        try:
            _, catalog, _ = _hydrate_server(guild)
            audit = EconomyEngine.evaluate_server_capability(catalog, _get_server_circulation(guild.id))
        except WorldPending:
            audit = None     # no world yet → coin blocked (AUDIT_FAILED), XP still paid

    for payout in payouts:
        quote  = quote_reward(config, payout.intervals, has_currency=has_currency, audit=audit)
        player = _get_player(guild.id, payout.user_id)
        player.wallet += quote.coin
        player.xp     += quote.xp
        player.level   = level_for_xp(player.xp)
        log.info(
            "Voice reward guild=%d user=%d intervals=%d → +%.4f %s, +%d XP%s",
            guild.id, payout.user_id, payout.intervals, quote.coin,
            manifest.ticker if manifest else "Fiat", quote.xp,
            f" (coin blocked: {quote.coin_blocked_reason})" if quote.coin_blocked_reason else "",
        )
        if player.automine:
            _run_auto_mine(guild, payout.user_id, player, payout.intervals)


# ── Auto mine ────────────────────────────────────────────────────────────────
# One automatic swing per completed voice reward interval (see automine_engine).

# Last swing per player, shown in /automine status.  In-memory only.
_AUTOMINE_LAST: Dict[PlayerKey, AutoSwing] = {}


def _run_auto_mine(guild: discord.Guild, user_id: int, player: PlayerProfile, swings: int) -> None:
    try:
        _, catalog, state = _hydrate_server(guild)
    except WorldPending:
        return   # world still forming; the next interval will mine
    pickaxe = PICKAXES.get(player.pickaxe_key, PICKAXES["copper_starter"])
    for _ in range(swings):
        swing = _auto_miner.swing(
            pickaxe     = pickaxe,
            stamina     = player.stamina,
            max_stamina = _DEFAULT_STAMINA,
            state       = state,
            catalog     = catalog,
            owner_id    = user_id,
            server_id   = guild.id,
        )
        if swing is None:
            log.info("Auto-mine guild=%d user=%d: no minable node left", guild.id, user_id)
            return
        player.stamina = swing.stamina_after
        if isinstance(swing.item, OreItem):
            player.ore_bag.append(swing.item)
        elif isinstance(swing.item, CrystalItem):
            player.crystal_bag.append(swing.item)
        _AUTOMINE_LAST[(guild.id, user_id)] = swing
        log.info(
            "Auto-mine guild=%d user=%d → %s %.4f t from %s%s",
            guild.id, user_id, swing.item.display_name if swing.item else "nothing",
            swing.result.amount_extracted, swing.node_label,
            " (CRIT)" if swing.result.critical_hit else "",
        )


def _regenerate_world() -> None:
    """One regeneration tick for every hydrated server (nodes refill slowly)."""
    for state in _GLOBAL_SPAWN_REGISTRY.values():
        _spawner.apply_regeneration_tick(state)


@tasks.loop(seconds=TRACKER_TICK_SECONDS)
async def _voice_tick() -> None:
    """Sync all guilds, pay completed intervals (+ auto mine), regenerate nodes, flush to Supabase."""
    _voice_tracker.retain_guilds(g.id for g in bot.guilds)
    for guild in bot.guilds:
        try:
            _sync_voice_guild(guild)
            _pay_voice_rewards(guild)
        except Exception:
            log.exception("Voice tick failed for guild %s", guild.id)
    try:
        _regenerate_world()
    except Exception:
        log.exception("Node regeneration failed")
    try:
        await _flush_players()
    except Exception:
        log.exception("Saving players to Supabase failed; will retry next tick.")


@_voice_tick.before_loop
async def _before_voice_tick() -> None:
    await bot.wait_until_ready()


def _build_voice_embed(
    member:    discord.Member,
    before_ch: Optional[discord.abc.GuildChannel],
    after_ch:  Optional[discord.abc.GuildChannel],
) -> discord.Embed:
    if before_ch is None:
        em = discord.Embed(
            description = f"{member.mention} masuk ke {after_ch.mention}",
            colour      = discord.Colour.green(),
        )
        em.add_field(name="👥  Member sekarang", value=f"**{len(after_ch.members)}**")
    elif after_ch is None:
        em = discord.Embed(
            description = f"{member.mention} meninggalkan {before_ch.mention}",
            colour      = discord.Colour.red(),
        )
        em.add_field(name="👥  Sisa member", value=f"**{len(before_ch.members)}**")
    else:
        em = discord.Embed(
            description = f"{member.mention} pindah dari {before_ch.mention} ke {after_ch.mention}",
            colour      = discord.Colour.blurple(),
        )
        em.add_field(name=f"👥  {before_ch.name}", value=f"**{len(before_ch.members)}** tersisa")
        em.add_field(name=f"👥  {after_ch.name}",  value=f"**{len(after_ch.members)}** sekarang")
    em.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    em.timestamp = discord.utils.utcnow()
    return em


async def _send_voice_notification(
    member:    discord.Member,
    before_ch: Optional[discord.abc.GuildChannel],
    after_ch:  Optional[discord.abc.GuildChannel],
) -> None:
    channel_id = _get_voice_config(member.guild.id).notify_channel_id
    if channel_id is None:
        return
    target = member.guild.get_channel(channel_id)
    if not isinstance(target, discord.TextChannel):
        return   # channel was deleted or changed type
    try:
        await target.send(
            embed            = _build_voice_embed(member, before_ch, after_ch),
            allowed_mentions = discord.AllowedMentions.none(),
        )
    except discord.HTTPException as exc:
        log.warning("Voice notification to #%s (guild %d) failed: %s", target.name, member.guild.id, exc)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState,
) -> None:
    if member.bot:
        return
    # Any change (join/leave/move/mute/deafen) can flip eligibility for everyone
    # in the affected channels, so re-checkpoint the whole guild.
    try:
        _sync_voice_guild(member.guild)
    except Exception:
        log.exception("Voice sync failed for guild %s", member.guild.id)

    if before.channel == after.channel:
        return   # mute/deafen/stream toggle only — no notification
    await _send_voice_notification(member, before.channel, after.channel)


@bot.tree.command(name="balance", description="Cek saldo mata uang server, XP, level, dan total waktu voice")
@app_commands.guild_only()
@app_commands.describe(user="Member yang mau dicek (kosongkan untuk diri sendiri)")
async def balance(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    target   = user or interaction.user
    guild_id = interaction.guild_id
    _sync_voice_guild(interaction.guild)   # fresh VC time & progress

    # Don't create a profile just because someone looked at it.
    player   = _GLOBAL_PLAYER_REGISTRY.get((guild_id, target.id)) or PlayerProfile()
    manifest = _GLOBAL_CURRENCY_REGISTRY.get(guild_id)
    config   = _get_voice_config(guild_id)

    level      = level_for_xp(player.xp)
    lvl_floor  = xp_for_level(level)
    lvl_span   = xp_for_level(level + 1) - lvl_floor
    lvl_bar    = _reserve_bar((player.xp - lvl_floor) / lvl_span)

    if manifest is not None:
        money = f"**{player.wallet:,.2f} {manifest.ticker}**\n*{manifest.currency_name}*"
    else:
        money = f"**{player.wallet:,.2f} Fiat**\n*Server belum punya mata uang resmi*"

    em = discord.Embed(title=f"💳  {target.display_name}", colour=discord.Colour.blurple())
    em.set_thumbnail(url=target.display_avatar.url)
    em.add_field(name="💰  Saldo", value=money, inline=True)
    em.add_field(
        name  = "⭐  Level",
        value = f"**{level}**  ({player.xp:,} XP)\n{lvl_bar}  {player.xp - lvl_floor}/{lvl_span}",
        inline = True,
    )
    em.add_field(name="🎙️  Total Waktu Voice", value=f"**{_format_duration(player.voice_seconds)}**", inline=True)

    progress = _voice_tracker.progress(guild_id, target.id)
    if progress is not None:
        seconds, reason = progress
        status = (
            f"{_reserve_bar(seconds / config.reward_interval_seconds)}  "
            f"{int(seconds // 60)}/{config.reward_interval_minutes} menit"
        )
        if reason is not None:
            status += f"\n⏸️  Dijeda: {_BLOCK_REASON_TEXT[reason]}"
        em.add_field(name="⏳  Reward Voice Berikutnya", value=status, inline=False)

    if manifest is None:
        em.set_footer(text="Reward voice saat ini hanya XP — admin perlu /found_currency untuk mengaktifkan coin.")
    await interaction.response.send_message(embed=em)


@bot.tree.command(name="leaderboard", description="Top 10 member server ini berdasarkan coin, xp, atau voicetime")
@app_commands.guild_only()
@app_commands.describe(kategori="Urutkan berdasarkan apa (default: coin)")
@app_commands.choices(kategori=[
    app_commands.Choice(name="coin",      value="coin"),
    app_commands.Choice(name="xp",        value="xp"),
    app_commands.Choice(name="voicetime", value="voicetime"),
])
async def leaderboard(
    interaction: discord.Interaction,
    kategori: Optional[app_commands.Choice[str]] = None,
) -> None:
    kind     = kategori.value if kategori else "coin"
    guild_id = interaction.guild_id
    manifest = _GLOBAL_CURRENCY_REGISTRY.get(guild_id)
    unit     = manifest.ticker if manifest else "Fiat"

    if kind == "coin":
        title = f"💰  Leaderboard {manifest.currency_name if manifest else 'Coin'}"
        def score(p: PlayerProfile) -> float: return p.wallet
        def shown(p: PlayerProfile) -> str:   return f"{p.wallet:,.2f} {unit}"
    elif kind == "xp":
        title = "⭐  Leaderboard XP"
        def score(p: PlayerProfile) -> float: return p.xp
        def shown(p: PlayerProfile) -> str:   return f"Lv {level_for_xp(p.xp)} · {p.xp:,} XP"
    else:
        title = "🎙️  Leaderboard Waktu Voice"
        def score(p: PlayerProfile) -> float: return p.voice_seconds
        def shown(p: PlayerProfile) -> str:   return _format_duration(p.voice_seconds)

    ranked = sorted(
        ((uid, p) for (gid, uid), p in _GLOBAL_PLAYER_REGISTRY.items() if gid == guild_id and score(p) > 0),
        key     = lambda entry: score(entry[1]),
        reverse = True,
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`#{i + 1}`'}  <@{uid}> — **{shown(p)}**"
        for i, (uid, p) in enumerate(ranked[:10])
    ]
    em = discord.Embed(title=title, description="\n".join(lines) or "*Belum ada data.*", colour=discord.Colour.gold())
    rank = next((i + 1 for i, (uid, _) in enumerate(ranked) if uid == interaction.user.id), None)
    em.set_footer(text=f"Peringkat kamu: #{rank} dari {len(ranked)}" if rank else "Kamu belum masuk peringkat.")
    await interaction.response.send_message(embed=em, allowed_mentions=discord.AllowedMentions.none())


def _build_voice_config_embed(guild: discord.Guild, config: VoiceConfig, *, updated: bool) -> discord.Embed:
    manifest = _GLOBAL_CURRENCY_REGISTRY.get(guild.id)
    unit = manifest.ticker if manifest else "(belum ada mata uang — reward hanya XP)"
    em = discord.Embed(
        title  = "✅  Voice config diperbarui" if updated else "⚙️  Voice config",
        colour = discord.Colour.green() if updated else discord.Colour.blurple(),
    )
    em.add_field(
        name   = "📢  Channel notifikasi",
        value  = f"<#{config.notify_channel_id}>" if config.notify_channel_id else "— (mati)",
        inline = False,
    )
    em.add_field(name="⏱️  Interval reward", value=f"{config.reward_interval_minutes} menit")
    em.add_field(name="💰  Reward per interval", value=f"{config.reward_amount:,.2f} {unit}\n+{XP_PER_INTERVAL} XP")
    em.add_field(
        name   = "🛡️  Anti-abuse (✅ = tidak dapat reward)",
        value  = (
            f"{_toggle(config.block_self_mute_deaf)} self-mute + deafen\n"
            f"{_toggle(config.block_afk_channel)} AFK channel\n"
            f"{_toggle(config.block_alone)} sendirian (<2 manusia)"
        ),
        inline = False,
    )
    em.set_footer(text="Reward coin dikali modifier Bank Sentral (lihat /market_status).")
    return em


@bot.tree.command(name="voiceconfig", description="[ADMIN] Atur notifikasi & reward voice server ini (tanpa opsi = lihat config)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    channel            = "Text channel untuk notifikasi join/leave voice",
    matikan_notifikasi = "True = berhenti mengirim notifikasi voice",
    interval_menit     = "Jarak antar reward dalam menit (1–1440)",
    reward             = "Jumlah mata uang server per interval (sebelum modifier Bank Sentral)",
    blok_mute_deafen   = "Blok reward saat self-mute + self-deafen",
    blok_afk           = "Blok reward di AFK channel",
    blok_sendirian     = "Blok reward saat sendirian di VC (<2 manusia)",
)
async def voiceconfig(
    interaction:        discord.Interaction,
    channel:            Optional[discord.TextChannel] = None,
    matikan_notifikasi: Optional[bool] = None,
    interval_menit:     Optional[app_commands.Range[int, MIN_REWARD_INTERVAL_MINUTES, MAX_REWARD_INTERVAL_MINUTES]] = None,
    reward:             Optional[app_commands.Range[float, 0.0, 1_000_000.0]] = None,
    blok_mute_deafen:   Optional[bool] = None,
    blok_afk:           Optional[bool] = None,
    blok_sendirian:     Optional[bool] = None,
) -> None:
    guild = interaction.guild
    if channel is not None and matikan_notifikasi:
        await interaction.response.send_message("❌ Pilih salah satu: set `channel` atau `matikan_notifikasi`.", ephemeral=True)
        return

    changes: Dict[str, Any] = {}
    if channel is not None:
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"❌ Bot butuh izin **View Channel**, **Send Messages**, dan **Embed Links** di {channel.mention}.",
                ephemeral=True,
            )
            return
        changes["notify_channel_id"] = channel.id
    if matikan_notifikasi:
        changes["notify_channel_id"] = None
    if interval_menit is not None:
        changes["reward_interval_minutes"] = int(interval_menit)
    if reward is not None:
        changes["reward_amount"] = float(reward)
    if blok_mute_deafen is not None:
        changes["block_self_mute_deaf"] = blok_mute_deafen
    if blok_afk is not None:
        changes["block_afk_channel"] = blok_afk
    if blok_sendirian is not None:
        changes["block_alone"] = blok_sendirian

    if not changes:
        await interaction.response.send_message(
            embed=_build_voice_config_embed(guild, _get_voice_config(guild.id), updated=False), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    new_config = dataclasses.replace(_get_voice_config(guild.id), **changes)
    try:
        await _run_db(_economy_db.upsert_voice_config, new_config)
    except Exception:
        log.exception("Saving voice config for guild %s failed", guild.id)
        await interaction.followup.send("❌ Gagal menyimpan ke database. Coba lagi nanti.", ephemeral=True)
        return

    _GLOBAL_VOICE_CONFIG_REGISTRY[guild.id] = new_config
    # Time already elapsed is credited under the old rules; new rules apply from now.
    _sync_voice_guild(guild)
    await interaction.followup.send(embed=_build_voice_config_embed(guild, new_config, updated=True), ephemeral=True)


@voiceconfig.error
async def _voiceconfig_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    # A local handler suppresses the tree's default logging, so log here.
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ Command ini khusus admin server."
    else:
        log.error("Error in /voiceconfig", exc_info=error)
        msg = "❌ Terjadi error. Cek log bot."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — AUTO MINE COMMANDS  (/automine daftar | berhenti | status)
# ─────────────────────────────────────────────────────────────────────────────

automine_group = app_commands.Group(
    name             = "automine",
    description      = "Nambang otomatis selama aktif di voice channel",
    guild_only       = True,
    # guild_only alone only sets the deprecated dm_permission on groups.
    allowed_contexts = app_commands.AppCommandContext(guild=True),
)


def _automine_state_line(guild: discord.Guild, user_id: int) -> str:
    progress = _voice_tracker.progress(guild.id, user_id)
    if progress is None:
        return "💤 Tidak di voice channel — masuk VC untuk mulai menambang."
    seconds, reason = progress
    if reason is not None:
        return f"⏸️ Dijeda: {_BLOCK_REASON_TEXT[reason]}."
    remaining = max(0.0, _get_voice_config(guild.id).reward_interval_seconds - seconds)
    return f"⛏️ Menambang — ayunan berikutnya ±{max(1, math.ceil(remaining / 60))} menit lagi."


def _build_automine_embed(
    guild:  discord.Guild,
    member: Union[discord.Member, discord.User],
    player: PlayerProfile,
    *,
    title:  str,
    colour: discord.Colour,
) -> discord.Embed:
    try:
        _, _, state = _hydrate_server(guild)
    except WorldPending:
        state = None
    pickaxe = PICKAXES.get(player.pickaxe_key, PICKAXES["copper_starter"])
    config  = _get_voice_config(guild.id)

    em = discord.Embed(title=title, colour=colour)
    em.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    em.add_field(
        name   = "📋  Pendaftaran",
        value  = "🟢 **Terdaftar**" if player.automine else "🔴 **Belum terdaftar**",
        inline = True,
    )
    em.add_field(
        name   = "🔨  Pickaxe",
        value  = f"**{pickaxe.name}**\nPower {pickaxe.power:.0f} · Eff {pickaxe.efficiency:.2f}",
        inline = True,
    )
    em.add_field(
        name   = "💪  Stamina",
        value  = f"{_reserve_bar(player.stamina / _DEFAULT_STAMINA, width=8)}\n**{player.stamina:.0f}** / {_DEFAULT_STAMINA:.0f}",
        inline = True,
    )
    if player.automine:
        em.add_field(name="📡  Kondisi", value=_automine_state_line(guild, member.id), inline=False)

    if state is None:
        target_text = _WORLD_PENDING_TEXT
    else:
        target_id = choose_best_node(state, pickaxe)
        target = (state.active_ores.get(target_id) or state.active_crystals.get(target_id)) if target_id else None
        target_text = (
            f"**{node_label(target)}**\n"
            f"{_reserve_bar(target.reserve_fraction())}  {target.current_reserve:.1f} / {target.max_reserve:.1f} t"
            if target else "🔴 Semua node habis — tunggu regenerasi."
        )
    em.add_field(name="🎯  Target node", value=target_text, inline=False)

    last = _AUTOMINE_LAST.get((guild.id, member.id))
    if last is not None:
        got = (
            f"**{last.item.display_name}** — {last.result.amount_extracted:.3f} t"
            if last.item else "Tidak ada hasil"
        )
        if last.result.critical_hit:
            got += "  ✨ CRIT"
        em.add_field(name="📦  Ayunan terakhir", value=got, inline=False)

    em.set_footer(text=(
        f"1 ayunan tiap {config.reward_interval_minutes} menit aktif di VC · "
        f"aturan anti-abuse voice berlaku · pickaxe = yang terakhir dipakai di /explore_mines"
    ))
    return em


async def _save_players_quietly() -> None:
    # Called after the interaction is answered; a failure is retried next tick.
    try:
        await _flush_players()
    except Exception:
        log.exception("Saving players to Supabase failed; will retry next tick.")


@automine_group.command(name="daftar", description="Daftar auto mine — nambang otomatis selama aktif di VC")
async def automine_daftar(interaction: discord.Interaction) -> None:
    player  = _get_player(interaction.guild_id, interaction.user.id)
    already = player.automine
    player.automine = True
    em = _build_automine_embed(
        interaction.guild, interaction.user, player,
        title  = "ℹ️  Kamu sudah terdaftar auto mine" if already else "✅  Auto mine aktif!",
        colour = discord.Colour.green(),
    )
    await interaction.response.send_message(embed=em, ephemeral=True)
    await _save_players_quietly()


@automine_group.command(name="berhenti", description="Berhenti dari auto mine")
async def automine_berhenti(interaction: discord.Interaction) -> None:
    player = _GLOBAL_PLAYER_REGISTRY.get((interaction.guild_id, interaction.user.id))
    if player is None or not player.automine:
        await interaction.response.send_message("ℹ️ Kamu memang belum terdaftar auto mine.", ephemeral=True)
        return
    player.automine = False
    em = _build_automine_embed(
        interaction.guild, interaction.user, player,
        title  = "⛔  Auto mine dimatikan",
        colour = discord.Colour.red(),
    )
    await interaction.response.send_message(embed=em, ephemeral=True)
    await _save_players_quietly()


@automine_group.command(name="status", description="Lihat status auto mine, target node, dan hasil terakhir")
async def automine_status(interaction: discord.Interaction) -> None:
    _sync_voice_guild(interaction.guild)   # fresh countdown
    player = _GLOBAL_PLAYER_REGISTRY.get((interaction.guild_id, interaction.user.id)) or PlayerProfile()
    em = _build_automine_embed(
        interaction.guild, interaction.user, player,
        title  = "⛏️  Status Auto Mine",
        colour = discord.Colour.from_rgb(140, 100, 60),
    )
    if not player.automine:
        em.description = "Belum terdaftar. Pakai `/automine daftar` untuk mulai."
    await interaction.response.send_message(embed=em, ephemeral=True)


bot.tree.add_command(automine_group)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11 — STAGE 0: WORLD REGISTRATION  (/worldproof)
# ─────────────────────────────────────────────────────────────────────────────
# Every bit of randomness entering worldgen comes from world_registry (drand
# quicknet).  DB work runs on the DB thread; drand HTTP runs on its own thread
# so a slow relay never delays player flushes.  No local fallback, ever.

_PENDING_WARNED: Dict[int, float] = {}
_PENDING_WARN_EVERY_SECONDS: float = 300.0


async def _try_resolve(record: WorldRecord) -> WorldRecord:
    """Fetch & store the beacon of a pending world; stays pending on any failure."""
    if record.status == STATUS_ACTIVE:
        return record
    try:
        beacon = await asyncio.to_thread(_world_registry.fetch_beacon, record)
    except BeaconNotYet:
        return record
    except BeaconUnavailable as exc:
        now = time.monotonic()
        if now - _PENDING_WARNED.get(record.guild_id, float("-inf")) >= _PENDING_WARN_EVERY_SECONDS:
            _PENDING_WARNED[record.guild_id] = now
            log.warning("World guild=%d still pending (round %d): %s", record.guild_id, record.target_round, exc)
        return record
    record = await _run_db(_world_registry.store_beacon, record, beacon)
    _GLOBAL_WORLD_RECORDS[record.guild_id] = record
    _PENDING_WARNED.pop(record.guild_id, None)
    log.info("World nonce locked: guild=%d round=%d nonce=%s…",
             record.guild_id, record.target_round, record.world_nonce[:16])
    _kick_witness()
    return record


async def _ensure_registered(guild: discord.Guild, *, wait_for_round: bool) -> WorldRecord:
    """Register the guild (atomic insert-or-get) and, if asked, wait ≤10 s for its round."""
    record = _GLOBAL_WORLD_RECORDS.get(guild.id)
    if record is None:
        record = await _run_db(_world_registry.register, guild.id)
        _GLOBAL_WORLD_RECORDS[guild.id] = record
        log.info("Stage 0: guild=%d registered (%s), target drand round %d",
                 guild.id, record.algo_version, record.target_round)
        _kick_witness()
    if record.status != STATUS_ACTIVE and wait_for_round:
        round_at = _world_registry.source_for(record).round_time(record.target_round)
        delay = (round_at - discord.utils.utcnow()).total_seconds()
        if 0 < delay <= 10:
            await asyncio.sleep(delay + 0.5)
        record = await _try_resolve(record)
    return record


@tasks.loop(seconds=10)
async def _resolve_pending_worlds() -> None:
    for record in [r for r in _GLOBAL_WORLD_RECORDS.values() if r.status != STATUS_ACTIVE]:
        try:
            await _try_resolve(record)
        except Exception:
            log.exception("Resolving world for guild %s failed", record.guild_id)


def _world_seed(record: WorldRecord) -> bytes:
    """Stage 1 seed of an ACTIVE world (secret: depends on the pepper — never display it)."""
    if record.status != STATUS_ACTIVE:
        raise RuntimeError(f"world of guild {record.guild_id} is still pending")
    return _seed_service.seed_for(record.algo_version, record.guild_id, record.world_nonce)


# ── External witness ─────────────────────────────────────────────────────────
# Pending events are derived (registry − world_witness_log), so nothing is lost
# across restarts.  Registration never waits for, or fails because of, the webhook.

_WITNESS_LOCK = asyncio.Lock()
_WITNESS_WARNED_AT: float = float("-inf")
_WITNESS_SEND_GAP_SECONDS: float = 0.5          # stay under Discord's webhook rate limit
_BACKGROUND_TASKS: Set[asyncio.Task] = set()


async def _deliver_witness_events() -> int:
    global _WITNESS_WARNED_AT
    if not _witness.enabled:
        return 0
    sent = 0
    async with _WITNESS_LOCK:
        pending = pending_events(
            _GLOBAL_WORLD_RECORDS.values(), _world_registry.source_for,
            _WORLD_COMMITMENT_ROWS, _WITNESS_DELIVERED,
        )
        for event in pending:
            try:
                await asyncio.to_thread(_witness.send, event)
            except Exception as exc:
                if time.monotonic() - _WITNESS_WARNED_AT >= _PENDING_WARN_EVERY_SECONDS:
                    _WITNESS_WARNED_AT = time.monotonic()
                    log.warning("Witness webhook failed (%d event(s) queued): %s", len(pending) - sent, exc)
                break          # keep chronological order; retry the rest next pass
            _WITNESS_DELIVERED.add(event.key)
            sent += 1
            try:
                await _run_db(_economy_db.insert_witness_row, event.key, event.event_id)
            except Exception:
                # Delivered but not recorded: after a restart it is re-sent with the
                # SAME event_id, so observers can dedupe.  Never blocks registration.
                log.exception("Recording witness delivery %s failed", event.key)
            await asyncio.sleep(_WITNESS_SEND_GAP_SECONDS)
    if sent:
        log.info("Witness: %d event(s) published", sent)
    return sent


def _kick_witness() -> None:
    """Fire-and-forget delivery right after an event; the 30 s loop is the safety net."""
    if not _witness.enabled:
        return
    task = asyncio.get_running_loop().create_task(_deliver_witness_events())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


@tasks.loop(seconds=30)
async def _witness_tick() -> None:
    try:
        await _deliver_witness_events()
    except Exception:
        log.exception("Witness delivery pass failed")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    try:
        await _ensure_registered(guild, wait_for_round=False)
    except Exception:
        log.exception("Stage 0 registration failed for guild %s", guild.id)


def _build_worldproof_embed(guild: discord.Guild, record: WorldRecord) -> discord.Embed:
    source = _world_registry.source_for(record)
    active = record.status == STATUS_ACTIVE
    em = discord.Embed(
        title       = f"🔏  World Proof — {guild.name}",
        description = (
            "Dunia server ini dikunci ke **beacon randomness publik (drand)**. "
            "Siapa pun bisa memverifikasi — tanpa harus percaya bot maupun operatornya."
        ),
        colour      = discord.Colour.green() if active else discord.Colour.gold(),
    )
    em.add_field(name="🆔  Guild ID", value=f"`{record.guild_id}`")
    em.add_field(name="🧬  Algo", value=f"`{record.algo_version}`")
    em.add_field(name="📌  Status", value="🟢 **Aktif**" if active else "🟡 **Pending**")
    em.add_field(
        name   = "🕰️  registered_at",
        value  = f"<t:{int(record.registered_at.timestamp())}:F>\n`{record.registered_at.isoformat()}`",
        inline = False,
    )
    round_ts = int(source.round_time(record.target_round).timestamp())
    em.add_field(
        name   = "🎲  Beacon",
        value  = (
            f"drand quicknet · ronde **{record.target_round}** (<t:{round_ts}:T>)\n"
            f"[Cocokkan di relay publik]({source.public_url(record.target_round)})"
        ),
        inline = False,
    )
    if active:
        em.add_field(name="🔑  world_nonce", value=f"```{record.world_nonce}```", inline=False)
        em.add_field(name="✍️  drand signature", value=f"```{record.beacon.signature}```", inline=False)
    else:
        em.add_field(
            name   = "⏳  Menunggu beacon",
            value  = (
                f"Ronde #{record.target_round} belum bisa diambil dari drand; dicoba ulang otomatis. "
                "Tidak ada fallback ke randomness lokal."
            ),
            inline = False,
        )
    em.add_field(name="✅  Cara verifikasi", value=source.verification_steps(), inline=False)
    commitment = _seed_service.commitments().get(record.algo_version)
    em.add_field(
        name   = f"📜  Pepper commitment ({record.algo_version})",
        value  = (
            f"```{commitment}```"
            "`seed = HMAC-SHA256(pepper, \"BAWAN|algo|guild_id|world_nonce\")` — pepper dibuka di akhir "
            "eksperimen; cek `SHA-256(pepper)` = commitment ini, lalu hitung ulang seed."
        ) if commitment else "—",
        inline = False,
    )
    em.set_footer(text=f"Sumber randomness: {record.source_id}"[:2048])
    return em


@bot.tree.command(name="worldproof", description="Bukti publik asal-usul dunia server ini (drand beacon)")
@app_commands.guild_only()
async def worldproof(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        record = await _ensure_registered(interaction.guild, wait_for_round=True)
    except Exception:
        log.exception("/worldproof failed for guild %s", interaction.guild_id)
        await interaction.followup.send("❌ Registry dunia tidak bisa diakses sekarang. Coba lagi nanti.")
        return
    await interaction.followup.send(embed=_build_worldproof_embed(interaction.guild, record))

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 12 — ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

def _handle_sigterm(signum: int, frame: Any) -> None:
    # systemd / docker stop the bot with SIGTERM.  Route it through the same
    # graceful path as Ctrl+C so BawanBot.close() flushes players to Supabase.
    raise KeyboardInterrupt


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("Cannot start: DISCORD_TOKEN is empty.")
        log.error("Create a .env file in the same directory containing:")
        log.error("    DISCORD_TOKEN=your_bot_token_here")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    log.info("Starting main_core.py...")
    bot.run(DISCORD_TOKEN, log_handler=None)