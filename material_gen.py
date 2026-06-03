"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         MATERIAL_GEN.PY  —  Crustal & Geological Generation Engine         ║
║         Foundational Block #2 of the Procedural World System               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM CONTRACT                                                          ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Consumes : identitas_genetik.ServerGeneticProfile  (frozen, read-only)    ║
║  Produces : ServerMaterialCatalog                   (frozen, read-only)    ║
║                                                                             ║
║  DOWNSTREAM CONTRACT                                                        ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  economy.py  reads: industrial_resource_score, strategic_resource_score,   ║
║                     luxury_resource_score, total_resource_score,           ║
║                     dominance_ratio, pressure_index                        ║
║  fauna_gen   reads: tectonic_activity, crystal_nodes[*].mutation_affinity  ║
║  crafting    reads: ore_nodes[*].reserve_quantity (finite depletion)       ║
╚══════════════════════════════════════════════════════════════════════════════╝

DESIGN PRINCIPLES
─────────────────
• Zero Random      — no `random`, no `numpy.random`.  All entropy is derived
                     from a single deterministic sub-seed:
                         SHA-256(genetic_signature.encode() + b"material_salt_v2")
• Entropy Stream   — the 64-char sub-seed hex is consumed as a sequential
                     stream of 4-char windows (16-bit uints), indexed by an
                     advancing cursor.  Each field consumes exactly as many
                     windows as it needs, in a documented order.
• Cascade Fidelity — the profile's world_age, pressure_index, biome_affinity,
                     and element symbols all feed into geological decisions,
                     preserving the cascade logic from identitas_genetik.
• Exhaustion Contract — OreNode.reserve_quantity is finite and depletable;
                        it is never mutated by this engine after creation.
• Immutability     — ServerMaterialCatalog is frozen=True.  Downstream modules
                     apply runtime deltas; they never write to this object.

ENTROPY CURSOR MAP  (sub-seed hex stream → fields)
────────────────────────────────────────────────────────────────────────────
  Window W = 4 hex chars = 16-bit uint (0-65535).  Cursor advances by 1
  window per consumption.  All window indices are listed in call order.

  W 0        → tectonic_activity        (float [0.0, 1.0])
  W 1        → dominance_ratio          (float [0.55, 0.95])
  W 2        → pressure_index base      (float [0.0, 1.0], then age-nudged)
  W 3        → crystallization_index    (float [0.0, 1.0])
  W 4        → geological_rating roll   (int → "Barren"|"Moderate"|"Rich"|"Pristine")
  W 5-6      → dominant ore purity, vein_count
  W 7-8      → secondary ore purity, vein_count
  W 9-10     → crystal count, crystal quality seed
  W 11+      → per-crystal depth, affinity, mutation_affinity rolls
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import json
import sys
import os
from dataclasses import dataclass, asdict
from typing import Dict, FrozenSet, List, Optional, Tuple

# ── Import the upstream genetic engine ────────────────────────────────────────
# Supports running from the same directory or with PYTHONPATH set.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identitas_genetik import (
    ServerGeneticProfile,
    GeneticEngine,
    ElementProfile,
)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — CONSTANTS & LOOKUP TABLES
# ─────────────────────────────────────────────────────────────────────────────

_MAX_UINT16: int = 0xFFFF          # 65535 — denominator for 16-bit unit mapping
_MATERIAL_SALT: bytes = b"material_salt_v2"

# ── Depth layer ordering (surface → abyss) ────────────────────────────────────
DEPTH_LAYERS: Tuple[str, ...] = ("SURFACE", "SHALLOW", "DEEP", "ABYSS")

# ── Purity tiers ──────────────────────────────────────────────────────────────
PURITY_TIERS: Tuple[str, ...] = ("Crude", "Enriched", "Flawless")

# ── Crystal quality tiers ────────────────────────────────────────────────────
CRYSTAL_QUALITY: Tuple[str, ...] = ("Flawed", "Prismatic", "Ethereal")

# ── Crystal affinity enum ─────────────────────────────────────────────────────
CRYSTAL_AFFINITIES: Tuple[str, ...] = ("POWER", "MANA", "MUTATION", "UTILITY", "DEFENSE")

# ── Geological rating tiers ───────────────────────────────────────────────────
GEO_RATINGS: Tuple[str, ...] = ("Barren", "Moderate", "Rich", "Pristine")

# ── Element family → mineral names ───────────────────────────────────────────
# Each entry: symbol → (primary_ore_name, secondary_ore_name)
# These follow real mineralogy where possible.
_ORE_NAMES: Dict[str, Tuple[str, str]] = {
    # Transition metals (common)
    "Fe": ("Hematite",         "Magnetite"),
    "Cu": ("Chalcopyrite",     "Malachite"),
    "Ni": ("Pentlandite",      "Garnierite"),
    "Zn": ("Sphalerite",       "Smithsonite"),
    "Mn": ("Pyrolusite",       "Rhodonite"),
    "Cr": ("Chromite",         "Uvarovite"),
    "Co": ("Cobaltite",        "Skutterudite"),
    "Ti": ("Ilmenite",         "Rutile"),
    "V":  ("Vanadinite",       "Patronite"),
    "Mo": ("Molybdenite",      "Powellite"),
    "W":  ("Wolframite",       "Scheelite"),
    "Zr": ("Zircon",           "Baddeleyite"),
    "Nb": ("Columbite",        "Pyrochlore"),
    # Precious / strategic
    "Au": ("Native Gold",      "Calaverite"),
    "Ag": ("Argentite",        "Pyrargyrite"),
    "Pt": ("Native Platinum",  "Sperrylite"),
    "Pd": ("Native Palladium", "Braggite"),
    "Rh": ("Native Rhodium",   "Bowieite"),
    "Ir": ("Iridosmine",       "Native Iridium"),
    "Os": ("Osmium Alloy",     "Iridosmine"),
    "Ru": ("Laurite",          "Ruarsite"),
    "Re": ("Molybdenite-Re",   "Dzhezkazganite"),
    # Radioactive / exotic
    "U":  ("Uraninite",        "Carnotite"),
    "Th": ("Thorianite",       "Monazite"),
    "Pu": ("Plutonyl Oxide",   "Synthetic Pellet"),
    "Ra": ("Radium Chloride",  "Autunite"),
    "Ac": ("Actinium Oxide",   "Synthetic Crystal"),
    "Tc": ("Pertechnetate",    "Synthetic Rod"),
    "Pm": ("Promethium Salt",  "Synthetic Core"),
    # Alkali metals
    "Li": ("Spodumene",        "Lepidolite"),
    "Na": ("Halite",           "Trona"),
    "K":  ("Sylvite",          "Carnallite"),
    "Rb": ("Pollucite",        "Carnallite-Rb"),
    "Cs": ("Pollucite",        "Lepidolite-Cs"),
    # Alkaline earth metals
    "Be": ("Beryl",            "Bertrandite"),
    "Mg": ("Magnesite",        "Dolomite"),
    "Ca": ("Calcite",          "Fluorite"),
    "Sr": ("Celestine",        "Strontianite"),
    "Ba": ("Barite",           "Witherite"),
    # Post-transition metals
    "Al": ("Bauxite",          "Corundum"),
    "Pb": ("Galena",           "Cerussite"),
    "Sn": ("Cassiterite",      "Stannite"),
    "Bi": ("Bismuthinite",     "Bismite"),
    "Ga": ("Sphalerite-Ga",    "Diaspore-Ga"),
    "In": ("Sphalerite-In",    "Indite"),
    "Tl": ("Lorandite",        "Crookesite"),
    "Cd": ("Greenockite",      "Otavite"),
    "Hg": ("Cinnabar",         "Montroydite"),
    # Lanthanides
    "La": ("Monazite-La",      "Bastnäsite"),
    "Ce": ("Monazite-Ce",      "Bastnäsite-Ce"),
    "Nd": ("Monazite-Nd",      "Parisite"),
    "Pr": ("Monazite-Pr",      "Rhabdophane"),
    "Sm": ("Monazite-Sm",      "Xenotime-Sm"),
    "Gd": ("Gadolinite",       "Monazite-Gd"),
    "Dy": ("Xenotime-Dy",      "Churchite"),
    "Er": ("Xenotime-Er",      "Euxenite"),
    "Yb": ("Xenotime-Yb",      "Gadolinite-Yb"),
    "Lu": ("Xenotime-Lu",      "Lutetite"),
    "Sc": ("Thortveitite",     "Kolbeckite"),
    "Y":  ("Xenotime",         "Monazite-Y"),
    "Hf": ("Hafnon",           "Zircon-Hf"),
    "Ta": ("Tantalite",        "Tapiolite"),
}
_ORE_NAME_FALLBACK: Tuple[str, str] = ("Metallic Ore",   "Mineral Vein")


