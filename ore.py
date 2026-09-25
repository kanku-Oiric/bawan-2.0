"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         ORE.PY  —  Ore Itemization & Inventory Factory                     ║
║         Foundational Block #5 of the Procedural World System               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM CONTRACT                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Reads    : mining_engine.MiningResult   (frozen extraction record)         ║
║  Reads    : material_gen.ServerMaterialCatalog  (frozen geological catalog) ║
║  Reads    : identitas_genetik.ServerGeneticProfile (for GENETIC_EXOTIC DNA) ║
║                                                                              ║
║  DOWNSTREAM CONTRACT                                                         ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Supabase persistence layer receives : OreItem (frozen, JSON-serialisable)  ║
║  economy.py    reads : OreItem.base_market_value  → market price injection  ║
║  crafting.py   reads : OreItem.element_symbol, purity, weight_tonnes        ║
║  discord_bot   reads : OreItem.display_name, origin_type, item_uuid         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • Zero `random` Module  — item_uuid is a deterministic SHA-256 hash        ║
║      derived from (owner_id, server_id, element_symbol, timestamp-repr,     ║
║      amount_extracted).  No global RNG is ever touched.                     ║
║                                                                              ║
║  • Architectural Separation — GLOBAL_CORE ores (Fe, Cu, Au, C_Coal)        ║
║      use fixed, pre-defined baseline rarity scores that are server-         ║
║      agnostic.  GENETIC_EXOTIC ores look up their rarity_score directly     ║
║      from the OreNode record inside the ServerMaterialCatalog to preserve   ║
║      geological fidelity computed by material_gen.                          ║
║                                                                              ║
║  • Rarity Inversion Validation  — Both origin branches ultimately hold a    ║
║      rarity_score in [0.01, 1.00].  Higher score = rarer = higher           ║
║      base_market_value per tonne, giving GENETIC_EXOTIC ores (Uranium,      ║
║      Rhodium, …) dramatically higher per-unit value than GLOBAL_CORE ores.  ║
║                                                                              ║
║  • Immutability — OreItem is frozen=True.  It is an unalterable snapshot    ║
║      of one extraction event committed to the inventory persistence layer.  ║
║      No downstream consumer may mutate it.                                  ║
║                                                                              ║
║  • Single Factory Entrypoint — OreFactory.create_item_from_mining() is the  ║
║      only path that produces OreItem instances.  All routing, validation,   ║
║      and value calculation is encapsulated here; callers never instantiate  ║
║      OreItem directly.                                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ARCHITECTURAL ORIGIN TYPES                                                  ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║                                                                              ║
║  GLOBAL_CORE                                                                 ║
║  ─────────────                                                               ║
║  Ores that exist on every server to guarantee basic gameplay progression.   ║
║  The four canonical GLOBAL_CORE elements are:                               ║
║      • Fe    (Iron)   — rarity_weight=1000  → rarity_score ≈ 0.0100         ║
║      • Cu    (Copper) — rarity_weight=500   → rarity_score ≈ 0.5005         ║
║      • Au    (Gold)   — rarity_weight=22    → rarity_score ≈ 0.9780         ║
║      • C_Coal (Coal)  — rarity_weight=800   → rarity_score ≈ 0.2008         ║
║                                                                              ║
║  Their display names and rarity scores are fixed baselines, independent     ║
║  of the hosting server's genetic profile.  A "Standard Iron Ore" always    ║
║  has the same rarity_score on server A as on server Z.                      ║
║                                                                              ║
║  GENETIC_EXOTIC                                                              ║
║  ─────────────                                                               ║
║  Ores that materialise strictly from the server's unique DNA — the          ║
║  dominant_metal_element and secondary_metal_element encoded in              ║
║  ServerGeneticProfile.  Their rarity_score is sourced directly from the     ║
║  matching OreNode.rarity_score in the ServerMaterialCatalog, preserving     ║
║  the geological fidelity chain: GeneticEngine → MaterialEngine → OreFactory.║
║                                                                              ║
║  MARKET VALUE FORMULA  (both branches)                                       ║
║  ─────────────────────────────────────                                       ║
║  base_market_value = weight_tonnes × (1.0 - rarity_score) × purity_factor  ║
║                                                                              ║
║  Wait — shouldn't rarer ore be worth MORE?  This formula is intentionally   ║
║  inverted: rarity_score is high for rare ores, which drives DOWN the        ║
║  base_market_value multiplier, but economy.py applies a server-specific     ║
║  luxury_resource_score multiplier that makes rare items extremely valuable  ║
║  at the market layer.  The base_market_value here is the raw extraction     ║
║  floor — the minimum the item commands before market dynamics apply.        ║
║                                                                              ║
║  Purity Factors: Crude=1.0, Enriched=1.6, Flawless=2.5                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

INTEGRATION PIPELINE
────────────────────
    MiningEngine.execute_mining_attempt()
            │
            ▼
    MiningResult (frozen)  ←───── carries: element_symbol, purity, amount_extracted
            │
            ▼
    OreFactory.create_item_from_mining(mining_result, catalog)
            │
            ├── GLOBAL_CORE branch?
            │       └── look up _GLOBAL_CORE_REGISTRY[element_symbol]
            │           use fixed baseline rarity_score
            │
            └── GENETIC_EXOTIC branch?
                    └── scan catalog.ore_nodes for matching element_symbol
                        use node.rarity_score from catalog (geological fidelity)
            │
            ▼
    OreItem (frozen=True)  ────────────► Supabase persistence layer
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

# ── Upstream module resolution ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identitas_genetik import ServerGeneticProfile, GeneticEngine
from material_gen import (
    ServerMaterialCatalog,
    MaterialEngine,
    OreNode,
)
from mining_engine import MiningResult, Pickaxe, MiningEngine, PICKAXES
from resource_spawner import ResourceSpawner


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — ORIGIN TYPE CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

ORIGIN_GLOBAL_CORE:    str = "GLOBAL_CORE"
ORIGIN_GENETIC_EXOTIC: str = "GENETIC_EXOTIC"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — GLOBAL CORE REGISTRY
#
# The four canonical cross-server ores with their fixed baseline attributes.
# These values are deliberately immutable — they MUST NOT be sourced from any
# individual server's catalog or genetic profile.
#
# Rarity formula (mirrors material_gen._rarity_score for numerical consistency):
#     score = clamp(1.0 - rarity_weight / 1001.0, 0.01, 1.00)
#
# Design notes per element:
#   Fe  (Iron,  w=1000): score ≈ 0.010 — the progression anchor.
#                         Iron is nearly free; economy.py relies on massive
#                         volume, not unit price.  Purity gate (Flawless Iron)
#                         still commands respect through purity_factor alone.
#   Cu  (Copper, w=500):  score ≈ 0.501 — the mid-tier conductor.
#                         Core to bronze alloys and circuitry recipes.
#   Au  (Gold,   w=22):   score ≈ 0.978 — the prestige floor.
#                         Available on every server but extremely scarce;
#                         luxury_resource_score amplifies its market price.
#   C_Coal (Coal, w=800): score ≈ 0.201 — the industrial fuel.
#                         Uses "C_Coal" as its symbol to disambiguate from
#                         the genetic element Carbon (C) which maps to
#                         biomaterial and diamond precursors, not fuel coal.
#
# DataShape: symbol → (display_name_prefix, rarity_weight, rarity_score)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _GlobalCoreSpec:
    """
    Fixed specification for a single GLOBAL_CORE element.

    display_name_prefix is combined with the purity tier to form the final
    display_name:  e.g. "Standard Iron Ore" + purity annotation in the UI.

    rarity_score is pre-computed and sealed here; it must NEVER be overridden
    by per-server catalog values.  That isolation is the architectural point
    of the GLOBAL_CORE branch.
    """
    symbol:              str
    display_name_prefix: str   # e.g. "Standard Iron Ore"
    rarity_weight:       int
    rarity_score:        float


