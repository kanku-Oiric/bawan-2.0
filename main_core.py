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
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • No `random` Module    — all game logic flows through deterministic       ║
║      engines.  This file contains zero stochastic calls.                   ║
║  • In-Memory Mock State  — two global dicts hold all runtime data until    ║
║      Supabase is wired in.  Architecture is pre-shaped for that swap.      ║
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
║  _GLOBAL_PLAYER_REGISTRY: Dict[int, PlayerProfile]                          ║
║      user_id → PlayerProfile(stamina, pickaxe_key, ore_bag, crystal_bag)   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  SETUP (one-time)                                                            ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1. Create .env file in the same directory:                                  ║
║         DISCORD_TOKEN=your_bot_token_here                                   ║
║  2. Install deps:                                                            ║
║         pip install discord.py python-dotenv                               ║
║  3. Run:                                                                     ║
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
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
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

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  [%(levelname)s]  %(name)s: %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main_core")

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


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — IN-MEMORY PERSISTENCE LAYER (Mock State)
# ─────────────────────────────────────────────────────────────────────────────
# Replace these dicts with Supabase async reads/writes when the DB layer lands.

_GLOBAL_SPAWN_REGISTRY:   Dict[int, ServerSpawnState]       = {}
_GLOBAL_CATALOG_REGISTRY: Dict[int, ServerMaterialCatalog]  = {}
_GLOBAL_PROFILE_REGISTRY: Dict[int, ServerGeneticProfile]   = {}
_GLOBAL_PLAYER_REGISTRY:  Dict[int, "PlayerProfile"]        = {}

# Constant for default player stamina.
_DEFAULT_STAMINA: float = 100.0


@dataclass
class PlayerProfile:
    """
    In-memory runtime profile for one Discord user.

    stamina        : Current stamina (0.0 – 100.0).  Decays per mining swing.
    pickaxe_key    : Key into mining_engine.PICKAXES; last-used tool.
    ore_bag        : List of OreItem frozen records harvested this session.
    crystal_bag    : List of CrystalItem frozen records harvested this session.

    When Supabase lands, this becomes an async read of the `players` table
    and the bags become append-only writes to `inventory_ore`/`inventory_crystal`.
    """
    stamina:      float            = _DEFAULT_STAMINA
    pickaxe_key:  str              = "copper_starter"
    ore_bag:      List[OreItem]    = field(default_factory=list)
    crystal_bag:  List[CrystalItem]= field(default_factory=list)
    wallet:       float            = 0.0             


def _get_player(user_id: int) -> PlayerProfile:
    """Return existing PlayerProfile or create a fresh one."""
    if user_id not in _GLOBAL_PLAYER_REGISTRY:
        _GLOBAL_PLAYER_REGISTRY[user_id] = PlayerProfile()
    return _GLOBAL_PLAYER_REGISTRY[user_id]


def _hydrate_server(guild: discord.Guild) -> tuple[
    ServerGeneticProfile, ServerMaterialCatalog, ServerSpawnState
]:
    """
    Ensure the server has a fully initialised world state.

    If the guild is already in the registry → return cached state (SKIP).
    If not → run the full cascade and cache the results.

    The guild's creation timestamp is derived from its Discord snowflake ID
    via guild.created_at, giving a stable, server-specific seed that never
    changes — identical to what identitas_genetik expects.

    Returns (profile, catalog, spawn_state) — all frozen/cached objects.
    """
    gid = guild.id

    if gid in _GLOBAL_SPAWN_REGISTRY:
        return (
            _GLOBAL_PROFILE_REGISTRY[gid],
            _GLOBAL_CATALOG_REGISTRY[gid],
            _GLOBAL_SPAWN_REGISTRY[gid],
        )

    log.info("Hydrating new server: %s (id=%d)", guild.name, gid)

    created_at: int = int(guild.created_at.timestamp())

    profile  = _genetic_engine.generate_profile(server_id=gid, created_at=created_at)
    catalog  = _material_engine.generate_geology(profile)
    state    = _spawner.initialise(profile, catalog)

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
        player = _get_player(interaction.user.id)
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
        player   = _get_player(self.user_id)
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

        player = _get_player(user_id)
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

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)


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
    profile, catalog, state = _hydrate_server(guild)

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
async def inventory(interaction: discord.Interaction) -> None:
    """Show the calling player's current in-memory inventory and stamina."""
    player = _get_player(interaction.user.id)

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
async def rest(interaction: discord.Interaction) -> None:
    """Manual stamina restore command. Equivalent to the Rest button on the embed."""
    player      = _get_player(interaction.user.id)
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

    profile, catalog, _ = _hydrate_server(guild)

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

