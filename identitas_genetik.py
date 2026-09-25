"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           IDENTITAS GENETIK.PY  —  Digital Genetics Engine  v2.0           ║
║           Foundational Block #1 of the Procedural World System             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REFACTOR SUMMARY (v1 → v2)                                                ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  FIXED: Index-bias element selection  → Cumulative weight search           ║
║  FIXED: Siloed/independent generation → Cascade interdependence pipeline   ║
║  FIXED: Missing macro DNA             → world_age, world_stability,        ║
║                                          resource_density added             ║
║  NEW  : rarity_weight on Element      → drives weighted RNG                ║
║  NEW  : ElementModifier mapping       → elements inject biome/scalar mods  ║
║  NEW  : Immutable profile (frozen)    → safe for downstream consumers      ║
╚══════════════════════════════════════════════════════════════════════════════╝

PURPOSE
-------
Given a Discord Server ID and its Unix creation timestamp, deterministically
derive a "Genetic Profile" that governs world generation (biomes, materials,
mob abundances, economy baseline) for that server — forever, on any machine.

DESIGN PRINCIPLES
-----------------
• Absolute Determinism    — The Stage-1 world seed (world_seed.py) is the sole
                            source of entropy, read through the Stage-2 stream
                            stream(seed, "genetic", i) (world_stream.py).
                            No platform-specific RNG is touched.
                            The `random` module is never imported.
• Entropy Slicing         — The 64-character hex digest is split into 10
                            non-overlapping 6-character windows (each yielding
                            a 24-bit uint) plus 2 residual chars as tie-breaker
                            salt.  Each window drives exactly one output field.
• Cumulative Weight Search— Element selection uses a deterministic CWS instead
                            of modulo-indexing, so rarity_weight controls the
                            true probability of each element appearing.
• Cascade Pipeline        — Elements are generated first (Phase 1), their
                            ElementModifiers mutate a scratch-pad of biome
                            weights and scalar nudges (Phase 2), then biomes
                            and scalars are finalised (Phase 3).
• Architectural Separation— ServerGeneticProfile is frozen after creation.
                            Downstream modules (economy.py, fauna_gen.py, etc.)
                            MUST layer runtime deltas on top; they must NEVER
                            mutate the profile object.

ENTROPY SLICE MAP  (SHA-256 hex digest → fields)
─────────────────────────────────────────────────────────────────────────────
  The 64-char hex string is partitioned into 10 × 6-char windows (60 chars)
  with the final 4 chars reserved as auxiliary salt.

  Slice  0  [ 0: 6]  →  dominant_metal_element   (Phase 1)
  Slice  1  [ 6:12]  →  dominant_nonmetal_element (Phase 1)
  Slice  2  [12:18]  →  secondary_metal_element   (Phase 1)
  Slice  3  [18:24]  →  secondary_nonmetal_element(Phase 1)
  Slice  4  [24:30]  →  world_age                 (Phase 3)
  Slice  5  [30:36]  →  base_world_stability       (Phase 3, pre-modifier)
  Slice  6  [36:42]  →  base_resource_density      (Phase 3, pre-modifier)
  Slice  7  [42:48]  →  base_mutation_index        (Phase 3, pre-modifier)
  Slice  8  [48:54]  →  biome count + primary biome(Phase 3)
  Slice  9  [54:60]  →  secondary + tertiary biome (Phase 3)
  Salt      [60:64]  →  reserved (tie-breaking, future use)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, FrozenSet, List, Optional, Tuple

from world_stream import DOMAIN_GENETIC, stream, unit, seed_fingerprint


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — PERIODIC TABLE ELEMENT DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Element:
    """
    Immutable representation of a periodic-table element.

    rarity_weight
    ─────────────
    Controls the probability that this element is selected as a world dominant.
    Weights are relative; the engine uses Cumulative Weight Search (CWS) so
    only the ratio between weights matters, not their absolute values.

    Tiers (game-design intent):
        ABUNDANT  (700-1000) — Common crustal elements; Iron, Aluminium, etc.
        COMMON    (300-699)  — Widely available; Copper, Zinc, Phosphorus, etc.
        UNCOMMON  (100-299)  — Moderate scarcity; Cobalt, Tin, Titanium, etc.
        RARE      ( 10- 99)  — Precious / strategic; Gold, Platinum, REEs, etc.
        EXOTIC    (  2-  9)  — Radioactive / synthetic; Uranium, Thorium, etc.
        LEGENDARY (  1)      — Theoretical / ultra-synthetic; Oganesson, etc.
    """
    atomic_number: int
    symbol:        str
    name:          str
    category:      str        # IUPAC classification string
    atomic_mass:   float      # unified atomic mass units (u)
    period:        int
    group:         Optional[int]   # None for lanthanides / actinides
    rarity_weight: int        # Cumulative Weight Search probability driver


# ── Metals ───────────────────────────────────────────────────────────────────
#    Sorted by atomic number for stable, auditable ordering.
# ─────────────────────────────────────────────────────────────────────────────