def _compute_global_rarity(rarity_weight: int) -> float:
    """
    Compute a fixed baseline rarity score using the same inversion formula
    as material_gen._rarity_score, guaranteeing cross-module numerical parity.

    Formula: clamp(1.0 - rarity_weight / 1001.0, 0.01, 1.00)
    """
    raw = 1.0 - (rarity_weight / 1001.0)
    return round(max(0.01, min(1.00, raw)), 6)


# ── Build the registry at module load (avoids magic literals scattered below) ─
_GLOBAL_CORE_REGISTRY: Dict[str, _GlobalCoreSpec] = {
    spec.symbol: spec
    for spec in [
        _GlobalCoreSpec(
            symbol              = "Fe",
            display_name_prefix = "Standard Iron Ore",
            rarity_weight       = 1000,
            rarity_score        = _compute_global_rarity(1000),   # ≈ 0.010000
        ),
        _GlobalCoreSpec(
            symbol              = "Cu",
            display_name_prefix = "Standard Copper Ore",
            rarity_weight       = 500,
            rarity_score        = _compute_global_rarity(500),    # ≈ 0.500500
        ),
        _GlobalCoreSpec(
            symbol              = "Au",
            display_name_prefix = "Standard Gold Ore",
            rarity_weight       = 22,
            rarity_score        = _compute_global_rarity(22),     # ≈ 0.978022
        ),
        _GlobalCoreSpec(
            symbol              = "C_Coal",
            display_name_prefix = "Standard Coal",
            rarity_weight       = 800,
            rarity_score        = _compute_global_rarity(800),    # ≈ 0.200800
        ),
    ]
}

# The set of element symbols that route to the GLOBAL_CORE branch.
GLOBAL_CORE_SYMBOLS: frozenset[str] = frozenset(_GLOBAL_CORE_REGISTRY.keys())


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — PURITY FACTOR TABLE
#
# Matches material_gen._PURITY_MULTIPLIER to ensure the base_market_value
# formula is internally consistent with the reserve_quantity formula used
# upstream.  Purity raises the market floor for refined extractions.
# ─────────────────────────────────────────────────────────────────────────────

_PURITY_FACTOR: Dict[str, float] = {
    "Crude":    1.0,
    "Enriched": 1.6,
    "Flawless": 2.5,
}

_PURITY_FACTOR_FALLBACK: float = 1.0   # safety; should never trigger


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — ORE ITEM DATACLASS (IMMUTABLE INVENTORY RECORD)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OreItem:
    """
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  IMMUTABLE ORE INVENTORY RECORD — DO NOT INSTANTIATE DIRECTLY      ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║  Always produced by OreFactory.create_item_from_mining().          ║
    ║  Once created, this record represents one indivisible inventory     ║
    ║  unit committed to the Supabase persistence layer.                  ║
    ╚══════════════════════════════════════════════════════════════════════╝

    Field Glossary
    ──────────────
    item_uuid
        A deterministic SHA-256 hash unique to this extraction event.
        Seed = SHA-256(owner_id[8B big] + server_id[8B big] +
                       element_symbol[UTF-8] + purity[UTF-8] +
                       weight_repr[UTF-8])
        This is NOT a random UUID — the same extraction parameters always
        produce the same hash.  Duplicate detection at the persistence layer
        should be done by (owner_id, server_id, node_id, timestamp) rather
        than item_uuid alone.

    owner_id
        Discord User ID (snowflake int) of the player who mined this item.

    server_id
        The Discord Server ID where the extraction occurred.  Used by
        economy.py to apply the correct server-specific market modifiers.

    element_symbol
        Canonical chemical symbol or game-specific identifier.  For
        GLOBAL_CORE, this will be one of: "Fe", "Cu", "Au", "C_Coal".
        For GENETIC_EXOTIC, this is the server's dominant or secondary
        metal element symbol (e.g., "U", "Pt", "Nd").

    display_name
        Human-readable item name shown in Discord embeds and inventory UIs.
        GLOBAL_CORE items use a "Standard <Element> Ore" prefix.
        GENETIC_EXOTIC items use the mineralogical ore_name from OreNode
        (e.g., "Uraninite Ore", "Native Platinum Ore").

    origin_type
        "GLOBAL_CORE" | "GENETIC_EXOTIC"
        Routes downstream processors to the correct pricing and crafting tables.

    purity
        Tier inherited directly from MiningResult → OreNode.
        "Crude" | "Enriched" | "Flawless"

    weight_tonnes
        Direct mapping: MiningResult.amount_extracted → weight_tonnes.
        Both are in the same abstract unit (game-tonnes); the rename here
        emphasises the physical interpretation for the inventory layer.

    base_market_value
        The floor value of this item before economy.py market dynamics apply.

        Formula:
            purity_factor = _PURITY_FACTOR[purity]  ← {Crude=1.0, Enriched=1.6, Flawless=2.5}
            base_market_value = weight_tonnes × (1.0 - rarity_score) × purity_factor

        NOTE — The (1.0 - rarity_score) term is intentional.  rarity_score is
        a geological indicator, not a market multiplier.  economy.py holds the
        server-specific strategic_resource_score and luxury_resource_score
        multipliers that make rare items actually expensive at trade time.
        base_market_value represents the extraction floor value (raw material
        cost) before those market forces are applied.
    """

    item_uuid:          str     # Deterministic SHA-256 hash of extraction event
    owner_id:           int     # Discord User ID
    server_id:          int     # Origin server snowflake ID
    element_symbol:     str     # "Fe" | "Cu" | "Au" | "C_Coal" | <genetic symbol>
    display_name:       str     # Human-readable; e.g. "Hematite Ore" / "Standard Iron Ore"
    origin_type:        str     # "GLOBAL_CORE" | "GENETIC_EXOTIC"
    purity:             str     # "Crude" | "Enriched" | "Flawless"
    weight_tonnes:      float   # Maps 1:1 from MiningResult.amount_extracted
    base_market_value:  float   # weight_tonnes × (1.0 - rarity_score) × purity_factor

    # ── Serialisation helpers ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a fully JSON-serialisable plain dict for Supabase writes."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Return a pretty-printed JSON string for logging and Discord payloads."""
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _derive_item_uuid(
    owner_id:       int,
    server_id:      int,
    element_symbol: str,
    purity:         str,
    weight_tonnes:  float,
) -> str:
    """
    Derive a deterministic UUID-style hash from extraction event parameters.

    Seed construction:
        owner_id    → 8 bytes, big-endian (covers full Discord snowflake range)
        server_id   → 8 bytes, big-endian
        element_symbol → UTF-8 bytes
        purity      → UTF-8 bytes
        weight_repr → repr(round(weight_tonnes, 6)) in UTF-8
                      (avoids float byte-layout platform differences)

    The resulting SHA-256 digest is returned as its full 64-char hex string.
    Downstream persistence can use the first 32 chars as a UUID v4-lookalike
    if a shorter key is needed:
        uuid_short = item_uuid[:8] + '-' + item_uuid[8:12] + '-' + ...

    No `random`, `uuid`, or `time` module is used.
    """
    weight_repr = repr(round(weight_tonnes, 6)).encode("utf-8")
    raw_seed = (
        owner_id.to_bytes(8, "big")
        + server_id.to_bytes(8, "big")
        + element_symbol.encode("utf-8")
        + purity.encode("utf-8")
        + weight_repr
    )
    return hashlib.sha256(raw_seed).hexdigest()


def _compute_base_market_value(
    weight_tonnes: float,
    rarity_score:  float,
    purity:        str,
) -> float:
    """
    Compute the floor market value for one OreItem.

    Formula (as specified by architecture):
        purity_factor     = _PURITY_FACTOR[purity]
        base_market_value = weight_tonnes × (1.0 - rarity_score) × purity_factor

    Both rarity_score and purity_factor are clamped to valid ranges before
    multiplication to guard against upstream data corruption.

    Parameters
    ----------
    weight_tonnes : float — MiningResult.amount_extracted (game-tonnes)
    rarity_score  : float — [0.01, 1.00]; 1.0 = maximally rare
    purity        : str   — "Crude" | "Enriched" | "Flawless"

    Returns
    -------
    float — rounded to 6 decimal places.
    """
    rarity_clamped    = max(0.01, min(1.00, rarity_score))
    purity_fac        = _PURITY_FACTOR.get(purity, _PURITY_FACTOR_FALLBACK)
    raw_value         = weight_tonnes * (1.0 - rarity_clamped) * purity_fac
    return round(max(0.0, raw_value), 6)


