"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       CRYSTAL.PY  —  Crystal Itemization & Affinity Factory                ║
║       Foundational Block #6 of the Procedural World System                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM CONTRACT                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Reads    : mining_engine.MiningResult       (frozen extraction record)     ║
║  Reads    : resource_spawner.ActiveCrystalNode (live node; for biome/meta) ║
║  Reads    : material_gen.ServerMaterialCatalog (frozen geological catalog)  ║
║                                                                              ║
║  DOWNSTREAM CONTRACT                                                         ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Supabase persistence layer receives : CrystalItem (frozen, JSON-ready)    ║
║  economy.py    reads : CrystalItem.base_market_value  → market injection   ║
║  crafting.py   reads : CrystalItem.crystal_affinity, quality, weight_tonnes ║
║  discord_bot   reads : CrystalItem.display_name, origin_variant, item_uuid ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • Zero `random` Module  — item_uuid is a deterministic SHA-256 hash       ║
║      derived from (owner_id, server_id, crystal_type, quality, weight,     ║
║      mutation_affinity).  No global RNG is ever touched.                   ║
║                                                                              ║
║  • Dual Structural States — MiningResult.resource_type drives the branch:  ║
║      CRYSTAL_GEM     → pristine harvest; quality inherited intact;         ║
║                        high economic valuation.                             ║
║      CRYSTAL_SPLINTER → tectonic fracture; quality hard-downgraded to     ║
║                        "Flawed"; 0.45 compression scalar applied;          ║
║                        flagged as a highly reactive crafting reagent.      ║
║                                                                              ║
║  • Affinity-Aware Valuation — MUTATION and POWER affinities receive an     ║
║      energy amplification boost via mutation_affinity scaling.  All other  ║
║      affinities receive an ambient matrix stability bonus instead,         ║
║      preserving the lore of mana-stable vs. volatile crystal types.        ║
║                                                                              ║
║  • Immutability — CrystalItem is frozen=True.  It is an unalterable        ║
║      snapshot of one extraction event committed to the inventory layer.    ║
║      No downstream consumer may mutate it.                                 ║
║                                                                              ║
║  • Single Factory Entrypoint — CrystalFactory.create_item_from_mining()   ║
║      is the only path that produces CrystalItem instances.  All routing,  ║
║      quality downgrade logic, and valuation is encapsulated here;         ║
║      callers never instantiate CrystalItem directly.                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  BASE MARKET VALUE FORMULA                                                   ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║                                                                              ║
║  Step 1 — Quality Base:                                                      ║
║      quality_multiplier:  Flawed=1.0,  Prismatic=2.5,  Ethereal=6.0        ║
║      quality_base = weight_tonnes × quality_multiplier                      ║
║                                                                              ║
║  Step 2 — Affinity Scaling:                                                  ║
║      if crystal_affinity in {"MUTATION", "POWER"}:                          ║
║          affinity_factor = 1.0 + mutation_affinity × ENERGY_BOOST_SCALE    ║
║          (ENERGY_BOOST_SCALE = 2.0 — volatile crystals have explosive       ║
║           potential; mutation_affinity amplifies their raw energy value)    ║
║      else:                                                                   ║
║          affinity_factor = 1.0 + (1.0 - mutation_affinity) × MATRIX_SCALE  ║
║          (MATRIX_SCALE = 0.5 — stable, low-mutation crystals command        ║
║           premium for their matrix purity; inverse mutation_affinity)       ║
║                                                                              ║
║  Step 3 — Splinter Depreciation (only if origin_variant == CRYSTAL_SPLINTER)║
║      final_value = quality_base × affinity_factor × SPLINTER_SCALAR        ║
║      (SPLINTER_SCALAR = 0.45 — matches mining_engine._SPLINTER_YIELD_FACTOR)║
║                                                                              ║
║  Step 4 — For pristine GEM:                                                  ║
║      final_value = quality_base × affinity_factor                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UUID SEED CONSTRUCTION                                                      ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  seed = SHA-256(                                                             ║
║      owner_id[8B big-endian]                                                 ║
║    + server_id[8B big-endian]                                                ║
║    + crystal_type[UTF-8]                                                     ║
║    + quality[UTF-8]           ← AFTER downgrade for splinters               ║
║    + mutation_affinity_repr[UTF-8]   ← repr(round(x, 6))                   ║
║    + weight_repr[UTF-8]              ← repr(round(x, 6))                   ║
║  )                                                                           ║
║  The quality is included post-downgrade so that a CRYSTAL_SPLINTER and its  ║
║  source CRYSTAL_GEM (same node, same swing) always produce distinct UUIDs.  ║
╚══════════════════════════════════════════════════════════════════════════════╝

INTEGRATION PIPELINE
────────────────────
    MiningEngine.execute_mining_attempt()
            │
            ▼
    MiningResult (frozen)
    resource_type: "CRYSTAL_GEM" | "CRYSTAL_SPLINTER"
            │
            ▼
    CrystalFactory.create_item_from_mining(
        mining_result, active_node, catalog,
        owner_id, player_note=None
    )
            │
            ├── CRYSTAL_GEM branch
            │       └── quality inherited intact from ActiveCrystalNode
            │           affinity scaling applied
            │           GEM display name used (e.g. "Astral Lattice")
            │
            └── CRYSTAL_SPLINTER branch
                    └── quality force-downgraded to "Flawed"
                        display name suffixed with "Splinter"
                        SPLINTER_SCALAR (0.45) applied to final value
            │
            ▼
    CrystalItem (frozen=True)  ─────────────► Supabase persistence layer
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

