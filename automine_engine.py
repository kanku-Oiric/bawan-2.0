"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         AUTOMINE_ENGINE.PY  —  Voice-Driven Auto Mining                      ║
║         Layer murni: tidak menyentuh Discord API maupun Supabase            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ALUR                                                                        ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Player yang terdaftar (/automine daftar) mendapat 1 ayunan otomatis untuk  ║
║  setiap interval reward voice yang selesai (voice_engine.py).  Jadi aturan  ║
║  anti-abuse voice (AFK, mute+deaf, sendirian) otomatis ikut berlaku.        ║
║                                                                              ║
║  Setiap ayunan:                                                              ║
║    1. choose_best_node() → node non-depleted dengan yield (ton) terbesar    ║
║       untuk pickaxe player, memakai rumus yang sama dengan MiningEngine.   ║
║    2. Kalau stamina < biaya ayunan → istirahat otomatis (setara /rest).    ║
║    3. MiningEngine.execute_mining_attempt() — satu-satunya jalur yang      ║
║       mengurangi cadangan node (kontrak resource_spawner tetap utuh).      ║
║    4. OreFactory / CrystalFactory membuat item seperti mining manual.      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from material_gen import ServerMaterialCatalog
from resource_spawner import ServerSpawnState, ActiveOreNode, ActiveCrystalNode
# Private helpers are imported on purpose: the node ranking must use exactly
# the same hardness / power / stamina formulas as execute_mining_attempt().
from mining_engine import (
    MiningEngine,
    MiningResult,
    Pickaxe,
    _node_hardness,
    _crystal_hardness_purity,
    _power_efficiency_factor,
    _stamina_cost,
)
from ore import OreFactory, OreItem
from crystal import CrystalFactory, CrystalItem, VARIANT_GEM, VARIANT_SPLINTER


Node = Union[ActiveOreNode, ActiveCrystalNode]


@dataclass(frozen=True)
class AutoSwing:
    """Hasil satu ayunan otomatis."""
    node_id:       str
    node_label:    str
    result:        MiningResult
    item:          Optional[Union[OreItem, CrystalItem]]
    stamina_after: float
    rested:        bool          # True kalau stamina diisi ulang sebelum ayunan


def node_label(node: Node) -> str:
    name = node.name if isinstance(node, ActiveCrystalNode) else node.ore_name
    return f"{name} [{node.depth_layer}]"


def _hardness(node: Node) -> float:
    purity = _crystal_hardness_purity(node) if isinstance(node, ActiveCrystalNode) else node.purity
    return _node_hardness(node.depth_layer, purity)


def expected_yield(node: Node, pickaxe: Pickaxe) -> float:
    """Yield (ton) sebelum scatter/crit — cermin Step 5 & 7 execute_mining_attempt()."""
    if node.is_depleted or node.current_reserve <= 0:
        return 0.0
    hardness    = _hardness(node)
    base_swings = max(1.0, (hardness / max(pickaxe.power, 1.0)) * 10.0)
    raw         = node.current_reserve / base_swings * pickaxe.efficiency
    return min(node.current_reserve, raw * _power_efficiency_factor(pickaxe.power, hardness))


def choose_best_node(state: ServerSpawnState, pickaxe: Pickaxe) -> Optional[str]:
    """Node non-depleted dengan expected_yield terbesar; seri → node_id terkecil (deterministik)."""
    best_id:    Optional[str] = None
    best_yield: float = 0.0
    for node_id, node in sorted([*state.active_ores.items(), *state.active_crystals.items()]):
        y = expected_yield(node, pickaxe)
        if y > best_yield:
            best_id, best_yield = node_id, y
    return best_id