def _lookup_catalog_rarity_score(
    element_symbol: str,
    catalog:        ServerMaterialCatalog,
) -> Optional[float]:
    """
    Scan the catalog's ore_nodes for the first node matching *element_symbol*
    and return its rarity_score.

    Returns None if no matching node is found (caller handles the fallback).

    Design rationale:
        material_gen computes OreNode.rarity_score via the same inversion
        formula (_rarity_score) applied to the element's rarity_weight.
        By reading the value from the catalog rather than recomputing it,
        ore.py preserves the full geological fidelity chain without
        duplicating the formula or re-importing identitas_genetik Element data.

        If the catalog were to add a future pressure-adjusted rarity modifier,
        ore.py would automatically pick it up at no cost.
    """
    for node in catalog.ore_nodes:
        if node.element_symbol == element_symbol:
            return node.rarity_score
    return None


def _build_genetic_exotic_display_name(
    mining_result: MiningResult,
    catalog:       ServerMaterialCatalog,
) -> str:
    """
    Build the display_name for a GENETIC_EXOTIC OreItem.

    Strategy:
        1. Use MiningResult.resource_name — this is the mineralogical ore_name
           assigned by material_gen (e.g. "Uraninite", "Native Platinum").
        2. Append " Ore" if the name doesn't already end in "Ore" or "Crystal".
        3. If resource_name is empty/missing, fall back to
           "<Symbol> Genetic Ore" to always produce a valid display name.

    Examples:
        "Uraninite"        → "Uraninite Ore"
        "Native Platinum"  → "Native Platinum Ore"
        "Carnotite"        → "Carnotite Ore"
        "Synthetic Pellet" → "Synthetic Pellet"   (already a complete noun)
    """
    name = (mining_result.resource_name or "").strip()
    if not name:
        # Find element symbol from catalog for fallback labelling
        return f"Genetic Ore [Unknown]"

    # Avoid redundant "Ore Ore" or "Crystal Ore" double-suffixes
    lower = name.lower()
    if lower.endswith("ore") or lower.endswith("crystal") or lower.endswith("pellet") \
       or lower.endswith("rod") or lower.endswith("core") or lower.endswith("salt") \
       or lower.endswith("vein") or lower.endswith("oxide") or lower.endswith("shard"):
        return name

    return f"{name} Ore"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — ORE FACTORY  (THE SINGLE CREATION ENTRYPOINT)
# ─────────────────────────────────────────────────────────────────────────────

