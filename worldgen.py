"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLDGEN.PY  —  Versi pembangkit dunia (worldgen_version)            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Dua versi yang TERPISAH:                                                    ║
║    • algo_version      — cara seed dibuat (Stage 0–2, world_seed.py)         ║
║    • worldgen_version  — cara dunia dibangun dari seed (tabel periodik,      ║
║                          genetik, geologi, spawn awal)                        ║
║                                                                              ║
║  worldgen_version dikunci per server saat registrasi (server_registry,      ║
║  insert-only) dan ikut di event saksi `registered`.                         ║
║                                                                              ║
║  "dev"  = masa pengembangan: dunia BOLEH berubah saat kode berubah; data    ║
║           server dev ditandai lewat server_registry.worldgen_version dan    ║
║           tidak dihapus.                                                    ║
║  "v1"   = dibekukan setelah LANGKAH 5 lulus: hash commit kodenya            ║
║           dipublikasikan (webhook saksi + README) dan kodenya tidak pernah  ║
║           dihapus.  Perubahan sesudahnya = v2, hanya untuk server baru.     ║
║                                                                              ║
║  Versi yang tidak dikenal → UnsupportedWorldgen.  Tidak ada fallback ke     ║
║  versi lain: server itu tidak di-hydrate sampai kodenya tersedia.           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from identitas_genetik import GeneticEngine, ServerGeneticProfile
from material_gen import MaterialEngine, ServerMaterialCatalog
from resource_spawner import ResourceSpawner, ServerSpawnState
from world_periodic import ServerPeriodicTable, build_periodic_table


# Versi untuk server yang BARU mendaftar.  Server lama selamanya memakai
# worldgen_version yang tersimpan di barisnya sendiri.
WORLDGEN_VERSION_CURRENT: str = "dev"


class UnsupportedWorldgen(Exception):
    """worldgen_version tersimpan tidak punya kode di build ini — dunia tidak dibangun."""


@dataclass(frozen=True)
class World:
    table:         ServerPeriodicTable
    profile:       ServerGeneticProfile
    catalog:       ServerMaterialCatalog
    initial_state: ServerSpawnState          # mutable runtime state starts here


def _generate_dev(server_id: int, seed: bytes, created_at: int) -> World:
    table   = build_periodic_table(seed)
    profile = GeneticEngine().generate_profile(server_id, seed, created_at, table=table)
    catalog = MaterialEngine().generate_geology(profile, seed)
    state   = ResourceSpawner().initialise(profile, catalog, seed)
    return World(table=table, profile=profile, catalog=catalog, initial_state=state)


_GENERATORS: Dict[str, Callable[[int, bytes, int], World]] = {
    "dev": _generate_dev,
}

SUPPORTED_WORLDGEN_VERSIONS = tuple(_GENERATORS)


def generate_world(worldgen_version: str, server_id: int, seed: bytes, created_at: int = 0) -> World:
    try:
        generator = _GENERATORS[worldgen_version]
    except KeyError:
        raise UnsupportedWorldgen(
            f"worldgen_version {worldgen_version!r} tidak didukung build ini "
            f"(didukung: {', '.join(SUPPORTED_WORLDGEN_VERSIONS)})"
        ) from None
    return generator(server_id, seed, created_at)
