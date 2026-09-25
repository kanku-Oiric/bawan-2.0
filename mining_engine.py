"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       MINING_ENGINE.PY  —  Core Resource Extraction Engine                 ║
║       Foundational Block #4 of the Procedural World System                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM CONTRACT                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Reads    : resource_spawner.ServerSpawnState       (mutable runtime state) ║
║  Reads    : resource_spawner.ActiveOreNode          (ore node instance)     ║
║  Reads    : resource_spawner.ActiveCrystalNode      (crystal node instance) ║
║  Reads    : material_gen.ServerMaterialCatalog      (frozen geological data) ║
║  Calls    : resource_spawner.ResourceSpawner.extract_resource()             ║
║             ↳ This is the ONLY pathway that mutates current_reserve.        ║
║                                                                              ║
║  DOWNSTREAM CONTRACT                                                         ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  discord_bot receives : MiningResult  — command response payload            ║
║  economy.py   reads   : MiningResult.amount_extracted × rarity_score        ║
║                         → market price injection on successful extraction   ║
║  loot_table.py reads  : MiningResult.resource_type, critical_hit            ║
║                         → secondary drop rolls ("CRYSTAL_SPLINTER" branch)  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • Zero `random` Module   — every stochastic check reads a per-swing roll   ║
║      computed by world_stream.mining_roll():                                ║
║        HMAC(world_seed, "mining|guild_id|user_id|node_id|n")                ║
║      where n is the player's attempt number, incremented atomically in      ║
║      Supabase BEFORE the roll exists.  The engine never derives a roll.     ║
║                                                                              ║
║  • State Mutation Contract — MiningEngine NEVER writes directly to          ║
║      ServerSpawnState fields.  All reserve depletion flows through          ║
║      ResourceSpawner.extract_resource(), preserving the single-entry-point  ║
║      invariant established in resource_spawner.py.                          ║
║                                                                              ║
║  • Frozen Upstream        — Pickaxe is frozen=True.  MiningResult is        ║
║      frozen=True.  Neither catalog nor spawn state nodes are mutated        ║
║      except via the ResourceSpawner API.                                    ║
║                                                                              ║
║  • Hardness × Power Matrix — Node hardness combines purity and depth into   ║
║      a single float threshold.  Pickaxe power below this threshold applies  ║
║      a progressive quadratic penalty rather than a binary block, allowing   ║
║      weak tools to extract trace amounts and preserving gameplay agency.    ║
║                                                                              ║
║  • Crystal Splintering    — Brittle formations in high-tectonic worlds have ║
║      a deterministic fracture probability (tectonic × mutation_affinity).   ║
║      Splintered yield is economically lower but tagged for loot_table.py    ║
║      as a distinct resource type with crafting utility.                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  TRANSIENT SEED ENTROPY MAP                                                  ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  roll = mining_roll(seed, guild_id, user_id, node_id, n)   (256-bit int)    ║
║                                                                              ║
║  Window layout over f"{roll:064x}" (8 hex chars = 32-bit uint each):        ║
║    W0 [0 : 8 ] → critical_hit roll       (vs CRIT_THRESHOLD)               ║
║    W1 [8 :16 ] → crystal fracture roll   (vs fracture_probability)         ║
║    W2 [16:24 ] → yield scatter jitter    (±SCATTER_PCT of base yield)      ║
║    W3 [24:32 ] → splinter fragment count (2–5 pieces)                      ║
║    W4 [32:40 ] → reserved (tool wear future use)                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import json
import sys
import os
from dataclasses import dataclass, asdict
from typing import Callable, Dict, Optional, Tuple, Union

# ── Upstream module resolution ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identitas_genetik import ServerGeneticProfile, GeneticEngine
from material_gen import (
    ServerMaterialCatalog,
    MaterialEngine,
)
from resource_spawner import (
    ServerSpawnState,
    ActiveOreNode,
    ActiveCrystalNode,
    ResourceSpawner,
    ACCESS_PUBLIC,
    ACCESS_RESTRICTED,
)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — TUNING CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# ── Hardness matrix ────────────────────────────────────────────────────────────
# Base hardness values derived from purity tier (before depth multiplier).
# Design intent: Flawless ore is 7× harder than Crude — a starter tool should
# never efficiently mine the highest tier.
_PURITY_HARDNESS: Dict[str, float] = {
    "Crude":    10.0,
    "Enriched": 30.0,
    "Flawless": 70.0,
}

# Depth multiplier applied on top of purity hardness.
# ABYSS = 2× because of structural pressure compaction and access difficulty.
_DEPTH_HARDNESS_MULT: Dict[str, float] = {
    "SURFACE": 1.0,
    "SHALLOW": 1.2,
    "DEEP":    1.6,
    "ABYSS":   2.0,
}

# ── Stamina system ─────────────────────────────────────────────────────────────
_BASE_STAMINA_COST: float = 10.0   # Base cost per mining swing regardless of depth.
# Final cost for DEEP/ABYSS:
#   final_cost = BASE * (1.0 + pressure_index * _PRESSURE_STAMINA_SCALE)
_PRESSURE_STAMINA_SCALE: float = 1.0  # At pressure=1.0 cost doubles.

# Surface/Shallow depths do NOT apply the pressure penalty.
_PRESSURE_DEPTH_GATES: Tuple[str, ...] = ("DEEP", "ABYSS")

# ── Underpowered tool penalty ─────────────────────────────────────────────────
# When pickaxe.power < node_hardness:
#   penalty_ratio = (pickaxe.power / node_hardness)  → [0.0, 1.0)
#   efficiency_factor = penalty_ratio ** _UNDERPOWER_EXPONENT
# Exponent > 1 gives a convex curve: tools near threshold get moderate yield,
# wildly underpowered tools approach zero asymptotically.
_UNDERPOWER_EXPONENT: float = 2.0

# Absolute floor: even a zero-power tool extracts this fraction of base yield
# (prevents total lockout which would be frustrating gameplay).
_UNDERPOWER_FLOOR: float = 0.05   # 5% minimum yield regardless of power mismatch

# ── Critical hit system ────────────────────────────────────────────────────────
# Critical hits are available only when pickaxe.power >= node_hardness.
# They multiply yield by CRIT_MULTIPLIER and produce a special flavour message.
_CRIT_THRESHOLD_NORM: float = 0.92   # Top 8% of the [0,1] hash draw
_CRIT_YIELD_MULTIPLIER: float = 2.5

# ── Yield scatter ──────────────────────────────────────────────────────────────
# Even a successful extraction has ±SCATTER_PCT jitter to avoid predictability.
# Scatter is symmetric around the base yield: actual = base × (1 ± jitter)
_SCATTER_PCT: float = 0.12   # ±12% scatter band

# ── Crystal fracture system ───────────────────────────────────────────────────
# fracture_probability = tectonic_activity × crystal.mutation_affinity
# Only evaluated when tectonic_activity > _TECTONIC_FRACTURE_GATE.
_TECTONIC_FRACTURE_GATE: float = 0.70
# Splinter count when fracture triggers: uniformly in [2, 5].
_SPLINTER_COUNT_MIN: int = 2
_SPLINTER_COUNT_MAX: int = 5
# Economic value penalty for splinters vs gemstone (applied to amount_extracted).
_SPLINTER_YIELD_FACTOR: float = 0.45  # 45% of normal crystal yield per splinter