# AMERTA INTEGRATION: Inject Perintah `/sell_crystal` Terbuka
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
    player = _get_player(user_id)

    # 1. Cari kristal yang namanya cocok di dalam crystal_bag milik player
    target_item = None
    for item in player.crystal_bag:
        # Kita normalisasi string-nya biar gak sensitif spasi dan huruf kapital
        if item.display_name.strip().lower() == crystal_name.strip().lower():
            target_item = item
            break

    if not target_item:
        await interaction.followup.send(f"❌ Di tas kristal lu gak ada kristal bernama `[{crystal_name}]`, Amerta!", ephemeral=True)
        return

    # 2. HITUNG VALUE LEWAT ORACLE EKONOMI LOKAL
    # Karena kristal membawa base_market_value murni dari konvergensi depth & quality, 
    # kita gunakan perhitungan berbasis multiplier afinitas dasar di ekonomi
    purity_map = {"Flawed": 1.0, "Prismatic": 1.6, "Ethereal": 2.5}
    purity_mod = purity_map.get(target_item.quality, 1.0)
    
    # Nilai dasar kristal dipengaruhi oleh strategic_resource_score atau luxury_resource_score server
    if target_item.crystal_affinity in ["POWER", "MANA", "MUTATION"]:
        base_multiplier = max(30.0, catalog.strategic_resource_score * 0.25)
    else:
        base_multiplier = max(20.0, catalog.luxury_resource_score * 0.15)
        
    scarcity_mult = max(0.5, 2.0 - catalog.dominance_ratio)
    
    # Kalkulasi nilai jual final
    price_per_unit = base_multiplier * scarcity_mult * purity_mod
    total_value = round(price_per_unit * (target_item.weight_tonnes * 0.1), 4) # Skala penyesuaian volume kristal

    # 3. MUTASI STATE PLAYER
    player.crystal_bag.remove(target_item)
    player.wallet += total_value

    # 4. RENDER EMBED TRANSAKSI MAKRO
    em = discord.Embed(title="🔮 NPC CRYSTAL MARKET TRANSACTION SUCCESS", color=discord.Color.blue())
    em.add_field(name="📦 Komoditas", value=f"`{target_item.display_name}`", inline=True)
    em.add_field(name="📊 Tipe Kristal", value=f"`{target_item.crystal_type}`", inline=True)
    em.add_field(name="🛡️ Afinitas Magis", value=f"`{target_item.crystal_affinity}`", inline=True)
    em.add_field(name="🔷 Kualitas", value=f"`{target_item.quality}`", inline=True)
    em.add_field(name="📈 Scarcity Mult", value=f"`{round(scarcity_mult, 4)}x`", inline=True)
    em.add_field(name="💸 Hasil Wallet", value=f"**+ {total_value:.2f} Fiat**\nSaldo Saldo saat ini: **{player.wallet:.2f} Fiat**", inline=False)
    em.set_footer(text=f"Tx ID: {target_item.item_uuid[:12]}... | Standard Jangkar UA")

    await interaction.followup.send(embed=em, ephemeral=True)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("Cannot start: DISCORD_TOKEN is empty.")
        log.error("Create a .env file in the same directory containing:")
        log.error("    DISCORD_TOKEN=your_bot_token_here")
        sys.exit(1)

    log.info("Starting main_core.py...")
    bot.run(DISCORD_TOKEN, log_handler=None)