_METAL_ELEMENTS: List[Element] = [
    # ── Alkali Metals ─────────────────────────────────────────────────────
    Element(3,   "Li", "Lithium",       "alkali metal",          6.941,   2,  1,  400),
    Element(11,  "Na", "Sodium",        "alkali metal",          22.990,  3,  1,  700),
    Element(19,  "K",  "Potassium",     "alkali metal",          39.098,  4,  1,  650),
    Element(37,  "Rb", "Rubidium",      "alkali metal",          85.468,  5,  1,   60),
    Element(55,  "Cs", "Caesium",       "alkali metal",          132.905, 6,  1,   25),
    Element(87,  "Fr", "Francium",      "alkali metal",          223.0,   7,  1,    2),

    # ── Alkaline Earth Metals ──────────────────────────────────────────────
    Element(4,   "Be", "Beryllium",     "alkaline earth metal",  9.012,   2,  2,  200),
    Element(12,  "Mg", "Magnesium",     "alkaline earth metal",  24.305,  3,  2,  750),
    Element(20,  "Ca", "Calcium",       "alkaline earth metal",  40.078,  4,  2,  800),
    Element(38,  "Sr", "Strontium",     "alkaline earth metal",  87.62,   5,  2,  150),
    Element(56,  "Ba", "Barium",        "alkaline earth metal",  137.327, 6,  2,  120),
    Element(88,  "Ra", "Radium",        "alkaline earth metal",  226.0,   7,  2,    3),

    # ── Transition Metals ─────────────────────────────────────────────────
    Element(21,  "Sc", "Scandium",      "transition metal",      44.956,  4,  3,   80),
    Element(22,  "Ti", "Titanium",      "transition metal",      47.867,  4,  4,  250),
    Element(23,  "V",  "Vanadium",      "transition metal",      50.942,  4,  5,  180),
    Element(24,  "Cr", "Chromium",      "transition metal",      51.996,  4,  6,  350),
    Element(25,  "Mn", "Manganese",     "transition metal",      54.938,  4,  7,  420),
    Element(26,  "Fe", "Iron",          "transition metal",      55.845,  4,  8, 1000),  # Most abundant crustal metal
    Element(27,  "Co", "Cobalt",        "transition metal",      58.933,  4,  9,  180),
    Element(28,  "Ni", "Nickel",        "transition metal",      58.693,  4, 10,  300),
    Element(29,  "Cu", "Copper",        "transition metal",      63.546,  4, 11,  500),
    Element(30,  "Zn", "Zinc",          "transition metal",      65.38,   4, 12,  400),
    Element(39,  "Y",  "Yttrium",       "transition metal",      88.906,  5,  3,   70),
    Element(40,  "Zr", "Zirconium",     "transition metal",      91.224,  5,  4,  160),
    Element(41,  "Nb", "Niobium",       "transition metal",      92.906,  5,  5,   90),
    Element(42,  "Mo", "Molybdenum",    "transition metal",      95.96,   5,  6,  130),
    Element(43,  "Tc", "Technetium",    "transition metal",      98.0,    5,  7,    4),  # Radioactive / no stable isotope
    Element(44,  "Ru", "Ruthenium",     "transition metal",      101.07,  5,  8,   20),
    Element(45,  "Rh", "Rhodium",       "transition metal",      102.906, 5,  9,   12),
    Element(46,  "Pd", "Palladium",     "transition metal",      106.42,  5, 10,   15),
    Element(47,  "Ag", "Silver",        "transition metal",      107.868, 5, 11,   35),
    Element(48,  "Cd", "Cadmium",       "transition metal",      112.411, 5, 12,  110),
    Element(72,  "Hf", "Hafnium",       "transition metal",      178.49,  6,  4,   80),
    Element(73,  "Ta", "Tantalum",      "transition metal",      180.948, 6,  5,   50),
    Element(74,  "W",  "Tungsten",      "transition metal",      183.84,  6,  6,  100),
    Element(75,  "Re", "Rhenium",       "transition metal",      186.207, 6,  7,   10),
    Element(76,  "Os", "Osmium",        "transition metal",      190.23,  6,  8,    8),
    Element(77,  "Ir", "Iridium",       "transition metal",      192.217, 6,  9,    8),
    Element(78,  "Pt", "Platinum",      "transition metal",      195.084, 6, 10,   18),
    Element(79,  "Au", "Gold",          "transition metal",      196.967, 6, 11,   22),
    Element(80,  "Hg", "Mercury",       "transition metal",      200.592, 6, 12,   40),
    Element(104, "Rf", "Rutherfordium", "transition metal",      267.0,   7,  4,    2),
    Element(105, "Db", "Dubnium",       "transition metal",      268.0,   7,  5,    2),
    Element(106, "Sg", "Seaborgium",    "transition metal",      271.0,   7,  6,    2),
    Element(107, "Bh", "Bohrium",       "transition metal",      272.0,   7,  7,    2),
    Element(108, "Hs", "Hassium",       "transition metal",      277.0,   7,  8,    2),
    Element(109, "Mt", "Meitnerium",    "transition metal",      276.0,   7,  9,    2),
    Element(110, "Ds", "Darmstadtium",  "transition metal",      281.0,   7, 10,    2),
    Element(111, "Rg", "Roentgenium",   "transition metal",      280.0,   7, 11,    2),
    Element(112, "Cn", "Copernicium",   "transition metal",      285.0,   7, 12,    2),

    # ── Post-Transition Metals ────────────────────────────────────────────
    Element(13,  "Al", "Aluminium",     "post-transition metal", 26.982,  3, 13,  900),  # Most abundant metal in crust
    Element(31,  "Ga", "Gallium",       "post-transition metal", 69.723,  4, 13,   60),
    Element(49,  "In", "Indium",        "post-transition metal", 114.818, 5, 13,   45),
    Element(50,  "Sn", "Tin",           "post-transition metal", 118.71,  5, 14,  170),
    Element(81,  "Tl", "Thallium",      "post-transition metal", 204.38,  6, 13,   30),
    Element(82,  "Pb", "Lead",          "post-transition metal", 207.2,   6, 14,  200),
    Element(83,  "Bi", "Bismuth",       "post-transition metal", 208.980, 6, 15,   55),
    Element(113, "Nh", "Nihonium",      "post-transition metal", 286.0,   7, 13,    1),
    Element(114, "Fl", "Flerovium",     "post-transition metal", 289.0,   7, 14,    1),
    Element(115, "Mc", "Moscovium",     "post-transition metal", 290.0,   7, 15,    1),
    Element(116, "Lv", "Livermorium",   "post-transition metal", 293.0,   7, 16,    1),

    # ── Lanthanides ───────────────────────────────────────────────────────
    Element(57,  "La", "Lanthanum",     "lanthanide",            138.905, 6, None, 60),
    Element(58,  "Ce", "Cerium",        "lanthanide",            140.116, 6, None, 70),
    Element(59,  "Pr", "Praseodymium",  "lanthanide",            140.908, 6, None, 45),
    Element(60,  "Nd", "Neodymium",     "lanthanide",            144.242, 6, None, 55),
    Element(61,  "Pm", "Promethium",    "lanthanide",            145.0,   6, None,  3),  # Radioactive
    Element(62,  "Sm", "Samarium",      "lanthanide",            150.36,  6, None, 40),
    Element(63,  "Eu", "Europium",      "lanthanide",            151.964, 6, None, 25),
    Element(64,  "Gd", "Gadolinium",    "lanthanide",            157.25,  6, None, 35),
    Element(65,  "Tb", "Terbium",       "lanthanide",            158.925, 6, None, 20),
    Element(66,  "Dy", "Dysprosium",    "lanthanide",            162.500, 6, None, 30),
    Element(67,  "Ho", "Holmium",       "lanthanide",            164.930, 6, None, 18),
    Element(68,  "Er", "Erbium",        "lanthanide",            167.259, 6, None, 22),
    Element(69,  "Tm", "Thulium",       "lanthanide",            168.934, 6, None, 12),
    Element(70,  "Yb", "Ytterbium",     "lanthanide",            173.045, 6, None, 28),
    Element(71,  "Lu", "Lutetium",      "lanthanide",            174.967, 6, None, 15),

    # ── Actinides ─────────────────────────────────────────────────────────
    Element(89,  "Ac", "Actinium",      "actinide",              227.0,   7, None,  3),
    Element(90,  "Th", "Thorium",       "actinide",              232.038, 7, None,  5),  # Radioactive, fertile
    Element(91,  "Pa", "Protactinium",  "actinide",              231.036, 7, None,  2),
    Element(92,  "U",  "Uranium",       "actinide",              238.029, 7, None,  4),  # Radioactive, fissile
    Element(93,  "Np", "Neptunium",     "actinide",              237.0,   7, None,  2),
    Element(94,  "Pu", "Plutonium",     "actinide",              244.0,   7, None,  2),
    Element(95,  "Am", "Americium",     "actinide",              243.0,   7, None,  2),
    Element(96,  "Cm", "Curium",        "actinide",              247.0,   7, None,  2),
    Element(97,  "Bk", "Berkelium",     "actinide",              247.0,   7, None,  1),
    Element(98,  "Cf", "Californium",   "actinide",              251.0,   7, None,  1),
    Element(99,  "Es", "Einsteinium",   "actinide",              252.0,   7, None,  1),
    Element(100, "Fm", "Fermium",       "actinide",              257.0,   7, None,  1),
    Element(101, "Md", "Mendelevium",   "actinide",              258.0,   7, None,  1),
    Element(102, "No", "Nobelium",      "actinide",              259.0,   7, None,  1),
    Element(103, "Lr", "Lawrencium",    "actinide",              266.0,   7, None,  1),
]

# ── Non-Metals ────────────────────────────────────────────────────────────────