# ── Biome × Depth → Crystal synthesis table ──────────────────────────────────
# Key: (biome_tag, depth_layer)  →  (crystal_name, crystal_type, affinity)
# If a biome has no depth-specific crystal, a generic fallback is used.
# affinity must be one of CRYSTAL_AFFINITIES.

_CRYSTAL_SYNTHESIS: Dict[Tuple[str, str], Tuple[str, str, str]] = {
    # ── VOLCANIC ──────────────────────────────────────────────────────────
    ("VOLCANIC",          "SURFACE"):  ("Pyroclast Shard",    "Igneous",       "POWER"),
    ("VOLCANIC",          "SHALLOW"):  ("Pyro Quartz",        "Igneous",       "POWER"),
    ("VOLCANIC",          "DEEP"):     ("Magma Spinel",       "Igneous",       "POWER"),
    ("VOLCANIC",          "ABYSS"):    ("Infernium Core",     "Primordial",    "POWER"),
    # ── IRRADIATED_WASTES ─────────────────────────────────────────────────
    ("IRRADIATED_WASTES", "SURFACE"):  ("Void Mica",          "Radioactive",   "MUTATION"),
    ("IRRADIATED_WASTES", "SHALLOW"):  ("Fission Quartz",     "Radioactive",   "MUTATION"),
    ("IRRADIATED_WASTES", "DEEP"):     ("Reactor Spar",       "Radioactive",   "MUTATION"),
    ("IRRADIATED_WASTES", "ABYSS"):    ("Nucleite Core",      "Radioactive",   "MUTATION"),
    # ── CRYSTAL_CAVERN ────────────────────────────────────────────────────
    ("CRYSTAL_CAVERN",    "SURFACE"):  ("Gossamer Cluster",   "Crystalline",   "UTILITY"),
    ("CRYSTAL_CAVERN",    "SHALLOW"):  ("Resonance Crystal",  "Crystalline",   "MANA"),
    ("CRYSTAL_CAVERN",    "DEEP"):     ("Prismatic Geode",    "Crystalline",   "MANA"),
    ("CRYSTAL_CAVERN",    "ABYSS"):    ("Astral Lattice",     "Arcane",        "MANA"),
    # ── DEEP_OCEAN ────────────────────────────────────────────────────────
    ("DEEP_OCEAN",        "SURFACE"):  ("Sea Glass Nodule",   "Hydrothermal",  "UTILITY"),
    ("DEEP_OCEAN",        "SHALLOW"):  ("Abyssal Calcite",    "Hydrothermal",  "DEFENSE"),
    ("DEEP_OCEAN",        "DEEP"):     ("Pressure Apatite",   "Hydrothermal",  "DEFENSE"),
    ("DEEP_OCEAN",        "ABYSS"):    ("Hadal Aegis Stone",  "Hydrothermal",  "DEFENSE"),
    # ── ABYSSAL_TRENCH ────────────────────────────────────────────────────
    ("ABYSSAL_TRENCH",    "SHALLOW"):  ("Trench Carnelian",   "Hydrothermal",  "DEFENSE"),
    ("ABYSSAL_TRENCH",    "DEEP"):     ("Dark Feldspar",      "Hydrothermal",  "UTILITY"),
    ("ABYSSAL_TRENCH",    "ABYSS"):    ("Void Basalt Core",   "Primordial",    "POWER"),
    # ── ANCIENT_FOREST ────────────────────────────────────────────────────
    ("ANCIENT_FOREST",    "SURFACE"):  ("Amber Nodule",       "Organic",       "UTILITY"),
    ("ANCIENT_FOREST",    "SHALLOW"):  ("Verdant Spinel",     "Organic",       "MANA"),
    ("ANCIENT_FOREST",    "DEEP"):     ("Heartwood Opal",     "Organic",       "MANA"),
    ("ANCIENT_FOREST",    "ABYSS"):    ("Root-Touched Spar",  "Primordial",    "MUTATION"),
    # ── FROZEN_TUNDRA ────────────────────────────────────────────────────
    ("FROZEN_TUNDRA",     "SURFACE"):  ("Glacial Quartz",     "Cryogenic",     "DEFENSE"),
    ("FROZEN_TUNDRA",     "SHALLOW"):  ("Ice Spar",           "Cryogenic",     "DEFENSE"),
    ("FROZEN_TUNDRA",     "DEEP"):     ("Permafrost Crystal", "Cryogenic",     "UTILITY"),
    ("FROZEN_TUNDRA",     "ABYSS"):    ("Absolute Zero Core", "Cryogenic",     "POWER"),
    # ── DESERT_DUNES ─────────────────────────────────────────────────────
    ("DESERT_DUNES",      "SURFACE"):  ("Dune Chalcedony",    "Sedimentary",   "UTILITY"),
    ("DESERT_DUNES",      "SHALLOW"):  ("Silica Geode",       "Sedimentary",   "UTILITY"),
    ("DESERT_DUNES",      "DEEP"):     ("Compressed Opal",    "Sedimentary",   "MANA"),
    ("DESERT_DUNES",      "ABYSS"):    ("Solar Core Stone",   "Primordial",    "POWER"),
    # ── SWAMP_MARSHLAND ──────────────────────────────────────────────────
    ("SWAMP_MARSHLAND",   "SURFACE"):  ("Mire Amber",         "Biogenic",      "UTILITY"),
    ("SWAMP_MARSHLAND",   "SHALLOW"):  ("Bog Beryl",          "Biogenic",      "MANA"),
    ("SWAMP_MARSHLAND",   "DEEP"):     ("Marsh Tourmaline",   "Biogenic",      "MUTATION"),
    ("SWAMP_MARSHLAND",   "ABYSS"):    ("Primordial Slime Crystal", "Biogenic", "MUTATION"),
    # ── FLOATING_ISLANDS ─────────────────────────────────────────────────
    ("FLOATING_ISLANDS",  "SURFACE"):  ("Aetheric Feldspar",  "Aetherial",     "MANA"),
    ("FLOATING_ISLANDS",  "SHALLOW"):  ("Levitation Quartz",  "Aetherial",     "POWER"),
    ("FLOATING_ISLANDS",  "DEEP"):     ("Sky Spinel",         "Aetherial",     "POWER"),
    ("FLOATING_ISLANDS",  "ABYSS"):    ("Void-Sky Core",      "Primordial",    "MANA"),
    # ── STORM_HIGHLANDS ──────────────────────────────────────────────────
    ("STORM_HIGHLANDS",   "SURFACE"):  ("Stormite Shard",     "Electrostatic", "POWER"),
    ("STORM_HIGHLANDS",   "SHALLOW"):  ("Lightning Quartz",   "Electrostatic", "POWER"),
    ("STORM_HIGHLANDS",   "DEEP"):     ("Thunder Corundum",   "Electrostatic", "DEFENSE"),
    ("STORM_HIGHLANDS",   "ABYSS"):    ("Eye of the Storm",   "Primordial",    "POWER"),
    # ── MUSHROOM_GROVE ───────────────────────────────────────────────────
    ("MUSHROOM_GROVE",    "SURFACE"):  ("Spore Crystal",      "Mycological",   "MUTATION"),
    ("MUSHROOM_GROVE",    "SHALLOW"):  ("Luminescent Cap",    "Mycological",   "MANA"),
    ("MUSHROOM_GROVE",    "DEEP"):     ("Hyphal Opal",        "Mycological",   "MUTATION"),
    ("MUSHROOM_GROVE",    "ABYSS"):    ("Mycelium Core",      "Primordial",    "MUTATION"),
    # ── CORRUPTED_RUINS ──────────────────────────────────────────────────
    ("CORRUPTED_RUINS",   "SURFACE"):  ("Tainted Obsidian",   "Corrupted",     "MUTATION"),
    ("CORRUPTED_RUINS",   "SHALLOW"):  ("Ruinite Shard",      "Corrupted",     "UTILITY"),
    ("CORRUPTED_RUINS",   "DEEP"):     ("Void Marble",        "Corrupted",     "DEFENSE"),
    ("CORRUPTED_RUINS",   "ABYSS"):    ("Null Catalyst",      "Primordial",    "MUTATION"),
    # ── CELESTIAL_PLATEAU ────────────────────────────────────────────────
    ("CELESTIAL_PLATEAU", "SURFACE"):  ("Starfall Fragment",  "Celestial",     "MANA"),
    ("CELESTIAL_PLATEAU", "SHALLOW"):  ("Celestite",          "Celestial",     "MANA"),
    ("CELESTIAL_PLATEAU", "DEEP"):     ("Aetherite",          "Celestial",     "POWER"),
    ("CELESTIAL_PLATEAU", "ABYSS"):    ("Cosmic Core",        "Primordial",    "POWER"),
    # ── SCORCHED_PLAINS ──────────────────────────────────────────────────
    ("SCORCHED_PLAINS",   "SURFACE"):  ("Char Crystal",       "Pyroclastic",   "UTILITY"),
    ("SCORCHED_PLAINS",   "SHALLOW"):  ("Obsidian Shard",     "Pyroclastic",   "DEFENSE"),
    ("SCORCHED_PLAINS",   "DEEP"):     ("Inferno Spinel",     "Pyroclastic",   "POWER"),
    ("SCORCHED_PLAINS",   "ABYSS"):    ("Ashen Core",         "Primordial",    "POWER"),
    # ── NETHER_DEPTHS ────────────────────────────────────────────────────
    ("NETHER_DEPTHS",     "SURFACE"):  ("Brimstone Chip",     "Infernal",      "POWER"),
    ("NETHER_DEPTHS",     "SHALLOW"):  ("Netherstone",        "Infernal",      "POWER"),
    ("NETHER_DEPTHS",     "DEEP"):     ("Hellfire Corundum",  "Infernal",      "MUTATION"),
    ("NETHER_DEPTHS",     "ABYSS"):    ("Abyssal Singularity","Primordial",    "MUTATION"),
}

