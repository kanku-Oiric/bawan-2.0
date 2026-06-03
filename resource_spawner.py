"""
╔══════════════════════════════════════════════════════════════════════════════╗
║      RESOURCE_SPAWNER.PY  —  Dynamic Resource Instantiation Engine          ║
║      Foundational Block #3 of the Procedural World System                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  UPSTREAM CONTRACT                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Consumes : material_gen.ServerMaterialCatalog    (frozen, read-only)       ║
║  Consumes : identitas_genetik.ServerGeneticProfile (frozen, read-only)      ║
║                                                                              ║
║  DOWNSTREAM CONTRACT                                                         ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  discord_bot reads : ServerSpawnState.active_ores   — channel gating        ║
║  discord_bot reads : ServerSpawnState.active_crystals — role gates          ║
║  economy.py  reads : extract_resource() return value — depletion events     ║
║  persistence reads : ServerSpawnState — serialised to storage layer         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DESIGN PRINCIPLES                                                           ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • Zero `random` Module     — all initial distributions are derived via     ║
║      SHA-256(genetic_signature + b"spawner_salt") hex-slice arithmetic.     ║
║  • Read-Only Upstream       — GeneticProfile and MaterialCatalog are never  ║
║      modified; all runtime state lives in ServerSpawnState exclusively.     ║
║  • State Mutability Contract— ActiveOreNode / ActiveCrystalNode current_    ║
║      reserve fields are intentionally mutable at runtime (depletion).       ║
║  • Deterministic Seeding    — identical catalog inputs always produce the   ║
║      same initial spawn layout on any machine, any run.                     ║
║  • Depth Gating             — SURFACE/SHALLOW nodes → "Public Access";      ║
║      DEEP/ABYSS nodes → "Restricted Access" for Discord role hooks.         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ENTROPY SLICE MAP  (spawner sub-seed hex)                                   ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Sub-seed = SHA-256(genetic_signature.encode() + b"spawner_salt")           ║
║  The 64-char hex digest is sliced in 6-char windows (24-bit uints) to       ║
║  derive per-vein reserve split ratios without any `random` calls.           ║
║  Window overflow uses a deterministic extension chain identical to the      ║
║  pattern established in material_gen._EntropyStream.                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import json
import sys
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ── Upstream module resolution ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from identitas_genetik import ServerGeneticProfile, GeneticEngine
from material_gen import (
    ServerMaterialCatalog,
    OreNode,
    CrystalNode,
    MaterialEngine,
)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

_SPAWNER_SALT: bytes = b"spawner_salt"

# Depth-layer access classification keys (used by Discord bot channel router).
ACCESS_PUBLIC:     str = "Public Access"
ACCESS_RESTRICTED: str = "Restricted Access"

_PUBLIC_DEPTHS:     Tuple[str, ...] = ("SURFACE", "SHALLOW")
_RESTRICTED_DEPTHS: Tuple[str, ...] = ("DEEP",    "ABYSS")

# Regeneration rate scaling bounds (units per tick, pre-tectonic scaling).
_REGEN_BASE_MIN: float = 0.10   # Slowest possible base regeneration
_REGEN_BASE_MAX: float = 2.50   # Fastest possible base regeneration

# Tectonic activity multiplier ceiling — high-activity worlds regen much faster.
# Formula: regen_rate = base_regen * (1.0 + tectonic_activity * _TECTONIC_REGEN_SCALE)
_TECTONIC_REGEN_SCALE: float = 4.0

# Crystal default starting reserve as a fraction of its quality-scaled maximum.
# Crystals do not have reserve_quantity in the catalog — we derive it from quality.
_CRYSTAL_QUALITY_MAX: Dict[str, float] = {
    "Flawed":    100.0,
    "Prismatic": 250.0,
    "Ethereal":  600.0,
}
_CRYSTAL_REGEN_BONUS_BY_AFFINITY: Dict[str, float] = {
    "POWER":    1.20,
    "MANA":     1.50,
    "MUTATION": 0.80,
    "UTILITY":  1.00,
    "DEFENSE":  0.90,
}

# Vein reserve distribution: weight spread used when splitting reserve_quantity
# across individual vein instances. Determines how "uneven" the split is.
_VEIN_WEIGHT_WINDOW: int = 6     # hex chars per vein weight draw (24-bit uint)
_VEIN_WEIGHT_RANGE:  int = 0xFFFFFF  # 16,777,215  (24-bit max)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — SPAWNER ENTROPY ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class _SpawnerEntropy:
    """
    Deterministic entropy source for the spawner, seeded from the genetic
    signature via SHA-256(genetic_signature.encode() + b"spawner_salt").

    Uses 6-char (24-bit) hex windows for higher resolution than material_gen's
    4-char windows, giving finer vein-weight granularity.  Overflow is handled
    identically to material_gen._EntropyStream (extension counter chaining).

    This class is PRIVATE to this module.  Downstream consumers MUST NOT
    access it directly — all deterministic values must be requested through
    ResourceSpawner public methods.
    """

    _WINDOW: int            = 6           # hex chars per window (24-bit uint)
    _MAX_U24: int           = 0xFFFFFF    # 16_777_215
    _WINDOWS_PER_BLOCK: int = 10          # 60 / 6 = 10 full windows per 64-char hash
    #  Note: 64 chars / 6 chars = 10 windows + 4 residual chars (ignored, same as
    #        the identitas_genetik salt residual convention).

    def __init__(self, genetic_signature: str) -> None:
        self._primary: str = hashlib.sha256(
            genetic_signature.encode("utf-8") + _SPAWNER_SALT
        ).hexdigest()
        self._extension_counter: int = 0
        self._cursor: int            = 0    # window index within current block
        self._current_block: str     = self._primary

    # ── Core stream consumption ───────────────────────────────────────────────

    def next_uint24(self) -> int:
        """Consume the next 24-bit unsigned integer from the entropy stream."""
        if self._cursor >= self._WINDOWS_PER_BLOCK:
            self._extension_counter += 1
            ext = hashlib.sha256(
                self._primary.encode("utf-8")
                + self._extension_counter.to_bytes(2, "big")
            ).hexdigest()
            self._current_block = ext
            self._cursor        = 0

        start  = self._cursor * self._WINDOW
        window = self._current_block[start : start + self._WINDOW]
        self._cursor += 1
        return int(window, 16)

    def next_unit(self) -> float:
        """Return a float uniformly distributed in [0.0, 1.0]."""
        return self.next_uint24() / self._MAX_U24

    def next_in_range(self, lo: float, hi: float) -> float:
        """Return a float in [lo, hi]."""
        return lo + self.next_unit() * (hi - lo)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — RUNTIME STATE DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActiveOreNode:
    """
    A single, live, mineable ore vein instance.

    Derived from one OreNode (catalog entry) split across vein_count instances.
    This IS mutable at runtime: current_reserve decreases as players extract,
    and is_depleted flips to True when current_reserve reaches 0.

    node_id format : "<server_id>:<element_symbol>:<ore_name_slug>:<index>"
    access_tier    : "Public Access" or "Restricted Access" (Discord channel gate)
    regeneration_rate: units per tick, scaled by tectonic_activity from catalog.
    """
    node_id:           str
    ore_name:          str
    element_symbol:    str
    depth_layer:       str
    access_tier:       str           # Discord channel gate
    current_reserve:   float
    max_reserve:       float
    regeneration_rate: float         # Scaled by tectonic_activity
    purity:            str           # Inherited from OreNode
    rarity_score:      float         # Inherited from OreNode (for economy.py)
    vein_index:        int           # Which vein instance (0-based) within OreNode
    is_depleted:       bool = False

    # ── Convenience helpers ───────────────────────────────────────────────────

    def reserve_fraction(self) -> float:
        """Return current fill level as [0.0, 1.0]."""
        if self.max_reserve <= 0:
            return 0.0
        return max(0.0, self.current_reserve / self.max_reserve)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActiveCrystalNode:
    """
    A single, live crystal formation instance.

    Crystals in the catalog carry no reserve_quantity (they regenerate on
    geological timescales).  The spawner assigns an initial reserve derived
    deterministically from crystal quality.

    node_id format : "<server_id>:crystal:<name_slug>:<index>"
    """
    node_id:          str
    name:             str
    crystal_type:     str
    crystal_affinity: str
    depth_layer:      str
    access_tier:      str           # Discord channel gate
    current_reserve:  float
    max_reserve:      float
    mutation_affinity: float        # Passed through from CrystalNode
    biome_affinity:   str           # Source biome tag
    quality:          str           # Flawed / Prismatic / Ethereal
    crystal_index:    int           # Ordinal among crystals of same name
    is_depleted:      bool = False

    def reserve_fraction(self) -> float:
        if self.max_reserve <= 0:
            return 0.0
        return max(0.0, self.current_reserve / self.max_reserve)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ServerSpawnState:
    """
    The complete, authoritative runtime state of all mineable nodes for a server.

    This is the ONLY mutable object in the pipeline.  GeneticProfile and
    MaterialCatalog are read-only; all depletion, regeneration, and channel
    assignments live here.

    The persistence layer serialises this object to storage; the Discord bot
    reads access_tier to gate mining commands per channel or role.

    Invariant: sum(n.current_reserve for n in active_ores.values()) ≤
               sum(n.max_reserve for n in active_ores.values())
    """
    server_id:       int
    active_ores:     Dict[str, ActiveOreNode]     # node_id → node
    active_crystals: Dict[str, ActiveCrystalNode] # node_id → node

    # ── Snapshot helpers ──────────────────────────────────────────────────────

    def depleted_ore_count(self) -> int:
        return sum(1 for n in self.active_ores.values() if n.is_depleted)

    def depleted_crystal_count(self) -> int:
        return sum(1 for n in self.active_crystals.values() if n.is_depleted)

    def total_ore_reserve(self) -> float:
        return sum(n.current_reserve for n in self.active_ores.values())

    def total_crystal_reserve(self) -> float:
        return sum(n.current_reserve for n in self.active_crystals.values())

    def public_ore_nodes(self) -> List[ActiveOreNode]:
        return [n for n in self.active_ores.values()
                if n.access_tier == ACCESS_PUBLIC]

    def restricted_ore_nodes(self) -> List[ActiveOreNode]:
        return [n for n in self.active_ores.values()
                if n.access_tier == ACCESS_RESTRICTED]

    def to_dict(self) -> dict:
        return {
            "server_id":       self.server_id,
            "active_ores":     {k: v.to_dict() for k, v in self.active_ores.items()},
            "active_crystals": {k: v.to_dict() for k, v in self.active_crystals.items()},
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _access_tier(depth_layer: str) -> str:
    """
    Map a depth layer string to a Discord channel access tier.

    SURFACE / SHALLOW → "Public Access"
    DEEP    / ABYSS   → "Restricted Access"

    Any unrecognised depth defaults to Restricted as a security-first fallback.
    """
    if depth_layer in _PUBLIC_DEPTHS:
        return ACCESS_PUBLIC
    return ACCESS_RESTRICTED


def _slugify(name: str) -> str:
    """
    Convert a display name to a compact, ID-safe slug.
    Lowercases, replaces spaces/hyphens with underscores, strips non-alnum chars.
    E.g. "Native Gold" → "native_gold"
    """
    slug = name.lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in slug if c.isalnum() or c == "_")


def _split_reserve_across_veins(
    total_reserve: float,
    vein_count:    int,
    entropy:       _SpawnerEntropy,
) -> List[float]:
    """
    Deterministically distribute total_reserve among vein_count instances using
    weighted random splits derived from the entropy stream (no `random` module).

    Algorithm (Weighted Split via Entropy Slicing):
      1. Draw `vein_count` raw 24-bit weights from the entropy stream.
      2. Normalise each weight to a fraction of the total weight sum.
      3. Multiply each fraction by total_reserve to get per-vein amounts.
      4. Apply rounding correction to ensure the sum exactly equals total_reserve.

    This preserves the full reserve_quantity from the catalog invariant while
    producing uneven, realistic vein distributions.
    """
    if vein_count <= 0:
        return []
    if vein_count == 1:
        return [total_reserve]

    raw_weights: List[int] = [entropy.next_uint24() + 1 for _ in range(vein_count)]
    total_weight: int      = sum(raw_weights)

    shares: List[float] = [
        round((w / total_weight) * total_reserve, 6)
        for w in raw_weights
    ]

    # Floating-point correction: nudge the largest share to absorb rounding error
    delta = total_reserve - sum(shares)
    if abs(delta) > 1e-9:
        max_idx = shares.index(max(shares))
        shares[max_idx] = round(shares[max_idx] + delta, 6)

    return shares


def _regeneration_rate(
    entropy:          _SpawnerEntropy,
    tectonic_activity: float,
) -> float:
    """
    Derive a deterministic, tectonic-scaled regeneration rate for one OreNode.

    Formula:
        base_regen = entropy.next_in_range(BASE_MIN, BASE_MAX)
        rate       = base_regen * (1.0 + tectonic_activity * TECTONIC_REGEN_SCALE)

    Meaning:
        A world with tectonic_activity = 0.0 regenerates at the raw base rate.
        A world with tectonic_activity = 1.0 regenerates at 5× the base rate,
        simulating active crustal plate movement continuously pushing ore upward.

    Returns a value rounded to 6 decimal places for deterministic JSON output.
    """
    base_regen = entropy.next_in_range(_REGEN_BASE_MIN, _REGEN_BASE_MAX)
    tectonic_multiplier = 1.0 + tectonic_activity * _TECTONIC_REGEN_SCALE
    return round(base_regen * tectonic_multiplier, 6)


def _crystal_max_reserve(quality: str, crystal_affinity: str) -> float:
    """
    Derive a crystal's maximum reserve from its quality tier and affinity bonus.

    Formula:
        max_reserve = QUALITY_BASE[quality] * AFFINITY_BONUS[affinity]
    """
    base   = _CRYSTAL_QUALITY_MAX.get(quality, 100.0)
    bonus  = _CRYSTAL_REGEN_BONUS_BY_AFFINITY.get(crystal_affinity, 1.0)
    return round(base * bonus, 4)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — RESOURCE SPAWNER ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ResourceSpawner:
    """
    Translates a frozen ServerMaterialCatalog into a live, mutable
    ServerSpawnState containing every mineable node instance.

    Usage:
        spawner = ResourceSpawner()
        state   = spawner.initialise(profile, catalog)

        # Later — mining action:
        extracted = spawner.extract_resource(state, node_id, amount=50.0)

        # Tick-based regen:
        spawner.apply_regeneration_tick(state)

    Thread Safety:
        extract_resource and apply_regeneration_tick are NOT thread-safe by
        design — the Discord bot layer is responsible for per-server locking
        before mutating state.  This keeps the spawner free of threading
        primitives so it can be unit-tested cleanly.
    """

    # ── Initialisation ─────────────────────────────────────────────────────────

    def initialise(
        self,
        profile: ServerGeneticProfile,
        catalog: ServerMaterialCatalog,
    ) -> ServerSpawnState:
        """
        Build the complete ServerSpawnState from a frozen profile + catalog pair.

        This is the only entry-point that reads from the upstream frozen objects.
        All subsequent mutations go through extract_resource() and
        apply_regeneration_tick().

        Returns a fully populated ServerSpawnState ready for runtime use.
        """
        entropy = _SpawnerEntropy(profile.genetic_signature)

        active_ores:     Dict[str, ActiveOreNode]     = {}
        active_crystals: Dict[str, ActiveCrystalNode] = {}

        # ── Expand OreNodes into individual vein instances ────────────────────
        for ore_node in catalog.ore_nodes:
            vein_reserves = _split_reserve_across_veins(
                total_reserve = ore_node.reserve_quantity,
                vein_count    = ore_node.vein_count,
                entropy       = entropy,
            )

            for idx, reserve_amount in enumerate(vein_reserves):
                regen = _regeneration_rate(entropy, catalog.tectonic_activity)

                node_id = (
                    f"{catalog.server_id}"
                    f":{ore_node.element_symbol}"
                    f":{_slugify(ore_node.ore_name)}"
                    f":{idx}"
                )

                node = ActiveOreNode(
                    node_id           = node_id,
                    ore_name          = ore_node.ore_name,
                    element_symbol    = ore_node.element_symbol,
                    depth_layer       = ore_node.depth_layer,
                    access_tier       = _access_tier(ore_node.depth_layer),
                    current_reserve   = reserve_amount,
                    max_reserve       = reserve_amount,
                    regeneration_rate = regen,
                    purity            = ore_node.purity,
                    rarity_score      = ore_node.rarity_score,
                    vein_index        = idx,
                    is_depleted       = False,
                )
                active_ores[node_id] = node

        # ── Instantiate CrystalNodes (each catalog entry → one active node) ───
        # Crystal count is typically small (2–6 per catalog) so each entry maps
        # 1-to-1 without vein expansion — crystals are singular formations.
        # A crystal_index suffix is still added for forward-compat if the same
        # crystal name appears multiple times (duplicate biome-depth combos).
        crystal_name_counters: Dict[str, int] = {}

        for crystal_node in catalog.crystal_nodes:
            slug  = _slugify(crystal_node.name)
            c_idx = crystal_name_counters.get(slug, 0)
            crystal_name_counters[slug] = c_idx + 1

            max_res = _crystal_max_reserve(
                crystal_node.quality,
                crystal_node.crystal_affinity,
            )

            # Crystals start at full reserve (freshly spawned formation).
            node_id = (
                f"{catalog.server_id}"
                f":crystal"
                f":{slug}"
                f":{c_idx}"
            )

            c_node = ActiveCrystalNode(
                node_id           = node_id,
                name              = crystal_node.name,
                crystal_type      = crystal_node.crystal_type,
                crystal_affinity  = crystal_node.crystal_affinity,
                depth_layer       = crystal_node.depth_layer,
                access_tier       = _access_tier(crystal_node.depth_layer),
                current_reserve   = max_res,
                max_reserve       = max_res,
                mutation_affinity = crystal_node.mutation_affinity,
                biome_affinity    = crystal_node.biome_affinity,
                quality           = crystal_node.quality,
                crystal_index     = c_idx,
                is_depleted       = False,
            )
            active_crystals[node_id] = c_node

        return ServerSpawnState(
            server_id       = catalog.server_id,
            active_ores     = active_ores,
            active_crystals = active_crystals,
        )

    # ── Depletion / Extraction ─────────────────────────────────────────────────

    def extract_resource(
        self,
        state:   ServerSpawnState,
        node_id: str,
        amount:  float,
    ) -> float:
        """
        Deduct `amount` from a node's current_reserve and return the actual
        quantity extracted (may be less than requested if near depletion).

        Behaviour:
          • Searches active_ores first, then active_crystals.
          • If the node is already DEPLETED, returns 0.0 immediately.
          • Caps extraction at current_reserve (no negative reserves).
          • Sets is_depleted = True and current_reserve = 0.0 when exhausted.

        Returns:
          float — the actual amount extracted (0.0 ≤ result ≤ amount).

        Raises:
          KeyError — if node_id is not found in either active dict.
          ValueError — if amount is negative.
        """
        if amount < 0:
            raise ValueError(f"Extraction amount must be non-negative, got {amount!r}")

        # ── Locate the node ───────────────────────────────────────────────────
        node: Optional[ActiveOreNode | ActiveCrystalNode] = (
            state.active_ores.get(node_id)
            or state.active_crystals.get(node_id)
        )
        if node is None:
            raise KeyError(
                f"node_id {node_id!r} not found in ServerSpawnState "
                f"(server_id={state.server_id})"
            )

        # ── Guard: already depleted ───────────────────────────────────────────
        if node.is_depleted:
            return 0.0

        # ── Calculate actual extraction ───────────────────────────────────────
        actual = min(amount, node.current_reserve)
        node.current_reserve = round(node.current_reserve - actual, 6)

        # ── Flip depleted flag when reserve hits zero ─────────────────────────
        if node.current_reserve <= 0.0:
            node.current_reserve = 0.0
            node.is_depleted     = True

        return actual

    # ── Regeneration Tick ──────────────────────────────────────────────────────

    def apply_regeneration_tick(
        self,
        state:          ServerSpawnState,
        ore_only:       bool = False,
        crystal_only:   bool = False,
    ) -> Dict[str, float]:
        """
        Advance one regeneration tick for all nodes in the state.

        Only OreNodes carry a regeneration_rate — crystals regenerate via
        a fixed quality-based rate rather than a tectonic one, so crystals
        use a simple quality-fraction-per-tick rule (0.5 % of max_reserve
        per tick when depleted, representing slow geological reformation).

        Parameters:
          ore_only     — if True, skip crystal regeneration this tick
          crystal_only — if True, skip ore regeneration this tick

        Returns a dict of {node_id: amount_regenerated} for logging.

        Note: Nodes at max_reserve are silently skipped (no overfill).
        """
        report: Dict[str, float] = {}

        if not crystal_only:
            for node in state.active_ores.values():
                if node.current_reserve >= node.max_reserve:
                    continue
                gain = min(
                    node.regeneration_rate,
                    node.max_reserve - node.current_reserve,
                )
                gain = round(gain, 6)
                node.current_reserve = round(node.current_reserve + gain, 6)
                if node.is_depleted and node.current_reserve > 0:
                    node.is_depleted = False
                report[node.node_id] = gain

        if not ore_only:
            for node in state.active_crystals.values():
                if node.current_reserve >= node.max_reserve:
                    continue
                # Crystal tick rate: 0.5% of max per tick (slow geological cycle)
                tick_rate = round(node.max_reserve * 0.005, 6)
                gain      = min(tick_rate, node.max_reserve - node.current_reserve)
                gain      = round(gain, 6)
                node.current_reserve = round(node.current_reserve + gain, 6)
                if node.is_depleted and node.current_reserve > 0:
                    node.is_depleted = False
                report[node.node_id] = gain

        return report

    # ── Query Helpers ──────────────────────────────────────────────────────────

    def get_node(
        self,
        state:   ServerSpawnState,
        node_id: str,
    ) -> ActiveOreNode | ActiveCrystalNode:
        """
        Retrieve any active node by ID regardless of type.

        Raises:
          KeyError — if node_id does not exist in either active dict.
        """
        node = state.active_ores.get(node_id) or state.active_crystals.get(node_id)
        if node is None:
            raise KeyError(f"node_id {node_id!r} not found in state (server={state.server_id})")
        return node

    def nodes_by_access_tier(
        self,
        state:  ServerSpawnState,
        tier:   str,
    ) -> Dict[str, ActiveOreNode | ActiveCrystalNode]:
        """
        Return all nodes (ores + crystals) matching a given access tier.
        Used by Discord bot to build per-channel node listings.
        """
        result: Dict[str, ActiveOreNode | ActiveCrystalNode] = {}
        for node_id, node in state.active_ores.items():
            if node.access_tier == tier:
                result[node_id] = node
        for node_id, node in state.active_crystals.items():
            if node.access_tier == tier:
                result[node_id] = node
        return result

    def snapshot_summary(self, state: ServerSpawnState) -> Dict:
        """
        Return a concise summary dict suitable for logging, the Discord status
        command, or persistence-layer heartbeat records.
        """
        total_max_ore  = sum(n.max_reserve     for n in state.active_ores.values())
        total_cur_ore  = sum(n.current_reserve for n in state.active_ores.values())
        total_max_crys = sum(n.max_reserve     for n in state.active_crystals.values())
        total_cur_crys = sum(n.current_reserve for n in state.active_crystals.values())

        return {
            "server_id":            state.server_id,
            "ore_node_count":       len(state.active_ores),
            "crystal_node_count":   len(state.active_crystals),
            "depleted_ore_count":   state.depleted_ore_count(),
            "depleted_crystal_count": state.depleted_crystal_count(),
            "ore_reserve_current":  round(total_cur_ore,  2),
            "ore_reserve_max":      round(total_max_ore,  2),
            "ore_fill_pct":         round(100 * total_cur_ore  / max(total_max_ore,  1), 2),
            "crystal_reserve_current": round(total_cur_crys, 2),
            "crystal_reserve_max":     round(total_max_crys, 2),
            "crystal_fill_pct":        round(100 * total_cur_crys / max(total_max_crys, 1), 2),
            "public_ore_nodes":     len(state.public_ore_nodes()),
            "restricted_ore_nodes": len(state.restricted_ore_nodes()),
        }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — DISPLAY HELPERS (for __main__ and Discord bot integration)
# ─────────────────────────────────────────────────────────────────────────────

_DIV_MAJOR: str = "  " + "═" * 76
_DIV_MINOR: str = "  " + "─" * 76

_DEPLETED_LABEL: str   = "🔴 DEPLETED"
_ACTIVE_LABEL:   str   = "🟢 ACTIVE  "
_LOCKED_LABEL:   str   = "🔒 RESTRICTED"
_OPEN_LABEL:     str   = "🔓 PUBLIC   "

_BAR_WIDTH: int = 30


def _reserve_bar(fraction: float, width: int = _BAR_WIDTH) -> str:
    """Render a simple ASCII reserve bar: [████████████░░░░░░░░] 60.0%"""
    filled   = int(fraction * width)
    empty    = width - filled
    bar      = "█" * filled + "░" * empty
    pct      = fraction * 100
    return f"[{bar}] {pct:5.1f}%"


def _print_ore_node(node: ActiveOreNode, prefix: str = "    ") -> None:
    status  = _DEPLETED_LABEL if node.is_depleted else _ACTIVE_LABEL
    access  = _LOCKED_LABEL   if node.access_tier == ACCESS_RESTRICTED else _OPEN_LABEL
    bar     = _reserve_bar(node.reserve_fraction())
    print(f"{prefix}{status} {access}  ID: {node.node_id}")
    print(f"{prefix}  Ore       : {node.ore_name:<26} Element : {node.element_symbol}")
    print(f"{prefix}  Depth     : {node.depth_layer:<26} Purity  : {node.purity}")
    print(f"{prefix}  Reserve   : {bar}")
    print(f"{prefix}  Current   : {node.current_reserve:>12.4f} / {node.max_reserve:>12.4f} units")
    print(f"{prefix}  Regen/tick: {node.regeneration_rate:<12.6f}   Rarity  : {node.rarity_score:.4f}")


def _print_crystal_node(node: ActiveCrystalNode, prefix: str = "    ") -> None:
    status = _DEPLETED_LABEL if node.is_depleted else _ACTIVE_LABEL
    access = _LOCKED_LABEL   if node.access_tier == ACCESS_RESTRICTED else _OPEN_LABEL
    bar    = _reserve_bar(node.reserve_fraction())
    print(f"{prefix}{status} {access}  ID: {node.node_id}")
    print(f"{prefix}  Crystal   : {node.name:<26} Type    : {node.crystal_type}")
    print(f"{prefix}  Affinity  : {node.crystal_affinity:<26} Quality : {node.quality}")
    print(f"{prefix}  Depth     : {node.depth_layer:<26} Biome   : {node.biome_affinity}")
    print(f"{prefix}  Reserve   : {bar}")
    print(f"{prefix}  Current   : {node.current_reserve:>12.4f} / {node.max_reserve:>12.4f} units")
    print(f"{prefix}  Mutation  : {node.mutation_affinity:.4f}")


def _print_spawn_state_header(state: ServerSpawnState, catalog: ServerMaterialCatalog) -> None:
    print(_DIV_MAJOR)
    print(f"  SERVER SPAWN STATE  —  server_id: {state.server_id}")
    print(f"  Tectonic Activity : {catalog.tectonic_activity:.6f}"
          f"   Geological Rating: {catalog.geological_rating}")
    print(f"  Ore Nodes (total) : {len(state.active_ores)}"
          f"   Crystal Nodes    : {len(state.active_crystals)}")
    print(f"  Public Nodes      : {len(state.public_ore_nodes())}"
          f"   Restricted Nodes : {len(state.restricted_ore_nodes())}")
    print(_DIV_MINOR)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — MOCK BUILDER (for __main__ standalone testing)
# ─────────────────────────────────────────────────────────────────────────────

def _build_mock_catalog(
    server_id: int,
    use_live_engine: bool = True,
) -> Tuple[ServerGeneticProfile, ServerMaterialCatalog]:
    """
    Build a deterministic mock ServerMaterialCatalog by running the full
    upstream pipeline (GeneticEngine → MaterialEngine), using the same test
    vector established in material_gen.py ("Neon Spire").

    If use_live_engine=False, raises NotImplementedError — production code
    should always use the live pipeline.  The flag is provided for future
    unit test injection.
    """
    if not use_live_engine:
        raise NotImplementedError("Manual mock injection not implemented in this build.")

    g_engine  = GeneticEngine()
    m_engine  = MaterialEngine()

    profile   = g_engine.generate_profile(
        server_id  = server_id,
        created_at = 1577836800,   # Neon Spire — 2020-01-01 00:00 UTC
    )
    catalog   = m_engine.generate_geology(profile)
    return profile, catalog


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — __main__ SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print()
    print(f"  {'╔' + '═' * 74 + '╗'}")
    print(f"  ║{'RESOURCE_SPAWNER.PY  —  Dynamic Resource Instantiation Engine':^74}║")
    print(f"  ║{'Block #3 — Procedural World System  |  Simulation Run':^74}║")
    print(f"  {'╚' + '═' * 74 + '╝'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 0 — Pipeline Bootstrap
    # Build the full upstream chain: GeneticProfile → MaterialCatalog
    # Using two contrasting test servers to demonstrate tectonic scaling.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 0 — PIPELINE BOOTSTRAP (GeneticEngine → MaterialEngine)")
    print(_DIV_MINOR)

    g_engine = GeneticEngine()
    m_engine = MaterialEngine()
    spawner  = ResourceSpawner()

    # ── Server A: Neon Spire — moderate tectonic, balanced world ─────────────
    profile_a = g_engine.generate_profile(server_id=100000000000000001, created_at=1577836800)
    catalog_a = m_engine.generate_geology(profile_a)

    # ── Server B: Iron Veil — high-pressure, ancient, stable world ───────────
    import hashlib as _hl
    iron_sig = _hl.sha256(b"iron_world_scenario_v1").hexdigest()
    # Build via a simplified helper that exercises live engine path
    profile_b = g_engine.generate_profile(server_id=987654321098765432, created_at=1609459200)
    catalog_b = m_engine.generate_geology(profile_b)

    print(f"  ✓ Server A — Neon Spire    (ID: {catalog_a.server_id})")
    print(f"      Tectonic Activity : {catalog_a.tectonic_activity:.6f}")
    print(f"      Geological Rating : {catalog_a.geological_rating}")
    print(f"      Ore Node Types    : {len(catalog_a.ore_nodes)}")
    print(f"      Crystal Types     : {len(catalog_a.crystal_nodes)}")
    print()
    print(f"  ✓ Server B — Iron Veil     (ID: {catalog_b.server_id})")
    print(f"      Tectonic Activity : {catalog_b.tectonic_activity:.6f}")
    print(f"      Geological Rating : {catalog_b.geological_rating}")
    print(f"      Ore Node Types    : {len(catalog_b.ore_nodes)}")
    print(f"      Crystal Types     : {len(catalog_b.crystal_nodes)}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 1 — Spawn Initialisation
    # Translate static catalogs into live ServerSpawnState objects.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 1 — SPAWN INITIALISATION")
    print(_DIV_MINOR)

    state_a = spawner.initialise(profile_a, catalog_a)
    state_b = spawner.initialise(profile_b, catalog_b)

    print(f"  ✓ ServerSpawnState created for Neon Spire")
    print(f"      Active Ore Nodes     : {len(state_a.active_ores)}")
    print(f"      Active Crystal Nodes : {len(state_a.active_crystals)}")
    print(f"      Public Access Nodes  : {len(state_a.public_ore_nodes())}")
    print(f"      Restricted Nodes     : {len(state_a.restricted_ore_nodes())}")
    print()
    print(f"  ✓ ServerSpawnState created for Iron Veil")
    print(f"      Active Ore Nodes     : {len(state_b.active_ores)}")
    print(f"      Active Crystal Nodes : {len(state_b.active_crystals)}")
    print(f"      Public Access Nodes  : {len(state_b.public_ore_nodes())}")
    print(f"      Restricted Nodes     : {len(state_b.restricted_ore_nodes())}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 — Full Spawn State Manifest (Neon Spire)
    # Show every node in the spawn state with access tiers.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 2 — FULL SPAWN MANIFEST  (Server A: Neon Spire)")
    _print_spawn_state_header(state_a, catalog_a)

    print("  ── ORE NODES ────────────────────────────────────────────────────")
    for node in state_a.active_ores.values():
        _print_ore_node(node)
        print()

    print("  ── CRYSTAL NODES ────────────────────────────────────────────────")
    for node in state_a.active_crystals.values():
        _print_crystal_node(node)
        print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 3 — Mining Simulation: Deplete a node to DEPLETED state
    # Pick the first ore node and mine it until empty.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 3 — MINING SIMULATION  (Depletion Run on first ore node)")
    print(_DIV_MINOR)

    # Select the first ore node for the simulation
    sim_node_id = next(iter(state_a.active_ores))
    sim_node    = state_a.active_ores[sim_node_id]

    print(f"  Target Node   : {sim_node_id}")
    print(f"  Ore           : {sim_node.ore_name}  [{sim_node.element_symbol}]")
    print(f"  Access Tier   : {sim_node.access_tier}")
    print(f"  Depth Layer   : {sim_node.depth_layer}")
    print(f"  Starting Res  : {sim_node.current_reserve:.4f} units")
    print(_DIV_MINOR)

    MINE_AMOUNT: float = sim_node.max_reserve / 7   # Mine in 7 big chunks
    tick: int = 0
    total_extracted: float = 0.0

    print(f"  {'Tick':<6} {'Action':<30} {'Extracted':>12} {'Remaining':>14} {'Status':<12}")
    print(f"  {'─'*6} {'─'*30} {'─'*12} {'─'*14} {'─'*12}")

    while not sim_node.is_depleted:
        tick += 1
        extracted = spawner.extract_resource(state_a, sim_node_id, MINE_AMOUNT)
        total_extracted += extracted
        status = _DEPLETED_LABEL if sim_node.is_depleted else _ACTIVE_LABEL
        print(
            f"  {tick:<6} "
            f"{'Player mines ' + str(round(MINE_AMOUNT,2)) + ' units':<30} "
            f"{extracted:>12.4f} "
            f"{sim_node.current_reserve:>14.4f} "
            f"{status}"
        )

    print(_DIV_MINOR)
    print(f"  ✓ Node DEPLETED after {tick} mining ticks.")
    print(f"  ✓ Total extracted    : {total_extracted:.4f} units")
    print(f"  ✓ Original max       : {sim_node.max_reserve:.4f} units")
    precision_ok = abs(total_extracted - sim_node.max_reserve) < 0.001
    print(f"  ✓ Reserve invariant  : {'PASS ✓' if precision_ok else 'FAIL ✗'}"
          f"  (delta = {abs(total_extracted - sim_node.max_reserve):.8f})")
    print()

    # Verify no further extraction is possible
    ghost_extract = spawner.extract_resource(state_a, sim_node_id, 9999.0)
    print(f"  Post-depletion extraction attempt → returned {ghost_extract}  "
          f"(expected 0.0) {'✓' if ghost_extract == 0.0 else '✗'}")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4 — Regeneration Tick Showcase
    # Demonstrate tectonic_activity scaling across the two servers.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 4 — REGENERATION TICK SHOWCASE  (Tectonic Activity Scaling)")
    print(_DIV_MINOR)

    tectonic_a = catalog_a.tectonic_activity
    tectonic_b = catalog_b.tectonic_activity

    print(f"  Server A (Neon Spire) tectonic_activity = {tectonic_a:.6f}")
    print(f"  Server B (Iron Veil)  tectonic_activity = {tectonic_b:.6f}")
    print()
    print(f"  Regen formula: rate = base_regen × (1.0 + tectonic × {_TECTONIC_REGEN_SCALE})")
    print()

    # Sample the first ore node from each server and show regen rates
    first_a_node = next(iter(state_a.active_ores.values()))
    first_b_node = next(iter(state_b.active_ores.values()))

    print(f"  {'Node':<50} {'Regen/tick':>12}  {'Tectonic':>10}")
    print(f"  {'─'*50} {'─'*12}  {'─'*10}")
    print(f"  {first_a_node.node_id:<50} {first_a_node.regeneration_rate:>12.6f}"
          f"  {tectonic_a:>10.6f}")
    print(f"  {first_b_node.node_id:<50} {first_b_node.regeneration_rate:>12.6f}"
          f"  {tectonic_b:>10.6f}")
    print()

    # Now apply 5 regeneration ticks to Server A and watch the depleted node recover
    print("  ── Applying 5 regeneration ticks to Server A (depleted node recovery):")
    print(f"  {'Tick':<6} {'Node Reserve':>15} {'Regen Gained':>14} {'Is Depleted':<14}")
    print(f"  {'─'*6} {'─'*15} {'─'*14} {'─'*14}")

    for t in range(1, 6):
        regen_report = spawner.apply_regeneration_tick(state_a)
        sim_node_post = state_a.active_ores[sim_node_id]
        gained = regen_report.get(sim_node_id, 0.0)
        print(
            f"  {t:<6} "
            f"{sim_node_post.current_reserve:>15.4f} "
            f"{gained:>14.6f} "
            f"{'Yes' if sim_node_post.is_depleted else 'No — RECOVERED ✓':<14}"
        )

    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 5 — Depth Gating Audit
    # Prove every node has been correctly classified for Discord channel routing.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 5 — DEPTH GATING AUDIT  (Discord Channel Router Verification)")
    print(_DIV_MINOR)

    gate_errors: int = 0
    for node in list(state_a.active_ores.values()) + list(state_a.active_crystals.values()):
        expected = ACCESS_PUBLIC if node.depth_layer in _PUBLIC_DEPTHS else ACCESS_RESTRICTED
        if node.access_tier != expected:
            print(f"  ✗ GATE MISMATCH: {node.node_id}  depth={node.depth_layer}"
                  f"  access={node.access_tier}  expected={expected}")
            gate_errors += 1

    if gate_errors == 0:
        total_nodes = len(state_a.active_ores) + len(state_a.active_crystals)
        print(f"  ✓ ALL {total_nodes} NODES CORRECTLY GATED  (0 mismatches)")

    public_count     = sum(
        1 for n in list(state_a.active_ores.values()) + list(state_a.active_crystals.values())
        if n.access_tier == ACCESS_PUBLIC
    )
    restricted_count = sum(
        1 for n in list(state_a.active_ores.values()) + list(state_a.active_crystals.values())
        if n.access_tier == ACCESS_RESTRICTED
    )
    print(f"  Public Access    : {public_count} nodes  → baseline Discord channels")
    print(f"  Restricted Access: {restricted_count} nodes  → deep mining / role-gated channels")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 6 — Idempotency Proof
    # Confirm that initialise() produces the identical spawn layout on re-run.
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 6 — IDEMPOTENCY PROOF  (Determinism Verification)")
    print(_DIV_MINOR)

    state_a2 = spawner.initialise(profile_a, catalog_a)

    all_pass = True
    # Compare every ore node ID and initial max_reserve
    for node_id, node in state_a.active_ores.items():
        node2 = state_a2.active_ores.get(node_id)
        ok = (
            node2 is not None
            and node2.max_reserve == node.max_reserve
            and node2.regeneration_rate == node.regeneration_rate
            and node2.access_tier == node.access_tier
        )
        if not ok:
            print(f"  ✗ FAIL  Ore node mismatch: {node_id}")
            all_pass = False

    for node_id, node in state_a.active_crystals.items():
        node2 = state_a2.active_crystals.get(node_id)
        ok = (
            node2 is not None
            and node2.max_reserve == node.max_reserve
            and node2.access_tier == node.access_tier
        )
        if not ok:
            print(f"  ✗ FAIL  Crystal node mismatch: {node_id}")
            all_pass = False

    if all_pass:
        print(f"  ✓ PASS — Re-initialised state is bit-for-bit identical to first run.")
        print(f"  ✓ PASS — {len(state_a.active_ores)} ore nodes verified.")
        print(f"  ✓ PASS — {len(state_a.active_crystals)} crystal nodes verified.")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 7 — Snapshot Summary (persistence-layer bridge demo)
    # ─────────────────────────────────────────────────────────────────────────

    print(_DIV_MAJOR)
    print("  PHASE 7 — SNAPSHOT SUMMARY  (Persistence-Layer Bridge Demo)")
    print(_DIV_MINOR)

    summary_a = spawner.snapshot_summary(state_a)
    print(f"  Server A — Neon Spire  (post-depletion, post-regen ticks):")
    for key, val in summary_a.items():
        print(f"    {key:<32}: {val}")
    print()

    summary_b = spawner.snapshot_summary(state_b)
    print(f"  Server B — Iron Veil   (pristine spawn state):")
    for key, val in summary_b.items():
        print(f"    {key:<32}: {val}")
    print()

    print(_DIV_MAJOR)
    print("  ✓ RESOURCE_SPAWNER.PY SIMULATION COMPLETE — all phases passed.")
    print(_DIV_MAJOR)
    print()