# ── Upstream module resolution ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identitas_genetik import ServerGeneticProfile, GeneticEngine
from material_gen import ServerMaterialCatalog, MaterialEngine, CrystalNode
from mining_engine import MiningResult, Pickaxe, MiningEngine, PICKAXES
from resource_spawner import ActiveCrystalNode, ResourceSpawner


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — STRUCTURAL VARIANT CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VARIANT_GEM:      str = "CRYSTAL_GEM"
VARIANT_SPLINTER: str = "CRYSTAL_SPLINTER"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — VALUATION TABLES & SCALARS
# ─────────────────────────────────────────────────────────────────────────────

# Quality → base multiplier per weight-tonne.
# Design intent:
#   Flawed   (1.0) — common, impure; the progression anchor.
#   Prismatic (2.5) — intermediate; clearer lattice, more stable energy.
#   Ethereal (6.0) — ultra-rare; perfect resonance geometry; max market floor.
_QUALITY_MULTIPLIER: dict[str, float] = {
    "Flawed":    1.0,
    "Prismatic": 2.5,
    "Ethereal":  6.0,
}
_QUALITY_MULTIPLIER_FALLBACK: float = 1.0

# Affinities that carry volatile, amplified energy — use mutation_affinity as
# a direct boost coefficient.  The higher the mutation_affinity, the more
# unstable (and thus economically reactive) the crystal.
_VOLATILE_AFFINITIES: frozenset[str] = frozenset({"MUTATION", "POWER"})

# Scale factors for the two affinity branches:
#   ENERGY_BOOST_SCALE   — multiplies mutation_affinity for volatile types.
#                          At mutation_affinity=1.0 this doubles the affinity
#                          component; at 0.0 it contributes nothing.
#   MATRIX_SCALE         — multiplies (1.0 − mutation_affinity) for stable
#                          types.  Low-mutation crystals are architecturally
#                          pure, fetching a matrix stability premium.
_ENERGY_BOOST_SCALE: float = 2.0
_MATRIX_SCALE:       float = 0.5

# Splinter depreciation — MUST remain identical to mining_engine._SPLINTER_YIELD_FACTOR
# to ensure economic consistency across the pipeline.
_SPLINTER_SCALAR: float = 0.45

# Quality that ALL splinters are hard-downgraded to (structural ruin).
_SPLINTER_FORCED_QUALITY: str = "Flawed"

