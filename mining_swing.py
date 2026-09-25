"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         MINING_SWING.PY  —  Satu-satunya jalur ayunan tambang                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Mining manual (/explore_mines) DAN auto mine memanggil execute_swing().    ║
║  Tidak ada jalur roll kedua:                                                 ║
║    roll   = world_stream.mining_roll(seed, guild, user, node, attempt)      ║
║    result = MiningEngine.execute_mining_attempt(..., roll=roll)             ║
║    item   = OreFactory / CrystalFactory (sama seperti sebelumnya)           ║
║                                                                              ║
║  `attempt` (n) WAJIB sudah di-increment atomik di Supabase oleh pemanggil   ║
║  (main_core._mining_swing) sebelum fungsi ini dipanggil — modul ini murni,  ║
║  tidak menyentuh DB, jadi ia tidak mungkin "mengarang" n sendiri.           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from crystal import CrystalFactory, CrystalItem, VARIANT_GEM, VARIANT_SPLINTER
from material_gen import ServerMaterialCatalog
from mining_engine import MiningEngine, MiningResult, Pickaxe
from ore import OreFactory, OreItem
from resource_spawner import ServerSpawnState
from world_stream import mining_roll


@dataclass(frozen=True)
class SwingOutcome:
    attempt: int                                   # n — shown to the player
    roll:    int                                   # 256-bit; recomputable after reveal
    result:  MiningResult
    item:    Optional[Union[OreItem, CrystalItem]]


def execute_swing(
    miner:           MiningEngine,
    crystal_factory: CrystalFactory,
    *,
    seed:     bytes,
    guild_id: int,
    user_id:  int,
    attempt:  int,
    node_id:  str,
    pickaxe:  Pickaxe,
    stamina:  float,
    state:    ServerSpawnState,
    catalog:  ServerMaterialCatalog,
) -> SwingOutcome:
    """
    Satu ayunan.  Raises KeyError kalau node_id tidak ada (sama seperti engine).
    Kegagalan pembuatan item (ValueError/KeyError dari factory) tidak
    membatalkan ayunan — kebijakan yang sama dengan mining manual sebelumnya.
    """
    roll = mining_roll(seed, guild_id, user_id, node_id, attempt)
    result = miner.execute_mining_attempt(
        player_pickaxe = pickaxe,
        player_stamina = stamina,
        state          = state,
        node_id        = node_id,
        catalog        = catalog,
        roll           = roll,
    )

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
            elif result.resource_type in {VARIANT_GEM, VARIANT_SPLINTER}:
                crystal_node = state.active_crystals.get(node_id)
                if crystal_node is not None:
                    item = crystal_factory.create_item_from_mining(
                        mining_result = result,
                        active_node   = crystal_node,
                        owner_id      = user_id,
                    )
        except (ValueError, KeyError):
            item = None
    return SwingOutcome(attempt=attempt, roll=roll, result=result, item=item)


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python mining_swing.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from identitas_genetik import GeneticEngine
    from material_gen import MaterialEngine
    from mining_engine import PICKAXES
    from resource_spawner import ResourceSpawner
    from world_stream import test_seed

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    GUILD, USER = 987654321098765432, 42
    seed = test_seed("mining_swing-selftest")
    profile = GeneticEngine().generate_profile(server_id=GUILD, seed=seed)
    catalog = MaterialEngine().generate_geology(profile, seed)

    def fresh_state():
        return ResourceSpawner().initialise(profile, catalog, seed)

    pick = PICKAXES["abyss_resonator"]
    node = max(fresh_state().active_ores, key=lambda nid: fresh_state().active_ores[nid].max_reserve)

    print("\n[1] Roll = mining_roll(...) dan deterministik")
    a = execute_swing(MiningEngine(), CrystalFactory(), seed=seed, guild_id=GUILD, user_id=USER, attempt=5,
                      node_id=node, pickaxe=pick, stamina=100.0, state=fresh_state(), catalog=catalog)
    b = execute_swing(MiningEngine(), CrystalFactory(), seed=seed, guild_id=GUILD, user_id=USER, attempt=5,
                      node_id=node, pickaxe=pick, stamina=100.0, state=fresh_state(), catalog=catalog)
    check("roll = mining_roll(seed, guild, user, node, n)", a.roll, mining_roll(seed, GUILD, USER, node, 5))
    check("input sama → hasil identik", (a.result, a.item), (b.result, b.item))
    check("item dibuat untuk ayunan sukses", a.item is not None, True)

    print("\n[2] Stamina 100 terus (/rest tiap ayunan), node sama, n berbeda → roll berbeda")
    st = fresh_state()
    outs = [execute_swing(MiningEngine(), CrystalFactory(), seed=seed, guild_id=GUILD, user_id=USER,
                          attempt=n, node_id=node, pickaxe=pick, stamina=100.0, state=st, catalog=catalog)
            for n in range(1, 101)]
    crits = [o.result.critical_hit for o in outs]
    check("100 roll unik", len({o.roll for o in outs}), 100)
    check("crit tidak selalu sama (bukan semua/tidak sama sekali)", 0 < sum(crits) < 100, True)
    check("laju crit wajar (2–20 dari 100; ekspektasi ~8)", 2 <= sum(crits) <= 20, True)

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