class AutoMineEngine:
    """Menjalankan ayunan otomatis lewat engine mining yang sudah ada."""

    def __init__(self, miner: MiningEngine, crystal_factory: CrystalFactory) -> None:
        self._miner = miner
        self._crystal_factory = crystal_factory

    def swing(
        self,
        *,
        pickaxe:     Pickaxe,
        stamina:     float,
        max_stamina: float,
        state:       ServerSpawnState,
        catalog:     ServerMaterialCatalog,
        owner_id:    int,
        server_id:   int,
    ) -> Optional[AutoSwing]:
        """Satu ayunan otomatis, atau None kalau tidak ada node yang bisa ditambang."""
        node_id = choose_best_node(state, pickaxe)
        if node_id is None:
            return None
        node = state.active_ores.get(node_id) or state.active_crystals[node_id]

        rested = stamina < _stamina_cost(node.depth_layer, catalog.pressure_index)
        if rested:
            stamina = max_stamina

        result = self._miner.execute_mining_attempt(
            player_pickaxe = pickaxe,
            player_stamina = stamina,
            state          = state,
            node_id        = node_id,
            catalog        = catalog,
        )

        item: Optional[Union[OreItem, CrystalItem]] = None
        if result.success and result.amount_extracted > 0.0:
            try:
                if result.resource_type == "ORE":
                    item = OreFactory.create_item_from_mining(
                        mining_result = result,
                        catalog       = catalog,
                        owner_id      = owner_id,
                        server_id     = server_id,
                    )
                elif result.resource_type in {VARIANT_GEM, VARIANT_SPLINTER}:
                    item = self._crystal_factory.create_item_from_mining(
                        mining_result = result,
                        active_node   = node,
                        owner_id      = owner_id,
                    )
            except (ValueError, KeyError):
                item = None   # same policy as manual mining: keep the swing, skip the item

        return AutoSwing(
            node_id       = node_id,
            node_label    = node_label(node),
            result        = result,
            item          = item,
            stamina_after = max(0.0, stamina - result.stamina_consumed),
            rested        = rested,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python automine_engine.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from world_stream import test_seed
    from identitas_genetik import GeneticEngine
    from material_gen import MaterialEngine
    from resource_spawner import ResourceSpawner
    from mining_engine import PICKAXES

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    GUILD = 987654321098765432
    profile = GeneticEngine().generate_profile(server_id=GUILD, seed=test_seed(f"{GUILD}:{1650000000}"), created_at=1650000000)
    catalog = MaterialEngine().generate_geology(profile, test_seed(f"{profile.server_id}:{profile.created_at}"))
    spawner = ResourceSpawner()
    state   = spawner.initialise(profile, catalog, test_seed(f"{profile.server_id}:{profile.created_at}"))
    engine  = AutoMineEngine(MiningEngine(), CrystalFactory())
    pick    = PICKAXES["iron_standard"]

    print("\n[1] choose_best_node memilih yield terbesar")
    all_nodes = {**state.active_ores, **state.active_crystals}
    best = choose_best_node(state, pick)
    check("node terpilih = max expected_yield", best,
          max(sorted(all_nodes), key=lambda nid: expected_yield(all_nodes[nid], pick)))

    print("\n[2] Ayunan menghasilkan item & mengurangi cadangan")
    before = all_nodes[best].current_reserve
    swing  = engine.swing(pickaxe=pick, stamina=100.0, max_stamina=100.0, state=state,
                          catalog=catalog, owner_id=42, server_id=GUILD)
    check("ada item", swing.item is not None, True)
    check("cadangan berkurang sesuai hasil", round(before - all_nodes[best].current_reserve, 4),
          swing.result.amount_extracted)
    check("stamina berkurang", swing.stamina_after, 100.0 - swing.result.stamina_consumed)
    check("tidak istirahat", swing.rested, False)
    check("owner item", swing.item.owner_id, 42)

    print("\n[3] Stamina kurang → istirahat otomatis dulu")
    swing = engine.swing(pickaxe=pick, stamina=1.0, max_stamina=100.0, state=state,
                         catalog=catalog, owner_id=42, server_id=GUILD)
    check("rested", swing.rested, True)
    check("ayunan penuh (bukan parsial)", swing.stamina_after, 100.0 - swing.result.stamina_consumed)

    print("\n[4] Node habis dilewati, pindah ke node lain")
    first = choose_best_node(state, pick)
    all_nodes[first].is_depleted = True
    second = choose_best_node(state, pick)
    check("node lain terpilih", second is not None and second != first, True)

    print("\n[5] Semua node habis → None")
    for n in all_nodes.values():
        n.is_depleted = True
    check("swing", engine.swing(pickaxe=pick, stamina=100.0, max_stamina=100.0, state=state,
                                catalog=catalog, owner_id=42, server_id=GUILD), None)

    print("\n[6] Regenerasi memulihkan node yang habis")
    for n in all_nodes.values():
        n.current_reserve, n.is_depleted = 0.0, True
    spawner.apply_regeneration_tick(state)
    check("ada node aktif lagi", choose_best_node(state, pick) is not None, True)

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