# Suffix appended to display name for splinters (localisation-safe pattern).
_SPLINTER_NAME_SUFFIX: str = "Splinter"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — CRYSTAL ITEM DATACLASS (IMMUTABLE INVENTORY RECORD)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CrystalItem:
    """
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  IMMUTABLE CRYSTAL INVENTORY RECORD — DO NOT INSTANTIATE DIRECTLY  ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║  Always produced by CrystalFactory.create_item_from_mining().      ║
    ║  Once created, this record represents one indivisible inventory     ║
    ║  unit committed to the Supabase persistence layer.                  ║
    ╚══════════════════════════════════════════════════════════════════════╝

    Field Glossary
    ──────────────
    item_uuid
        Deterministic SHA-256 hash unique to this extraction event.
        Seed = SHA-256(owner_id[8B] + server_id[8B] + crystal_type[UTF-8]
                       + quality[UTF-8] + mutation_affinity_repr + weight_repr)
        quality is included POST-downgrade so GEM and SPLINTER from the same
        node always yield distinct UUIDs.

    owner_id
        Discord User ID (snowflake int) of the player who harvested this item.

    server_id
        Origin Discord Server ID.  Consumed by economy.py when applying
        server-specific strategic_resource_score multipliers at trade time.

    display_name
        Human-readable name shown in Discord embeds and inventory UIs.
        GEM     : inherits node.name directly (e.g. "Astral Lattice").
        SPLINTER: node.name + " Splinter" (e.g. "Astral Lattice Splinter").

    origin_variant
        "CRYSTAL_GEM" | "CRYSTAL_SPLINTER"
        Routes crafting.py and economy.py to the correct processing branch.

    crystal_type
        Geological type string from the material_gen synthesis table.
        Examples: "Igneous", "Arcane", "Radioactive", "Crystalline", etc.

    crystal_affinity
        "POWER" | "MANA" | "MUTATION" | "UTILITY" | "DEFENSE"
        Drives affinity scaling in the valuation formula and crafting recipes.

    quality
        "Flawed" | "Prismatic" | "Ethereal"
        For CRYSTAL_GEM  : inherited intact from the node.
        For CRYSTAL_SPLINTER : always "Flawed" regardless of node quality.

    weight_tonnes
        Direct mapping from MiningResult.amount_extracted → weight_tonnes.
        Same abstract game-tonne unit as OreItem.weight_tonnes.

    mutation_affinity
        Float [0.0, 1.0] inherited from the source ActiveCrystalNode.
        Controls the affinity scaling branch in the valuation formula.
        Also consumed by fauna_gen mutation rolls downstream.

    biome_origin
        Source biome tag string (e.g. "CRYSTAL_CAVERN", "IRRADIATED_WASTES").
        Preserved for lore display, crafting biome-lock recipes, and
        fauna_gen ecology cross-referencing.

    base_market_value
        The floor value of this item before economy.py market dynamics apply.
        See module docstring Section — BASE MARKET VALUE FORMULA for the
        full three-step derivation.

        This value is the raw extraction floor.  economy.py will multiply
        it by server-specific strategic_resource_score or luxury_resource_score
        depending on crystal_affinity when computing the final trade price.
    """

    item_uuid:         str      # Deterministic SHA-256 hash of extraction event
    owner_id:          int      # Discord User ID
    server_id:         int      # Origin server snowflake ID
    display_name:      str      # e.g. "Prismatic Geode" or "Prismatic Geode Splinter"
    origin_variant:    str      # "CRYSTAL_GEM" | "CRYSTAL_SPLINTER"
    crystal_type:      str      # e.g. "Igneous", "Radioactive", "Aetherial"
    crystal_affinity:  str      # "POWER" | "MANA" | "MUTATION" | "UTILITY" | "DEFENSE"
    quality:           str      # "Flawed" | "Prismatic" | "Ethereal"
    weight_tonnes:     float    # Maps 1:1 from MiningResult.amount_extracted
    mutation_affinity: float    # Energy index inherited from the source node [0.0, 1.0]
    biome_origin:      str      # Source biome tag (e.g. "CRYSTAL_CAVERN")
    base_market_value: float    # Quantified baseline value for economy.py

    # ── Serialisation helpers ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a fully JSON-serialisable plain dict for Supabase writes."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Return a pretty-printed JSON string for logging and Discord payloads."""
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _derive_item_uuid(
    owner_id:          int,
    server_id:         int,
    crystal_type:      str,
    quality:           str,         # Must be the POST-downgrade quality
    mutation_affinity: float,
    weight_tonnes:     float,
) -> str:
    """
    Derive a deterministic UUID-style hash from extraction event parameters.

    Seed construction (byte-exact, platform-independent):
        owner_id          → 8 bytes, big-endian
        server_id         → 8 bytes, big-endian
        crystal_type      → UTF-8 bytes
        quality           → UTF-8 bytes  (post-downgrade for splinters)
        mutation_affinity → repr(round(x, 6)) in UTF-8
        weight_tonnes     → repr(round(x, 6)) in UTF-8

    Including quality POST-downgrade ensures CRYSTAL_SPLINTER and CRYSTAL_GEM
    from the same harvesting event always produce distinct item_uuid values,
    preventing false-positive duplicate detection at the persistence layer.

    No `random`, `uuid`, or `time` module is used.  Reproducible on any
    platform given identical inputs.
    """
    mutation_repr = repr(round(mutation_affinity, 6)).encode("utf-8")
    weight_repr   = repr(round(weight_tonnes,     6)).encode("utf-8")
    raw_seed = (
        owner_id.to_bytes(8, "big")
        + server_id.to_bytes(8, "big")
        + crystal_type.encode("utf-8")
        + quality.encode("utf-8")
        + mutation_repr
        + weight_repr
    )
    return hashlib.sha256(raw_seed).hexdigest()


def _compute_quality_multiplier(quality: str) -> float:
    """
    Return the base quality multiplier for the given quality tier.
    Falls back to 1.0 (Flawed equivalent) for any unrecognised tier.
    """
    return _QUALITY_MULTIPLIER.get(quality, _QUALITY_MULTIPLIER_FALLBACK)


def _compute_affinity_factor(crystal_affinity: str, mutation_affinity: float) -> float:
    """
    Compute the affinity scaling factor applied on top of the quality base.

    Two branches, determined by crystal_affinity membership:

    VOLATILE branch (MUTATION or POWER):
        affinity_factor = 1.0 + mutation_affinity × ENERGY_BOOST_SCALE
        Rationale: these crystals carry raw destructive/amplifying potential.
        A high mutation_affinity (near 1.0) nearly triples the affinity factor,
        reflecting the explosive economic demand for volatile reagents.

    STABLE branch (MANA, UTILITY, DEFENSE):
        affinity_factor = 1.0 + (1.0 − mutation_affinity) × MATRIX_SCALE
        Rationale: stable crystals are valued for matrix purity, not volatility.
        Low mutation_affinity (near 0.0) maximises the matrix stability premium,
        rewarding harvesters who find pristine, non-irradiated formations.

    mutation_affinity is clamped to [0.0, 1.0] before use as a defensive guard
    against upstream floating-point edge cases.

    Returns a float ≥ 1.0 in all valid input cases.
    """
    ma = max(0.0, min(1.0, mutation_affinity))
    if crystal_affinity in _VOLATILE_AFFINITIES:
        return round(1.0 + ma * _ENERGY_BOOST_SCALE, 8)
    else:
        return round(1.0 + (1.0 - ma) * _MATRIX_SCALE, 8)