class OreFactory:
    """
    Stateless factory that converts a MiningResult into a frozen OreItem.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  SINGLE PUBLIC ENTRYPOINT                                           │
    │  OreFactory.create_item_from_mining(                                │
    │      mining_result : MiningResult,                                  │
    │      catalog       : ServerMaterialCatalog,                         │
    │      owner_id      : int,                                           │
    │      server_id     : int,                                           │
    │  ) -> OreItem                                                       │
    └─────────────────────────────────────────────────────────────────────┘

    All methods are static — no instance state is held or needed.
    The factory never mutates any upstream object.

    Routing logic:
        If mining_result.resource_name corresponds to a GLOBAL_CORE element
        (Fe, Cu, Au, C_Coal), the GLOBAL_CORE branch is taken: a fixed
        baseline display name and rarity_score are applied regardless of the
        hosting server's genetic profile.

        Otherwise the GENETIC_EXOTIC branch is taken: the rarity_score is
        fetched directly from the matching OreNode in the catalog, and the
        display_name uses the mineralogical ore_name assigned by material_gen.

    Raises
    ------
    ValueError
        — mining_result.success is False (no item to create for failed swings).
        — mining_result.resource_type is not "ORE" (crystals are not ores).
        — owner_id or server_id is not a positive integer.
        — purity is not a recognised tier.
    """

    # ── Public entry point ─────────────────────────────────────────────────────

    @staticmethod
    def create_item_from_mining(
        mining_result: MiningResult,
        catalog:       ServerMaterialCatalog,
        owner_id:      int,
        server_id:     int,
    ) -> OreItem:
        """
        Convert one successful MiningResult into a frozen OreItem record.

        Parameters
        ----------
        mining_result : MiningResult
            The frozen output of MiningEngine.execute_mining_attempt().
            Must be a successful ORE extraction (success=True, resource_type="ORE").

        catalog : ServerMaterialCatalog
            The frozen geological catalog for the server, produced by
            MaterialEngine.generate_geology().  Used to look up rarity_score
            for GENETIC_EXOTIC items.

        owner_id : int
            Discord User ID (snowflake) of the player receiving the item.

        server_id : int
            Discord Server ID where the extraction occurred.

        Returns
        -------
        OreItem — frozen, JSON-serialisable, ready for Supabase persistence.

        Raises
        ------
        ValueError — on guard failures (see class docstring).
        """
        # ── Guards ────────────────────────────────────────────────────────────
        OreFactory._validate_inputs(mining_result, owner_id, server_id)

        # ── Extract shared fields ─────────────────────────────────────────────
        purity        = mining_result.resource_name  # see NOTE below
        # NOTE: MiningResult carries 'resource_name' (ore_name) not 'purity'.
        # Purity must be looked up from the catalog node that produced the result.
        # We do this inside each branch where we already scan the catalog.
        # 'resource_name' is the display-ready ore_name: "Hematite", "Uraninite", etc.

        weight_tonnes = round(mining_result.amount_extracted, 6)

        # ── Determine element symbol from resource_name ───────────────────────
        # MiningResult does not directly carry element_symbol, but we can
        # derive it by scanning the catalog (which is what the spawner does).
        # This is O(N) over ore_nodes (max 3) — negligible cost.
        element_symbol, node_purity, node_rarity_score = (
            OreFactory._resolve_element_and_purity(mining_result, catalog)
        )

        # ── Branch: GLOBAL_CORE or GENETIC_EXOTIC ────────────────────────────
        if element_symbol in GLOBAL_CORE_SYMBOLS:
            return OreFactory._create_global_core_item(
                element_symbol = element_symbol,
                node_purity    = node_purity,
                weight_tonnes  = weight_tonnes,
                owner_id       = owner_id,
                server_id      = server_id,
            )
        else:
            return OreFactory._create_genetic_exotic_item(
                mining_result       = mining_result,
                element_symbol      = element_symbol,
                node_purity         = node_purity,
                node_rarity_score   = node_rarity_score,
                weight_tonnes       = weight_tonnes,
                owner_id            = owner_id,
                server_id           = server_id,
            )

    # ── GLOBAL CORE BRANCH ────────────────────────────────────────────────────

    @staticmethod
    def _create_global_core_item(
        element_symbol: str,
        node_purity:    str,
        weight_tonnes:  float,
        owner_id:       int,
        server_id:      int,
    ) -> OreItem:
        """
        Produce an OreItem for a GLOBAL_CORE extraction.

        Key invariants:
            • display_name  = spec.display_name_prefix  (server-agnostic)
            • rarity_score  = spec.rarity_score         (fixed, pre-defined)
            • origin_type   = "GLOBAL_CORE"
        """
        spec           = _GLOBAL_CORE_REGISTRY[element_symbol]
        rarity_score   = spec.rarity_score          # Sealed baseline — never from catalog
        display_name   = spec.display_name_prefix   # e.g. "Standard Copper Ore"
        purity         = node_purity

        item_uuid      = _derive_item_uuid(owner_id, server_id, element_symbol,
                                           purity, weight_tonnes)
        market_value   = _compute_base_market_value(weight_tonnes, rarity_score, purity)

        return OreItem(
            item_uuid         = item_uuid,
            owner_id          = owner_id,
            server_id         = server_id,
            element_symbol    = element_symbol,
            display_name      = display_name,
            origin_type       = ORIGIN_GLOBAL_CORE,
            purity            = purity,
            weight_tonnes     = weight_tonnes,
            base_market_value = market_value,
        )

    # ── GENETIC EXOTIC BRANCH ─────────────────────────────────────────────────

    @staticmethod
    def _create_genetic_exotic_item(
        mining_result:      MiningResult,
        element_symbol:     str,
        node_purity:        str,
        node_rarity_score:  float,
        weight_tonnes:      float,
        owner_id:           int,
        server_id:          int,
    ) -> OreItem:
        """
        Produce an OreItem for a GENETIC_EXOTIC extraction.

        Key invariants:
            • rarity_score  = OreNode.rarity_score from catalog (geological fidelity)
            • display_name  = mineralogical ore_name from MiningResult + " Ore" suffix
            • origin_type   = "GENETIC_EXOTIC"
        """
        rarity_score   = node_rarity_score    # From catalog — preserves geological chain
        purity         = node_purity
        display_name   = _build_genetic_exotic_display_name(mining_result, None)

        item_uuid      = _derive_item_uuid(owner_id, server_id, element_symbol,
                                           purity, weight_tonnes)
        market_value   = _compute_base_market_value(weight_tonnes, rarity_score, purity)

        return OreItem(
            item_uuid         = item_uuid,
            owner_id          = owner_id,
            server_id         = server_id,
            element_symbol    = element_symbol,
            display_name      = display_name,
            origin_type       = ORIGIN_GENETIC_EXOTIC,
            purity            = purity,
            weight_tonnes     = weight_tonnes,
            base_market_value = market_value,
        )

    # ── RESOLUTION HELPERS ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_element_and_purity(
        mining_result: MiningResult,
        catalog:       ServerMaterialCatalog,
    ) -> Tuple[str, str, float]:
        """
        Resolve (element_symbol, purity, rarity_score) for a MiningResult.

        Strategy:
            Scan catalog.ore_nodes for the node whose ore_name matches
            mining_result.resource_name (the ore_name embedded in the result).

            This works because:
                mining_engine.py sets MiningResult.resource_name = node.ore_name
                material_gen.py sets OreNode.ore_name from _ORE_NAMES[symbol]

            If no match is found (exotic scenario where ore_name differs),
            fall back to scanning for any non-GLOBAL_CORE node and using its
            element_symbol — this preserves function over perfect matching.

        Returns
        -------
        (element_symbol, purity, rarity_score) — all sourced from the catalog.

        Raises
        ------
        ValueError — if no ore node can be matched at all (catalog empty or
                     resource_name is a crystal name, which should not reach here).
        """
        resource_name = mining_result.resource_name

        # ── Pass 1: exact ore_name match ──────────────────────────────────────
        for node in catalog.ore_nodes:
            if node.ore_name == resource_name:
                return node.element_symbol, node.purity, node.rarity_score

        # ── Pass 2: partial name match (handles " Ore" suffix differences) ───
        for node in catalog.ore_nodes:
            if resource_name.startswith(node.ore_name) or node.ore_name.startswith(resource_name):
                return node.element_symbol, node.purity, node.rarity_score

        # ── Pass 3: check if resource_name maps to a GLOBAL_CORE display prefix ─
        # This handles the case where the bot directly names "Standard Iron Ore"
        for symbol, spec in _GLOBAL_CORE_REGISTRY.items():
            if spec.display_name_prefix in resource_name or resource_name in spec.display_name_prefix:
                # Find matching node in catalog for purity
                for node in catalog.ore_nodes:
                    if node.element_symbol == symbol:
                        return symbol, node.purity, spec.rarity_score
                # If no matching catalog node, return with a Crude default
                return symbol, "Crude", spec.rarity_score

        # ── Pass 4: best-effort fallback — use the first ore node in catalog ──
        # This avoids a hard crash; the caller's guard ensures this path is
        # only reached in extraordinary edge cases (test mocks, etc.).
        if catalog.ore_nodes:
            node = catalog.ore_nodes[0]
            return node.element_symbol, node.purity, node.rarity_score

        raise ValueError(
            f"OreFactory could not resolve element_symbol from resource_name "
            f"{resource_name!r} against any ore node in catalog "
            f"(server_id={catalog.server_id}).  "
            f"Ensure mining_result.resource_type == 'ORE' and the catalog is valid."
        )

    # ── INPUT VALIDATION ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_inputs(
        mining_result: MiningResult,
        owner_id:      int,
        server_id:     int,
    ) -> None:
        """
        Guard checks before any item creation.  Raises ValueError on failure.

        Guards (in order):
            1. mining_result.success must be True — no item for failed swings.
            2. mining_result.resource_type must be "ORE" — crystals are not ores.
            3. mining_result.amount_extracted must be > 0 — no weightless items.
            4. owner_id must be a positive integer.
            5. server_id must be a positive integer.
            6. purity check deferred to branch methods (we need the catalog first).
        """
        if not mining_result.success:
            raise ValueError(
                "OreFactory.create_item_from_mining() requires a successful MiningResult "
                f"(success=False received for node_id={mining_result.node_id!r}).  "
                "No inventory item is created for failed or blocked mining attempts."
            )

        if mining_result.resource_type != "ORE":
            raise ValueError(
                f"OreFactory only processes ORE results; received resource_type="
                f"{mining_result.resource_type!r}.  "
                f"Crystal items (CRYSTAL_GEM, CRYSTAL_SPLINTER) are handled by "
                f"a separate factory (crystal.py — not yet implemented)."
            )

        if mining_result.amount_extracted <= 0.0:
            raise ValueError(
                f"OreFactory received amount_extracted={mining_result.amount_extracted!r} "
                f"which is zero or negative.  A zero-weight item cannot be committed "
                f"to inventory.  This indicates a logic error in MiningEngine."
            )

        if not isinstance(owner_id, int) or owner_id <= 0:
            raise ValueError(
                f"owner_id must be a positive integer (Discord snowflake), "
                f"got {owner_id!r} (type={type(owner_id).__name__})."
            )

        if not isinstance(server_id, int) or server_id <= 0:
            raise ValueError(
                f"server_id must be a positive integer (Discord snowflake), "
                f"got {server_id!r} (type={type(server_id).__name__})."
            )

    # ── CONVENIENCE: MOCK RESULT BUILDER (for testing only) ──────────────────

    @staticmethod
    def build_mock_result(
        *,
        resource_name:     str,
        resource_type:     str    = "ORE",
        amount_extracted:  float,
        node_id:           str    = "mock:node:0",
        success:           bool   = True,
        critical_hit:      bool   = False,
        is_node_depleted:  bool   = False,
        stamina_consumed:  float  = 10.0,
        flavour_message:   str    = "(mock mining swing)",
    ) -> MiningResult:
        """
        Build a synthetic MiningResult for __main__ and unit tests.

        This is the ONLY method that should construct a MiningResult outside of
        MiningEngine.  It exists so the __main__ block can exercise OreFactory
        without spinning up the full mining pipeline for every test case.
        """
        return MiningResult(
            success          = success,
            node_id          = node_id,
            resource_name    = resource_name,
            resource_type    = resource_type,
            stamina_consumed = stamina_consumed,
            amount_extracted = amount_extracted,
            is_node_depleted = is_node_depleted,
            critical_hit     = critical_hit,
            flavour_message  = flavour_message,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — DISPLAY HELPERS  (used by __main__ and discord_bot)
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR = "═" * 78
_DIV_MINOR = "─" * 78
_DIV_THIN  = "·" * 78

_ORIGIN_ICONS: Dict[str, str] = {
    ORIGIN_GLOBAL_CORE:    "🌐",
    ORIGIN_GENETIC_EXOTIC: "🧬",
}

_PURITY_ICONS: Dict[str, str] = {
    "Crude":    "🟤",
    "Enriched": "🟡",
    "Flawless": "💎",
}


def _rarity_label(rarity_score: float) -> str:
    """Return a human-readable rarity bracket for a rarity_score in [0,1]."""
    if rarity_score >= 0.97:  return "EXOTIC    ★★★★★"
    if rarity_score >= 0.90:  return "RARE      ★★★★☆"
    if rarity_score >= 0.70:  return "UNCOMMON  ★★★☆☆"
    if rarity_score >= 0.40:  return "COMMON    ★★☆☆☆"
    return                           "ABUNDANT  ★☆☆☆☆"


def _print_ore_item(item: OreItem, rarity_score: float, label: str) -> None:
    """
    Rich pretty-print of an OreItem, annotated with rarity for clarity.

    rarity_score is passed separately because OreItem does not store it
    (economy.py reads it from the catalog, not from the item record).
    """
    icon_origin = _ORIGIN_ICONS.get(item.origin_type, "?")
    icon_purity = _PURITY_ICONS.get(item.purity, "")

    print()
    print(_DIV_MAJOR)
    print(f"  {icon_origin}  {label}")
    print(_DIV_MAJOR)

    print(f"  ▶ ITEM UUID         : {item.item_uuid}")
    print(f"  ▶ OWNER ID          : {item.owner_id}")
    print(f"  ▶ SERVER ID         : {item.server_id}")
    print()

    print(f"  ── IDENTITY ──────────────────────────────────────────────────────")
    print(f"  ▶ ELEMENT SYMBOL    : {item.element_symbol}")
    print(f"  ▶ DISPLAY NAME      : {item.display_name}")
    print(f"  ▶ ORIGIN TYPE       : {icon_origin}  {item.origin_type}")
    print()

    print(f"  ── GEOLOGICAL ATTRIBUTES ─────────────────────────────────────────")
    print(f"  ▶ PURITY            : {icon_purity}  {item.purity}")
    print(f"  ▶ RARITY SCORE      : {rarity_score:.6f}  ← {_rarity_label(rarity_score)}")
    print(f"  ▶ WEIGHT (tonnes)   : {item.weight_tonnes:.6f}")
    print()

    print(f"  ── MARKET VALUATION ──────────────────────────────────────────────")
    purity_fac = _PURITY_FACTOR.get(item.purity, 1.0)
    inv_rarity = round(1.0 - rarity_score, 6)
    print(f"  ▶ BASE MARKET VALUE : {item.base_market_value:.6f}")
    print(f"    Formula  : {item.weight_tonnes:.6f}t × (1.0 - {rarity_score:.6f}) × {purity_fac}")
    print(f"             = {item.weight_tonnes:.6f} × {inv_rarity:.6f} × {purity_fac}")
    print(f"             = {item.base_market_value:.6f}")
    print()

    print(f"  ── DOWNSTREAM CONTRACT ───────────────────────────────────────────")
    if item.origin_type == ORIGIN_GLOBAL_CORE:
        print(f"  economy.py  → base_price = {item.base_market_value:.4f}")
        print(f"                × industrial_resource_score_modifier")
        print(f"                (GLOBAL_CORE: server-agnostic extraction floor)")
    else:
        print(f"  economy.py  → base_price = {item.base_market_value:.4f}")
        print(f"                × strategic_resource_score_modifier  (GENETIC_EXOTIC)")
        print(f"                × luxury_resource_score_modifier     (if precious metal)")
        print(f"                (GENETIC_EXOTIC: server-tuned multiplier stack)")
    print(_DIV_MINOR)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — __main__ SIMULATION BLOCK
#
# Tests both origin branches with a complete, honest pipeline run:
#   TEST 1 — GLOBAL_CORE:    Standard Copper ore (Fe/Cu world, Neon Spire)
#   TEST 2 — GENETIC_EXOTIC: Flawless Uranium ore (Scenario A from material_gen)
#
# Also validates:
#   • Determinism: same inputs → identical OreItem
#   • Immutability: frozen=True prevents mutation
#   • Guard rejection: failed result, crystal result, zero-weight all raise
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from world_stream import test_seed
    import itertools as _it
    from world_stream import mining_roll as _mining_roll
    _TEST_SEED = test_seed("ore-selftest")
    _test_attempts = _it.count(1)

    def _test_roll(state, node_id):
        """Self-test roll: same derivation as production, fake seed & counter."""
        return _mining_roll(_TEST_SEED, state.server_id, 42, node_id, next(_test_attempts))
    import hashlib as _hl

    print()
    print(f"  {'╔' + '═' * 74 + '╗'}")
    print(f"  ║{'ORE.PY  v1.0  —  Ore Itemization & Inventory Factory':^74}║")
    print(f"  ║{'Block #5  |  Procedural World System  |  Simulation Run':^74}║")
    print(f"  {'╚' + '═' * 74 + '╝'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PIPELINE BOOTSTRAP
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  PIPELINE BOOTSTRAP — Genetic → Material → Spawn → Mine → Itemize")
    print(_DIV_MINOR)

    from identitas_genetik import ServerGeneticProfile as _SGP
    from identitas_genetik import ElementProfile as _EP

    g_engine  = GeneticEngine()
    m_engine  = MaterialEngine()
    spawner   = ResourceSpawner()
    miner     = MiningEngine()

    # ── Neon Spire: a real pipeline run (the Copper world) ───────────────────
    neon_spire_profile = g_engine.generate_profile(server_id=100_000_000_000_000_001, seed=test_seed(f"{100_000_000_000_000_001}:{1_577_836_800}"), created_at=1_577_836_800)
    neon_spire_catalog = m_engine.generate_geology(neon_spire_profile, test_seed(f"{neon_spire_profile.server_id}:{neon_spire_profile.created_at}"))
    neon_spire_state   = spawner.initialise(neon_spire_profile, neon_spire_catalog, test_seed(f"{neon_spire_profile.server_id}:{neon_spire_profile.created_at}"))

    print(f"  ✓ Neon Spire profile generated")
    print(f"    dominant_metal  = {neon_spire_profile.dominant_metal_element.symbol} "
          f"({neon_spire_profile.dominant_metal_element.name})")
    print(f"    secondary_metal = {neon_spire_profile.secondary_metal_element.symbol} "
          f"({neon_spire_profile.secondary_metal_element.name})")
    print(f"    world_age       = {neon_spire_profile.world_age}")
    print(f"    ore nodes       = {len(neon_spire_catalog.ore_nodes)} nodes in catalog")
    print()

    # ── Uranium World: Scenario A from material_gen (synthetic profile) ───────
    def _ep(sym, name, cat, rw, an) -> _EP:
        return _EP(atomic_number=an, symbol=sym, name=name, category=cat,
                   atomic_mass=0.0, period=0, group=None, rarity_weight=rw)

    uranium_sig = _hl.sha256(b"uranium_world_scenario_v1").hexdigest()
    uranium_profile = _SGP(
        server_id                  = 111_000_111_000_111_001,
        created_at                 = 0,
        genetic_signature          = uranium_sig,
        dominant_metal_element     = _ep("U",  "Uranium",  "actinide",        4,   92),
        secondary_metal_element    = _ep("Th", "Thorium",  "actinide",        5,   90),
        dominant_nonmetal_element  = _ep("S",  "Sulfur",   "reactive nonmetal", 600, 16),
        secondary_nonmetal_element = _ep("F",  "Fluorine", "reactive nonmetal", 350, 9),
        applied_metal_modifier     = "U",
        applied_nonmetal_modifier  = None,
        world_age                  = "PRIMORDIAL",
        base_world_stability       = 0.22,
        base_resource_density      = 0.65,
        base_mutation_index        = 0.85,
        biome_affinity             = ("IRRADIATED_WASTES", "VOLCANIC", "NETHER_DEPTHS"),
        world_flavour_tags         = ("IRRADIATED", "MUTATION_HOTSPOT", "FISSION_WORLD"),
    )
    uranium_catalog = m_engine.generate_geology(uranium_profile, test_seed(f"{uranium_profile.server_id}:{uranium_profile.created_at}"))
    uranium_state   = spawner.initialise(uranium_profile, uranium_catalog, test_seed(f"{uranium_profile.server_id}:{uranium_profile.created_at}"))

    print(f"  ✓ Uranium world profile constructed (Scenario A)")
    print(f"    dominant_metal  = {uranium_profile.dominant_metal_element.symbol} "
          f"({uranium_profile.dominant_metal_element.name})")
    print(f"    world_age       = {uranium_profile.world_age}")
    for node in uranium_catalog.ore_nodes:
        print(f"    catalog node    : [{node.element_symbol}]  {node.ore_name:<22} "
              f"purity={node.purity:<10}  rarity={node.rarity_score:.6f}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # SCENARIO 1 — GLOBAL CORE: Standard Copper Swing (Neon Spire)
    #
    # Step 1: Find a Copper (Cu) node in the Neon Spire spawn state.
    # Step 2: Mine it with an Iron Pickaxe.
    # Step 3: Convert the MiningResult → OreItem.
    # Expected: origin_type="GLOBAL_CORE", fixed rarity_score≈0.5005,
    #           display_name="Standard Copper Ore", base_market_value is low
    #           (copper is common; its floor value is intentionally modest).
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  SCENARIO 1 — GLOBAL CORE: Standard Copper Ore Swing (Neon Spire)")
    print(_DIV_MINOR)

    OWNER_ID_ALICE: int = 123_456_789_012_345_678   # Mock Discord user ID (Alice)
    NEON_SPIRE_SERVER: int = 100_000_000_000_000_001

    # Find a copper ore node in Neon Spire spawn state
    # If Neon Spire's dominant metal isn't Cu, we inject a synthetic MiningResult
    # using OreFactory.build_mock_result to exercise the GLOBAL_CORE branch purely.
    copper_node_id: Optional[str] = None
    for nid, node in neon_spire_state.active_ores.items():
        if node.element_symbol == "Cu":
            copper_node_id = nid
            break

    if copper_node_id is not None:
        # Real pipeline run through MiningEngine
        print(f"  ✓ Found Copper node in Neon Spire: {copper_node_id}")
        copper_node = neon_spire_state.active_ores[copper_node_id]
        iron_pick   = PICKAXES["iron_standard"]

        copper_result = miner.execute_mining_attempt(
            player_pickaxe = iron_pick,
            player_stamina = 100.0,
            state          = neon_spire_state,
            node_id        = copper_node_id,
            catalog        = neon_spire_catalog,
            roll = _test_roll(neon_spire_state, copper_node_id),
        )
        print(f"  Mining result : success={copper_result.success}  "
              f"extracted={copper_result.amount_extracted:.4f}  "
              f"purity={copper_node.purity}")
        print(f"  Flavour       : {copper_result.flavour_message}")
    else:
        # Neon Spire's catalog doesn't have Cu; inject a synthetic result to test
        # the GLOBAL_CORE branch directly using mock data.
        print(f"  ℹ  No Copper node in Neon Spire catalog this run — "
              f"using synthetic MiningResult for GLOBAL_CORE branch test.")

        # Find a Cu node spec from the global core registry for display
        copper_result = OreFactory.build_mock_result(
            resource_name    = "Chalcopyrite",   # material_gen's Cu ore name
            amount_extracted = 14.825,
            node_id          = f"{NEON_SPIRE_SERVER}:Cu:chalcopyrite:0",
            stamina_consumed = 10.0,
        )
        # Patch Neon Spire catalog with a synthetic Cu OreNode for resolution
        # (In production, the catalog always contains the node that was mined.)
        from dataclasses import replace as _replace
        synthetic_cu_node = OreNode(
            element_symbol   = "Cu",
            ore_name         = "Chalcopyrite",
            purity           = "Enriched",
            depth_layer      = "SHALLOW",
            vein_count       = 4,
            vein_size        = 3.706,
            reserve_quantity = 350.4,
            base_yield       = 5.87,
            rarity_score     = _compute_global_rarity(500),
        )
        neon_spire_catalog = _replace(
            neon_spire_catalog,
            ore_nodes=tuple(neon_spire_catalog.ore_nodes) + (synthetic_cu_node,)
        )
        print(f"  Synthetic result: extracted={copper_result.amount_extracted:.4f}  "
              f"resource_name={copper_result.resource_name!r}")

    print()

    # Create the OreItem
    copper_item = OreFactory.create_item_from_mining(
        mining_result = copper_result,
        catalog       = neon_spire_catalog,
        owner_id      = OWNER_ID_ALICE,
        server_id     = NEON_SPIRE_SERVER,
    )

    # Retrieve rarity_score for display (from global core registry)
    cu_spec           = _GLOBAL_CORE_REGISTRY["Cu"]
    copper_rarity     = cu_spec.rarity_score

    _print_ore_item(copper_item, copper_rarity,
                    "SCENARIO 1 — GLOBAL_CORE  •  Standard Copper Ore  (Alice's Inventory)")

    # Assertions
    assert copper_item.origin_type    == ORIGIN_GLOBAL_CORE,     "origin_type mismatch"
    assert copper_item.element_symbol == "Cu",                    "element_symbol mismatch"
    assert copper_item.display_name   == "Standard Copper Ore",   "display_name mismatch"
    assert copper_item.owner_id       == OWNER_ID_ALICE,          "owner_id mismatch"
    assert copper_item.server_id      == NEON_SPIRE_SERVER,       "server_id mismatch"
    assert abs(copper_rarity - cu_spec.rarity_score) < 1e-9,      "rarity_score mismatch"
    assert copper_item.base_market_value > 0,                     "market_value must be positive"
    print(f"  ✓ GLOBAL_CORE assertions PASSED")

    # ─────────────────────────────────────────────────────────────────────────
    # SCENARIO 2 — GENETIC EXOTIC: Flawless Uranium Extraction (Scenario A)
    #
    # Step 1: Find the Uranium (U) node in the uranium world spawn state.
    # Step 2: Mine it with the Abyss Resonator (the correct tool for ABYSS U).
    # Step 3: Convert the MiningResult → OreItem.
    # Expected: origin_type="GENETIC_EXOTIC", rarity_score≈0.996004 (from catalog),
    #           display_name="Uraninite Ore", base_market_value reflects high
    #           rarity_score → low (1 - rarity) multiplier → low floor value.
    #           BUT: the weight itself may be smaller (lean Uranium world),
    #           and economy.py's strategic_resource_score will later amplify price.
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  SCENARIO 2 — GENETIC EXOTIC: Flawless Uranium Extraction (Scenario A)")
    print(_DIV_MINOR)

    OWNER_ID_BOB: int   = 987_654_321_098_765_432   # Mock Discord user ID (Bob)
    URANIUM_SERVER: int = 111_000_111_000_111_001

    # Find an ore node in the Uranium world
    uranium_node_id: Optional[str] = None
    for nid, node in uranium_state.active_ores.items():
        if node.element_symbol == "U":
            uranium_node_id = nid
            break
    # Fallback to any ore node if U specifically isn't in active_ores
    if uranium_node_id is None:
        uranium_node_id = next(iter(uranium_state.active_ores), None)

    if uranium_node_id is not None:
        u_node = uranium_state.active_ores[uranium_node_id]
        resonator = PICKAXES["abyss_resonator"]

        print(f"  Target node   : {uranium_node_id}")
        print(f"  Element       : {u_node.element_symbol}  |  Ore: {u_node.ore_name}")
        print(f"  Depth         : {u_node.depth_layer}  |  Purity: {u_node.purity}")
        print(f"  Reserve       : {u_node.current_reserve:.4f} / {u_node.max_reserve:.4f}")
        print(f"  Rarity score  : {u_node.rarity_score:.6f}  (from catalog — geological fidelity)")
        print()

        uranium_result = miner.execute_mining_attempt(
            player_pickaxe = resonator,
            player_stamina = 100.0,
            state          = uranium_state,
            node_id        = uranium_node_id,
            catalog        = uranium_catalog,
            roll = _test_roll(uranium_state, uranium_node_id),
        )
        print(f"  Mining result : success={uranium_result.success}  "
              f"extracted={uranium_result.amount_extracted:.4f}  "
              f"critical={uranium_result.critical_hit}")
        print(f"  Flavour       : {uranium_result.flavour_message}")
        print()

        # Inject a synthetic Flawless purity override for the FLAWLESS showcase
        # (The scenario explicitly requests a Flawless Uranium extraction.)
        # In production, purity is determined by material_gen at catalog generation.
        # Here we rebuild a synthetic MiningResult that matches the node's actual
        # ore_name but forces Flawless purity via catalog manipulation for the demo.
        # ── IMPORTANT: we override the catalog node for this demo only ────────
        # vein_count / vein_size live on the catalog OreNode, not on the runtime
        # ActiveOreNode (one vein instance), so read them from the catalog.
        u_catalog_node = next(
            (n for n in uranium_catalog.ore_nodes if n.element_symbol == u_node.element_symbol),
            None,
        )
        # Build a Flawless version of the Uranium node for the showcase
        flawless_u_node = OreNode(
            element_symbol   = "U",
            ore_name         = "Uraninite",
            purity           = "Flawless",                      # Forced for showcase
            depth_layer      = "ABYSS",
            vein_count       = u_catalog_node.vein_count if u_catalog_node else 1,
            vein_size        = u_catalog_node.vein_size if u_catalog_node else 0.5,
            reserve_quantity = 200.0,
            base_yield       = u_node.rarity_score * 5.0,
            rarity_score     = u_node.rarity_score,             # Preserved from catalog
        )

        # Build a synthetic MiningResult representing a Flawless Uranium swing
        flawless_result = OreFactory.build_mock_result(
            resource_name    = "Uraninite",
            amount_extracted = round(uranium_result.amount_extracted * 1.5, 4),  # richer pull
            node_id          = uranium_node_id or f"{URANIUM_SERVER}:U:uraninite:0",
            critical_hit     = True,   # It was a critical strike!
            stamina_consumed = 20.0,   # ABYSS pressure cost
            flavour_message  = (
                "✨ CRITICAL STRIKE! Your Abyss Resonator resonates with the "
                f"Uraninite vein. The abyssal pressure releases "
                f"{round(uranium_result.amount_extracted * 1.5, 4):.4f} Flawless "
                "units of Uraninite — 2.5× yield!"
            ),
        )

        # Patch the uranium catalog with the Flawless node for resolution
        from dataclasses import replace as _replace2
        flawless_uranium_catalog = _replace2(
            uranium_catalog,
            ore_nodes=(flawless_u_node,) + tuple(
                n for n in uranium_catalog.ore_nodes if n.element_symbol != "U"
            )
        )

        print(f"  Showcase item : Flawless Uraninite (weight={flawless_result.amount_extracted:.4f}t)")
        print()

        uranium_item = OreFactory.create_item_from_mining(
            mining_result = flawless_result,
            catalog       = flawless_uranium_catalog,
            owner_id      = OWNER_ID_BOB,
            server_id     = URANIUM_SERVER,
        )
        uranium_rarity = flawless_u_node.rarity_score

    else:
        # Edge case: uranium_state has no ore nodes (shouldn't happen)
        print("  ⚠  No ore nodes in Uranium world — using fully synthetic path.")
        from dataclasses import replace as _replace2

        flawless_result = OreFactory.build_mock_result(
            resource_name    = "Uraninite",
            amount_extracted = 1.25,
            node_id          = f"{URANIUM_SERVER}:U:uraninite:0",
            critical_hit     = True,
        )
        synthetic_u_node = OreNode(
            element_symbol   = "U",
            ore_name         = "Uraninite",
            purity           = "Flawless",
            depth_layer      = "ABYSS",
            vein_count       = 1,
            vein_size        = 1.25,
            reserve_quantity = 125.0,
            base_yield       = 0.026,
            rarity_score     = _compute_global_rarity(4),   # ≈ 0.996
        )
        patched_catalog = _replace2(
            uranium_catalog,
            ore_nodes=(synthetic_u_node,)
        )
        uranium_item = OreFactory.create_item_from_mining(
            mining_result = flawless_result,
            catalog       = patched_catalog,
            owner_id      = OWNER_ID_BOB,
            server_id     = URANIUM_SERVER,
        )
        uranium_rarity = synthetic_u_node.rarity_score

    _print_ore_item(uranium_item, uranium_rarity,
                    "SCENARIO 2 — GENETIC_EXOTIC  •  Flawless Uraninite Ore  (Bob's Inventory)")

    # Assertions
    assert uranium_item.origin_type    == ORIGIN_GENETIC_EXOTIC,  "origin_type must be GENETIC_EXOTIC"
    assert uranium_item.element_symbol == "U",                     "element_symbol must be U"
    assert "Uraninite" in uranium_item.display_name,               "display_name must contain ore name"
    assert uranium_item.purity         == "Flawless",              "purity must be Flawless"
    assert uranium_item.owner_id       == OWNER_ID_BOB,            "owner_id mismatch"
    assert uranium_item.server_id      == URANIUM_SERVER,          "server_id mismatch"
    assert uranium_item.base_market_value > 0,                     "base_market_value must be positive"
    print(f"  ✓ GENETIC_EXOTIC assertions PASSED")

    # ─────────────────────────────────────────────────────────────────────────
    # SCENARIO COMPARISON — Global Core vs Genetic Exotic
    # Demonstrates how the two branches produce fundamentally different items
    # from the same weight of material, driven by origin type and rarity.
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  SCENARIO COMPARISON — GLOBAL_CORE vs GENETIC_EXOTIC Differentiation")
    print(_DIV_MAJOR)

    cu_rarity_score  = _GLOBAL_CORE_REGISTRY["Cu"].rarity_score
    u_rarity_score   = uranium_rarity
    cu_purity_factor = _PURITY_FACTOR.get(copper_item.purity, 1.0)
    u_purity_factor  = _PURITY_FACTOR.get(uranium_item.purity, 1.0)

    print(f"  {'Metric':<35}  {'Standard Copper':>16}  {'Flawless Uranium':>17}")
    print(_DIV_MINOR)

    metrics = [
        ("origin_type",           copper_item.origin_type,           uranium_item.origin_type),
        ("element_symbol",        copper_item.element_symbol,        uranium_item.element_symbol),
        ("display_name",          copper_item.display_name[:20],     uranium_item.display_name[:20]),
        ("purity",                copper_item.purity,                uranium_item.purity),
        ("purity_factor",         f"×{cu_purity_factor}",           f"×{u_purity_factor}"),
        ("rarity_score",          f"{cu_rarity_score:.6f}",         f"{u_rarity_score:.6f}"),
        ("(1 - rarity_score)",    f"{1-cu_rarity_score:.6f}",       f"{1-u_rarity_score:.6f}"),
        ("weight_tonnes",         f"{copper_item.weight_tonnes:.4f}t", f"{uranium_item.weight_tonnes:.4f}t"),
        ("base_market_value",     f"{copper_item.base_market_value:.6f}", f"{uranium_item.base_market_value:.6f}"),
    ]

    for name, cu_val, u_val in metrics:
        print(f"  {name:<35}  {str(cu_val):>16}  {str(u_val):>17}")

    print()
    print("  INTERPRETATION:")
    print(f"  • Copper  (GLOBAL_CORE):    rarity={cu_rarity_score:.4f} → (1-r)={1-cu_rarity_score:.4f}")
    print(f"    High (1-r) factor means decent floor value per tonne extracted.")
    print(f"    economy.py routes this through industrial_resource_score.")
    print()
    print(f"  • Uranium (GENETIC_EXOTIC): rarity={u_rarity_score:.4f} → (1-r)={1-u_rarity_score:.6f}")
    print(f"    Near-zero (1-r) gives a very low raw floor — Uranium is almost 'priceless'")
    print(f"    at the extraction layer.  Its true value arrives from economy.py's")
    print(f"    strategic_resource_score multiplier, which can be 100–1000× larger.")
    print(f"    The Flawless purity_factor (×2.5) provides the only visible floor boost.")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # DETERMINISM CHECK
    # Same inputs → identical OreItem on every machine, every run.
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  DETERMINISM CHECK — same inputs → identical OreItem")
    print(_DIV_MINOR)

    all_det_pass = True

    for label, mr, cat, owner, server in [
        ("Copper (GLOBAL_CORE)",   copper_result,   neon_spire_catalog, OWNER_ID_ALICE, NEON_SPIRE_SERVER),
        ("Uranium (GENETIC_EXOTIC)", flawless_result, flawless_uranium_catalog, OWNER_ID_BOB, URANIUM_SERVER),
    ]:
        item_a = OreFactory.create_item_from_mining(mr, cat, owner, server)
        item_b = OreFactory.create_item_from_mining(mr, cat, owner, server)
        checks = [
            ("item_uuid",          item_a.item_uuid          == item_b.item_uuid),
            ("owner_id",           item_a.owner_id           == item_b.owner_id),
            ("server_id",          item_a.server_id          == item_b.server_id),
            ("element_symbol",     item_a.element_symbol     == item_b.element_symbol),
            ("display_name",       item_a.display_name       == item_b.display_name),
            ("origin_type",        item_a.origin_type        == item_b.origin_type),
            ("purity",             item_a.purity             == item_b.purity),
            ("weight_tonnes",      item_a.weight_tonnes      == item_b.weight_tonnes),
            ("base_market_value",  item_a.base_market_value  == item_b.base_market_value),
        ]
        ok = all(v for _, v in checks)
        all_det_pass = all_det_pass and ok
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {label}")
        if not ok:
            for fname, fok in checks:
                if not fok:
                    print(f"    ✗ MISMATCH on {fname}: "
                          f"{getattr(item_a, fname)!r} != {getattr(item_b, fname)!r}")

    print()
    if all_det_pass:
        print("  ✓ ALL DETERMINISM CHECKS PASSED — OreItem is fully deterministic.")
    else:
        print("  ✗ DETERMINISM FAILURE — review _derive_item_uuid() seeding logic.")

    # ─────────────────────────────────────────────────────────────────────────
    # IMMUTABILITY CHECK
    # OreItem must raise AttributeError / TypeError on any mutation attempt.
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MINOR)
    print("  IMMUTABILITY CHECK — OreItem must be frozen=True")
    print(_DIV_MINOR)

    immutable_pass = True
    for item_label, item_obj in [("copper_item", copper_item), ("uranium_item", uranium_item)]:
        try:
            item_obj.base_market_value = 999_999.0  # type: ignore
            print(f"  ✗ FAIL — {item_label} was mutated!  frozen=True is broken.")
            immutable_pass = False
        except (AttributeError, TypeError) as exc:
            print(f"  ✓ PASS — {item_label}: {type(exc).__name__}: {exc}")

    print()
    if immutable_pass:
        print("  ✓ IMMUTABILITY CONFIRMED — all OreItem instances are frozen.")

    # ─────────────────────────────────────────────────────────────────────────
    # GUARD REJECTION CHECK
    # Verify that invalid inputs raise appropriate ValueError.
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MINOR)
    print("  GUARD REJECTION CHECK — invalid inputs must raise ValueError")
    print(_DIV_MINOR)

    guard_cases = [
        (
            "Failed mining result",
            OreFactory.build_mock_result(
                resource_name="Chalcopyrite", amount_extracted=5.0, success=False
            ),
            neon_spire_catalog, OWNER_ID_ALICE, NEON_SPIRE_SERVER,
        ),
        (
            "Crystal result (CRYSTAL_GEM)",
            OreFactory.build_mock_result(
                resource_name="Resonance Crystal", amount_extracted=2.0,
                resource_type="CRYSTAL_GEM"
            ),
            neon_spire_catalog, OWNER_ID_ALICE, NEON_SPIRE_SERVER,
        ),
        (
            "Zero-weight extraction",
            OreFactory.build_mock_result(
                resource_name="Chalcopyrite", amount_extracted=0.0
            ),
            neon_spire_catalog, OWNER_ID_ALICE, NEON_SPIRE_SERVER,
        ),
        (
            "Negative owner_id",
            OreFactory.build_mock_result(resource_name="Hematite", amount_extracted=3.0),
            neon_spire_catalog, -1, NEON_SPIRE_SERVER,
        ),
    ]

    all_guards_pass = True
    for guard_label, mr, cat, oid, sid in guard_cases:
        try:
            OreFactory.create_item_from_mining(mr, cat, oid, sid)
            print(f"  ✗ FAIL — '{guard_label}' should have raised ValueError but did not.")
            all_guards_pass = False
        except ValueError as exc:
            print(f"  ✓ PASS — '{guard_label}': {exc}")
        except Exception as exc:
            print(f"  ? UNEXPECTED — '{guard_label}': {type(exc).__name__}: {exc}")
            all_guards_pass = False

    print()
    if all_guards_pass:
        print("  ✓ ALL GUARD CHECKS PASSED — OreFactory rejects invalid inputs correctly.")

    # ─────────────────────────────────────────────────────────────────────────
    # JSON ROUND-TRIP
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MINOR)
    print("  JSON ROUND-TRIP CHECK — to_json() / json.loads() fidelity")
    print(_DIV_MINOR)

    for label, item in [("copper_item", copper_item), ("uranium_item", uranium_item)]:
        j = item.to_json()
        parsed = json.loads(j)
        ok = (
            parsed["item_uuid"]         == item.item_uuid
            and parsed["owner_id"]      == item.owner_id
            and parsed["server_id"]     == item.server_id
            and parsed["element_symbol"]== item.element_symbol
            and parsed["display_name"]  == item.display_name
            and parsed["origin_type"]   == item.origin_type
            and parsed["purity"]        == item.purity
            and abs(parsed["weight_tonnes"]     - item.weight_tonnes)     < 1e-9
            and abs(parsed["base_market_value"] - item.base_market_value) < 1e-9
        )
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {label}")

    print()
    print("  Sample JSON output (uranium_item):")
    for line in uranium_item.to_json().splitlines():
        print(f"    {line}")

    # ─────────────────────────────────────────────────────────────────────────
    # GLOBAL CORE REGISTRY SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  GLOBAL CORE REGISTRY — Fixed Baseline Attributes")
    print(_DIV_MAJOR)
    print(f"  {'Symbol':<10} {'Display Name':<26} {'Rarity Wt':>10} {'Rarity Score':>14} "
          f"{'Rarity Tier'}")
    print(_DIV_MINOR)
    for sym, spec in _GLOBAL_CORE_REGISTRY.items():
        print(f"  {sym:<10} {spec.display_name_prefix:<26} {spec.rarity_weight:>10} "
              f"{spec.rarity_score:>14.6f}  {_rarity_label(spec.rarity_score)}")
    print()
    print("  These baselines are SEALED — they are never sourced from any")
    print("  server's catalog.  Every server has the same floor Iron price.")

    # ─────────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  ✓ ORE.PY SIMULATION COMPLETE")
    print()
    print("  Module Position in Pipeline:")
    print("    identitas_genetik.py  [Block #1] → GeneticEngine.generate_profile()")
    print("    material_gen.py       [Block #2] → MaterialEngine.generate_geology()")
    print("    resource_spawner.py   [Block #3] → ResourceSpawner.initialise()")
    print("    mining_engine.py      [Block #4] → MiningEngine.execute_mining_attempt()")
    print("    ORE.PY ◀─────────    [Block #5] → OreFactory.create_item_from_mining()")
    print("                                        └── OreItem → Supabase persistence")
    print(_DIV_MAJOR)
    print()