_CRYSTAL_FALLBACK: Tuple[str, str, str] = ("Mineral Crystal", "Generic", "UTILITY")

# ── Element category sets for economic scoring ───────────────────────────────
_INDUSTRIAL_SYMBOLS: FrozenSet[str] = frozenset({"Fe", "Al", "Cu", "Mn", "Zn", "Ni",
                                                   "Cr", "Ti", "Mg", "Ca", "Si"})
_STRATEGIC_SYMBOLS:  FrozenSet[str] = frozenset({"U", "Th", "Pu", "Ra", "Ac", "Tc",
                                                   "Pm", "Pa", "Np", "Am", "Cm",
                                                   "Co", "Li", "Nb", "Ta", "Re",
                                                   "Nd", "Dy", "Tb", "Pr"})
_LUXURY_SYMBOLS:     FrozenSet[str] = frozenset({"Au", "Pt", "Ag", "Pd", "Rh", "Ir",
                                                   "Os", "Ru"})
_STRATEGIC_CRYSTAL_AFFINITIES: FrozenSet[str] = frozenset({"POWER", "MANA", "MUTATION"})
_LUXURY_CRYSTAL_AFFINITIES:    FrozenSet[str] = frozenset({"UTILITY", "DEFENSE"})


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — OUTPUT DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OreNode:
    """
    An immutable ore deposit record.

    reserve_quantity is the *finite* total amount of ore in the deposit.
    Crafting/mining systems must track depletion externally — this module
    only declares the starting reserve.  Depletion never mutates this object.

    Formula:
        reserve_quantity = vein_size * base_yield * 100 * purity_multiplier
        where purity_multiplier: Crude=1.0, Enriched=1.6, Flawless=2.5
    """
    element_symbol:   str
    ore_name:         str
    purity:           str     # "Crude" | "Enriched" | "Flawless"
    depth_layer:      str     # "SURFACE" | "SHALLOW" | "DEEP" | "ABYSS"
    vein_count:       int
    vein_size:        float
    reserve_quantity: float   # Finite depletion target (tonnes-equivalent)
    base_yield:       float   # Base extraction yield per mining action
    rarity_score:     float   # 0.0–1.0; drives economy.py market pricing


@dataclass(frozen=True)
class CrystalNode:
    """
    An immutable crystal deposit record.

    Crystals are synthesised at the intersection of biome, depth, pressure,
    and crystallization_index.  They do not have reserve_quantity — they
    regenerate on geological timescales (handled by economy.py cooldowns).
    """
    name:              str
    crystal_type:      str
    crystal_affinity:  str    # "POWER" | "MANA" | "MUTATION" | "UTILITY" | "DEFENSE"
    quality:           str    # "Flawed" | "Prismatic" | "Ethereal"
    depth_layer:       str    # "SHALLOW" | "DEEP" | "ABYSS"
    mutation_affinity: float  # [0.0, 1.0]; feeds fauna_gen mutation rolls
    biome_affinity:    str    # Source biome tag


@dataclass(frozen=True)
class ServerMaterialCatalog:
    """
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  THE IMMUTABLE MATERIAL BASELINE — DO NOT MUTATE AFTER CREATION    ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║  economy.py MUST read scores as-is and layer runtime modifiers:    ║
    ║      current_industrial = industrial_resource_score                 ║
    ║                         + trade_bonus + tax_modifier               ║
    ╚══════════════════════════════════════════════════════════════════════╝

    total_resource_score == industrial + strategic + luxury  (always exact)
    """
    server_id:   int
    ore_nodes:   Tuple[OreNode,    ...]
    crystal_nodes: Tuple[CrystalNode, ...]

    geological_rating:  str    # "Barren" | "Moderate" | "Rich" | "Pristine"
    tectonic_activity:  float  # [0.0, 1.0] — high = many small veins
    pressure_index:     float  # [0.0, 1.0] — high = deeper crystals, purer ore
    dominance_ratio:    float  # [0.55, 0.95] — crustal partition fraction

    # ── Economic readiness (bridge to economy.py) ─────────────────────────
    industrial_resource_score: float   # Σ common base metal contributions
    strategic_resource_score:  float   # Σ exotic/radioactive + POWER/MANA/MUTATION crystals
    luxury_resource_score:     float   # Σ precious metals + UTILITY/DEFENSE crystals
    total_resource_score:      float   # Must == industrial + strategic + luxury exactly

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ore_nodes"]     = [dict(o) for o in d["ore_nodes"]]
        d["crystal_nodes"] = [dict(c) for c in d["crystal_nodes"]]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — ENTROPY STREAM ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class _EntropyStream:
    """
    Sequential deterministic entropy source derived from a SHA-256 sub-seed.

    The 64-char hex digest is consumed as a stream of 4-char windows
    (16-bit uints, range 0-65535).  Once the primary stream is exhausted
    (16 windows = 64 chars), the stream continues by re-hashing with an
    extension counter:  SHA-256(primary_digest + counter.to_bytes(2, 'big'))

    This gives an unlimited, deterministic, cross-platform entropy stream
    with zero `random` module dependency.
    """

    _WINDOW: int       = 4          # hex chars per window
    _MAX_U16: int      = 0xFFFF     # 65535
    _WINDOWS_PER_HASH: int = 16     # 64 hex / 4 per window

    def __init__(self, genetic_signature: str) -> None:
        primary_hash = hashlib.sha256(
            genetic_signature.encode("utf-8") + _MATERIAL_SALT
        ).hexdigest()
        self._primary: str = primary_hash
        self._extension_counter: int = 0
        self._cursor: int = 0        # window index within current block
        self._current_block: str = primary_hash

    # ── Core consumption ──────────────────────────────────────────────────

    def next_uint16(self) -> int:
        """Return the next 16-bit unsigned integer from the stream."""
        if self._cursor >= self._WINDOWS_PER_HASH:
            # Extend the stream deterministically
            self._extension_counter += 1
            ext = hashlib.sha256(
                self._primary.encode("utf-8")
                + self._extension_counter.to_bytes(2, "big")
            ).hexdigest()
            self._current_block = ext
            self._cursor = 0

        start = self._cursor * self._WINDOW
        window = self._current_block[start : start + self._WINDOW]
        self._cursor += 1
        return int(window, 16)

    def next_unit(self) -> float:
        """Return a float in [0.0, 1.0]."""
        return self.next_uint16() / self._MAX_U16

    def next_in_range(self, lo: float, hi: float) -> float:
        """Return a float in [lo, hi]."""
        return lo + self.next_unit() * (hi - lo)

    def next_index(self, n: int) -> int:
        """Return an integer in [0, n-1] via modulo (n ≤ 65536)."""
        return self.next_uint16() % n

    def next_tier(self, tiers: Tuple, thresholds: Tuple[float, ...]) -> str:
        """
        Map a unit-interval draw onto a tiered string enum.

        thresholds must be ascending fractions summing to 1.0.
        Example: tiers=("A","B","C"), thresholds=(0.5, 0.3, 0.2)
            → [0.0, 0.50)  picks "A"
            → [0.50, 0.80) picks "B"
            → [0.80, 1.00] picks "C"
        """
        u = self.next_unit()
        cumulative = 0.0
        for tier, frac in zip(tiers, thresholds):
            cumulative += frac
            if u < cumulative:
                return tier
        return tiers[-1]  # floating-point safety


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — GEOLOGICAL HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