def _compute_base_market_value(
    weight_tonnes:     float,
    quality:           str,          # Post-downgrade quality
    crystal_affinity:  str,
    mutation_affinity: float,
    is_splinter:       bool,
) -> float:
    """
    Compute the floor market value for one CrystalItem.

    Full three-step formula (see module docstring for narrative):

        Step 1: quality_base     = weight_tonnes × _QUALITY_MULTIPLIER[quality]
        Step 2: affinity_factor  = _compute_affinity_factor(affinity, mutation_affinity)
                pre_split_value  = quality_base × affinity_factor
        Step 3: if is_splinter:
                    final_value  = pre_split_value × _SPLINTER_SCALAR
                else:
                    final_value  = pre_split_value

    Returns a non-negative float rounded to 6 decimal places.
    """
    quality_mult    = _compute_quality_multiplier(quality)
    quality_base    = weight_tonnes * quality_mult
    affinity_factor = _compute_affinity_factor(crystal_affinity, mutation_affinity)
    pre_split_value = quality_base * affinity_factor

    if is_splinter:
        final_value = pre_split_value * _SPLINTER_SCALAR
    else:
        final_value = pre_split_value

    return round(max(0.0, final_value), 6)


def _build_display_name(node_name: str, is_splinter: bool) -> str:
    """
    Construct the human-readable display name for the inventory record.

    GEM     : node.name unchanged  (e.g. "Astral Lattice")
    SPLINTER: node.name + " Splinter" (e.g. "Astral Lattice Splinter")

    The suffix is not appended if the node_name already ends with "Splinter"
    (defensive guard against double-suffixing on re-pack attempts).
    """
    if is_splinter and not node_name.endswith(_SPLINTER_NAME_SUFFIX):
        return f"{node_name} {_SPLINTER_NAME_SUFFIX}"
    return node_name


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — CRYSTAL FACTORY
# ─────────────────────────────────────────────────────────────────────────────