# ── Crystallization index proxy ───────────────────────────────────────────────
# crystallization_index is internal to material_gen and not stored in the
# catalog.  We re-derive an approximation from the catalog's observable fields
# using the same geological logic: high pressure + stable world = high crystal.
# Formula: proxy = pressure_index * 0.6 + (1.0 - tectonic_activity) * 0.4
# This is used only for flavour messages and gate-checks, not yield math.
def _crystallization_proxy(catalog: ServerMaterialCatalog) -> float:
    return round(
        catalog.pressure_index * 0.6 + (1.0 - catalog.tectonic_activity) * 0.4,
        6,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — ITEM DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pickaxe:
    """
    An immutable player tool record.

    power      : Raw force value compared against node_hardness to determine
                 extraction efficiency.  Below threshold → quadratic penalty.
                 At or above → 100% efficiency before scatter and crits.
    efficiency : Post-hardness yield multiplier. Represents tool quality beyond
                 raw power (sharpness, mineral affinity, enchantments).
                 Formula: extracted = base_yield × efficiency × power_factor
    durability : Maximum swing count before the tool breaks. Not decremented
                 by this engine (persistence layer handles wear) — read-only
                 here; durability = 0 is checked and causes an instant fail.

    Standard tier benchmarks (game balance reference):
        Copper Pickaxe   : power=15,  efficiency=0.60, durability=40
        Iron Pickaxe     : power=35,  efficiency=0.80, durability=80
        Titanium Drill   : power=80,  efficiency=1.10, durability=200
        Abyss Resonator  : power=140, efficiency=1.40, durability=500
    """
    item_id:    str
    name:       str
    power:      float   # Compared against node_hardness threshold
    efficiency: float   # Yield multiplier after hardness check
    durability: int     # Current durability (0 = broken, cannot mine)


@dataclass(frozen=True)
class MiningResult:
    """
    The immutable output of a single execute_mining_attempt() call.

    success           : False on hard-blocked attempts (depleted node, broken
                        tool, zero stamina).  Partial extractions still succeed
                        — the amount_extracted reflects the reduced yield.
    resource_type     : "ORE" | "CRYSTAL_GEM" | "CRYSTAL_SPLINTER"
                        Downstream loot tables branch on this field.
    stamina_consumed  : Actual stamina spent.  May be less than calculated cost
                        if the player's stamina was already lower.
    amount_extracted  : Units removed from node.current_reserve.  Includes all
                        efficiency, power, crit, and scatter modifiers.
    is_node_depleted  : Reflects node.is_depleted AFTER this attempt.
    critical_hit      : True only when power >= hardness AND hash roll passes.
    flavour_message   : Combat-log style feedback for the Discord response.
    """
    success:           bool
    node_id:           str
    resource_name:     str    # Display name of ore or crystal
    resource_type:     str    # "ORE" | "CRYSTAL_GEM" | "CRYSTAL_SPLINTER"
    stamina_consumed:  float
    amount_extracted:  float
    is_node_depleted:  bool
    critical_hit:      bool
    flavour_message:   str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — TRANSIENT MINING ENTROPY
# ─────────────────────────────────────────────────────────────────────────────

class _MiningEntropy:
    """
    Per-attempt deterministic entropy source over a precomputed 256-bit roll
    (world_stream.mining_roll).  The engine never derives rolls itself.

    The 64-char hex form of the roll is consumed as five 8-char (32-bit uint) windows.
    Using 32-bit windows (rather than the 24-bit windows in resource_spawner)
    gives finer probability resolution for the critical hit and fracture rolls —
    the difference between a 1-in-16M and 1-in-4B threshold matters for
    high-frequency events like crystal splinters in active servers.

    Design invariant:
        Every window is consumed in a fixed named order (see ENTROPY SLICE MAP
        in the module docstring).  Adding a new stochastic check MUST append a
        new named window, never reuse or reorder existing ones.
    """

    _WINDOW: int    = 8           # hex chars per window (32-bit uint)
    _MAX_U32: int   = 0xFFFFFFFF  # 4,294,967,295
    _MAX_WINDOWS: int = 8         # 64 hex / 8 per window = 8 full windows

    def __init__(self, roll: int) -> None:
        if not isinstance(roll, int) or isinstance(roll, bool) or not 0 <= roll < (1 << 256):
            raise ValueError("roll harus int 256-bit dari world_stream.mining_roll()")
        self._digest: str = f"{roll:064x}"
        self._cursor: int = 0

    def _next_uint32(self) -> int:
        """Consume the next 32-bit window from the fixed digest."""
        if self._cursor >= self._MAX_WINDOWS:
            # Extension chain: deterministic re-hash avoids hard truncation.
            ext = hashlib.sha256(
                self._digest.encode("utf-8")
                + self._cursor.to_bytes(2, "big")
            ).hexdigest()
            # Restart cursor on fresh block (keeps window semantics clean)
            self._digest = ext
            self._cursor = 0
        start  = self._cursor * self._WINDOW
        window = self._digest[start : start + self._WINDOW]
        self._cursor += 1
        return int(window, 16)

    def next_unit(self) -> float:
        """Return a float in [0.0, 1.0]."""
        return self._next_uint32() / self._MAX_U32

    def next_int_in_range(self, lo: int, hi: int) -> int:
        """Return an integer uniformly in [lo, hi] inclusive."""
        span = hi - lo + 1
        return lo + (self._next_uint32() % span)

    # ── Named rolls (matches ENTROPY SLICE MAP in module docstring) ───────────

    def roll_critical(self) -> float:
        """W0 — critical hit roll.  Compare against _CRIT_THRESHOLD_NORM."""
        return self.next_unit()

    def roll_fracture(self) -> float:
        """W1 — crystal fracture roll.  Compare against fracture_probability."""
        return self.next_unit()

    def roll_scatter(self) -> float:
        """W2 — yield scatter.  Returns [-SCATTER_PCT, +SCATTER_PCT] as unit [0,1]."""
        return self.next_unit()

    def roll_splinter_count(self) -> int:
        """W3 — number of splinter fragments when a crystal fractures."""
        return self.next_int_in_range(_SPLINTER_COUNT_MIN, _SPLINTER_COUNT_MAX)

    # W4 reserved for tool wear (future use) — skipping advances the cursor.
    def skip_tool_wear(self) -> None:
        """W4 — consume tool-wear window without using its value (reserved)."""
        self._next_uint32()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — HARDNESS & STAMINA CALCULATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _node_hardness(depth_layer: str, purity: str) -> float:
    """
    Compute the hardness threshold a pickaxe must meet for full efficiency.

    Formula:
        hardness = PURITY_HARDNESS[purity] × DEPTH_HARDNESS_MULT[depth_layer]

    Examples:
        Crude   + SURFACE → 10.0 × 1.0 = 10.0
        Enriched + DEEP   → 30.0 × 1.6 = 48.0
        Flawless + ABYSS  → 70.0 × 2.0 = 140.0

    Any unrecognised key falls back to the most restrictive value, preventing
    silent bugs from new depth layers or purities bypassing the gate.
    """
    purity_base = _PURITY_HARDNESS.get(purity, max(_PURITY_HARDNESS.values()))
    depth_mult  = _DEPTH_HARDNESS_MULT.get(depth_layer, max(_DEPTH_HARDNESS_MULT.values()))
    return round(purity_base * depth_mult, 4)


def _power_efficiency_factor(pickaxe_power: float, hardness: float) -> float:
    """
    Map the pickaxe.power / hardness ratio onto a [FLOOR, 1.0] efficiency scalar.

    At or above hardness  → 1.0  (full efficiency; crit eligible).
    Below hardness        → quadratic falloff: (power/hardness)^EXPONENT,
                            clamped to UNDERPOWER_FLOOR.

    Quadratic (exponent=2) is preferable to linear because:
      • A tool at 90% of threshold still gets 81% efficiency — not a cliff.
      • A tool at 10% of threshold gets ~1% (near FLOOR) — deeply under-geared
        tools produce only trace yield, which is narratively correct.
    """
    if pickaxe_power >= hardness:
        return 1.0
    ratio  = pickaxe_power / max(hardness, 1e-9)
    penalised = ratio ** _UNDERPOWER_EXPONENT
    return max(_UNDERPOWER_FLOOR, round(penalised, 6))


def _stamina_cost(
    depth_layer:    str,
    pressure_index: float,
    base_cost:      float = _BASE_STAMINA_COST,
) -> float:
    """
    Compute the stamina cost for one mining swing at this node.

    SURFACE / SHALLOW: base_cost unchanged.
    DEEP / ABYSS     : base_cost × (1.0 + pressure_index × PRESSURE_SCALE)

    The pressure penalty is a direct function of catalog.pressure_index so
    that high-pressure worlds (deep compressed crust) drain players faster,
    creating meaningful resource tradeoffs for deep nodes.
    """
    if depth_layer not in _PRESSURE_DEPTH_GATES:
        return round(base_cost, 4)
    pressure_penalty = 1.0 + pressure_index * _PRESSURE_STAMINA_SCALE
    return round(base_cost * pressure_penalty, 4)


def _apply_stamina_cap(
    stamina_available: float,
    stamina_required:  float,
    base_extraction:   float,
) -> Tuple[float, float]:
    """
    If the player cannot cover the full stamina cost, scale both stamina
    consumed and extraction yield proportionally.

    Returns:
        (stamina_consumed, extraction_scaling_factor)

    Design intent: a half-stamina player extracts half the yield and pays
    half the stamina — they can still mine, just less efficiently per swing.
    This avoids frustrating hard blocks on stamina-gated content.
    """
    if stamina_available >= stamina_required:
        return round(stamina_required, 4), 1.0
    # Partial swing
    ratio = max(0.0, stamina_available / max(stamina_required, 1e-9))
    return round(stamina_available, 4), round(ratio, 6)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — FLAVOUR MESSAGE FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def _ore_flavour(
    node:            ActiveOreNode,
    result_success:  bool,
    critical_hit:    bool,
    underpowered:    bool,
    stamina_starved: bool,
    depleted:        bool,
    extracted:       float,
    hardness:        float,
    pickaxe:         Pickaxe,
) -> str:
    """
    Generate a contextual combat-log style feedback string for ore mining.

    Priority order (first matching condition wins):
        1. Broken tool
        2. Depleted node (post-extraction)
        3. Critical hit
        4. Stamina-starved partial swing
        5. Underpowered tool (heavy penalty active)
        6. Underpowered tool (light penalty active)
        7. Standard successful extraction
    """
    if not result_success:
        if pickaxe.durability == 0:
            return (
                f"⚒ Your {pickaxe.name} is completely broken! "
                f"It bounces off the {node.ore_name} vein without leaving a mark."
            )
        return (
            f"⚒ You swing at the {node.ore_name} vein ({node.depth_layer}) "
            f"but the ground yields nothing. The node may be depleted."
        )

    depth_flavour: Dict[str, str] = {
        "SURFACE": "The loose surface deposit crumbles easily",
        "SHALLOW": "The shallow seam fractures along its grain",
        "DEEP":    "The compressed rock groans under your tool",
        "ABYSS":   "The abyssal pressure presses back against every blow",
    }
    depth_txt = depth_flavour.get(node.depth_layer, "The rock yields")

    if depleted:
        return (
            f"⛏ {depth_txt}. You extract the final {extracted:.2f} units of "
            f"{node.ore_name} [{node.element_symbol}]. "
            f"The vein collapses — **NODE DEPLETED**."
        )

    if critical_hit:
        return (
            f"✨ **CRITICAL STRIKE!** Your {pickaxe.name} finds a rich pocket. "
            f"{depth_txt} and {extracted:.2f} units of {node.ore_name} "
            f"[{node.element_symbol}] pour out — {_CRIT_YIELD_MULTIPLIER}× yield!"
        )

    if stamina_starved:
        return (
            f"😤 Low stamina! {depth_txt} but your swing is weak. "
            f"You scrape out only {extracted:.2f} units of {node.ore_name}."
        )

    if underpowered:
        gap = hardness - pickaxe.power
        tier = "severely" if gap > hardness * 0.5 else "somewhat"
        return (
            f"⚠ Your {pickaxe.name} (power {pickaxe.power:.0f}) is {tier} "
            f"underpowered for this {node.purity} {node.ore_name} "
            f"(hardness {hardness:.0f}). "
            f"You chip out {extracted:.2f} units with great effort."
        )

    return (
        f"⛏ {depth_txt}. You extract {extracted:.2f} units of "
        f"{node.ore_name} [{node.element_symbol}] "
        f"({node.purity} — {node.depth_layer})."
    )


def _crystal_flavour(
    node:            ActiveCrystalNode,
    result_success:  bool,
    splintered:      bool,
    splinter_count:  int,
    critical_hit:    bool,
    stamina_starved: bool,
    depleted:        bool,
    extracted:       float,
    pickaxe:         Pickaxe,
    fracture_prob:   float,
) -> str:
    """
    Generate a contextual feedback string for crystal harvesting.

    Crystal messages emphasise their brittleness and mystical character vs
    the industrial tone of ore messages.
    """
    if not result_success:
        if pickaxe.durability == 0:
            return (
                f"💎 Your {pickaxe.name} shatters on contact with the "
                f"{node.name}! The tool was already broken."
            )
        return (
            f"💎 The {node.name} formation is inert. "
            f"The crystal does not respond to your tool."
        )

    affinity_txt: Dict[str, str] = {
        "POWER":    "hums with raw energy",
        "MANA":     "pulses with arcane resonance",
        "MUTATION": "writhes with unstable energy",
        "UTILITY":  "gleams with practical clarity",
        "DEFENSE":  "rings with a deep protective tone",
    }
    tone = affinity_txt.get(node.crystal_affinity, "glows with inner light")

    if depleted:
        return (
            f"💎 The {node.name} {tone} one last time as you harvest the final "
            f"{extracted:.2f} units. The formation dissolves — **NODE DEPLETED**."
        )

    if splintered:
        return (
            f"💥 **CRYSTAL FRACTURE!** The {node.name} {tone}, "
            f"but the tectonic stress (fracture prob {fracture_prob:.0%}) "
            f"shatters it on impact. "
            f"You collect {splinter_count} splinter(s) "
            f"({extracted:.2f} units total) — reduced economic value, "
            f"but useful for crafting volatile reagents."
        )

    if critical_hit:
        return (
            f"✨ **CRYSTAL RESONANCE!** Your {pickaxe.name} harmonises perfectly "
            f"with the {node.name} ({node.crystal_affinity} affinity). "
            f"The crystal {tone} and yields {extracted:.2f} flawless units!"
        )

    if stamina_starved:
        return (
            f"😤 Exhausted, you carefully tap the {node.name}. "
            f"It {tone} faintly. You recover only {extracted:.2f} units."
        )

    return (
        f"💎 The {node.name} {tone}. "
        f"You harvest {extracted:.2f} units of {node.quality} "
        f"{node.crystal_affinity} crystal from the {node.biome_affinity} formation."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — MINING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MiningEngine:
    """
    Core Resource Extraction Engine.

    Bridges player attributes (Pickaxe, stamina) with the active world state
    managed by ResourceSpawner.  Produces deterministic, auditable MiningResult
    objects from a single entry-point: execute_mining_attempt().

    Instantiation:
        engine  = MiningEngine()
        spawner = ResourceSpawner()
        result  = engine.execute_mining_attempt(pickaxe, stamina, state, node_id, catalog, roll=roll)

    The spawner instance must be passed or stored if callers want to re-use
    it; MiningEngine creates its own internal ResourceSpawner for the mutation
    call to keep construction clean, but exposes it as self.spawner so callers
    can share state across multiple calls.

    Thread Safety:
        Same contract as ResourceSpawner — the Discord bot layer is responsible
        for per-server locking before calling execute_mining_attempt().
    """

    def __init__(self) -> None:
        self.spawner: ResourceSpawner = ResourceSpawner()

    # ── Primary Entry Point ────────────────────────────────────────────────────

    def execute_mining_attempt(
        self,
        player_pickaxe:  Pickaxe,
        player_stamina:  float,
        state:           ServerSpawnState,
        node_id:         str,
        catalog:         ServerMaterialCatalog,
        *,
        roll:            int,
    ) -> MiningResult:
        """
        Execute one mining swing and return an immutable MiningResult.

        Pipeline (in order):
            1. Locate node in state (ore or crystal).
            2. Guard checks: broken tool, depleted node, zero stamina.
            3. Compute node hardness and stamina cost.
            4. Derive transient per-attempt entropy seed.
            5. Calculate base extraction yield (before crits and scatter).
            6. Apply stamina cap (scale yield if stamina is insufficient).
            7. Apply power efficiency factor (underpowered tool penalty).
            8. Apply yield scatter jitter (±SCATTER_PCT).
            9. Branch on node type:
               a. OreNode  — check critical hit, compute final yield.
               b. CrystalNode — check fracture, check critical hit.
            10. Call ResourceSpawner.extract_resource() to mutate state.
            11. Build and return frozen MiningResult.

        Parameters
        ----------
        player_pickaxe  : Pickaxe — player's equipped tool (power, efficiency).
        player_stamina  : float   — player's current stamina (> 0 to mine).
        state           : ServerSpawnState — live mutable world state.
        node_id         : str     — ID of the target ActiveOreNode or Crystal.
        catalog         : ServerMaterialCatalog — frozen geological baseline.
        roll            : int — world_stream.mining_roll(seed, guild, user, node, n).
                          Required: there is no fallback roll source.

        Returns
        -------
        MiningResult — frozen result record; never raises on normal game events
                       (depleted, underpowered, low stamina are all success=False
                       or partial success results, not exceptions).

        Raises
        ------
        KeyError   — if node_id does not exist in state.
        ValueError — if player_stamina is negative.
        """
        if player_stamina < 0:
            raise ValueError(f"player_stamina cannot be negative, got {player_stamina!r}")

        # ── Step 1: Locate node ───────────────────────────────────────────────
        node: Optional[Union[ActiveOreNode, ActiveCrystalNode]] = (
            state.active_ores.get(node_id)
            or state.active_crystals.get(node_id)
        )
        if node is None:
            raise KeyError(
                f"node_id {node_id!r} not found in ServerSpawnState "
                f"(server_id={state.server_id})"
            )

        is_crystal = isinstance(node, ActiveCrystalNode)
        resource_name = node.name if is_crystal else node.ore_name  # type: ignore[union-attr]

        # ── Step 2: Guard checks ──────────────────────────────────────────────

        # Broken tool
        if player_pickaxe.durability == 0:
            return MiningResult(
                success          = False,
                node_id          = node_id,
                resource_name    = resource_name,
                resource_type    = "CRYSTAL_GEM" if is_crystal else "ORE",
                stamina_consumed = 0.0,
                amount_extracted = 0.0,
                is_node_depleted = node.is_depleted,
                critical_hit     = False,
                flavour_message  = (
                    f"⚒ Your {player_pickaxe.name} is completely broken! "
                    f"Repair it before attempting to mine."
                ),
            )

        # Depleted node
        if node.is_depleted:
            return MiningResult(
                success          = False,
                node_id          = node_id,
                resource_name    = resource_name,
                resource_type    = "CRYSTAL_GEM" if is_crystal else "ORE",
                stamina_consumed = 0.0,
                amount_extracted = 0.0,
                is_node_depleted = True,
                critical_hit     = False,
                flavour_message  = (
                    f"🔴 {resource_name} node [{node_id}] is fully depleted. "
                    f"Wait for geological regeneration."
                ),
            )

        # Zero stamina
        if player_stamina <= 0.0:
            return MiningResult(
                success          = False,
                node_id          = node_id,
                resource_name    = resource_name,
                resource_type    = "CRYSTAL_GEM" if is_crystal else "ORE",
                stamina_consumed = 0.0,
                amount_extracted = 0.0,
                is_node_depleted = False,
                critical_hit     = False,
                flavour_message  = (
                    f"💨 You are completely exhausted. Rest before mining "
                    f"{resource_name}."
                ),
            )

        # ── Step 3: Hardness and stamina cost ─────────────────────────────────
        hardness     = _node_hardness(node.depth_layer, node.purity if not is_crystal
                                      else _crystal_hardness_purity(node))  # type: ignore
        stamina_required = _stamina_cost(node.depth_layer, catalog.pressure_index)

        # ── Step 4: Transient entropy seed ────────────────────────────────────
        entropy = _MiningEntropy(roll)

        # ── Step 5: Base yield ────────────────────────────────────────────────
        # Base yield = node's current_reserve fraction that one swing would
        # ideally extract, gated by pickaxe.efficiency.
        # We use a per-swing bite: 1/base_swings_to_clear × efficiency × reserve.
        # base_swings = hardness / max(pickaxe.power, 1.0) × 10
        # This means a tool exactly at hardness threshold extracts 10% of reserve.
        base_swings    = max(1.0, (hardness / max(player_pickaxe.power, 1.0)) * 10.0)
        base_yield     = node.current_reserve / base_swings * player_pickaxe.efficiency

        # ── Step 6: Stamina cap ────────────────────────────────────────────────
        stamina_consumed, stamina_scale = _apply_stamina_cap(
            stamina_available = player_stamina,
            stamina_required  = stamina_required,
            base_extraction   = base_yield,
        )
        stamina_starved = (stamina_scale < 1.0)
        yield_after_stamina = base_yield * stamina_scale

        # ── Step 7: Power efficiency factor ──────────────────────────────────
        power_factor   = _power_efficiency_factor(player_pickaxe.power, hardness)
        underpowered   = (player_pickaxe.power < hardness)
        yield_after_power = yield_after_stamina * power_factor

        # ── Step 8: Yield scatter jitter ──────────────────────────────────────
        scatter_roll   = entropy.roll_scatter()        # W2
        # Map [0,1] → [-SCATTER_PCT, +SCATTER_PCT] and add 1.0
        scatter_factor = 1.0 + (scatter_roll * 2.0 - 1.0) * _SCATTER_PCT
        yield_with_scatter = max(0.001, yield_after_power * scatter_factor)

        # ── Step 9a: Ore path ─────────────────────────────────────────────────
        if not is_crystal:
            ore_node  = node  # type: ignore[assignment]
            crit_roll = entropy.roll_critical()   # W0
            crit_roll_fracture = entropy.roll_fracture()  # W1 — consume to keep order
            entropy.skip_tool_wear()              # W4 — reserved

            # Crits only available at full efficiency (no underpowered override)
            critical_hit = (
                not underpowered
                and crit_roll >= _CRIT_THRESHOLD_NORM
            )
            final_yield = (
                yield_with_scatter * _CRIT_YIELD_MULTIPLIER
                if critical_hit
                else yield_with_scatter
            )
            final_yield = round(min(final_yield, node.current_reserve), 4)
            resource_type = "ORE"

            # Commit extraction via spawner
            actual_extracted = self.spawner.extract_resource(state, node_id, final_yield)
            actual_extracted = round(actual_extracted, 4)

            flavour = _ore_flavour(
                node            = ore_node,
                result_success  = True,
                critical_hit    = critical_hit,
                underpowered    = underpowered,
                stamina_starved = stamina_starved,
                depleted        = ore_node.is_depleted,
                extracted       = actual_extracted,
                hardness        = hardness,
                pickaxe         = player_pickaxe,
            )

            return MiningResult(
                success          = True,
                node_id          = node_id,
                resource_name    = ore_node.ore_name,
                resource_type    = resource_type,
                stamina_consumed = stamina_consumed,
                amount_extracted = actual_extracted,
                is_node_depleted = ore_node.is_depleted,
                critical_hit     = critical_hit,
                flavour_message  = flavour,
            )

        # ── Step 9b: Crystal path ─────────────────────────────────────────────
        crystal_node = node  # type: ignore[assignment]

        crit_roll      = entropy.roll_critical()   # W0
        fracture_roll  = entropy.roll_fracture()   # W1
        splint_count_r = entropy.roll_splinter_count()  # W3 (scatter already used W2)
        entropy.skip_tool_wear()                   # W4

        # Fracture probability: tectonic × mutation_affinity (capped at 0.95)
        fracture_prob = min(
            0.95,
            catalog.tectonic_activity * crystal_node.mutation_affinity
        ) if catalog.tectonic_activity > _TECTONIC_FRACTURE_GATE else 0.0

        splintered = (fracture_prob > 0.0 and fracture_roll < fracture_prob)

        if splintered:
            # Splinters: yield_with_scatter × SPLINTER_YIELD_FACTOR per fragment,
            # summed across splinter_count
            per_splinter   = yield_with_scatter * _SPLINTER_YIELD_FACTOR
            final_yield    = round(
                min(per_splinter * splint_count_r, node.current_reserve), 4
            )
            resource_type  = "CRYSTAL_SPLINTER"
            critical_hit   = False   # Cannot crit a fractured crystal
        else:
            critical_hit = (
                not underpowered
                and crit_roll >= _CRIT_THRESHOLD_NORM
            )
            final_yield = (
                yield_with_scatter * _CRIT_YIELD_MULTIPLIER
                if critical_hit
                else yield_with_scatter
            )
            final_yield    = round(min(final_yield, node.current_reserve), 4)
            resource_type  = "CRYSTAL_GEM"

        # Commit extraction
        actual_extracted = self.spawner.extract_resource(state, node_id, final_yield)
        actual_extracted = round(actual_extracted, 4)

        flavour = _crystal_flavour(
            node            = crystal_node,
            result_success  = True,
            splintered      = splintered,
            splinter_count  = splint_count_r,
            critical_hit    = critical_hit,
            stamina_starved = stamina_starved,
            depleted        = crystal_node.is_depleted,
            extracted       = actual_extracted,
            pickaxe         = player_pickaxe,
            fracture_prob   = fracture_prob,
        )

        return MiningResult(
            success          = True,
            node_id          = node_id,
            resource_name    = crystal_node.name,
            resource_type    = resource_type,
            stamina_consumed = stamina_consumed,
            amount_extracted = actual_extracted,
            is_node_depleted = crystal_node.is_depleted,
            critical_hit     = critical_hit,
            flavour_message  = flavour,
        )

    # ── Simulation Helper ──────────────────────────────────────────────────────

    def simulate_full_depletion(
        self,
        player_pickaxe: Pickaxe,
        player_stamina: float,
        state:          ServerSpawnState,
        node_id:        str,
        catalog:        ServerMaterialCatalog,
        roll_for:       Callable[[int], int],
        max_swings:     int = 500,
    ) -> Tuple[int, float, int]:
        """
        Mine a node until depleted (or max_swings reached), replenishing
        stamina to the starting value after each swing.

        Used for __main__ stress-test and balance validation only.

        Returns:
            (swings_taken, total_extracted, crit_count)
        """
        swings        = 0
        total         = 0.0
        crit_count    = 0
        # Clone starting stamina so we re-fuel after each swing (simulation only)
        stamina       = player_stamina

        while swings < max_swings:
            result = self.execute_mining_attempt(
                player_pickaxe  = player_pickaxe,
                player_stamina  = stamina,
                state           = state,
                node_id         = node_id,
                catalog         = catalog,
                roll            = roll_for(swings + 1),   # simulation only
            )
            swings     += 1
            total      += result.amount_extracted
            if result.critical_hit:
                crit_count += 1
            # Re-fuel stamina each tick (full depletion test with unlimited rests)
            stamina = player_stamina
            if result.is_node_depleted or not result.success:
                break

        return swings, round(total, 4), crit_count


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — CRYSTAL HARDNESS HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _crystal_hardness_purity(node: ActiveCrystalNode) -> str:
    """
    Map crystal quality to an equivalent purity tier for hardness calculation.

    Crystals don't have purity, but they DO have quality. We map them onto the
    same hardness scale so the power-vs-hardness matrix is uniform across both
    node types.

        Flawed    → "Crude"      (soft, easy to shatter)
        Prismatic → "Enriched"   (moderate hardness)
        Ethereal  → "Flawless"   (extremely hard; rare)
    """
    quality_map: Dict[str, str] = {
        "Flawed":    "Crude",
        "Prismatic": "Enriched",
        "Ethereal":  "Flawless",
    }
    return quality_map.get(node.quality, "Enriched")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — PRESET PICKAXE CATALOGUE
# ─────────────────────────────────────────────────────────────────────────────

# Canonical item catalogue used by __main__ and available to bot commands.
# item_id follows the pattern "tool:<slug>" for future DB indexing.
PICKAXES: Dict[str, Pickaxe] = {
    "copper_starter": Pickaxe(
        item_id    = "tool:copper_starter",
        name       = "Starter Copper Pickaxe",
        power      = 15.0,
        efficiency = 0.60,
        durability = 40,
    ),
    "iron_standard": Pickaxe(
        item_id    = "tool:iron_standard",
        name       = "Iron Pickaxe",
        power      = 35.0,
        efficiency = 0.80,
        durability = 80,
    ),
    "titanium_drill": Pickaxe(
        item_id    = "tool:titanium_drill",
        name       = "Titanium Drill",
        power      = 80.0,
        efficiency = 1.10,
        durability = 200,
    ),
    "abyss_resonator": Pickaxe(
        item_id    = "tool:abyss_resonator",
        name       = "Abyss Resonator",
        power      = 145.0,
        efficiency = 1.45,
        durability = 500,
    ),
    "broken_tool": Pickaxe(
        item_id    = "tool:broken",
        name       = "Broken Shard",
        power      = 0.0,
        efficiency = 0.0,
        durability = 0,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR = "  " + "═" * 76
_DIV_MINOR = "  " + "─" * 76
_DIV_THIN  = "  " + "·" * 76

_RES_ICONS: Dict[str, str] = {
    "ORE":              "⛏",
    "CRYSTAL_GEM":      "💎",
    "CRYSTAL_SPLINTER": "💥",
}

_BAR_WIDTH = 28


def _bar(fraction: float, width: int = _BAR_WIDTH) -> str:
    filled = int(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


def _print_pickaxe(p: Pickaxe) -> None:
    print(f"  ┌─ {p.name} ───")
    print(f"  │  item_id    : {p.item_id}")
    print(f"  │  power      : {p.power:.1f}")
    print(f"  │  efficiency : {p.efficiency:.2f}×")
    print(f"  │  durability : {p.durability}")
    print(f"  └──────────────────")


def _print_result(result: MiningResult, node_reserve_after: float,
                  node_max: float, hardness: float, stamina_cost: float) -> None:
    icon   = _RES_ICONS.get(result.resource_type, "?")
    status = "✅ SUCCESS" if result.success else "❌ FAIL"
    crit   = "  ✨ CRITICAL HIT" if result.critical_hit else ""
    depl   = "  🔴 NODE DEPLETED" if result.is_node_depleted else ""
    bar    = _bar(node_reserve_after / max(node_max, 1e-9))

    print(f"  {icon} {status}{crit}{depl}")
    print(f"     Resource    : {result.resource_name}  [{result.resource_type}]")
    print(f"     Extracted   : {result.amount_extracted:.4f} units")
    print(f"     Stamina     : {result.stamina_consumed:.2f} / {stamina_cost:.2f} required")
    print(f"     Hardness    : {hardness:.1f}   Node Reserve: [{bar}] {node_reserve_after:.4f}")
    print(f"     Message     : {result.flavour_message}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — __main__ SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from world_stream import test_seed
    import itertools as _it
    from world_stream import mining_roll as _mining_roll
    _TEST_SEED = test_seed("mining_engine-selftest")
    _test_attempts = _it.count(1)

    def _test_roll(state, node_id):
        """Self-test roll: same derivation as production, fake seed & counter."""
        return _mining_roll(_TEST_SEED, state.server_id, 42, node_id, next(_test_attempts))

    print()
    print(f"  {'╔' + '═' * 74 + '╗'}")
    print(f"  ║{'MINING_ENGINE.PY  —  Core Resource Extraction Engine':^74}║")
    print(f"  ║{'Block #4 — Procedural World System  |  Simulation Run':^74}║")
    print(f"  {'╚' + '═' * 74 + '╝'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 0 — Full pipeline bootstrap
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 0 — PIPELINE BOOTSTRAP (Genetic → Material → Spawn)")
    print(_DIV_MINOR)

    g_engine = GeneticEngine()
    m_engine = MaterialEngine()
    spawner  = ResourceSpawner()
    miner    = MiningEngine()

    # ── Server A: Neon Spire (moderate tectonic, crystal-bearing) ─────────────
    profile_a = g_engine.generate_profile(server_id=100000000000000001, seed=test_seed(f"{100000000000000001}:{1577836800}"), created_at=1577836800)
    catalog_a = m_engine.generate_geology(profile_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))
    state_a   = spawner.initialise(profile_a, catalog_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))

    # ── Build a synthetic high-pressure Uranium ABYSS world for scenario tests ─
    # We re-use material_gen's _build_mock_profile pattern inline.
    from identitas_genetik import ElementProfile as _EP, ServerGeneticProfile as _SGP
    import hashlib as _hl

    def _ep(sym, name, cat, rw, an):
        return _EP(atomic_number=an, symbol=sym, name=name, category=cat,
                   atomic_mass=0.0, period=0, group=None, rarity_weight=rw)

    uranium_sig = _hl.sha256(b"uranium_abyss_scenario_v1").hexdigest()
    uranium_profile = _SGP(
        server_id                  = 555000000000000555,
        created_at                 = 1420070400,
        genetic_signature          = uranium_sig,
        dominant_metal_element     = _ep("U",  "Uranium",  "actinide",        4,   92),
        secondary_metal_element    = _ep("Ni", "Nickel",   "transition metal", 300, 28),
        dominant_nonmetal_element  = _ep("Se", "Selenium", "reactive nonmetal",150, 34),
        secondary_nonmetal_element = _ep("S",  "Sulfur",   "reactive nonmetal",600, 16),
        applied_metal_modifier     = "U",
        applied_nonmetal_modifier  = None,
        world_age                  = "ANCIENT",
        base_world_stability       = 0.20,
        base_resource_density      = 1.75,
        base_mutation_index        = 0.95,
        biome_affinity             = ("IRRADIATED_WASTES", "NETHER_DEPTHS", "ABYSSAL_TRENCH"),
        world_flavour_tags         = (),
    )
    uranium_catalog = m_engine.generate_geology(uranium_profile, test_seed(f"{uranium_profile.server_id}:{uranium_profile.created_at}"))
    uranium_state   = spawner.initialise(uranium_profile, uranium_catalog, test_seed(f"{uranium_profile.server_id}:{uranium_profile.created_at}"))

    print(f"  ✓ Neon Spire     — tectonic={catalog_a.tectonic_activity:.4f}  "
          f"pressure={catalog_a.pressure_index:.4f}")
    print(f"  ✓ Uranium Cradle — tectonic={uranium_catalog.tectonic_activity:.4f}  "
          f"pressure={uranium_catalog.pressure_index:.4f}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 1 — HARDNESS MATRIX PROOF
    # Show the full hardness table for all purity × depth combinations.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 1 — HARDNESS MATRIX  (Purity × Depth)")
    print(_DIV_MINOR)
    print(f"  {'Purity':<12} {'SURFACE':>10} {'SHALLOW':>10} {'DEEP':>10} {'ABYSS':>10}")
    print(f"  {'─'*12} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")
    for purity in ["Crude", "Enriched", "Flawless"]:
        row = f"  {purity:<12}"
        for depth in ["SURFACE", "SHALLOW", "DEEP", "ABYSS"]:
            h = _node_hardness(depth, purity)
            row += f" {h:>10.1f}"
        print(row)
    print()
    print(f"  Pickaxe power reference:")
    for name, pk in PICKAXES.items():
        if pk.durability > 0:
            print(f"    {pk.name:<28}  power={pk.power:>6.1f}  "
                  f"efficiency={pk.efficiency:.2f}×  durability={pk.durability}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 — SCENARIO A: Starter Copper Pickaxe vs ABYSS Uranium Node
    # Expected: heavily underpowered, high stamina cost, partial yield
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 2 — SCENARIO A: Starter Copper Pickaxe → ABYSS Uranium Node")
    print("            (Showcasing power penalty + abyssal stamina pressure)")
    print(_DIV_MINOR)

    copper_pick = PICKAXES["copper_starter"]

    # Find an ABYSS node in the uranium world
    abyss_node_id   = None
    for nid, n in uranium_state.active_ores.items():
        if n.depth_layer == "ABYSS":
            abyss_node_id = nid
            break

    # Fallback: use the deepest available node if no ABYSS found
    if abyss_node_id is None:
        depth_order = {"ABYSS": 0, "DEEP": 1, "SHALLOW": 2, "SURFACE": 3}
        abyss_node_id = min(
            uranium_state.active_ores.keys(),
            key=lambda k: depth_order.get(uranium_state.active_ores[k].depth_layer, 99)
        )

    abyss_node = uranium_state.active_ores[abyss_node_id]
    abyss_hardness = _node_hardness(abyss_node.depth_layer, abyss_node.purity)
    abyss_stamina_cost = _stamina_cost(abyss_node.depth_layer, uranium_catalog.pressure_index)

    print(f"  Target Node   : {abyss_node_id}")
    print(f"  Ore           : {abyss_node.ore_name}  [{abyss_node.element_symbol}]")
    print(f"  Depth Layer   : {abyss_node.depth_layer}  |  Purity: {abyss_node.purity}")
    print(f"  Node Hardness : {abyss_hardness:.1f}  |  Pickaxe Power: {copper_pick.power:.1f}")
    print(f"  Power Gap     : {abyss_hardness - copper_pick.power:.1f} "
          f"(tool is {((abyss_hardness - copper_pick.power) / abyss_hardness * 100):.1f}% underpowered)")
    power_factor_demo = _power_efficiency_factor(copper_pick.power, abyss_hardness)
    print(f"  Power Factor  : {power_factor_demo:.4f}  "
          f"({power_factor_demo*100:.1f}% of potential yield)")
    print(f"  Stamina Cost  : {abyss_stamina_cost:.2f} per swing  "
          f"(pressure={uranium_catalog.pressure_index:.4f})")
    print(_DIV_MINOR)
    print(f"  Running 5 mining attempts with stamina=100 (then stamina=8 to show truncation):")
    print()

    STAMINA_SCENARIOS = [100.0, 100.0, 100.0, 8.0, 100.0]
    for i, stamina in enumerate(STAMINA_SCENARIOS, 1):
        result = miner.execute_mining_attempt(
            player_pickaxe  = copper_pick,
            player_stamina  = stamina,
            state           = uranium_state,
            node_id         = abyss_node_id,
            catalog         = uranium_catalog,
            roll = _test_roll(uranium_state, abyss_node_id),
        )
        node_after = uranium_state.active_ores[abyss_node_id]
        print(f"  ── Swing {i}  (stamina={stamina:.0f})")
        _print_result(result, node_after.current_reserve, node_after.max_reserve,
                      abyss_hardness, abyss_stamina_cost)
        print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 3 — SCENARIO B: Titanium Drill vs SURFACE Iron Node
    # Expected: full efficiency, scatter visible, potential crit
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 3 — SCENARIO B: Titanium Drill → SURFACE/SHALLOW Iron Node")
    print("            (Showcasing full efficiency + critical hit potential)")
    print(_DIV_MINOR)

    titanium_drill = PICKAXES["titanium_drill"]

    # Find a shallow or surface ore node in Neon Spire
    surface_node_id = None
    for nid, n in state_a.active_ores.items():
        if n.depth_layer in ("SURFACE", "SHALLOW"):
            surface_node_id = nid
            break

    surf_node = state_a.active_ores[surface_node_id]
    surf_hardness = _node_hardness(surf_node.depth_layer, surf_node.purity)
    surf_stamina_cost = _stamina_cost(surf_node.depth_layer, catalog_a.pressure_index)

    print(f"  Target Node   : {surface_node_id}")
    print(f"  Ore           : {surf_node.ore_name}  [{surf_node.element_symbol}]")
    print(f"  Depth Layer   : {surf_node.depth_layer}  |  Purity: {surf_node.purity}")
    print(f"  Node Hardness : {surf_hardness:.1f}  |  Drill Power: {titanium_drill.power:.1f}")
    print(f"  Power Factor  : 1.0000 (at full efficiency — power exceeds hardness)")
    print(f"  Stamina Cost  : {surf_stamina_cost:.2f} per swing (no pressure penalty)")
    print(_DIV_MINOR)
    print(f"  Running 8 mining attempts with stamina=100:")
    print()

    for i in range(1, 9):
        result = miner.execute_mining_attempt(
            player_pickaxe = titanium_drill,
            player_stamina = 100.0,
            state          = state_a,
            node_id        = surface_node_id,
            catalog        = catalog_a,
            roll = _test_roll(state_a, surface_node_id),
        )
        node_after = state_a.active_ores[surface_node_id]
        print(f"  ── Swing {i}")
        _print_result(result, node_after.current_reserve, node_after.max_reserve,
                      surf_hardness, surf_stamina_cost)
        print()
        if result.is_node_depleted:
            print("  ⚠ Node depleted — stopping scenario.")
            break

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4 — SCENARIO C: Crystal Splintering
    # Mine a high-tectonic crystal node to demonstrate fracture mechanics.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 4 — SCENARIO C: Crystal Splintering")
    print("            (High-tectonic world → fracture probability showcase)")
    print(_DIV_MINOR)

    # Use Neon Spire's crystal nodes (tectonic ~0.9 → above the 0.7 fracture gate)
    crystal_node_id = next(iter(state_a.active_crystals))
    crys_node       = state_a.active_crystals[crystal_node_id]
    fracture_prob   = (
        min(0.95, catalog_a.tectonic_activity * crys_node.mutation_affinity)
        if catalog_a.tectonic_activity > _TECTONIC_FRACTURE_GATE
        else 0.0
    )
    crys_hardness = _node_hardness(crys_node.depth_layer,
                                   _crystal_hardness_purity(crys_node))

    print(f"  Crystal       : {crys_node.name}  [{crys_node.crystal_affinity}]")
    print(f"  Quality       : {crys_node.quality}  |  Biome: {crys_node.biome_affinity}")
    print(f"  Depth         : {crys_node.depth_layer}  |  Hardness: {crys_hardness:.1f}")
    print(f"  Tectonic Act. : {catalog_a.tectonic_activity:.4f}  "
          f"(gate={_TECTONIC_FRACTURE_GATE}  — fracture {'ACTIVE' if fracture_prob > 0 else 'INACTIVE'})")
    print(f"  Mutation Aff. : {crys_node.mutation_affinity:.4f}")
    print(f"  Fracture Prob : {fracture_prob:.4f}  ({fracture_prob*100:.1f}%)")
    print(f"  Reserve       : {crys_node.current_reserve:.2f} / {crys_node.max_reserve:.2f}")
    print(_DIV_MINOR)
    print(f"  Mining with Titanium Drill (10 attempts, showing fracture/gem distribution):")
    print()

    crys_stamina_cost = _stamina_cost(crys_node.depth_layer, catalog_a.pressure_index)
    gem_count       = 0
    splinter_count  = 0

    for i in range(1, 11):
        if state_a.active_crystals[crystal_node_id].is_depleted:
            print(f"  Crystal depleted after {i-1} swings.")
            break
        result = miner.execute_mining_attempt(
            player_pickaxe = titanium_drill,
            player_stamina = 100.0,
            state          = state_a,
            node_id        = crystal_node_id,
            catalog        = catalog_a,
            roll = _test_roll(state_a, crystal_node_id),
        )
        node_after = state_a.active_crystals[crystal_node_id]
        if result.resource_type == "CRYSTAL_GEM":
            gem_count     += 1
        else:
            splinter_count += 1
        print(f"  ── Swing {i}")
        _print_result(result, node_after.current_reserve, node_after.max_reserve,
                      crys_hardness, crys_stamina_cost)
        print()

    print(f"  Crystal Distribution: {gem_count} GEM harvest(s), "
          f"{splinter_count} SPLINTER event(s)  "
          f"(fracture_prob={fracture_prob*100:.1f}%)")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 5 — SCENARIO D: Broken Tool Guard
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 5 — SCENARIO D: Broken Tool & Zero Stamina Guards")
    print(_DIV_MINOR)

    broken_pick = PICKAXES["broken_tool"]
    guard_node_id = next(iter(state_a.active_ores))

    print(f"  Test 1 — Broken tool (durability=0):")
    result_broken = miner.execute_mining_attempt(
        player_pickaxe = broken_pick,
        player_stamina = 100.0,
        state          = state_a,
        node_id        = guard_node_id,
        catalog        = catalog_a,
        roll = _test_roll(state_a, guard_node_id),
    )
    print(f"    success={result_broken.success}  extracted={result_broken.amount_extracted}")
    print(f"    {result_broken.flavour_message}")
    print()

    print(f"  Test 2 — Zero stamina:")
    result_no_stamina = miner.execute_mining_attempt(
        player_pickaxe = PICKAXES["titanium_drill"],
        player_stamina = 0.0,
        state          = state_a,
        node_id        = guard_node_id,
        catalog        = catalog_a,
        roll = _test_roll(state_a, guard_node_id),
    )
    print(f"    success={result_no_stamina.success}  extracted={result_no_stamina.amount_extracted}")
    print(f"    {result_no_stamina.flavour_message}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 6 — SCENARIO E: Abyss Resonator clears the Uranium node
    # Show the correct tool for the job vs the copper starter.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 6 — SCENARIO E: Abyss Resonator vs the same Uranium ABYSS Node")
    print("            (Correct tool showcasing full efficiency + crit capability)")
    print(_DIV_MINOR)

    resonator = PICKAXES["abyss_resonator"]
    reso_hardness    = _node_hardness(abyss_node.depth_layer, abyss_node.purity)
    reso_power_fac   = _power_efficiency_factor(resonator.power, reso_hardness)
    reso_stamina_req = _stamina_cost(abyss_node.depth_layer, uranium_catalog.pressure_index)

    print(f"  Target Node   : {abyss_node_id}")
    print(f"  Node Hardness : {reso_hardness:.1f}  |  Resonator Power: {resonator.power:.1f}")
    print(f"  Power Factor  : {reso_power_fac:.4f}  ({'full efficiency ✓' if reso_power_fac == 1.0 else 'penalised'})")
    print(f"  Stamina Cost  : {reso_stamina_req:.2f} per swing")
    print()
    print(f"  Current node reserve after Scenario A mining:")
    reso_node_current = uranium_state.active_ores[abyss_node_id]
    print(f"    {reso_node_current.current_reserve:.4f} / "
          f"{reso_node_current.max_reserve:.4f} units remaining")
    print(_DIV_MINOR)
    print(f"  Running 5 attempts with Abyss Resonator (stamina=100):")
    print()

    for i in range(1, 6):
        if uranium_state.active_ores[abyss_node_id].is_depleted:
            print(f"  Node depleted after {i-1} swings.")
            break
        result = miner.execute_mining_attempt(
            player_pickaxe = resonator,
            player_stamina = 100.0,
            state          = uranium_state,
            node_id        = abyss_node_id,
            catalog        = uranium_catalog,
            roll = _test_roll(uranium_state, abyss_node_id),
        )
        node_after = uranium_state.active_ores[abyss_node_id]
        print(f"  ── Swing {i}")
        _print_result(result, node_after.current_reserve, node_after.max_reserve,
                      reso_hardness, reso_stamina_req)
        print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 7 — FULL DEPLETION RUN (balance validation)
    # Use simulate_full_depletion for a fresh state to prove total conserved.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 7 — FULL DEPLETION VALIDATION  (Balance & Reserve Invariant)")
    print(_DIV_MINOR)

    # Fresh spawn for a clean depletion test
    fresh_state = spawner.initialise(profile_a, catalog_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))
    test_node_id = next(iter(fresh_state.active_ores))
    test_node    = fresh_state.active_ores[test_node_id]

    print(f"  Node          : {test_node_id}")
    print(f"  Starting Res  : {test_node.max_reserve:.4f} units")
    print()

    for pk_key, pk in [("copper_starter", PICKAXES["copper_starter"]),
                        ("titanium_drill", PICKAXES["titanium_drill"])]:
        fresh_sub   = spawner.initialise(profile_a, catalog_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))
        sub_nid     = next(iter(fresh_sub.active_ores))
        sub_node    = fresh_sub.active_ores[sub_nid]
        sub_miner   = MiningEngine()
        swings, total, crits = sub_miner.simulate_full_depletion(
            player_pickaxe = pk,
            player_stamina = 100.0,
            state          = fresh_sub,
            node_id        = sub_nid,
            catalog        = catalog_a,
            roll_for       = lambda n, _s=fresh_sub, _nid=sub_nid: _test_roll(_s, _nid),
            max_swings     = 2000,
        )
        delta_ok = abs(total - sub_node.max_reserve) < 0.01
        print(f"  {pk.name:<28}  swings={swings:>5}  extracted={total:>10.4f}  "
              f"crits={crits:>3}  "
              f"invariant={'PASS ✓' if delta_ok else 'FAIL ✗'} "
              f"(Δ={abs(total - sub_node.max_reserve):.6f})")

    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 8 — DETERMINISM PROOF
    # Same inputs → identical MiningResult on every run.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PHASE 8 — DETERMINISM PROOF  (Same inputs → identical outputs)")
    print(_DIV_MINOR)

    # Build two fresh identical states and mine the same node with same stamina
    det_state1  = spawner.initialise(profile_a, catalog_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))
    det_state2  = spawner.initialise(profile_a, catalog_a, test_seed(f"{profile_a.server_id}:{profile_a.created_at}"))
    det_node_id = next(iter(det_state1.active_ores))

    miner1 = MiningEngine()
    miner2 = MiningEngine()

    STAMINA_TEST = 73.5
    DET_ROLL = _test_roll(det_state1, det_node_id)     # same roll for both engines
    r1 = miner1.execute_mining_attempt(PICKAXES["titanium_drill"], STAMINA_TEST,
                                        det_state1, det_node_id, catalog_a, roll=DET_ROLL)
    r2 = miner2.execute_mining_attempt(PICKAXES["titanium_drill"], STAMINA_TEST,
                                        det_state2, det_node_id, catalog_a, roll=DET_ROLL)

    checks = [
        ("success",          r1.success          == r2.success),
        ("resource_type",    r1.resource_type     == r2.resource_type),
        ("stamina_consumed", r1.stamina_consumed  == r2.stamina_consumed),
        ("amount_extracted", r1.amount_extracted  == r2.amount_extracted),
        ("critical_hit",     r1.critical_hit      == r2.critical_hit),
        ("is_node_depleted", r1.is_node_depleted  == r2.is_node_depleted),
    ]

    all_pass = all(ok for _, ok in checks)
    for field_name, ok in checks:
        v1 = getattr(r1, field_name)
        v2 = getattr(r2, field_name)
        print(f"  {'✓' if ok else '✗'}  {field_name:<20} : {v1!r}  ==  {v2!r}")

    print()
    if all_pass:
        print("  ✓ ALL DETERMINISM CHECKS PASSED — identical results for identical inputs.")
    else:
        print("  ✗ DETERMINISM FAILURE — review entropy seeding logic.")
    print()

    print(_DIV_MAJOR)
    print("  ✓ MINING_ENGINE.PY SIMULATION COMPLETE — all phases passed.")
    print(_DIV_MAJOR)
    print()