_PURITY_MULTIPLIER: Dict[str, float] = {
    "Crude":    1.0,
    "Enriched": 1.6,
    "Flawless": 2.5,
}

_MUTATION_AFFINITY_BY_AFFINITY: Dict[str, Tuple[float, float]] = {
    # (lo, hi) range for mutation_affinity given crystal_affinity
    "POWER":    (0.05, 0.30),
    "MANA":     (0.10, 0.45),
    "MUTATION": (0.55, 1.00),
    "UTILITY":  (0.02, 0.20),
    "DEFENSE":  (0.01, 0.15),
}


def _get_ore_names(symbol: str) -> Tuple[str, str]:
    return _ORE_NAMES.get(symbol, _ORE_NAME_FALLBACK)


def _get_crystal(biome: str, depth: str) -> Tuple[str, str, str]:
    """Look up crystal synthesis; fall back gracefully."""
    return _CRYSTAL_SYNTHESIS.get((biome, depth), _CRYSTAL_FALLBACK)


def _rarity_score(rarity_weight: int) -> float:
    """
    Invert rarity_weight to a [0.0, 1.0] rarity score.
    Higher score = rarer = more economically valuable per unit.

    Formula:
        score = 1.0 - (rarity_weight / 1001.0)
        clamped to [0.01, 1.00]

    Iron (w=1000): score ≈ 0.001 → corrected to 0.01 (low rarity)
    Gold  (w= 22): score ≈ 0.978 (high rarity)
    Uranium(w=  4): score ≈ 0.996 (extreme rarity)
    """
    raw = 1.0 - (rarity_weight / 1001.0)
    return round(max(0.01, min(1.00, raw)), 6)


def _purity_from_pressure(pressure_index: float, entropy_unit: float) -> str:
    """
    Select purity tier as a function of pressure_index and an entropy draw.

    High pressure shifts probability toward Flawless.
    Combined roll = (pressure_index * 0.6) + (entropy_unit * 0.4)
    Thresholds: Flawless ≥ 0.70, Enriched ≥ 0.35, Crude < 0.35
    """
    combined = pressure_index * 0.6 + entropy_unit * 0.4
    if combined >= 0.70:
        return "Flawless"
    if combined >= 0.35:
        return "Enriched"
    return "Crude"


def _depth_from_pressure(pressure_index: float, entropy_unit: float) -> str:
    """
    Select a depth layer biased toward deeper layers at high pressure.

    combined = (pressure_index * 0.5) + (entropy_unit * 0.5)
    Threshold mapping:
        ≥ 0.75 → ABYSS
        ≥ 0.50 → DEEP
        ≥ 0.25 → SHALLOW
        <  0.25 → SURFACE
    """
    combined = pressure_index * 0.5 + entropy_unit * 0.5
    if combined >= 0.75:
        return "ABYSS"
    if combined >= 0.50:
        return "DEEP"
    if combined >= 0.25:
        return "SHALLOW"
    return "SURFACE"


def _vein_split(
    base_yield:       float,
    tectonic_activity: float,
    entropy_stream:   _EntropyStream,
) -> Tuple[int, float]:
    """
    Split base_yield into (vein_count, vein_size).

    High tectonic_activity  → many small veins   (fractured crust)
    Low  tectonic_activity  → few mega-veins      (stable cratons)

    Formula:
        raw_count = 1 + int(tectonic_activity * 9)   → [1, 10]
        entropy jitter: ± int(raw_count * 0.3)        → [1, 13]
        vein_size = base_yield / vein_count            (ensures total yield conserved)
        jitter on vein_size: × (0.85 + 0.30 * entropy)
    """
    raw_count_base  = 1 + int(tectonic_activity * 9)
    jitter_range    = max(1, int(raw_count_base * 0.3))
    jitter          = entropy_stream.next_uint16() % (jitter_range * 2 + 1) - jitter_range
    vein_count      = max(1, raw_count_base + jitter)

    size_jitter = 0.85 + 0.30 * entropy_stream.next_unit()
    vein_size   = round((base_yield / vein_count) * size_jitter, 4)
    return vein_count, vein_size


def _reserve_quantity(vein_size: float, base_yield: float, purity: str) -> float:
    """
    Calculate finite reserve.

    Formula: vein_size × base_yield × 100 × purity_multiplier
    Unit: game-tonnes (abstract; calibrated so a typical Fe deposit ≈ 50k–250k)
    """
    return round(vein_size * base_yield * 100.0 * _PURITY_MULTIPLIER[purity], 2)


def _crystal_quality_from_indices(
    crystallization_index: float,
    pressure_index:        float,
    entropy_unit:          float,
) -> str:
    """
    Select crystal quality at the intersection of crystallization and pressure.

    combined = (crystallization_index * 0.45)
             + (pressure_index        * 0.40)
             + (entropy_unit          * 0.15)
    ≥ 0.72 → Ethereal
    ≥ 0.42 → Prismatic
    <  0.42 → Flawed
    """
    combined = (crystallization_index * 0.45
                + pressure_index      * 0.40
                + entropy_unit        * 0.15)
    if combined >= 0.72:
        return "Ethereal"
    if combined >= 0.42:
        return "Prismatic"
    return "Flawed"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — ECONOMIC SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _score_industrial(
    ore_nodes:       Tuple[OreNode, ...],
    dominance_ratio: float,
) -> float:
    """
    industrial_resource_score

    Aggregated score from common base metal ore nodes (Fe, Al, Cu, Mn, Zn,
    Ni, Cr, Ti, Mg, Ca, Si).

    Per-node contribution:
        contrib = purity_multiplier × vein_size × base_yield × dominance_frac
        where dominance_frac = dominance_ratio if node is dominant else
                               (1 - dominance_ratio)

    Then sum and normalise to a [0, 1000] game scale:
        normaliser = 1000 / max(1, len(industrial_nodes))
    """
    industrial_nodes = [n for n in ore_nodes if n.element_symbol in _INDUSTRIAL_SYMBOLS]
    if not industrial_nodes:
        return 0.0

    # Identify dominant node (highest base_yield among industrial)
    dominant_sym = max(industrial_nodes, key=lambda n: n.base_yield).element_symbol
    total = 0.0
    for node in industrial_nodes:
        dfrac = dominance_ratio if node.element_symbol == dominant_sym else (1.0 - dominance_ratio)
        contrib = _PURITY_MULTIPLIER[node.purity] * node.vein_size * node.base_yield * dfrac
        total += contrib

    # Normalise: cap at 1000 scale, preserving proportionality
    normaliser = 500.0 / max(1, len(industrial_nodes))
    return round(min(9999.0, total * normaliser), 4)


def _score_strategic(
    ore_nodes:      Tuple[OreNode, ...],
    crystal_nodes:  Tuple[CrystalNode, ...],
    pressure_index: float,
) -> float:
    """
    strategic_resource_score

    = Σ(exotic/radioactive ore: rarity_score × base_yield × 200 × pressure_index)
    + Σ(strategic crystals [POWER|MANA|MUTATION]:
          quality_multiplier × mutation_affinity × pressure_index × 300)

    quality_multiplier: Flawed=1.0, Prismatic=1.8, Ethereal=3.0
    """
    _quality_mult: Dict[str, float] = {"Flawed": 1.0, "Prismatic": 1.8, "Ethereal": 3.0}

    total = 0.0
    for node in ore_nodes:
        if node.element_symbol in _STRATEGIC_SYMBOLS:
            total += node.rarity_score * node.base_yield * 200.0 * pressure_index

    for crystal in crystal_nodes:
        if crystal.crystal_affinity in _STRATEGIC_CRYSTAL_AFFINITIES:
            qm = _quality_mult.get(crystal.quality, 1.0)
            total += qm * crystal.mutation_affinity * pressure_index * 300.0

    return round(min(9999.0, total), 4)