_NONMETAL_ELEMENTS: List[Element] = [
    # ── Reactive Non-Metals ───────────────────────────────────────────────
    Element(1,   "H",  "Hydrogen",      "reactive nonmetal",     1.008,   1,  1,  950),  # Most abundant element
    Element(6,   "C",  "Carbon",        "reactive nonmetal",     12.011,  2, 14,  800),
    Element(7,   "N",  "Nitrogen",      "reactive nonmetal",     14.007,  2, 15,  700),
    Element(8,   "O",  "Oxygen",        "reactive nonmetal",     15.999,  2, 16,  900),
    Element(9,   "F",  "Fluorine",      "reactive nonmetal",     18.998,  2, 17,  350),
    Element(15,  "P",  "Phosphorus",    "reactive nonmetal",     30.974,  3, 15,  500),
    Element(16,  "S",  "Sulfur",        "reactive nonmetal",     32.06,   3, 16,  600),
    Element(17,  "Cl", "Chlorine",      "reactive nonmetal",     35.45,   3, 17,  550),
    Element(34,  "Se", "Selenium",      "reactive nonmetal",     78.971,  4, 16,  150),
    Element(35,  "Br", "Bromine",       "reactive nonmetal",     79.904,  4, 17,  200),
    Element(53,  "I",  "Iodine",        "reactive nonmetal",     126.904, 5, 17,  120),
    Element(85,  "At", "Astatine",      "reactive nonmetal",     210.0,   6, 17,    4),  # Radioactive

    # ── Metalloids ────────────────────────────────────────────────────────
    Element(5,   "B",  "Boron",         "metalloid",             10.81,   2, 13,  300),
    Element(14,  "Si", "Silicon",       "metalloid",             28.085,  3, 14,  850),  # 2nd most abundant crustal
    Element(32,  "Ge", "Germanium",     "metalloid",             72.630,  4, 14,  130),
    Element(33,  "As", "Arsenic",       "metalloid",             74.922,  4, 15,  250),
    Element(51,  "Sb", "Antimony",      "metalloid",             121.760, 5, 15,  100),
    Element(52,  "Te", "Tellurium",     "metalloid",             127.60,  5, 16,   80),
    Element(84,  "Po", "Polonium",      "metalloid",             209.0,   6, 16,    3),  # Highly radioactive

    # ── Noble Gases ───────────────────────────────────────────────────────
    Element(2,   "He", "Helium",        "noble gas",             4.003,   1, 18,  400),
    Element(10,  "Ne", "Neon",          "noble gas",             20.180,  2, 18,  200),
    Element(18,  "Ar", "Argon",         "noble gas",             39.948,  3, 18,  350),
    Element(36,  "Kr", "Krypton",       "noble gas",             83.798,  4, 18,   70),
    Element(54,  "Xe", "Xenon",         "noble gas",             131.293, 5, 18,   40),
    Element(86,  "Rn", "Radon",         "noble gas",             222.0,   6, 18,    5),  # Radioactive
    Element(118, "Og", "Oganesson",     "noble gas",             294.0,   7, 18,    1),  # Theoretical
]


# ── Category classification sets ──────────────────────────────────────────────

_METAL_CATEGORIES: FrozenSet[str] = frozenset({
    "alkali metal",
    "alkaline earth metal",
    "transition metal",
    "post-transition metal",
    "lanthanide",
    "actinide",
})

_NONMETAL_CATEGORIES: FrozenSet[str] = frozenset({
    "reactive nonmetal",
    "metalloid",
    "noble gas",
})

