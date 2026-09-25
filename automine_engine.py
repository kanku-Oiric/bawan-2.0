"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         AUTOMINE_ENGINE.PY  —  Voice-Driven Auto Mining (perencanaan)        ║
║         Layer murni: tidak menyentuh Discord API maupun Supabase            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ALUR                                                                        ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  Player yang terdaftar (/automine daftar) mendapat 1 ayunan otomatis untuk  ║
║  setiap interval reward voice yang selesai (voice_engine.py).  Jadi aturan  ║
║  anti-abuse voice (AFK, mute+deaf, sendirian) otomatis ikut berlaku.        ║
║                                                                              ║
║  Modul ini HANYA merencanakan ayunan:                                        ║
║    1. choose_best_node() → node non-depleted dengan yield (ton) terbesar    ║
║       untuk pickaxe player, memakai rumus yang sama dengan MiningEngine.   ║
║    2. min_stamina = biaya ayunan node itu; kalau stamina di bawahnya,       ║
║       main_core istirahat otomatis dulu (setara /rest yang memang gratis).  ║
║  Ayunannya sendiri lewat main_core._mining_swing → mining_swing.execute_    ║
║  swing — JALUR YANG SAMA PERSIS dengan mining manual (counter atomik di     ║
║  Supabase, roll = mining_roll(...)).  Tidak ada jalur roll kedua.           ║
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
    Pickaxe,
    _node_hardness,
    _crystal_hardness_purity,
    _power_efficiency_factor,
    _stamina_cost,
)
from mining_swing import SwingOutcome


Node = Union[ActiveOreNode, ActiveCrystalNode]


@dataclass(frozen=True)
class AutoPlan:
    """Rencana satu ayunan otomatis."""
    node_id:     str
    node_label:  str
    min_stamina: float        # biaya ayunan node ini; di bawahnya → istirahat dulu


@dataclass(frozen=True)
class AutoSwing:
    """Catatan ayunan otomatis terakhir (untuk /automine status)."""
    node_label:    str
    outcome:       SwingOutcome
    stamina_after: float
    rested:        bool

    @property
    def result(self):
        return self.outcome.result

    @property
    def item(self):
        return self.outcome.item

    @property
    def attempt(self) -> int:
        return self.outcome.attempt


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


def plan_swing(state: ServerSpawnState, catalog: ServerMaterialCatalog, pickaxe: Pickaxe) -> Optional[AutoPlan]:
    """Rencana ayunan berikutnya, atau None kalau tidak ada node yang bisa ditambang."""
    node_id = choose_best_node(state, pickaxe)
    if node_id is None:
        return None
    node = state.active_ores.get(node_id) or state.active_crystals[node_id]
    return AutoPlan(node_id, node_label(node), _stamina_cost(node.depth_layer, catalog.pressure_index))


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python automine_engine.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from world_stream import test_seed
    from identitas_genetik import GeneticEngine
    from material_gen import MaterialEngine
    from resource_spawner import ResourceSpawner
    from mining_engine import MiningEngine, PICKAXES
    from crystal import CrystalFactory
    from mining_swing import execute_swing

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    GUILD = 987654321098765432
    seed    = test_seed(f"{GUILD}:1650000000")
    profile = GeneticEngine().generate_profile(server_id=GUILD, seed=seed, created_at=1650000000)
    catalog = MaterialEngine().generate_geology(profile, seed)
    spawner = ResourceSpawner()
    state   = spawner.initialise(profile, catalog, seed)
    pick    = PICKAXES["iron_standard"]
    all_nodes = {**state.active_ores, **state.active_crystals}

    print("\n[1] choose_best_node memilih yield terbesar")
    best = choose_best_node(state, pick)
    check("node terpilih = max expected_yield", best,
          max(sorted(all_nodes), key=lambda nid: expected_yield(all_nodes[nid], pick)))

    print("\n[2] plan_swing memberi node & biaya stamina")
    plan = plan_swing(state, catalog, pick)
    check("plan.node_id = best", plan.node_id, best)
    check("min_stamina > 0", plan.min_stamina > 0, True)

    print("\n[3] Rencana + execute_swing (jalur yang sama dengan manual) mengurangi cadangan")
    before = all_nodes[best].current_reserve
    out = execute_swing(MiningEngine(), CrystalFactory(), seed=seed, guild_id=GUILD, user_id=42, attempt=1,
                        node_id=plan.node_id, pickaxe=pick, stamina=100.0, state=state, catalog=catalog)
    check("cadangan berkurang sesuai hasil", round(before - all_nodes[best].current_reserve, 4),
          out.result.amount_extracted)
    rec = AutoSwing(plan.node_label, out, 100.0 - out.result.stamina_consumed, False)
    check("AutoSwing mengekspos attempt/result/item", (rec.attempt, rec.result is out.result, rec.item is out.item),
          (1, True, True))

    print("\n[4] Node habis dilewati, semua habis → None")
    all_nodes[best].is_depleted = True
    check("node lain terpilih", plan_swing(state, catalog, pick).node_id != best, True)
    for n in all_nodes.values():
        n.is_depleted = True
    check("semua habis", plan_swing(state, catalog, pick), None)

    print("\n[5] Regenerasi memulihkan node yang habis")
    for n in all_nodes.values():
        n.current_reserve, n.is_depleted = 0.0, True
    spawner.apply_regeneration_tick(state)
    check("ada node aktif lagi", plan_swing(state, catalog, pick) is not None, True)

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