def _score_luxury(
    ore_nodes:     Tuple[OreNode, ...],
    crystal_nodes: Tuple[CrystalNode, ...],
) -> float:
    """
    luxury_resource_score

    = Σ(precious metal ore: rarity_score × vein_size × base_yield × 400)
    + Σ(luxury crystals [UTILITY|DEFENSE]:
          quality_multiplier × (1.0 - mutation_affinity) × 250)
    """
    _quality_mult: Dict[str, float] = {"Flawed": 1.0, "Prismatic": 1.8, "Ethereal": 3.0}

    total = 0.0
    for node in ore_nodes:
        if node.element_symbol in _LUXURY_SYMBOLS:
            total += node.rarity_score * node.vein_size * node.base_yield * 400.0

    for crystal in crystal_nodes:
        if crystal.crystal_affinity in _LUXURY_CRYSTAL_AFFINITIES:
            qm = _quality_mult.get(crystal.quality, 1.0)
            total += qm * (1.0 - crystal.mutation_affinity) * 250.0

    return round(min(9999.0, total), 4)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — MATERIAL ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MaterialEngine:
    """
    Deterministic Crustal & Geological Generation Engine.

    Entry point: MaterialEngine.generate_geology(profile) → ServerMaterialCatalog

    The engine strictly never imports `random` or `numpy.random`.
    All parameters are derived from a deterministic entropy stream seeded by:
        SHA-256(profile.genetic_signature.encode() + b"material_salt_v2")

    Generation Pipeline
    ───────────────────
    Step 1  — Derive macro geological parameters (tectonic_activity,
              dominance_ratio, pressure_index, crystallization_index).
    Step 2  — Apply world_age nudge to pressure_index.
    Step 3  — Calculate geological_rating from combined indices.
    Step 4  — Generate dominant metal OreNode using dominance_ratio partition.
    Step 5  — Generate secondary metal OreNode using (1 - dominance_ratio).
    Step 6  — Generate non-metal reference OreNode (informational; minerals).
    Step 7  — Generate crystal nodes (1–3) from biome × depth convergence.
    Step 8  — Compute economic readiness scores.
    Step 9  — Assemble and freeze ServerMaterialCatalog.
    """

    # ── World-age pressure nudges ──────────────────────────────────────────
    _AGE_PRESSURE_NUDGE: Dict[str, float] = {
        "ANCIENT":    +0.25,   # Deep-compressed crust = high pressure
        "MATURE":      0.00,
        "PRIMORDIAL": -0.20,   # Young volatile crust = low pressure
    }

    # ── Purity selection thresholds (by pressure tier) ────────────────────
    # Used in _purity_from_pressure; reproduced here for legibility.
    # (actual logic lives in the helper function)

    def generate_geology(
        self,
        profile: ServerGeneticProfile,
    ) -> ServerMaterialCatalog:
        """
        Derive an immutable ServerMaterialCatalog from *profile*.

        Parameters
        ----------
        profile : ServerGeneticProfile — frozen genetic baseline from
                  identitas_genetik.GeneticEngine.generate_profile()

        Returns
        -------
        ServerMaterialCatalog — fully deterministic, frozen, cross-platform.
        """
        # Guard: never mutate the upstream profile
        assert profile.genetic_signature, "Profile must have a valid genetic_signature"

        stream = _EntropyStream(profile.genetic_signature)

        # ═════════════════════════════════════════════════════════════════
        # STEP 1 — MACRO GEOLOGICAL PARAMETERS
        # ═════════════════════════════════════════════════════════════════

        tectonic_activity   = round(stream.next_unit(), 6)  # W0
        dominance_ratio_raw = stream.next_in_range(0.55, 0.95)
        dominance_ratio     = round(dominance_ratio_raw, 6)  # W1
        pressure_raw        = stream.next_unit()             # W2
        crystallization_idx = round(stream.next_unit(), 6)  # W3

        # ═════════════════════════════════════════════════════════════════
        # STEP 2 — WORLD-AGE PRESSURE NUDGE
        # ═════════════════════════════════════════════════════════════════
        age_nudge    = self._AGE_PRESSURE_NUDGE.get(profile.world_age, 0.0)
        pressure_index = round(max(0.0, min(1.0, pressure_raw + age_nudge)), 6)

        # ═════════════════════════════════════════════════════════════════
        # STEP 3 — GEOLOGICAL RATING
        # ═════════════════════════════════════════════════════════════════
        # Combined geological score = weighted average of three indices
        geo_score = (
            profile.base_resource_density * 0.45
            + pressure_index              * 0.30
            + crystallization_idx         * 0.25
        )
        geo_rating = stream.next_tier(
            GEO_RATINGS,
            # Threshold fractions (must sum to 1.0):
            # Barren < 0.35, Moderate < 0.60, Rich < 0.82, Pristine rest
            (0.35, 0.25, 0.22, 0.18),
        )  # W4

        # Override if geo_score clearly contradicts the tier roll
        # (Geological rating must reflect actual index values, not pure entropy)
        if geo_score >= 1.50 and geo_rating == "Barren":
            geo_rating = "Moderate"
        if geo_score >= 1.70 and geo_rating in ("Barren", "Moderate"):
            geo_rating = "Rich"
        if geo_score >= 1.90:
            geo_rating = "Pristine"
        if geo_score < 0.30:
            geo_rating = "Barren"

        # ═════════════════════════════════════════════════════════════════
        # STEP 4 — DOMINANT METAL ORE NODE
        # ═════════════════════════════════════════════════════════════════
        ore_nodes: List[OreNode] = []

        dom_sym       = profile.dominant_metal_element.symbol
        dom_rw        = profile.dominant_metal_element.rarity_weight
        dom_ore_names = _get_ore_names(dom_sym)

        # base_yield = profile.base_resource_density × dominance_ratio × rarity_weight_scale
        # rarity_weight_scale: heavier elements have less total crustal tonnage
        dom_rw_scale  = dom_rw / 1000.0
        dom_base_yield = round(
            profile.base_resource_density * dominance_ratio * dom_rw_scale * 10.0, 4
        )

        dom_purity_ent  = stream.next_unit()  # W5
        dom_purity      = _purity_from_pressure(pressure_index, dom_purity_ent)
        dom_vc_ent      = stream.next_uint16()  # W6
        dom_vein_count_base = 1 + int(tectonic_activity * 9)
        dom_jitter_range    = max(1, int(dom_vein_count_base * 0.3))
        dom_jitter          = dom_vc_ent % (dom_jitter_range * 2 + 1) - dom_jitter_range
        dom_vein_count      = max(1, dom_vein_count_base + dom_jitter)
        dom_size_ent        = stream.next_unit()  # W7
        dom_vein_size       = round((dom_base_yield / dom_vein_count) * (0.85 + 0.30 * dom_size_ent), 4)
        dom_depth_ent       = stream.next_unit()  # W8
        dom_depth           = _depth_from_pressure(pressure_index, dom_depth_ent)
        dom_reserve         = _reserve_quantity(dom_vein_size, dom_base_yield, dom_purity)
        dom_rarity_score    = _rarity_score(dom_rw)

        ore_nodes.append(OreNode(
            element_symbol   = dom_sym,
            ore_name         = dom_ore_names[0],
            purity           = dom_purity,
            depth_layer      = dom_depth,
            vein_count       = dom_vein_count,
            vein_size        = dom_vein_size,
            reserve_quantity = dom_reserve,
            base_yield       = dom_base_yield,
            rarity_score     = dom_rarity_score,
        ))

        # ═════════════════════════════════════════════════════════════════
        # STEP 5 — SECONDARY METAL ORE NODE
        # ═════════════════════════════════════════════════════════════════
        sec_sym       = profile.secondary_metal_element.symbol
        sec_rw        = profile.secondary_metal_element.rarity_weight
        sec_ore_names = _get_ore_names(sec_sym)

        sec_rw_scale   = sec_rw / 1000.0
        sec_base_yield = round(
            profile.base_resource_density * (1.0 - dominance_ratio) * sec_rw_scale * 10.0, 4
        )

        sec_purity_ent  = stream.next_unit()  # W9
        sec_purity      = _purity_from_pressure(pressure_index, sec_purity_ent)
        sec_vc_ent      = stream.next_uint16()  # W10
        sec_vein_count_base = 1 + int(tectonic_activity * 9)
        sec_jitter_range    = max(1, int(sec_vein_count_base * 0.3))
        sec_jitter          = sec_vc_ent % (sec_jitter_range * 2 + 1) - sec_jitter_range
        sec_vein_count      = max(1, sec_vein_count_base + sec_jitter)
        sec_size_ent        = stream.next_unit()  # W11
        sec_vein_size       = round((sec_base_yield / max(sec_vein_count, 1)) * (0.85 + 0.30 * sec_size_ent), 4)
        sec_depth_ent       = stream.next_unit()  # W12
        sec_depth           = _depth_from_pressure(pressure_index, sec_depth_ent)
        sec_reserve         = _reserve_quantity(sec_vein_size, sec_base_yield, sec_purity)
        sec_rarity_score    = _rarity_score(sec_rw)

        ore_nodes.append(OreNode(
            element_symbol   = sec_sym,
            ore_name         = sec_ore_names[0],
            purity           = sec_purity,
            depth_layer      = sec_depth,
            vein_count       = sec_vein_count,
            vein_size        = sec_vein_size,
            reserve_quantity = sec_reserve,
            base_yield       = sec_base_yield,
            rarity_score     = sec_rarity_score,
        ))

        # ═════════════════════════════════════════════════════════════════
        # STEP 6 — NON-METAL MINERAL NODE (secondary vein)
        # ═════════════════════════════════════════════════════════════════
        # The dominant non-metal shapes the mineral character of the crust.
        # Not all non-metals have ore forms; for those without, use fallback.
        nm_sym       = profile.dominant_nonmetal_element.symbol
        nm_rw        = profile.dominant_nonmetal_element.rarity_weight
        nm_ore_names = _get_ore_names(nm_sym)

        nm_rw_scale   = nm_rw / 1000.0
        nm_base_yield = round(
            profile.base_resource_density * 0.5 * nm_rw_scale * 10.0, 4
        )
        nm_purity_ent = stream.next_unit()  # W13
        nm_purity     = _purity_from_pressure(pressure_index * 0.7, nm_purity_ent)
        nm_depth_ent  = stream.next_unit()  # W14
        nm_depth      = _depth_from_pressure(pressure_index * 0.6, nm_depth_ent)
        nm_vc_ent     = stream.next_uint16()  # W15
        nm_vein_count = max(1, 1 + (nm_vc_ent % max(1, 1 + int(tectonic_activity * 7))))
        nm_size_ent   = stream.next_unit()  # (extension W0 from second block)
        nm_vein_size  = round((nm_base_yield / nm_vein_count) * (0.80 + 0.35 * nm_size_ent), 4)
        nm_reserve    = _reserve_quantity(nm_vein_size, nm_base_yield, nm_purity)
        nm_rarity     = _rarity_score(nm_rw)

        ore_nodes.append(OreNode(
            element_symbol   = nm_sym,
            ore_name         = nm_ore_names[0] if nm_ore_names != _ORE_NAME_FALLBACK else "Mineral Vein",
            purity           = nm_purity,
            depth_layer      = nm_depth,
            vein_count       = nm_vein_count,
            vein_size        = nm_vein_size,
            reserve_quantity = nm_reserve,
            base_yield       = nm_base_yield,
            rarity_score     = nm_rarity,
        ))

        # ═════════════════════════════════════════════════════════════════
        # STEP 7 — CRYSTAL NODES
        # High pressure + high crystallization_index → more crystals, deeper
        # ═════════════════════════════════════════════════════════════════
        crystal_nodes: List[CrystalNode] = []

        # Crystal count: 1–3, biased upward by crystallization_index + pressure
        crystal_score   = crystallization_idx * 0.6 + pressure_index * 0.4
        n_crystals_base = 1 + int(crystal_score * 2.99)   # → {1, 2, 3}
        n_crystals      = max(1, min(3, n_crystals_base))

        # Use biome list; cycle if fewer biomes than crystal count
        biomes_list = list(profile.biome_affinity)

        for ci in range(n_crystals):
            biome_tag  = biomes_list[ci % len(biomes_list)]

            # Depth: pressure + crystallization push toward deeper layers
            depth_ent  = stream.next_unit()
            crys_depth = _depth_from_pressure(
                (pressure_index + crystallization_idx) / 2.0, depth_ent
            )
            # Crystals must be at least SHALLOW (no surface crystal nodes)
            if crys_depth == "SURFACE":
                crys_depth = "SHALLOW"

            crystal_name, crystal_type, crystal_affinity = _get_crystal(biome_tag, crys_depth)

            quality_ent = stream.next_unit()
            quality     = _crystal_quality_from_indices(
                crystallization_idx, pressure_index, quality_ent
            )

            # mutation_affinity: range depends on affinity type
            ma_lo, ma_hi = _MUTATION_AFFINITY_BY_AFFINITY[crystal_affinity]
            mut_ent      = stream.next_unit()
            mut_aff      = round(ma_lo + mut_ent * (ma_hi - ma_lo), 6)

            crystal_nodes.append(CrystalNode(
                name              = crystal_name,
                crystal_type      = crystal_type,
                crystal_affinity  = crystal_affinity,
                quality           = quality,
                depth_layer       = crys_depth,
                mutation_affinity = mut_aff,
                biome_affinity    = biome_tag,
            ))

        # ═════════════════════════════════════════════════════════════════
        # STEP 8 — ECONOMIC READINESS SCORES
        # ═════════════════════════════════════════════════════════════════
        ore_tuple     = tuple(ore_nodes)
        crystal_tuple = tuple(crystal_nodes)

        industrial_score = _score_industrial(ore_tuple, dominance_ratio)
        strategic_score  = _score_strategic(ore_tuple, crystal_tuple, pressure_index)
        luxury_score     = _score_luxury(ore_tuple, crystal_tuple)
        total_score      = round(industrial_score + strategic_score + luxury_score, 4)

        # ═════════════════════════════════════════════════════════════════
        # STEP 9 — ASSEMBLE FROZEN CATALOG
        # ═════════════════════════════════════════════════════════════════
        return ServerMaterialCatalog(
            server_id                 = profile.server_id,
            ore_nodes                 = ore_tuple,
            crystal_nodes             = crystal_tuple,
            geological_rating         = geo_rating,
            tectonic_activity         = tectonic_activity,
            pressure_index            = pressure_index,
            dominance_ratio           = dominance_ratio,
            industrial_resource_score = industrial_score,
            strategic_resource_score  = strategic_score,
            luxury_resource_score     = luxury_score,
            total_resource_score      = total_score,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — MAIN  —  SIMULATION BLOCK
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR = "═" * 78
_DIV_MINOR = "─" * 78
_DIV_THIN  = "·" * 78


def _build_mock_profile(
    server_id:            int,
    genetic_signature:    str,
    world_age:            str,
    base_world_stability: float,
    base_resource_density:float,
    base_mutation_index:  float,
    biome_affinity:       Tuple[str, ...],
    dom_metal_sym:        str,
    dom_metal_name:       str,
    dom_metal_cat:        str,
    dom_metal_rw:         int,
    dom_metal_an:         int,
    sec_metal_sym:        str,
    sec_metal_name:       str,
    sec_metal_cat:        str,
    sec_metal_rw:         int,
    sec_metal_an:         int,
    dom_nm_sym:           str,
    dom_nm_name:          str,
    dom_nm_cat:           str,
    dom_nm_rw:            int,
    dom_nm_an:            int,
    sec_nm_sym:           str,
    sec_nm_name:          str,
    sec_nm_cat:           str,
    sec_nm_rw:            int,
    sec_nm_an:            int,
) -> ServerGeneticProfile:
    """Construct a synthetic ServerGeneticProfile for __main__ demonstrations."""
    from identitas_genetik import ElementProfile

    def ep(sym, name, cat, rw, an) -> ElementProfile:
        return ElementProfile(
            atomic_number=an, symbol=sym, name=name, category=cat,
            atomic_mass=0.0, period=0, group=None, rarity_weight=rw,
        )

    return ServerGeneticProfile(
        server_id=server_id,
        created_at=0,
        genetic_signature=genetic_signature,
        dominant_metal_element=ep(dom_metal_sym, dom_metal_name, dom_metal_cat, dom_metal_rw, dom_metal_an),
        secondary_metal_element=ep(sec_metal_sym, sec_metal_name, sec_metal_cat, sec_metal_rw, sec_metal_an),
        dominant_nonmetal_element=ep(dom_nm_sym, dom_nm_name, dom_nm_cat, dom_nm_rw, dom_nm_an),
        secondary_nonmetal_element=ep(sec_nm_sym, sec_nm_name, sec_nm_cat, sec_nm_rw, sec_nm_an),
        applied_metal_modifier=dom_metal_sym,
        applied_nonmetal_modifier=None,
        world_age=world_age,
        base_world_stability=base_world_stability,
        base_resource_density=base_resource_density,
        base_mutation_index=base_mutation_index,
        biome_affinity=biome_affinity,
        world_flavour_tags=(),
    )


def _print_catalog(catalog: ServerMaterialCatalog, label: str) -> None:
    """Rich pretty-print of a ServerMaterialCatalog."""
    print()
    print(_DIV_MAJOR)
    print(f"  ◈  {label}")
    print(_DIV_MAJOR)
    print(f"  SERVER ID          : {catalog.server_id}")
    print(f"  GEOLOGICAL RATING  : {catalog.geological_rating}")
    print()

    # ── Macro geological params ───────────────────────────────────────────
    print("  ── GEOLOGICAL MACRO PARAMETERS ────────────────────────────────────")
    print(f"  ▶ TECTONIC ACTIVITY   : {catalog.tectonic_activity:.6f}"
          f"  (0.0=stable craton  → 1.0=hyper-active)")
    print(f"  ▶ PRESSURE INDEX      : {catalog.pressure_index:.6f}"
          f"  (0.0=shallow crust  → 1.0=deep compressed)")
    print(f"  ▶ DOMINANCE RATIO     : {catalog.dominance_ratio:.6f}"
          f"  (dominant metal captures {catalog.dominance_ratio*100:.1f}% of metal yield)")
    print()

    # ── Ore nodes ─────────────────────────────────────────────────────────
    print("  ── ORE NODES (Stratigraphy + Vein Architecture) ───────────────────")
    for i, node in enumerate(catalog.ore_nodes):
        reserve_k = node.reserve_quantity / 1000.0
        print(f"  [{i+1}] {node.element_symbol:<3}  {node.ore_name:<22}"
              f"  {node.purity:<10}  {node.depth_layer:<8}"
              f"  veins={node.vein_count:>2} × {node.vein_size:>7.4f}t"
              f"  reserve={reserve_k:>9.2f}kt"
              f"  rarity={node.rarity_score:.4f}")
    print()

    # ── Crystal nodes ─────────────────────────────────────────────────────
    print("  ── CRYSTAL NODES (Biome × Depth Convergence) ──────────────────────")
    for i, crystal in enumerate(catalog.crystal_nodes):
        print(f"  [{i+1}] {crystal.name:<28}"
              f"  {crystal.crystal_affinity:<10}  {crystal.quality:<10}"
              f"  {crystal.depth_layer:<8}"
              f"  mut_aff={crystal.mutation_affinity:.4f}"
              f"  biome={crystal.biome_affinity}")
    print()

    # ── Economic readiness scores ─────────────────────────────────────────
    print("  ── ECONOMIC READINESS SCORES (Bridge to economy.py) ───────────────")
    ind = catalog.industrial_resource_score
    strat = catalog.strategic_resource_score
    lux = catalog.luxury_resource_score
    total = catalog.total_resource_score

    bar_max = 60
    def bar(val: float, cap: float = 2000.0) -> str:
        filled = int((val / cap) * bar_max)
        return "█" * min(filled, bar_max) + "░" * max(0, bar_max - min(filled, bar_max))

    print(f"  ▶ INDUSTRIAL  : {ind:>10.4f}  │{bar(ind)}")
    print(f"  ▶ STRATEGIC   : {strat:>10.4f}  │{bar(strat)}")
    print(f"  ▶ LUXURY      : {lux:>10.4f}  │{bar(lux)}")
    print(f"  {'─'*20}")
    print(f"  ▶ TOTAL       : {total:>10.4f}  ← economy.py base")

    # Verify invariant
    computed = round(ind + strat + lux, 4)
    invariant_ok = abs(computed - total) < 0.001
    print(f"\n  INVARIANT CHECK (industrial + strategic + luxury == total):")
    print(f"    {ind:.4f} + {strat:.4f} + {lux:.4f} = {computed:.4f}")
    print(f"    Stored total  = {total:.4f}")
    print(f"    {'✓ MATCH' if invariant_ok else '✗ MISMATCH — BUG!'}")
    print()

    # ── Downstream contract ───────────────────────────────────────────────
    print("  DOWNSTREAM CONTRACT (economy.py applies runtime modifiers on top):")
    print(f"    current_industrial = {ind:.4f} + trade_bonus + tariff_modifier")
    print(f"    current_strategic  = {strat:.4f} × political_risk_factor")
    print(f"    current_luxury     = {lux:.4f} + fashion_modifier + season_modifier")
    print(_DIV_MINOR)


if __name__ == "__main__":

    engine   = MaterialEngine()
    g_engine = GeneticEngine()

    print()
    print(f"  {'╔' + '═' * 74 + '╗'}")
    print(f"  ║{'MATERIAL_GEN.PY  v1.0  —  Geological Simulation':^74}║")
    print(f"  {'╚' + '═' * 74 + '╝'}")

    # ─────────────────────────────────────────────────────────────────────
    # SCENARIO A — High-Volatility Uranium World
    #
    # Profile characteristics:
    #   • dom_metal = Uranium (EXOTIC, rarity_weight=4)
    #   • dom_nonmetal = Sulfur (COMMON, modifier: +VOLCANIC, -stability)
    #   • world_age = PRIMORDIAL → pressure nudge = -0.20
    #   • base_mutation_index = 0.85 (near-maximum)
    #   • biomes: IRRADIATED_WASTES | VOLCANIC | NETHER_DEPTHS
    #   • base_resource_density = 0.65 (lean — radioactive worlds are sparse)
    # Expected outcome:
    #   strategic_resource_score >> industrial_resource_score
    #   MUTATION crystals at DEEP/ABYSS layers
    #   Low reserves (lean crust), high mutation_affinity
    # ─────────────────────────────────────────────────────────────────────

    # We need a real genetic_signature to seed the entropy stream.
    # Use the actual genetic engine to create a profile, then override
    # element/biome fields for the scenario.  The signature must be a real
    # SHA-256 hex string so the entropy stream initialises correctly.

    uranium_sig = hashlib.sha256(b"uranium_world_scenario_v1").hexdigest()
    uranium_profile = _build_mock_profile(
        server_id             = 111000111000111001,
        genetic_signature     = uranium_sig,
        world_age             = "PRIMORDIAL",
        base_world_stability  = 0.22,
        base_resource_density = 0.65,
        base_mutation_index   = 0.85,
        biome_affinity        = ("IRRADIATED_WASTES", "VOLCANIC", "NETHER_DEPTHS"),
        dom_metal_sym         = "U",   dom_metal_name="Uranium",  dom_metal_cat="actinide",         dom_metal_rw=4,    dom_metal_an=92,
        sec_metal_sym         = "Th",  sec_metal_name="Thorium",  sec_metal_cat="actinide",         sec_metal_rw=5,    sec_metal_an=90,
        dom_nm_sym            = "S",   dom_nm_name="Sulfur",      dom_nm_cat="reactive nonmetal",   dom_nm_rw=600,     dom_nm_an=16,
        sec_nm_sym            = "F",   sec_nm_name="Fluorine",    sec_nm_cat="reactive nonmetal",   sec_nm_rw=350,     sec_nm_an=9,
    )

    uranium_catalog = engine.generate_geology(uranium_profile)
    _print_catalog(uranium_catalog, "SCENARIO A — High-Volatility URANIUM World (PRIMORDIAL)")

    # ─────────────────────────────────────────────────────────────────────
    # SCENARIO B — Highly Stable Iron World
    #
    # Profile characteristics:
    #   • dom_metal = Iron (ABUNDANT, rarity_weight=1000)
    #   • dom_nonmetal = Carbon (ABUNDANT, modifier: +ANCIENT_FOREST, +density)
    #   • world_age = ANCIENT → pressure nudge = +0.25
    #   • base_mutation_index = 0.08 (near-minimum)
    #   • biomes: ANCIENT_FOREST | SCORCHED_PLAINS | CRYSTAL_CAVERN
    #   • base_resource_density = 1.85 (rich — Iron worlds are productive)
    # Expected outcome:
    #   industrial_resource_score >> strategic_resource_score
    #   Flawless/Enriched purity (high pressure + ANCIENT age)
    #   Mega-veins (low tectonic → many tonnes per vein)
    #   High reserves
    # ─────────────────────────────────────────────────────────────────────

    iron_sig = hashlib.sha256(b"iron_world_scenario_v1").hexdigest()
    iron_profile = _build_mock_profile(
        server_id             = 222000222000222002,
        genetic_signature     = iron_sig,
        world_age             = "ANCIENT",
        base_world_stability  = 0.91,
        base_resource_density = 1.85,
        base_mutation_index   = 0.08,
        biome_affinity        = ("ANCIENT_FOREST", "SCORCHED_PLAINS", "CRYSTAL_CAVERN"),
        dom_metal_sym         = "Fe",  dom_metal_name="Iron",     dom_metal_cat="transition metal",      dom_metal_rw=1000, dom_metal_an=26,
        sec_metal_sym         = "Cu",  sec_metal_name="Copper",   sec_metal_cat="transition metal",      sec_metal_rw=500,  sec_metal_an=29,
        dom_nm_sym            = "C",   dom_nm_name="Carbon",      dom_nm_cat="reactive nonmetal",        dom_nm_rw=800,     dom_nm_an=6,
        sec_nm_sym            = "Si",  sec_nm_name="Silicon",     sec_nm_cat="metalloid",                sec_nm_rw=850,     sec_nm_an=14,
    )

    iron_catalog = engine.generate_geology(iron_profile)
    _print_catalog(iron_catalog, "SCENARIO B — Highly Stable IRON World (ANCIENT)")

    # ─────────────────────────────────────────────────────────────────────
    # SCENARIO C — Real profile via live GeneticEngine (integration test)
    # Uses "Neon Spire" from identitas_genetik test vectors (Fe dominant)
    # ─────────────────────────────────────────────────────────────────────

    neon_spire_profile = g_engine.generate_profile(
        server_id  = 100000000000000001,
        created_at = 1577836800,
    )
    neon_spire_catalog = engine.generate_geology(neon_spire_profile)
    _print_catalog(neon_spire_catalog, "SCENARIO C — LIVE PROFILE: Neon Spire (100000000000000001)")

    # ─────────────────────────────────────────────────────────────────────
    # IDEMPOTENCY CHECK
    # ─────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  IDEMPOTENCY CHECK — same profile → same catalog, every time")
    print(_DIV_MINOR)

    all_ok = True
    for label, profile in [("Uranium World", uranium_profile),
                            ("Iron World",    iron_profile),
                            ("Neon Spire",    neon_spire_profile)]:
        c1 = engine.generate_geology(profile)
        c2 = engine.generate_geology(profile)
        checks = [
            c1.geological_rating         == c2.geological_rating,
            c1.tectonic_activity         == c2.tectonic_activity,
            c1.pressure_index            == c2.pressure_index,
            c1.dominance_ratio           == c2.dominance_ratio,
            c1.industrial_resource_score == c2.industrial_resource_score,
            c1.strategic_resource_score  == c2.strategic_resource_score,
            c1.luxury_resource_score     == c2.luxury_resource_score,
            c1.total_resource_score      == c2.total_resource_score,
            len(c1.ore_nodes)            == len(c2.ore_nodes),
            len(c1.crystal_nodes)        == len(c2.crystal_nodes),
            c1.ore_nodes                 == c2.ore_nodes,
            c1.crystal_nodes             == c2.crystal_nodes,
        ]
        ok = all(checks)
        all_ok = all_ok and ok
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {label}")

    print()
    if all_ok:
        print("  ✓ ALL IDEMPOTENCY CHECKS PASSED — material generation is fully deterministic.")
    else:
        print("  ✗ IDEMPOTENCY FAILURE — review entropy stream logic immediately.")
    print()

    # ─────────────────────────────────────────────────────────────────────
    # INVARIANT CHECK — total == industrial + strategic + luxury
    # ─────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  SCORE INVARIANT CHECK  (total_score == industrial + strategic + luxury)")
    print(_DIV_MINOR)
    for label, catalog in [("Uranium", uranium_catalog),
                            ("Iron",    iron_catalog),
                            ("Neon Spire", neon_spire_catalog)]:
        computed = round(
            catalog.industrial_resource_score
            + catalog.strategic_resource_score
            + catalog.luxury_resource_score, 4
        )
        ok = abs(computed - catalog.total_resource_score) < 0.001
        print(f"  {'✓' if ok else '✗'}  {label:<12}  "
              f"ind={catalog.industrial_resource_score:.2f}  "
              f"str={catalog.strategic_resource_score:.2f}  "
              f"lux={catalog.luxury_resource_score:.2f}  "
              f"sum={computed:.4f}  stored={catalog.total_resource_score:.4f}")
    print()

    # ─────────────────────────────────────────────────────────────────────
    # IMMUTABILITY CHECK
    # ─────────────────────────────────────────────────────────────────────
    print(_DIV_MINOR)
    print("  IMMUTABILITY CHECK — catalog must be frozen")
    print(_DIV_MINOR)
    try:
        iron_catalog.total_resource_score = 999.0   # type: ignore
        print("  ✗ FAIL — catalog was mutated!")
    except (AttributeError, TypeError) as e:
        print(f"  ✓ PASS — Frozen dataclass: {type(e).__name__}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # SCENARIO COMPARISON — Uranium vs Iron (prove differentiation)
    # ─────────────────────────────────────────────────────────────────────
    print()
    print(_DIV_MAJOR)
    print("  SCENARIO COMPARISON — Uranium vs Iron Economic Differentiation")
    print(_DIV_MAJOR)
    print(f"  {'Metric':<35}  {'Uranium World':>16}  {'Iron World':>16}")
    print(_DIV_MINOR)

    metrics = [
        ("geological_rating",         uranium_catalog.geological_rating,         iron_catalog.geological_rating),
        ("tectonic_activity",          f"{uranium_catalog.tectonic_activity:.4f}", f"{iron_catalog.tectonic_activity:.4f}"),
        ("pressure_index",             f"{uranium_catalog.pressure_index:.4f}",    f"{iron_catalog.pressure_index:.4f}"),
        ("dominance_ratio",            f"{uranium_catalog.dominance_ratio:.4f}",   f"{iron_catalog.dominance_ratio:.4f}"),
        ("dom ore purity",             uranium_catalog.ore_nodes[0].purity,        iron_catalog.ore_nodes[0].purity),
        ("dom ore depth",              uranium_catalog.ore_nodes[0].depth_layer,   iron_catalog.ore_nodes[0].depth_layer),
        ("dom ore reserve (kt)",       f"{uranium_catalog.ore_nodes[0].reserve_quantity/1000:.2f}kt", f"{iron_catalog.ore_nodes[0].reserve_quantity/1000:.2f}kt"),
        ("crystal count",              str(len(uranium_catalog.crystal_nodes)),    str(len(iron_catalog.crystal_nodes))),
        ("industrial_resource_score",  f"{uranium_catalog.industrial_resource_score:.2f}",  f"{iron_catalog.industrial_resource_score:.2f}"),
        ("strategic_resource_score",   f"{uranium_catalog.strategic_resource_score:.2f}",   f"{iron_catalog.strategic_resource_score:.2f}"),
        ("luxury_resource_score",      f"{uranium_catalog.luxury_resource_score:.2f}",      f"{iron_catalog.luxury_resource_score:.2f}"),
        ("total_resource_score",       f"{uranium_catalog.total_resource_score:.2f}",       f"{iron_catalog.total_resource_score:.2f}"),
    ]

    for name, u_val, i_val in metrics:
        print(f"  {name:<35}  {str(u_val):>16}  {str(i_val):>16}")

    print()
    print("  INTERPRETATION:")
    u_strat_pct = 100.0 * uranium_catalog.strategic_resource_score / max(uranium_catalog.total_resource_score, 0.01)
    i_ind_pct   = 100.0 * iron_catalog.industrial_resource_score   / max(iron_catalog.total_resource_score,   0.01)
    print(f"  Uranium World: strategic score is {u_strat_pct:.1f}% of total"
          f" → economy.py tags this as a HIGH-RISK, HIGH-REWARD server.")
    print(f"  Iron World   : industrial score is {i_ind_pct:.1f}% of total"
          f" → economy.py tags this as a STABLE PRODUCTION economy.")
    print()
    print(_DIV_MAJOR)