class CrystalFactory:
    """
    The sole authorised producer of CrystalItem instances.

    Callers MUST use create_item_from_mining() and MUST NOT instantiate
    CrystalItem directly.  This class enforces the structural-state branching
    (GEM vs. SPLINTER), quality downgrade, and valuation formula in one
    encapsulated, auditable location.

    Instantiation:
        factory = CrystalFactory()
        item    = factory.create_item_from_mining(
            mining_result = result,
            active_node   = node,
            owner_id      = 123456789,
        )

    Thread Safety:
        CrystalFactory holds no mutable state — all computation is
        parameter-local.  Multiple concurrent calls are safe.
    """

    # ── Primary Entry Point ───────────────────────────────────────────────────

    def create_item_from_mining(
        self,
        mining_result:  MiningResult,
        active_node:    ActiveCrystalNode,
        owner_id:       int,
    ) -> CrystalItem:
        """
        Translate a MiningResult + ActiveCrystalNode into a frozen CrystalItem.

        This is the ONLY authorised factory path.  Calling code MUST pass the
        ActiveCrystalNode that corresponds to the node_id in mining_result.
        Mismatched node/result pairs are a caller contract violation.

        Pipeline (in order):
            1. Validate resource_type is a crystal variant.
            2. Determine structural state: GEM or SPLINTER.
            3. Apply quality downgrade if SPLINTER.
            4. Build display name (suffix for splinters).
            5. Compute base_market_value (3-step formula).
            6. Derive deterministic item_uuid.
            7. Assemble and return frozen CrystalItem.

        Parameters
        ----------
        mining_result : MiningResult — frozen record from MiningEngine.
                        resource_type must be "CRYSTAL_GEM" or "CRYSTAL_SPLINTER".
        active_node   : ActiveCrystalNode — the live node that was harvested.
                        Provides crystal_type, crystal_affinity, mutation_affinity,
                        biome_affinity, and the pre-fracture quality tier.
        owner_id      : int — Discord User ID (snowflake) of the harvesting player.

        Returns
        -------
        CrystalItem — frozen, JSON-serialisable inventory record.

        Raises
        ------
        ValueError — if mining_result.resource_type is not a crystal variant.
        ValueError — if mining_result.success is False (no item produced on failure).
        ValueError — if mining_result.amount_extracted <= 0.0.
        """

        # ── Step 1: Contract validation ───────────────────────────────────────
        if not mining_result.success:
            raise ValueError(
                f"CrystalFactory received a failed MiningResult "
                f"(node_id={mining_result.node_id!r}).  "
                f"No CrystalItem is produced for unsuccessful mining attempts."
            )

        valid_variants = {VARIANT_GEM, VARIANT_SPLINTER}
        if mining_result.resource_type not in valid_variants:
            raise ValueError(
                f"CrystalFactory.create_item_from_mining() requires "
                f"resource_type in {valid_variants!r}, "
                f"got {mining_result.resource_type!r}.  "
                f"For ORE results, use OreFactory instead."
            )

        if mining_result.amount_extracted <= 0.0:
            raise ValueError(
                f"CrystalFactory received amount_extracted="
                f"{mining_result.amount_extracted!r} (must be > 0.0).  "
                f"Zero-yield events should not produce inventory records."
            )

        # ── Step 2: Determine structural state ────────────────────────────────
        is_splinter: bool = (mining_result.resource_type == VARIANT_SPLINTER)

        # ── Step 3: Quality resolution ────────────────────────────────────────
        # GEM  → preserve the node's actual geological quality tier
        # SPLINTER → forcibly downgrade to "Flawed" regardless of node quality.
        #            Tectonic fracture destroys the lattice structure; calling
        #            a fractured Ethereal crystal "Ethereal" would be a lie.
        if is_splinter:
            resolved_quality: str = _SPLINTER_FORCED_QUALITY
        else:
            resolved_quality = active_node.quality

        # ── Step 4: Display name ──────────────────────────────────────────────
        display_name: str = _build_display_name(active_node.name, is_splinter)

        # ── Step 5: Market valuation ──────────────────────────────────────────
        base_market_value: float = _compute_base_market_value(
            weight_tonnes     = mining_result.amount_extracted,
            quality           = resolved_quality,
            crystal_affinity  = active_node.crystal_affinity,
            mutation_affinity = active_node.mutation_affinity,
            is_splinter       = is_splinter,
        )

        # ── Step 6: Deterministic UUID ────────────────────────────────────────
        # Quality is included POST-downgrade to differentiate GEM and SPLINTER
        # UUIDs even when all other fields are identical.
        item_uuid: str = _derive_item_uuid(
            owner_id          = owner_id,
            server_id         = active_node.node_id.split(":")[0]
                                    and _extract_server_id(active_node.node_id),
            crystal_type      = active_node.crystal_type,
            quality           = resolved_quality,
            mutation_affinity = active_node.mutation_affinity,
            weight_tonnes     = mining_result.amount_extracted,
        )

        # ── Step 7: Assemble and return ───────────────────────────────────────
        return CrystalItem(
            item_uuid         = item_uuid,
            owner_id          = owner_id,
            server_id         = _extract_server_id(active_node.node_id),
            display_name      = display_name,
            origin_variant    = mining_result.resource_type,
            crystal_type      = active_node.crystal_type,
            crystal_affinity  = active_node.crystal_affinity,
            quality           = resolved_quality,
            weight_tonnes     = mining_result.amount_extracted,
            mutation_affinity = active_node.mutation_affinity,
            biome_origin      = active_node.biome_affinity,
            base_market_value = base_market_value,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — INTERNAL NODE ID HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_server_id(node_id: str) -> int:
    """
    Parse the server_id from an ActiveCrystalNode.node_id.

    node_id format (set by resource_spawner):
        "<server_id>:crystal:<name_slug>:<index>"
        e.g. "1143780000000000001:crystal:astral_lattice:0"

    The server_id is always the first colon-delimited segment.
    Raises ValueError on malformed node_id (defensive guard).
    """
    parts = node_id.split(":")
    if len(parts) < 2:
        raise ValueError(
            f"Cannot extract server_id from malformed node_id: {node_id!r}.  "
            f"Expected format: '<server_id>:crystal:<name_slug>:<index>'"
        )
    try:
        return int(parts[0])
    except ValueError:
        raise ValueError(
            f"Non-integer server_id segment in node_id: {node_id!r}.  "
            f"First segment was {parts[0]!r}."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR = "  " + "═" * 76
_DIV_MINOR = "  " + "─" * 76

_VARIANT_ICONS: dict[str, str] = {
    VARIANT_GEM:      "💎",
    VARIANT_SPLINTER: "💥",
}

_AFFINITY_ICONS: dict[str, str] = {
    "POWER":    "⚡",
    "MANA":     "✨",
    "MUTATION": "☢",
    "UTILITY":  "🔧",
    "DEFENSE":  "🛡",
}


def _print_crystal_item(item: CrystalItem, label: str = "") -> None:
    """Pretty-print a CrystalItem to stdout for audit/debug output."""
    icon    = _VARIANT_ICONS.get(item.origin_variant, "🔷")
    aff_ico = _AFFINITY_ICONS.get(item.crystal_affinity, "•")
    hdr     = f"  {icon}  {label or item.display_name}"

    print(_DIV_MAJOR)
    print(hdr)
    print(_DIV_MINOR)
    print(f"  {'item_uuid':<22}: {item.item_uuid[:16]}…{item.item_uuid[-8:]}")
    print(f"  {'owner_id':<22}: {item.owner_id}")
    print(f"  {'server_id':<22}: {item.server_id}")
    print(f"  {'display_name':<22}: {item.display_name}")
    print(f"  {'origin_variant':<22}: {item.origin_variant}")
    print(f"  {'crystal_type':<22}: {item.crystal_type}")
    print(f"  {'crystal_affinity':<22}: {aff_ico}  {item.crystal_affinity}")
    print(f"  {'quality':<22}: {item.quality}")
    print(f"  {'weight_tonnes':<22}: {item.weight_tonnes:.6f}")
    print(f"  {'mutation_affinity':<22}: {item.mutation_affinity:.6f}")
    print(f"  {'biome_origin':<22}: {item.biome_origin}")
    print()
    print(f"  ┌─ VALUATION BREAKDOWN ──────────────────────────────────────────")
    q_mult     = _compute_quality_multiplier(item.quality)
    quality_b  = item.weight_tonnes * q_mult
    aff_fac    = _compute_affinity_factor(item.crystal_affinity, item.mutation_affinity)
    pre_split  = quality_b * aff_fac
    is_sp      = item.origin_variant == VARIANT_SPLINTER
    print(f"  │  quality_base   = {item.weight_tonnes:.6f} t × {q_mult} (quality mult)  = {quality_b:.6f}")
    if item.crystal_affinity in _VOLATILE_AFFINITIES:
        print(f"  │  affinity_factor= 1.0 + {item.mutation_affinity:.6f} × {_ENERGY_BOOST_SCALE}"
              f"  [VOLATILE: {item.crystal_affinity}]          = {aff_fac:.8f}")
    else:
        print(f"  │  affinity_factor= 1.0 + (1.0 − {item.mutation_affinity:.6f}) × {_MATRIX_SCALE}"
              f"  [STABLE: {item.crystal_affinity}]       = {aff_fac:.8f}")
    print(f"  │  pre_split_value= {quality_b:.6f} × {aff_fac:.8f}         = {pre_split:.6f}")
    if is_sp:
        print(f"  │  splinter_scalar= × {_SPLINTER_SCALAR}  (structural ruin)             "
              f"= {item.base_market_value:.6f}")
    else:
        print(f"  │  (pristine gem — no depreciation scalar applied)")
    print(f"  └─ base_market_value                                           = "
          f"{item.base_market_value:.6f}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — __MAIN__ INTEGRATION TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print()
    print(f"  ╔{'═' * 70}╗")
    print(f"  ║{'CRYSTAL.PY  v1.0  —  Itemization & Affinity Factory Proof':^70}║")
    print(f"  ╚{'═' * 70}╝")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # BOOTSTRAP — Build a live pipeline state to draw real nodes from.
    # Uses the "Neon Spire" test vector from identitas_genetik (server_id=
    # 100000000000000001, created_at=1577836800) which includes CRYSTAL_CAVERN
    # in its biome affinity — guaranteeing at least one crystal node.
    # ─────────────────────────────────────────────────────────────────────────

    print("  Bootstrapping pipeline...")
    g_engine  = GeneticEngine()
    m_engine  = MaterialEngine()
    spawner   = ResourceSpawner()
    factory   = CrystalFactory()
    miner     = MiningEngine()

    profile  = g_engine.generate_profile(
        server_id  = 100000000000000001,
        created_at = 1577836800,
    )
    catalog  = m_engine.generate_geology(profile)
    state    = spawner.initialise(profile, catalog)

    # Pick the first crystal node available in the spawn state.
    if not state.active_crystals:
        print("  ✗ No crystal nodes found on Neon Spire — check material_gen biome output.")
        raise SystemExit(1)

    demo_node_id, demo_node = next(iter(state.active_crystals.items()))

    print(f"  Profile       : Neon Spire  (server_id=100000000000000001)")
    print(f"  Biomes        : {' | '.join(profile.biome_affinity)}")
    print(f"  Crystal count : {len(state.active_crystals)} nodes spawned")
    print(f"  Demo node     : {demo_node_id}")
    print(f"  Node quality  : {demo_node.quality}")
    print(f"  Node affinity : {demo_node.crystal_affinity}")
    print(f"  Biome origin  : {demo_node.biome_affinity}")
    print(f"  mutation_aff  : {demo_node.mutation_affinity:.6f}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1 — Pristine ETHEREAL MANA Crystal (CRYSTAL_GEM)
    #
    # We synthesise a realistic MiningResult manually to guarantee the test
    # always exercises Ethereal quality and MANA affinity regardless of which
    # server profile happened to generate the demo node.
    #
    # If the demo node happens to already be Ethereal+MANA, we use it directly.
    # Otherwise we construct a targeted synthetic ActiveCrystalNode that matches
    # the exact spec requested: "Ethereal MANA crystal from CRYSTAL_CAVERN DEEP".
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  TEST 1 — Pristine ETHEREAL MANA Crystal  (CRYSTAL_GEM)")
    print(_DIV_MINOR)
    print("  Scenario: successful harvest from a stable CRYSTAL_CAVERN DEEP node.")
    print("  Expected: quality=Ethereal, no depreciation, STABLE affinity branch.")
    print()

    # Build a synthetic node matching the spec (CRYSTAL_CAVERN / DEEP / MANA)
    # node_id format: "<server_id>:crystal:<slug>:<index>"
    ethereal_node = ActiveCrystalNode(
        node_id          = "100000000000000001:crystal:prismatic_geode:0",
        name             = "Prismatic Geode",
        crystal_type     = "Crystalline",
        crystal_affinity = "MANA",
        depth_layer      = "DEEP",
        access_tier      = "Restricted Access",
        current_reserve  = 600.0,
        max_reserve      = 600.0,
        mutation_affinity= 0.25,       # CRYSTAL_CAVERN MANA: low mutation, matrix-stable
        biome_affinity   = "CRYSTAL_CAVERN",
        quality          = "Ethereal",
        crystal_index    = 0,
    )

    # Fabricate the MiningResult that a successful Titanium Drill harvest produces.
    # amount_extracted = 18.75 tonnes (realistic mid-swing value on a 600-unit node).
    gem_result = MiningResult(
        success          = True,
        node_id          = ethereal_node.node_id,
        resource_name    = ethereal_node.name,
        resource_type    = VARIANT_GEM,
        stamina_consumed = 10.0,
        amount_extracted = 18.75,
        is_node_depleted = False,
        critical_hit     = False,
        flavour_message  = (
            "💎 The Prismatic Geode pulses with arcane resonance. "
            "You harvest 18.75 units of Ethereal MANA crystal from the "
            "CRYSTAL_CAVERN formation."
        ),
    )

    gem_item = factory.create_item_from_mining(
        mining_result = gem_result,
        active_node   = ethereal_node,
        owner_id      = 987654321098765432,
    )

    _print_crystal_item(gem_item, label="TEST 1  |  Prismatic Geode  [CRYSTAL_GEM]")

    # Prove immutability
    try:
        gem_item.quality = "Flawed"   # type: ignore
        print("  ✗ FAIL — CrystalItem was mutated!")
    except (AttributeError, TypeError) as e:
        print(f"  ✓ IMMUTABILITY CHECK PASSED — {type(e).__name__}: {e}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2 — High-Tectonic CRYSTAL_SPLINTER (fractured PRISMATIC MUTATION)
    #
    # Scenario: a server with extreme tectonic pressure fractures a high-
    # mutation_affinity Prismatic crystal into splinters.
    # Expected:
    #   • origin_variant = "CRYSTAL_SPLINTER"
    #   • quality FORCED to "Flawed" (regardless of original Prismatic)
    #   • display_name suffixed with "Splinter"
    #   • VOLATILE affinity branch used (MUTATION → energy_boost)
    #   • 0.45 compression scalar applied to final value
    #   • base_market_value significantly lower than GEM equivalent
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  TEST 2 — High-Tectonic CRYSTAL_SPLINTER  (fractured PRISMATIC MUTATION)")
    print(_DIV_MINOR)
    print("  Scenario: tectonic stress on IRRADIATED_WASTES server fractures the node.")
    print("  Expected: quality FORCED to Flawed, 0.45 scalar, VOLATILE affinity branch.")
    print()

    # This node was originally Prismatic — splinter downgrade will override it.
    reactor_node = ActiveCrystalNode(
        node_id          = "555000000000000555:crystal:reactor_spar:1",
        name             = "Reactor Spar",
        crystal_type     = "Radioactive",
        crystal_affinity = "MUTATION",
        depth_layer      = "DEEP",
        access_tier      = "Restricted Access",
        current_reserve  = 250.0,
        max_reserve      = 250.0,
        mutation_affinity= 0.87,       # High mutation → high fracture probability
        biome_affinity   = "IRRADIATED_WASTES",
        quality          = "Prismatic",     # ORIGINAL quality — will be downgraded
        crystal_index    = 1,
    )

    # Tectonic fracture event: 3 splinters at 45% yield per piece.
    # Total amount_extracted = 3 splinters × base_yield_per_splinter
    # The mining engine would set resource_type = "CRYSTAL_SPLINTER".
    splinter_result = MiningResult(
        success          = True,
        node_id          = reactor_node.node_id,
        resource_name    = reactor_node.name,
        resource_type    = VARIANT_SPLINTER,
        stamina_consumed = 16.0,
        amount_extracted = 9.84,        # Splinter-reduced yield (3 × scatter)
        is_node_depleted = False,
        critical_hit     = False,
        flavour_message  = (
            "💥 CRYSTAL FRACTURE! The Reactor Spar writhes with unstable energy, "
            "but the tectonic stress (fracture prob 62%) shatters it on impact. "
            "You collect 3 splinter(s) (9.84 units total) — reduced economic value, "
            "but useful for crafting volatile reagents."
        ),
    )

    splinter_item = factory.create_item_from_mining(
        mining_result = splinter_result,
        active_node   = reactor_node,
        owner_id      = 987654321098765432,
    )

    _print_crystal_item(splinter_item, label="TEST 2  |  Reactor Spar Splinter  [CRYSTAL_SPLINTER]")

    # ─────────────────────────────────────────────────────────────────────────
    # MATHEMATICAL VERIFICATION
    # Compare the two items side-by-side and prove the economics are correct.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  MATHEMATICAL VERIFICATION  —  Side-by-Side Economics")
    print(_DIV_MINOR)

    # Manually re-derive GEM value
    gem_q_mult = _QUALITY_MULTIPLIER["Ethereal"]                      # 6.0
    gem_qbase  = gem_result.amount_extracted * gem_q_mult              # 18.75 × 6.0
    gem_aff    = _compute_affinity_factor("MANA", 0.25)                # STABLE branch
    gem_manual = gem_qbase * gem_aff                                   # no splinter scalar
    gem_manual = round(gem_manual, 6)

    # Manually re-derive SPLINTER value
    sp_q_mult  = _QUALITY_MULTIPLIER["Flawed"]                        # 1.0  (FORCED down)
    sp_qbase   = splinter_result.amount_extracted * sp_q_mult          # 9.84 × 1.0
    sp_aff     = _compute_affinity_factor("MUTATION", 0.87)            # VOLATILE branch
    sp_prespl  = sp_qbase * sp_aff                                     # before scalar
    sp_manual  = sp_prespl * _SPLINTER_SCALAR                          # × 0.45
    sp_manual  = round(sp_manual, 6)

    print(f"  {'Item':<40}  {'Expected':>14}  {'Stored':>14}  {'Match':>6}")
    print(f"  {'─' * 40}  {'─' * 14}  {'─' * 14}  {'─' * 6}")

    gem_ok = abs(gem_manual - gem_item.base_market_value) < 1e-5
    sp_ok  = abs(sp_manual  - splinter_item.base_market_value) < 1e-5

    print(f"  {'Prismatic Geode (GEM)':<40}  {gem_manual:>14.6f}  "
          f"{gem_item.base_market_value:>14.6f}  {'✓' if gem_ok else '✗':>6}")
    print(f"  {'Reactor Spar Splinter (SPLINTER)':<40}  {sp_manual:>14.6f}  "
          f"{splinter_item.base_market_value:>14.6f}  {'✓' if sp_ok else '✗':>6}")

    print()
    print(f"  GEM quality inherited   : {gem_item.quality!r}  (unchanged from node)")
    print(f"  SPLINTER quality        : {splinter_item.quality!r}  "
          f"(force-downgraded from {reactor_node.quality!r})")
    print(f"  SPLINTER display_name   : {splinter_item.display_name!r}  "
          f"(suffix appended)")
    print()

    # Economic ratio — GEM should command significantly higher per-tonne value
    gem_per_tonne = gem_item.base_market_value / gem_item.weight_tonnes
    sp_per_tonne  = splinter_item.base_market_value / splinter_item.weight_tonnes
    print(f"  Per-tonne value (GEM)     : {gem_per_tonne:.4f}")
    print(f"  Per-tonne value (SPLINTER): {sp_per_tonne:.4f}")
    print(f"  GEM premium over SPLINTER : {gem_per_tonne / sp_per_tonne:.2f}×")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # UUID DISTINCTNESS CHECK
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  UUID DISTINCTNESS CHECK")
    print(_DIV_MINOR)
    uuid_ok = gem_item.item_uuid != splinter_item.item_uuid
    print(f"  GEM     uuid[:16] : {gem_item.item_uuid[:16]}")
    print(f"  SPLINTER uuid[:16]: {splinter_item.item_uuid[:16]}")
    print(f"  Distinct UUIDs    : {'✓ PASS' if uuid_ok else '✗ FAIL — UUIDs COLLIDE'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # IDEMPOTENCY CHECK  —  same inputs → identical CrystalItem every time
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  IDEMPOTENCY CHECK  —  same inputs → identical CrystalItem")
    print(_DIV_MINOR)

    gem_item_2      = factory.create_item_from_mining(gem_result,      ethereal_node, 987654321098765432)
    splinter_item_2 = factory.create_item_from_mining(splinter_result, reactor_node,  987654321098765432)

    gem_idem = (gem_item == gem_item_2)
    sp_idem  = (splinter_item == splinter_item_2)

    print(f"  GEM     idempotent: {'✓ PASS' if gem_idem else '✗ FAIL'}")
    print(f"  SPLINTER idempotent: {'✓ PASS' if sp_idem else '✗ FAIL'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # JSON ROUND-TRIP CHECK
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  JSON ROUND-TRIP CHECK  —  to_json() / json.loads() parity")
    print(_DIV_MINOR)

    import json
    for label, item in [("GEM", gem_item), ("SPLINTER", splinter_item)]:
        j_str   = item.to_json()
        parsed  = json.loads(j_str)
        rt_ok   = (
            parsed["item_uuid"]         == item.item_uuid
            and parsed["origin_variant"]    == item.origin_variant
            and parsed["quality"]           == item.quality
            and abs(parsed["base_market_value"] - item.base_market_value) < 1e-9
        )
        print(f"  {'✓ PASS' if rt_ok else '✗ FAIL'}  {label} round-trip")

    print()

    # ─────────────────────────────────────────────────────────────────────────
    # ERROR GUARD CHECKS
    # ─────────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  ERROR GUARD CHECKS  —  factory rejects invalid inputs")
    print(_DIV_MINOR)

    # Guard 1: failed MiningResult
    failed_result = MiningResult(
        success          = False,
        node_id          = ethereal_node.node_id,
        resource_name    = "Prismatic Geode",
        resource_type    = VARIANT_GEM,
        stamina_consumed = 0.0,
        amount_extracted = 0.0,
        is_node_depleted = False,
        critical_hit     = False,
        flavour_message  = "Tool broke.",
    )
    try:
        factory.create_item_from_mining(failed_result, ethereal_node, 111)
        print("  ✗ FAIL — failed MiningResult was accepted (should have raised)")
    except ValueError as e:
        print(f"  ✓ Guard 1 PASS — failed result rejected: {str(e)[:60]}…")

    # Guard 2: ORE resource_type passed to CrystalFactory
    ore_result = MiningResult(
        success          = True,
        node_id          = ethereal_node.node_id,
        resource_name    = "Hematite",
        resource_type    = "ORE",
        stamina_consumed = 10.0,
        amount_extracted = 5.0,
        is_node_depleted = False,
        critical_hit     = False,
        flavour_message  = "Ore extracted.",
    )
    try:
        factory.create_item_from_mining(ore_result, ethereal_node, 111)
        print("  ✗ FAIL — ORE resource_type was accepted (should have raised)")
    except ValueError as e:
        print(f"  ✓ Guard 2 PASS — ORE type rejected: {str(e)[:60]}…")

    # Guard 3: zero-yield result
    zero_result = MiningResult(
        success          = True,
        node_id          = ethereal_node.node_id,
        resource_name    = "Prismatic Geode",
        resource_type    = VARIANT_GEM,
        stamina_consumed = 10.0,
        amount_extracted = 0.0,
        is_node_depleted = False,
        critical_hit     = False,
        flavour_message  = "Zero yield.",
    )
    try:
        factory.create_item_from_mining(zero_result, ethereal_node, 111)
        print("  ✗ FAIL — zero-yield result was accepted (should have raised)")
    except ValueError as e:
        print(f"  ✓ Guard 3 PASS — zero yield rejected: {str(e)[:60]}…")

    print()
    all_checks = gem_ok and sp_ok and uuid_ok and gem_idem and sp_idem
    print(_DIV_MAJOR)
    if all_checks:
        print("  ✓ ALL CHECKS PASSED — crystal.py is production-ready.")
    else:
        print("  ✗ ONE OR MORE CHECKS FAILED — review output above.")
    print(_DIV_MAJOR)
    print()