# Build O(1) lookup by atomic number
_ELEMENT_BY_ATOMIC: Dict[int, Element] = {
    e.atomic_number: e
    for e in _METAL_ELEMENTS + _NONMETAL_ELEMENTS
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — BIOME REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

# Canonical biome tag list — index positions are stable; never reorder.
_BIOME_TAGS: Tuple[str, ...] = (
    "VOLCANIC",            # 0
    "DEEP_OCEAN",          # 1
    "CRYSTAL_CAVERN",      # 2
    "ANCIENT_FOREST",      # 3
    "IRRADIATED_WASTES",   # 4
    "FROZEN_TUNDRA",       # 5
    "DESERT_DUNES",        # 6
    "SWAMP_MARSHLAND",     # 7
    "FLOATING_ISLANDS",    # 8
    "ABYSSAL_TRENCH",      # 9
    "STORM_HIGHLANDS",     # 10
    "MUSHROOM_GROVE",      # 11
    "CORRUPTED_RUINS",     # 12
    "CELESTIAL_PLATEAU",   # 13
    "SCORCHED_PLAINS",     # 14
    "NETHER_DEPTHS",       # 15
)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — ELEMENT MODIFIER MAPPING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ElementModifier:
    """
    Static world-shaping rules injected by a dominant element.

    All fields are *deltas* or *weight-boosts* applied to a mutable scratch-pad
    inside the cascade pipeline.  They are never stored in the final profile.

    biome_weight_boosts
        Dict mapping biome tag → additive integer weight boost.
        These are added to the base weight (100) of that biome, making it
        more likely to be selected in Phase 3.

    mutation_index_nudge
        Float delta in [-1.0, +1.0] added to the raw mutation scalar before
        final clamping.  Positive = more mutation, negative = more stable.

    world_stability_nudge
        Float delta in [-1.0, +1.0] added to the raw stability scalar.
        Positive = more stable world, negative = more chaotic.

    resource_density_nudge
        Float delta in [-2.0, +2.0] added to the raw resource density scalar.
        Positive = richer deposits, negative = leaner world.

    flavour_tags
        Informational strings consumed by fauna_gen.py / material_gen.py to
        gate special material unlocks or creature archetypes.
    """
    biome_weight_boosts:    Dict[str, int]
    mutation_index_nudge:   float
    world_stability_nudge:  float
    resource_density_nudge: float
    flavour_tags:           Tuple[str, ...]


# ── Element → World Modifier mapping ─────────────────────────────────────────
#
# Only elements that warrant a distinct world signature are listed.
# Elements absent from this dict produce no modifier (zero deltas).
#
_ELEMENT_MODIFIERS: Dict[str, ElementModifier] = {

    # ── Iron (Fe) — The Foundation Metal ─────────────────────────────────
    "Fe": ElementModifier(
        biome_weight_boosts={"SCORCHED_PLAINS": 300, "STORM_HIGHLANDS": 200,
                              "ANCIENT_FOREST": 100},
        mutation_index_nudge=-0.05,
        world_stability_nudge=+0.15,
        resource_density_nudge=+0.30,
        flavour_tags=("IRON_AGE", "FORGE_WORLD", "HEAVY_INDUSTRY"),
    ),

    # ── Copper (Cu) — Conductor World ─────────────────────────────────────
    "Cu": ElementModifier(
        biome_weight_boosts={"ANCIENT_FOREST": 200, "SWAMP_MARSHLAND": 150,
                              "CRYSTAL_CAVERN": 100},
        mutation_index_nudge=+0.00,
        world_stability_nudge=+0.10,
        resource_density_nudge=+0.20,
        flavour_tags=("BRONZE_AGE", "CONDUCTOR_WORLD"),
    ),

    # ── Aluminium (Al) — Light World ──────────────────────────────────────
    "Al": ElementModifier(
        biome_weight_boosts={"FLOATING_ISLANDS": 400, "CELESTIAL_PLATEAU": 250,
                              "STORM_HIGHLANDS": 200},
        mutation_index_nudge=-0.03,
        world_stability_nudge=+0.08,
        resource_density_nudge=+0.15,
        flavour_tags=("AERIAL_WORLD", "LIGHT_ALLOY"),
    ),

    # ── Gold (Au) — The Wealth Attractor ──────────────────────────────────
    "Au": ElementModifier(
        biome_weight_boosts={"DESERT_DUNES": 350, "CRYSTAL_CAVERN": 300,
                              "CELESTIAL_PLATEAU": 150},
        mutation_index_nudge=-0.10,
        world_stability_nudge=+0.20,
        resource_density_nudge=+0.50,    # Rich but rare world
        flavour_tags=("GILDED_WORLD", "MERCHANT_HAVEN", "HIGH_VALUE"),
    ),

    # ── Platinum (Pt) — Catalyst World ────────────────────────────────────
    "Pt": ElementModifier(
        biome_weight_boosts={"CRYSTAL_CAVERN": 300, "CELESTIAL_PLATEAU": 200,
                              "DEEP_OCEAN": 100},
        mutation_index_nudge=-0.08,
        world_stability_nudge=+0.18,
        resource_density_nudge=+0.40,
        flavour_tags=("CATALYST_WORLD", "PRECISION_INDUSTRY"),
    ),

    # ── Uranium (U) — Irradiated Hellscape ────────────────────────────────
    "U": ElementModifier(
        biome_weight_boosts={"IRRADIATED_WASTES": 600, "NETHER_DEPTHS": 300,
                              "CORRUPTED_RUINS": 200, "SCORCHED_PLAINS": 100},
        mutation_index_nudge=+0.45,      # Forces high mutation
        world_stability_nudge=-0.35,
        resource_density_nudge=-0.20,
        flavour_tags=("IRRADIATED", "MUTATION_HOTSPOT", "FISSION_WORLD"),
    ),

    # ── Thorium (Th) — The Fertile Reactor ────────────────────────────────
    "Th": ElementModifier(
        biome_weight_boosts={"IRRADIATED_WASTES": 350, "NETHER_DEPTHS": 200,
                              "VOLCANIC": 150},
        mutation_index_nudge=+0.25,
        world_stability_nudge=-0.20,
        resource_density_nudge=+0.10,
        flavour_tags=("IRRADIATED", "FERTILE_REACTOR", "SLOW_BURN"),
    ),

    # ── Plutonium (Pu) — The Weapon World ─────────────────────────────────
    "Pu": ElementModifier(
        biome_weight_boosts={"IRRADIATED_WASTES": 700, "CORRUPTED_RUINS": 400,
                              "NETHER_DEPTHS": 200},
        mutation_index_nudge=+0.55,
        world_stability_nudge=-0.50,
        resource_density_nudge=-0.30,
        flavour_tags=("IRRADIATED", "EXTINCTION_RISK", "WEAPONS_GRADE"),
    ),

    # ── Sulfur (S) — The Volcanic Nonmetal ────────────────────────────────
    "S": ElementModifier(
        biome_weight_boosts={"VOLCANIC": 500, "SCORCHED_PLAINS": 300,
                              "NETHER_DEPTHS": 250, "SWAMP_MARSHLAND": 150},
        mutation_index_nudge=+0.12,
        world_stability_nudge=-0.22,
        resource_density_nudge=+0.05,
        flavour_tags=("SULFUROUS", "VOLCANIC_ACTIVITY", "ACID_RAIN"),
    ),

    # ── Carbon (C) — The Life Element ─────────────────────────────────────
    "C": ElementModifier(
        biome_weight_boosts={"ANCIENT_FOREST": 400, "SWAMP_MARSHLAND": 300,
                              "MUSHROOM_GROVE": 250, "CRYSTAL_CAVERN": 100},
        mutation_index_nudge=+0.05,
        world_stability_nudge=+0.05,
        resource_density_nudge=+0.25,
        flavour_tags=("CARBON_RICH", "BIOSPHERE_PRIME", "LIFE_SEED"),
    ),

    # ── Nitrogen (N) — The Atmosphere Builder ─────────────────────────────
    "N": ElementModifier(
        biome_weight_boosts={"ANCIENT_FOREST": 300, "STORM_HIGHLANDS": 200,
                              "FLOATING_ISLANDS": 150, "CELESTIAL_PLATEAU": 100},
        mutation_index_nudge=-0.02,
        world_stability_nudge=+0.12,
        resource_density_nudge=+0.10,
        flavour_tags=("FERTILE_ATMOSPHERE", "NITROGEN_CYCLE"),
    ),

    # ── Oxygen (O) — Oxidiser World ───────────────────────────────────────
    "O": ElementModifier(
        biome_weight_boosts={"ANCIENT_FOREST": 350, "DEEP_OCEAN": 300,
                              "STORM_HIGHLANDS": 200},
        mutation_index_nudge=-0.05,
        world_stability_nudge=+0.10,
        resource_density_nudge=+0.20,
        flavour_tags=("OXIDISER_WORLD", "BREATHABLE"),
    ),

    # ── Silicon (Si) — The Crystal Architect ──────────────────────────────
    "Si": ElementModifier(
        biome_weight_boosts={"CRYSTAL_CAVERN": 450, "DESERT_DUNES": 300,
                              "FLOATING_ISLANDS": 200},
        mutation_index_nudge=-0.04,
        world_stability_nudge=+0.08,
        resource_density_nudge=+0.15,
        flavour_tags=("SILICON_WORLD", "CRYSTAL_LATTICE", "TECH_SEED"),
    ),

    # ── Hydrogen (H) — The Primordial ─────────────────────────────────────
    "H": ElementModifier(
        biome_weight_boosts={"DEEP_OCEAN": 350, "FLOATING_ISLANDS": 200,
                              "STORM_HIGHLANDS": 150},
        mutation_index_nudge=+0.08,
        world_stability_nudge=-0.08,
        resource_density_nudge=+0.05,
        flavour_tags=("PRIMORDIAL_SEA", "HYDROGEN_RICH"),
    ),

    # ── Fluorine (F) — Corrosive World ────────────────────────────────────
    "F": ElementModifier(
        biome_weight_boosts={"CORRUPTED_RUINS": 400, "NETHER_DEPTHS": 250,
                              "CRYSTAL_CAVERN": 150},
        mutation_index_nudge=+0.20,
        world_stability_nudge=-0.28,
        resource_density_nudge=-0.10,
        flavour_tags=("CORROSIVE_ATMOSPHERE", "ACID_WORLD"),
    ),

    # ── Phosphorus (P) — Bioluminescent World ─────────────────────────────
    "P": ElementModifier(
        biome_weight_boosts={"MUSHROOM_GROVE": 400, "SWAMP_MARSHLAND": 300,
                              "ANCIENT_FOREST": 200},
        mutation_index_nudge=+0.08,
        world_stability_nudge=+0.00,
        resource_density_nudge=+0.20,
        flavour_tags=("BIOLUMINESCENT", "PHOSPHORESCENT", "GROWTH_WORLD"),
    ),

    # ── Argon (Ar) — The Inert World ──────────────────────────────────────
    "Ar": ElementModifier(
        biome_weight_boosts={"FROZEN_TUNDRA": 300, "DESERT_DUNES": 200,
                              "CELESTIAL_PLATEAU": 150},
        mutation_index_nudge=-0.15,
        world_stability_nudge=+0.25,
        resource_density_nudge=-0.10,
        flavour_tags=("INERT_ATMOSPHERE", "STABLE_WORLD", "LOW_REACTIVITY"),
    ),

    # ── Radon (Rn) — The Creeping Hazard ─────────────────────────────────
    "Rn": ElementModifier(
        biome_weight_boosts={"IRRADIATED_WASTES": 400, "NETHER_DEPTHS": 300,
                              "ABYSSAL_TRENCH": 200},
        mutation_index_nudge=+0.30,
        world_stability_nudge=-0.25,
        resource_density_nudge=-0.15,
        flavour_tags=("IRRADIATED", "RADON_SEEP", "SLOW_POISON"),
    ),

    # ── Polonium (Po) — Extreme Toxicity ─────────────────────────────────
    "Po": ElementModifier(
        biome_weight_boosts={"IRRADIATED_WASTES": 500, "CORRUPTED_RUINS": 350,
                              "NETHER_DEPTHS": 150},
        mutation_index_nudge=+0.38,
        world_stability_nudge=-0.40,
        resource_density_nudge=-0.25,
        flavour_tags=("IRRADIATED", "TOXIC_WORLD", "ALPHA_EMITTER"),
    ),

    # ── Arsenic (As) — The Poisoner ───────────────────────────────────────
    "As": ElementModifier(
        biome_weight_boosts={"SWAMP_MARSHLAND": 350, "CORRUPTED_RUINS": 300,
                              "MUSHROOM_GROVE": 150},
        mutation_index_nudge=+0.18,
        world_stability_nudge=-0.15,
        resource_density_nudge=+0.00,
        flavour_tags=("TOXIC_WORLD", "POISONED_BIOSPHERE"),
    ),

    # ── Mercury (Hg) — The Liquid Metal ───────────────────────────────────
    "Hg": ElementModifier(
        biome_weight_boosts={"DEEP_OCEAN": 300, "ABYSSAL_TRENCH": 300,
                              "SWAMP_MARSHLAND": 200},
        mutation_index_nudge=+0.15,
        world_stability_nudge=-0.18,
        resource_density_nudge=+0.05,
        flavour_tags=("MERCURY_SEAS", "TOXIC_WORLD", "DENSE_WORLD"),
    ),

    # ── Titanium (Ti) — The Endurance World ───────────────────────────────
    "Ti": ElementModifier(
        biome_weight_boosts={"STORM_HIGHLANDS": 350, "CRYSTAL_CAVERN": 200,
                              "FLOATING_ISLANDS": 150},
        mutation_index_nudge=-0.08,
        world_stability_nudge=+0.20,
        resource_density_nudge=+0.25,
        flavour_tags=("TITANIUM_ALLOY", "HIGH_STRENGTH", "ENDURANCE_WORLD"),
    ),

    # ── Helium (He) — The Gas World ───────────────────────────────────────
    "He": ElementModifier(
        biome_weight_boosts={"FLOATING_ISLANDS": 450, "CELESTIAL_PLATEAU": 300,
                              "STORM_HIGHLANDS": 100},
        mutation_index_nudge=-0.10,
        world_stability_nudge=+0.05,
        resource_density_nudge=-0.05,
        flavour_tags=("GAS_GIANT_ESQUE", "BUOYANT_WORLD"),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — WORLD AGE ENUM
# ─────────────────────────────────────────────────────────────────────────────

_WORLD_AGE_TIERS: Tuple[str, ...] = (
    "PRIMORDIAL",   # 0 → 0.33  — Fresh, volatile, pre-biosphere
    "MATURE",       # 0.33 → 0.66 — Stable, life-bearing era
    "ANCIENT",      # 0.66 → 1.0  — Old, post-peak, resource-depleted surface
)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — OUTPUT DATACLASSES (IMMUTABLE CONTRACT)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ElementProfile:
    """
    Serialisable, immutable snapshot of a selected periodic-table element.
    Downstream modules consume this; they must not reconstruct Element objects.
    """
    atomic_number: int
    symbol:        str
    name:          str
    category:      str
    atomic_mass:   float
    period:        int
    group:         Optional[int]
    rarity_weight: int          # Exposed so material_gen.py can weight yields


def _element_to_profile(e: Element) -> ElementProfile:
    """Convert an internal Element to the downstream-safe ElementProfile."""
    return ElementProfile(
        atomic_number=e.atomic_number,
        symbol=e.symbol,
        name=e.name,
        category=e.category,
        atomic_mass=e.atomic_mass,
        period=e.period,
        group=e.group,
        rarity_weight=e.rarity_weight,
    )


@dataclass(frozen=True)
class ServerGeneticProfile:
    """
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  THE IMMUTABLE GENETIC BASELINE — DO NOT MUTATE AFTER CREATION     ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║  This dataclass is the READ-ONLY genetic contract between the       ║
    ║  Genetic Engine and all downstream simulation modules.              ║
    ║                                                                      ║
    ║  Downstream usage contract:                                          ║
    ║  ─────────────────────────                                           ║
    ║  economy.py    : current_stability = base_world_stability            ║
    ║                                    + war_modifier                    ║
    ║                                    + market_modifier                 ║
    ║  fauna_gen.py  : spawn_rate = base_resource_density * biome_factor   ║
    ║  material_gen  : yield = base_resource_density * element.rarity_wt  ║
    ║                                                                      ║
    ║  ALL fields prefixed `base_` are genetic immutables.                ║
    ╚══════════════════════════════════════════════════════════════════════╝

    Entropy Slice Map   (slice i = stream(seed, "genetic", i), 256-bit)
    ─────────────────────────────────────────────────────────────────────
    Slice 0  → dominant_metal_element        (Phase 1 – CWS)
    Slice 1  → dominant_nonmetal_element      (Phase 1 – CWS)
    Slice 2  → secondary_metal_element        (Phase 1 – CWS)
    Slice 3  → secondary_nonmetal_element     (Phase 1 – CWS)
    Slice 4  → world_age                      (Phase 3)
    Slice 5  → base_world_stability            (Phase 3, mod-adjusted)
    Slice 6  → base_resource_density           (Phase 3, mod-adjusted)
    Slice 7  → base_mutation_index             (Phase 3, mod-adjusted)
    Slice 8  → biome count (high 128 bit) + primary (low 128 bit)
    Slice 9  → biome secondary (high 128 bit) + tertiary (low 128 bit)
    ─────────────────────────────────────────────────────────────────────
    """

    # ── Identity ──────────────────────────────────────────────────────────
    server_id:          int
    created_at:         int      # Informational only — does NOT influence the world
    genetic_signature:  str      # PUBLIC fingerprint of the seed (world_stream.seed_fingerprint)

    # ── Phase 1: Dominant Elements ────────────────────────────────────────
    dominant_metal_element:    ElementProfile
    secondary_metal_element:   ElementProfile
    dominant_nonmetal_element: ElementProfile
    secondary_nonmetal_element: ElementProfile

    # ── Phase 2 (informational): Applied modifier symbols ─────────────────
    # Which element symbols had modifiers that were applied in the cascade.
    # Useful for downstream modules to know *why* biomes look the way they do.
    applied_metal_modifier:    Optional[str]   # symbol or None
    applied_nonmetal_modifier: Optional[str]   # symbol or None

    # ── Phase 3: Macro DNA (World Characteristics) ────────────────────────
    world_age:             str    # "PRIMORDIAL" | "MATURE" | "ANCIENT"
    base_world_stability:  float  # [0.0, 1.0]  — ecosystem/market baseline
    base_resource_density: float  # [0.1, 2.0]  — yield multiplier baseline
    base_mutation_index:   float  # [0.0, 1.0]  — genetic drift baseline
    biome_affinity:        Tuple[str, ...]  # 1–3 unique biome tags

    # ── Informational flavour tags (merged from applied modifiers) ─────────
    world_flavour_tags: Tuple[str, ...]   # e.g. ("IRON_AGE", "VOLCANIC_ACTIVITY")

    # ─────────────────────────────────────────────────────────────────────
    # Serialisation helpers
    # ─────────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a fully JSON-serialisable plain dict."""
        d = asdict(self)
        # Convert tuples to lists for JSON compatibility
        d["biome_affinity"]     = list(d["biome_affinity"])
        d["world_flavour_tags"] = list(d["world_flavour_tags"])
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — GENETIC ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class GeneticEngine:
    """
    Deterministic, cascade-based world-seed generator.

    Architecture: Three-Phase Cascade
    ──────────────────────────────────
    Phase 1 — Elements First
        Pick dominant & secondary metals/non-metals using Cumulative Weight
        Search so rarity_weight actually controls selection probability.

    Phase 2 — Element Modifiers (the Cascade Effect)
        The dominant metal and non-metal symbols are looked up in
        _ELEMENT_MODIFIERS.  Any matching modifier injects biome weight
        boosts and scalar nudges into a mutable scratch-pad.

    Phase 3 — Biome & Scalar Finalisation
        Biomes are selected using the modifier-adjusted weight table via CWS.
        Scalar fields (stability, density, mutation) are computed from their
        entropy slices then clamped after the modifier nudges are applied.

    No external state is touched.  No `random` module is used.
    Every output is purely a function of the Stage-1 world seed.
    """

    # ── Entropy configuration ─────────────────────────────────────────────
    # Slice i = stream(seed, DOMAIN_GENETIC, i).  The stream is unbounded;
    # new fields take new indices >= _N_SLICES and never shift existing ones.
    _N_SLICES: int  = 10
    _HALF_BITS: int = 128                    # biome slices are split in two halves
    _HALF_MASK: int = (1 << 128) - 1

    # ── Slice index constants ─────────────────────────────────────────────
    _S_DOM_METAL    = 0
    _S_DOM_NONMETAL = 1
    _S_SEC_METAL    = 2
    _S_SEC_NONMETAL = 3
    _S_WORLD_AGE    = 4
    _S_STABILITY    = 5
    _S_DENSITY      = 6
    _S_MUTATION     = 7
    _S_BIOME_A      = 8    # biome count + primary
    _S_BIOME_B      = 9    # secondary + tertiary offsets

    # ── Canonical pools (frozen at class level for re-use safety) ─────────
    _METALS:    Tuple[Element, ...] = tuple(
        sorted(_METAL_ELEMENTS,    key=lambda e: e.atomic_number)
    )
    _NONMETALS: Tuple[Element, ...] = tuple(
        sorted(_NONMETAL_ELEMENTS, key=lambda e: e.atomic_number)
    )
    _BIOMES:    Tuple[str, ...]     = _BIOME_TAGS

    # ── Pre-computed cumulative weight tables ─────────────────────────────
    # Format: list of (cumulative_weight, element) for fast CWS
    _METAL_CW:    Tuple[Tuple[int, Element], ...] = ()
    _NONMETAL_CW: Tuple[Tuple[int, Element], ...] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

    def __init__(self) -> None:
        # Build cumulative weight tables once per instance
        self._METAL_CW    = self._build_cumulative_table(self._METALS)
        self._NONMETAL_CW = self._build_cumulative_table(self._NONMETALS)

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────

    def generate_profile(
        self,
        server_id:  int,
        seed:       bytes,
        created_at: int = 0,
    ) -> ServerGeneticProfile:
        """
        Derive an immutable ServerGeneticProfile from the Stage-1 *seed*
        using the three-phase cascade pipeline.

        Parameters
        ----------
        server_id  : Discord snowflake ID (identity only; not an entropy input —
                     it already sits inside the seed pre-image).
        seed       : 32-byte Stage-1 world seed (world_seed.derive_seed).
        created_at : Informational Unix timestamp; does NOT affect the world.

        Returns
        -------
        ServerGeneticProfile — fully deterministic, frozen, cross-platform.
        """
        # ── Stage-2 stream slices ─────────────────────────────────────────
        slices = [stream(seed, DOMAIN_GENETIC, i) for i in range(self._N_SLICES)]

        # ═════════════════════════════════════════════════════════════════
        # PHASE 1 — ELEMENT SELECTION (Cumulative Weight Search)
        # ═════════════════════════════════════════════════════════════════
        dom_metal    = self._cws_element(slices[self._S_DOM_METAL],    self._METAL_CW)
        dom_nonmetal = self._cws_element(slices[self._S_DOM_NONMETAL], self._NONMETAL_CW)
        sec_metal    = self._cws_element(slices[self._S_SEC_METAL],    self._METAL_CW,
                                         exclude_symbol=dom_metal.symbol)
        sec_nonmetal = self._cws_element(slices[self._S_SEC_NONMETAL], self._NONMETAL_CW,
                                         exclude_symbol=dom_nonmetal.symbol)

        # ═════════════════════════════════════════════════════════════════
        # PHASE 2 — ELEMENT MODIFIER CASCADE (inject into scratch-pad)
        # ═════════════════════════════════════════════════════════════════
        # Start with uniform biome weights; each biome has equal base weight.
        biome_weights: Dict[str, int] = {tag: 100 for tag in self._BIOMES}

        mutation_nudge   = 0.0
        stability_nudge  = 0.0
        density_nudge    = 0.0
        flavour_tags: List[str] = []

        applied_metal_mod:    Optional[str] = None
        applied_nonmetal_mod: Optional[str] = None

        # Dominant metal modifier
        metal_mod = _ELEMENT_MODIFIERS.get(dom_metal.symbol)
        if metal_mod is not None:
            applied_metal_mod = dom_metal.symbol
            for biome_tag, boost in metal_mod.biome_weight_boosts.items():
                if biome_tag in biome_weights:
                    biome_weights[biome_tag] += boost
            mutation_nudge  += metal_mod.mutation_index_nudge
            stability_nudge += metal_mod.world_stability_nudge
            density_nudge   += metal_mod.resource_density_nudge
            flavour_tags.extend(metal_mod.flavour_tags)

        # Dominant non-metal modifier
        nonmetal_mod = _ELEMENT_MODIFIERS.get(dom_nonmetal.symbol)
        if nonmetal_mod is not None:
            applied_nonmetal_mod = dom_nonmetal.symbol
            for biome_tag, boost in nonmetal_mod.biome_weight_boosts.items():
                if biome_tag in biome_weights:
                    biome_weights[biome_tag] += boost
            mutation_nudge  += nonmetal_mod.mutation_index_nudge
            stability_nudge += nonmetal_mod.world_stability_nudge
            density_nudge   += nonmetal_mod.resource_density_nudge
            flavour_tags.extend(tag for tag in nonmetal_mod.flavour_tags
                                 if tag not in flavour_tags)

        # ═════════════════════════════════════════════════════════════════
        # PHASE 3 — BIOME & SCALAR FINALISATION
        # ═════════════════════════════════════════════════════════════════

        # ── World age ─────────────────────────────────────────────────────
        world_age = self._derive_world_age(slices[self._S_WORLD_AGE])

        # ── Scalars (raw unit interval, then nudge + clamp) ────────────────
        raw_stability = self._to_unit(slices[self._S_STABILITY])
        raw_density   = self._to_unit(slices[self._S_DENSITY])
        raw_mutation  = self._to_unit(slices[self._S_MUTATION])

        base_stability = round(max(0.0, min(1.0, raw_stability + stability_nudge)), 6)
        base_density   = round(max(0.1, min(2.0, 0.1 + raw_density * 1.9 + density_nudge)), 6)
        base_mutation  = round(max(0.0, min(1.0, raw_mutation  + mutation_nudge)),  6)

        # ── Biome selection (weighted CWS over modifier-adjusted table) ───
        biome_cw_table = self._build_biome_cumulative_table(biome_weights)
        biomes = self._derive_biomes(
            slices[self._S_BIOME_A],
            slices[self._S_BIOME_B],
            biome_cw_table,
        )

        # ─────────────────────────────────────────────────────────────────
        # ASSEMBLE IMMUTABLE PROFILE
        # ─────────────────────────────────────────────────────────────────
        return ServerGeneticProfile(
            server_id=server_id,
            created_at=created_at,
            genetic_signature=seed_fingerprint(seed),
            dominant_metal_element=_element_to_profile(dom_metal),
            secondary_metal_element=_element_to_profile(sec_metal),
            dominant_nonmetal_element=_element_to_profile(dom_nonmetal),
            secondary_nonmetal_element=_element_to_profile(sec_nonmetal),
            applied_metal_modifier=applied_metal_mod,
            applied_nonmetal_modifier=applied_nonmetal_mod,
            world_age=world_age,
            base_world_stability=base_stability,
            base_resource_density=base_density,
            base_mutation_index=base_mutation,
            biome_affinity=tuple(biomes),
            world_flavour_tags=tuple(flavour_tags),
        )

    # ─────────────────────────────────────────────────────────────────────
    # INSPECTION HELPERS (stateless utilities for downstream callers)
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_metal(element: Element) -> bool:
        return element.category in _METAL_CATEGORIES

    @staticmethod
    def is_nonmetal(element: Element) -> bool:
        return element.category in _NONMETAL_CATEGORIES

    @staticmethod
    def metal_pool() -> Tuple[Element, ...]:
        """Return the canonical ordered metal pool."""
        return GeneticEngine._METALS

    @staticmethod
    def nonmetal_pool() -> Tuple[Element, ...]:
        """Return the canonical ordered non-metal pool."""
        return GeneticEngine._NONMETALS

    @staticmethod
    def biome_pool() -> Tuple[str, ...]:
        """Return the canonical biome tag pool."""
        return GeneticEngine._BIOMES

    @staticmethod
    def modifier_for(symbol: str) -> Optional[ElementModifier]:
        """Return the ElementModifier for *symbol*, or None if undefined."""
        return _ELEMENT_MODIFIERS.get(symbol)

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE — Entropy extraction
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_unit(h: int) -> float:
        """Map a stream value onto [0.0, 1.0) — world_stream.unit (53-bit)."""
        return unit(h)

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE — Cumulative Weight Search (CWS) engine
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_cumulative_table(
        pool: Tuple[Element, ...],
    ) -> Tuple[Tuple[int, Element], ...]:
        """
        Pre-compute a cumulative weight table for CWS.

        Returns a tuple of (cumulative_weight_ceiling, element) pairs sorted
        in ascending cumulative weight order.  The last entry's ceiling equals
        the total weight of the entire pool.

        Example (3 elements, weights 1000, 500, 10):
            total = 1510
            table = [(1000, Fe), (1500, Cu), (1510, Au)]
        """
        cumulative = 0
        table: List[Tuple[int, Element]] = []
        for element in pool:
            cumulative += element.rarity_weight
            table.append((cumulative, element))
        return tuple(table)

    def _cws_element(
        self,
        h: int,
        cum_table: Tuple[Tuple[int, Element], ...],
        exclude_symbol: Optional[str] = None,
    ) -> Element:
        """
        Deterministic Cumulative Weight Search.

        Maps the stream value *h* onto the total weight range of
        *cum_table*, then performs a linear scan to find the element whose
        cumulative ceiling first exceeds the mapped target.

        If *exclude_symbol* is given and the CWS result matches it, the search
        continues to the next entry in the table (wrapping if needed), ensuring
        dominant ≠ secondary without discarding the entropy.

        Parameters
        ----------
        h               : Stream value (256-bit; modulo bias <= total/2**256).
        cum_table       : Pre-computed cumulative weight table.
        exclude_symbol  : Symbol of an element to skip (for secondary picks).

        Returns
        -------
        Element — exactly one element, deterministically selected.
        """
        total_weight = cum_table[-1][0]
        # Map the stream value to [0, total_weight - 1]
        target   = h % total_weight          # bounded, deterministic

        # Linear CWS scan
        selected: Optional[Element] = None
        for ceiling, element in cum_table:
            if target < ceiling:
                selected = element
                break

        # Fallback safety (should never trigger with a correct table)
        if selected is None:
            selected = cum_table[-1][1]

        # Exclusion: step to next entry if we hit the excluded symbol
        if exclude_symbol is not None and selected.symbol == exclude_symbol:
            # Find this element's position and take the next one (wrapping)
            for i, (_, element) in enumerate(cum_table):
                if element.symbol == selected.symbol:
                    next_idx = (i + 1) % len(cum_table)
                    selected = cum_table[next_idx][1]
                    break

        return selected

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE — World Age
    # ─────────────────────────────────────────────────────────────────────

    def _derive_world_age(self, h: int) -> str:
        """
        Map the slice to one of the three world age tiers.
        Tiers are equally spaced across the [0.0, 1.0] unit interval.
        """
        u    = self._to_unit(h)
        idx  = min(int(u * len(_WORLD_AGE_TIERS)), len(_WORLD_AGE_TIERS) - 1)
        return _WORLD_AGE_TIERS[idx]

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE — Biome selection (modifier-weighted CWS)
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_biome_cumulative_table(
        weights: Dict[str, int],
    ) -> Tuple[Tuple[int, str], ...]:
        """
        Build a cumulative weight table for biome CWS from the modifier-
        adjusted weights dict.  Order follows _BIOME_TAGS for stability.
        """
        cumulative = 0
        table: List[Tuple[int, str]] = []
        for tag in _BIOME_TAGS:
            w = weights.get(tag, 100)
            cumulative += w
            table.append((cumulative, tag))
        return tuple(table)

    def _cws_biome(
        self,
        h:           int,
        cum_table:   Tuple[Tuple[int, str], ...],
        exclude_set: FrozenSet[str],
    ) -> str:
        """
        CWS over biome table, skipping any biome tag already in *exclude_set*.

        If the first hit is excluded, advance forward through the table
        (wrapping) until a non-excluded tag is found.  Because the pool
        always has more entries than the max biome count (3), this always
        terminates.
        """
        total_weight = cum_table[-1][0]
        target       = h % total_weight

        start_idx: int = 0
        for i, (ceiling, _) in enumerate(cum_table):
            if target < ceiling:
                start_idx = i
                break

        # Walk forward until a non-excluded tag is found
        n = len(cum_table)
        for offset in range(n):
            idx = (start_idx + offset) % n
            tag = cum_table[idx][1]
            if tag not in exclude_set:
                return tag

        # Should never happen; pool always exceeds max biome count
        raise RuntimeError("CWS biome search exhausted — pool too small.")

    def _derive_biomes(
        self,
        slice_a:    int,
        slice_b:    int,
        cum_table:  Tuple[Tuple[int, str], ...],
    ) -> List[str]:
        """
        Derive 1–3 unique biome tags using the modifier-weighted CWS table.

        Slice A — high 128 bits drive count {1,2,3}; low 128 bits drive primary.
        Slice B — high 128 bits drive secondary;     low 128 bits drive tertiary.

        Uniqueness is enforced by passing an exclude_set to each subsequent
        CWS call, so no biome can appear twice regardless of weight shape.
        """
        # Split each 256-bit slice into two independent 128-bit halves
        a_count   = slice_a >> self._HALF_BITS    # biome count
        a_primary = slice_a &  self._HALF_MASK    # primary biome
        b_second  = slice_b >> self._HALF_BITS    # secondary biome
        b_third   = slice_b &  self._HALF_MASK    # tertiary biome

        n_biomes = (a_count % 3) + 1                      # → {1, 2, 3}

        primary   = self._cws_biome(a_primary, cum_table, frozenset())
        biomes    = [primary]

        if n_biomes >= 2:
            secondary = self._cws_biome(b_second, cum_table, frozenset(biomes))
            biomes.append(secondary)

        if n_biomes == 3:
            tertiary  = self._cws_biome(b_third,  cum_table, frozenset(biomes))
            biomes.append(tertiary)

        return biomes


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — MAIN — DETERMINISM & CASCADE PROOF
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR = "═" * 74
_DIV_MINOR = "─" * 74
_DIV_THIN  = "·" * 74


def _rarity_tier(weight: int) -> str:
    """Return a human-readable rarity label for a given rarity_weight."""
    if weight >= 700:  return "ABUNDANT "
    if weight >= 300:  return "COMMON   "
    if weight >= 100:  return "UNCOMMON "
    if weight >= 10:   return "RARE     "
    if weight >= 2:    return "EXOTIC   "
    return              "LEGENDARY"


def _print_profile(profile: ServerGeneticProfile, label: str) -> None:
    """Rich pretty-print of a ServerGeneticProfile demonstrating cascade."""

    def elem_line(tag: str, ep: ElementProfile, modifier_applied: bool) -> str:
        rarity = _rarity_tier(ep.rarity_weight)
        mod_flag = "  ◀ CASCADE MODIFIER ACTIVE" if modifier_applied else ""
        return (
            f"  ▶ {tag:<26} [{ep.atomic_number:>3}] {ep.symbol:<2}  "
            f"{ep.name:<16} ({ep.category})\n"
            f"    {'':26}  rarity_weight={ep.rarity_weight:<5}  tier={rarity}{mod_flag}"
        )

    print()
    print(_DIV_MAJOR)
    print(f"  ◈  {label}")
    print(_DIV_MAJOR)
    print(f"  SERVER ID          : {profile.server_id}")
    print(f"  CREATED AT         : {profile.created_at}")
    print(f"  GENETIC SIGNATURE  : {profile.genetic_signature}")
    print()

    # ── Phase 1: Elements ─────────────────────────────────────────────────
    print("  ── PHASE 1: ELEMENT SELECTION (Cumulative Weight Search) ──────")
    print(elem_line("DOMINANT  METAL",
                    profile.dominant_metal_element,
                    profile.applied_metal_modifier == profile.dominant_metal_element.symbol))
    print()
    print(elem_line("SECONDARY METAL",
                    profile.secondary_metal_element, False))
    print()
    print(elem_line("DOMINANT  NON-METAL",
                    profile.dominant_nonmetal_element,
                    profile.applied_nonmetal_modifier == profile.dominant_nonmetal_element.symbol))
    print()
    print(elem_line("SECONDARY NON-METAL",
                    profile.secondary_nonmetal_element, False))
    print()

    # ── Phase 2: Cascade ──────────────────────────────────────────────────
    print("  ── PHASE 2: ELEMENT MODIFIER CASCADE ──────────────────────────")
    if profile.applied_metal_modifier:
        mod = _ELEMENT_MODIFIERS[profile.applied_metal_modifier]
        print(f"  Metal    [{profile.applied_metal_modifier:<2}] injected:")
        for biome, boost in sorted(mod.biome_weight_boosts.items(), key=lambda x: -x[1]):
            print(f"              +{boost:<4} → {biome}")
        print(f"              mutation_nudge={mod.mutation_index_nudge:+.2f}  "
              f"stability_nudge={mod.world_stability_nudge:+.2f}  "
              f"density_nudge={mod.resource_density_nudge:+.2f}")
    else:
        print(f"  Metal    [{profile.dominant_metal_element.symbol:<2}] — no modifier defined")

    if profile.applied_nonmetal_modifier:
        mod = _ELEMENT_MODIFIERS[profile.applied_nonmetal_modifier]
        print(f"  Nonmetal [{profile.applied_nonmetal_modifier:<2}] injected:")
        for biome, boost in sorted(mod.biome_weight_boosts.items(), key=lambda x: -x[1]):
            print(f"              +{boost:<4} → {biome}")
        print(f"              mutation_nudge={mod.mutation_index_nudge:+.2f}  "
              f"stability_nudge={mod.world_stability_nudge:+.2f}  "
              f"density_nudge={mod.resource_density_nudge:+.2f}")
    else:
        print(f"  Nonmetal [{profile.dominant_nonmetal_element.symbol:<2}] — no modifier defined")
    print()

    # ── Phase 3: Macro DNA & Biomes ───────────────────────────────────────
    print("  ── PHASE 3: MACRO DNA & BIOMES (post-cascade) ─────────────────")
    print(f"  ▶ WORLD AGE              : {profile.world_age}")
    print(f"  ▶ BASE WORLD STABILITY   : {profile.base_world_stability:.6f}"
          f"  (0.0=chaotic → 1.0=stable)")
    print(f"  ▶ BASE RESOURCE DENSITY  : {profile.base_resource_density:.6f}"
          f"  (0.1=barren  → 2.0=abundant)")
    print(f"  ▶ BASE MUTATION INDEX    : {profile.base_mutation_index:.6f}"
          f"  (0.0=locked  → 1.0=volatile)")
    print(f"  ▶ BIOME AFFINITY         : {' | '.join(profile.biome_affinity)}")
    print()

    if profile.world_flavour_tags:
        print(f"  ▶ WORLD FLAVOUR TAGS     : {', '.join(profile.world_flavour_tags)}")
    else:
        print(f"  ▶ WORLD FLAVOUR TAGS     : (none — no recognised modifiers active)")
    print(_DIV_MINOR)

    # ── Downstream module contract hint ───────────────────────────────────
    print()
    print("  DOWNSTREAM CONTRACT (example — never modify these base values):")
    print(f"    economy.py   → current_stability = {profile.base_world_stability:.4f} + war_mod + market_mod")
    print(f"    material_gen → base_yield        = {profile.base_resource_density:.4f} × element.rarity_weight")
    print(f"    fauna_gen    → mutation_roll      = {profile.base_mutation_index:.4f} + era_modifier")


if __name__ == "__main__":
    from world_stream import test_seed
    engine = GeneticEngine()

    # ── Test vectors — outputs MUST be identical on every machine ─────────
    TEST_SERVERS = [
        # (label,                   server_id,            created_at,    expected flavour)
        ("Aegis Reach",             1143780000000000001,  1672531200),   # 2023-01-01 00:00 UTC
        ("Iron Veil",               987654321098765432,   1609459200),   # 2021-01-01 00:00 UTC
        ("Neon Spire",              100000000000000001,   1577836800),   # 2020-01-01 00:00 UTC
        ("Uranium Cradle (stress)", 555000000000000555,   1420070400),   # 2015-01-01 00:00 UTC
        ("Sulfur Hollow (stress)",  314159265358979323,   1388534400),   # 2014-01-01 00:00 UTC
    ]

    print()
    print(f"  {'╔' + '═' * 70 + '╗'}")
    print(f"  ║{'IDENTITAS GENETIK.PY  v2.0  —  Cascade Determinism Proof':^70}║")
    print(f"  {'╚' + '═' * 70 + '╝'}")
    print()
    print(f"  Metal pool         : {len(GeneticEngine.metal_pool()):>3} elements"
          f"   (total weight = {sum(e.rarity_weight for e in GeneticEngine.metal_pool()):,})")
    print(f"  Non-metal pool     : {len(GeneticEngine.nonmetal_pool()):>3} elements"
          f"   (total weight = {sum(e.rarity_weight for e in GeneticEngine.nonmetal_pool()):,})")
    print(f"  Biome pool         : {len(GeneticEngine.biome_pool()):>3} biomes")
    print(f"  Modifiers defined  : {len(_ELEMENT_MODIFIERS):>3} elements")
    print()

    profiles: List[ServerGeneticProfile] = []
    for entry in TEST_SERVERS:
        label, sid, cat = entry
        profile = engine.generate_profile(server_id=sid, seed=test_seed(f"{sid}:{cat}"), created_at=cat)
        profiles.append(profile)
        _print_profile(profile, label)
        print()

    # ── Idempotency assertion ─────────────────────────────────────────────
    print(_DIV_MAJOR)
    print("  IDEMPOTENCY CHECK — same inputs must always produce same outputs")
    print(_DIV_MINOR)
    PASS = "✓ PASS"
    FAIL = "✗ FAIL"
    all_pass = True
    for entry in TEST_SERVERS:
        label, sid, cat = entry
        p1 = engine.generate_profile(sid, test_seed(f"{sid}:{cat}"), cat)
        p2 = engine.generate_profile(sid, test_seed(f"{sid}:{cat}"), cat)
        checks = [
            p1.genetic_signature        == p2.genetic_signature,
            p1.dominant_metal_element   == p2.dominant_metal_element,
            p1.dominant_nonmetal_element== p2.dominant_nonmetal_element,
            p1.world_age                == p2.world_age,
            p1.base_world_stability     == p2.base_world_stability,
            p1.base_resource_density    == p2.base_resource_density,
            p1.base_mutation_index      == p2.base_mutation_index,
            p1.biome_affinity           == p2.biome_affinity,
            p1.world_flavour_tags       == p2.world_flavour_tags,
        ]
        ok = all(checks)
        all_pass = all_pass and ok
        print(f"  {PASS if ok else FAIL}  {label}")

    print()
    if all_pass:
        print("  ✓ ALL IDEMPOTENCY CHECKS PASSED — output is fully deterministic.")
    else:
        print("  ✗ IDEMPOTENCY FAILURE — review entropy slice logic immediately.")
    print()

    # ── JSON serialisation round-trip check ───────────────────────────────
    print(_DIV_MINOR)
    print("  JSON ROUND-TRIP CHECK")
    print(_DIV_MINOR)
    sample = profiles[0]
    json_str     = sample.to_json()
    parsed       = json.loads(json_str)
    rt_ok        = (
        parsed["genetic_signature"]   == sample.genetic_signature
        and parsed["world_age"]       == sample.world_age
        and parsed["biome_affinity"]  == list(sample.biome_affinity)
        and parsed["world_flavour_tags"] == list(sample.world_flavour_tags)
    )
    print(f"  {'✓ PASS' if rt_ok else '✗ FAIL'}  to_json() / json.loads() round-trip")
    print()
    print("  Sample JSON output (first server):")
    for line in json_str.splitlines()[:18]:
        print(f"    {line}")
    print("    ...")
    print()
    print(_DIV_MAJOR)