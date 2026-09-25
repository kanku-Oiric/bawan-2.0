"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLD_PERIODIC.PY  —  LANGKAH 4: Tabel Periodik per Server           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Tabel server = 19 unsur CORE (selalu ada) + 9 slot variabel, stratified:   ║
║                                                                              ║
║      structural 2 · conductive 2 · reactive 2 · catalytic 1                 ║
║      rare_earth 1 · exotic 1                                                ║
║                                                                              ║
║  Slot diundi berbobot rarity_weight TANPA pengembalian, per role, urutan    ║
║  role tetap, kandidat urut nomor atom, entropi = stream(seed, "periodic",i) ║
║  (world_stream.DomainStream).  Tidak ada `random`, tidak ada input selain   ║
║  seed.                                                                      ║
║                                                                              ║
║  Setiap unsur 1..118 punya TEPAT SATU status: core / <role> / excluded /    ║
║  synthetic.  excluded & synthetic tidak pernah di-sampling (synthetic       ║
║  disimpan untuk crafting nanti).  Partisi ini dicek saat import — data      ║
║  yang rusak membuat bot menolak start, bukan diam-diam kehilangan unsur.    ║
║                                                                              ║
║  GeneticEngine dan material_gen hanya boleh memakai unsur dari tabel ini.   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

from identitas_genetik import ELEMENTS, Element, GeneticEngine
from world_stream import DOMAIN_PERIODIC, DomainStream, seed_fingerprint


# ─────────────────────────────────────────────────────────────────────────────
# STATUS & ROLE (keputusan LANGKAH 4)
# ─────────────────────────────────────────────────────────────────────────────

STATUS_CORE:      str = "core"
STATUS_EXCLUDED:  str = "excluded"
STATUS_SYNTHETIC: str = "synthetic"

ROLE_STRUCTURAL: str = "structural"
ROLE_CONDUCTIVE: str = "conductive"
ROLE_REACTIVE:   str = "reactive"
ROLE_CATALYTIC:  str = "catalytic"
ROLE_RARE_EARTH: str = "rare_earth"
ROLE_EXOTIC:     str = "exotic"

# Urutan draw — JANGAN diubah tanpa worldgen_version baru.
ROLE_ORDER: Tuple[str, ...] = (
    ROLE_STRUCTURAL, ROLE_CONDUCTIVE, ROLE_REACTIVE, ROLE_CATALYTIC, ROLE_RARE_EARTH, ROLE_EXOTIC,
)

ROLE_SLOTS: Mapping[str, int] = {
    ROLE_STRUCTURAL: 2,
    ROLE_CONDUCTIVE: 2,
    ROLE_REACTIVE:   2,
    ROLE_CATALYTIC:  1,
    ROLE_RARE_EARTH: 1,
    ROLE_EXOTIC:     1,
}

CORE_SET: Tuple[str, ...] = (
    "Fe", "Al", "Cu", "Zn", "Ni", "Mn", "Cr", "Pb", "Sn", "Mg", "Ca", "Au",
    "C", "Si", "S", "P", "O", "N", "H",
)

ROLE_MEMBERS: Mapping[str, Tuple[str, ...]] = {
    ROLE_STRUCTURAL: ("Ti", "V", "Zr", "Nb", "Mo", "Hf", "Ta", "W", "Be", "B", "Co"),  # Co: superalloy
    ROLE_CONDUCTIVE: ("Ag", "Ga", "In", "Ge", "Se", "Te", "Sb", "Bi", "Tl", "Cd", "Hg", "As"),
    ROLE_REACTIVE:   ("Li", "Na", "K", "Rb", "Cs", "Sr", "Ba", "Cl", "F", "Br", "I"),
    ROLE_CATALYTIC:  ("Ru", "Rh", "Pd", "Os", "Ir", "Pt", "Re"),
    ROLE_RARE_EARTH: ("La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm",
                      "Yb", "Lu", "Sc", "Y"),
    # Aktinida alami (termasuk jejak: Np, Pu) + Po, At, Fr, Ra, Tc.
    ROLE_EXOTIC:     ("Ac", "Th", "Pa", "U", "Np", "Pu", "Po", "At", "Fr", "Ra", "Tc"),
}

EXCLUDED: Tuple[str, ...] = ("He", "Ne", "Ar", "Kr", "Xe", "Rn")        # gas mulia: bukan bijih

# Am–Lr (95–103) + 104–118: tidak di-sampling, disimpan untuk crafting.
SYNTHETIC: Tuple[str, ...] = tuple(e.symbol for e in ELEMENTS if e.atomic_number >= 95)

ELEMENT_BY_SYMBOL: Dict[str, Element] = {e.symbol: e for e in ELEMENTS}


class PeriodicPartitionError(RuntimeError):
    """Data unsur / status tidak membentuk partisi yang sah — worldgen tidak boleh jalan."""


def _status_entries() -> Tuple[Tuple[str, str], ...]:
    return (
        tuple((STATUS_CORE, s) for s in CORE_SET)
        + tuple((role, s) for role in ROLE_ORDER for s in ROLE_MEMBERS[role])
        + tuple((STATUS_EXCLUDED, s) for s in EXCLUDED)
        + tuple((STATUS_SYNTHETIC, s) for s in SYNTHETIC)
    )


def partition_report() -> Dict[str, object]:
    """Rekonsiliasi: setiap unsur di data harus punya tepat satu status."""
    entries = _status_entries()
    counts = Counter(symbol for _, symbol in entries)
    return {
        "elements":   len(ELEMENTS),
        "entries":    len(entries),
        "unique":     len(counts),
        "duplicates": sorted((s for s, c in counts.items() if c > 1), key=lambda s: ELEMENT_BY_SYMBOL[s].atomic_number),
        "missing":    [e.symbol for e in ELEMENTS if e.symbol not in counts],
        "unknown":    sorted(s for s in counts if s not in ELEMENT_BY_SYMBOL),
        "z_complete": [e.atomic_number for e in ELEMENTS] == list(range(1, 119)),
        "by_status":  dict(Counter(STATUS_CORE if k == STATUS_CORE else k if k in (STATUS_EXCLUDED, STATUS_SYNTHETIC)
                                   else "role" for k, _ in entries)),
    }


def _verify_partition() -> Dict[str, str]:
    report = partition_report()
    problems = [f"{k}={report[k]}" for k in ("duplicates", "missing", "unknown") if report[k]]
    if not report["z_complete"] or report["elements"] != 118:
        problems.append(f"data unsur bukan Z 1..118 lengkap ({report['elements']} unsur)")
    for role in ROLE_ORDER:
        if len(ROLE_MEMBERS[role]) < ROLE_SLOTS[role]:
            problems.append(f"role {role}: {len(ROLE_MEMBERS[role])} anggota < {ROLE_SLOTS[role]} slot")
    if set(ROLE_SLOTS) != set(ROLE_ORDER):
        problems.append("ROLE_SLOTS ≠ ROLE_ORDER")
    core = [ELEMENT_BY_SYMBOL[s] for s in CORE_SET if s in ELEMENT_BY_SYMBOL]
    # GeneticEngine butuh ≥2 logam & ≥2 non-logam (dominan + sekunder); core menjaminnya.
    if sum(GeneticEngine.is_metal(e) for e in core) < 2 or sum(GeneticEngine.is_nonmetal(e) for e in core) < 2:
        problems.append("core harus berisi ≥2 logam dan ≥2 non-logam")
    if problems:
        raise PeriodicPartitionError("partisi tabel periodik rusak: " + "; ".join(problems))
    return {symbol: status for status, symbol in _status_entries()}


STATUS_BY_SYMBOL: Mapping[str, str] = _verify_partition()


def element_status(symbol: str) -> str:
    """core / <role> / excluded / synthetic.  KeyError untuk simbol yang tidak ada."""
    return STATUS_BY_SYMBOL[symbol]


# ─────────────────────────────────────────────────────────────────────────────
# TABEL SERVER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ServerPeriodicTable:
    """
    Tabel periodik satu server.  `seed_fingerprint` mengikat tabel ke seed
    asalnya: GeneticEngine menolak tabel yang dipasangkan dengan seed lain.
    """
    seed_fingerprint: str
    core:  Tuple[Element, ...]                 # urutan CORE_SET
    slots: Tuple[Tuple[str, Element], ...]     # (role, unsur) dalam urutan draw

    @property
    def elements(self) -> Tuple[Element, ...]:
        return self.core + tuple(e for _, e in self.slots)

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(e.symbol for e in self.elements)

    @property
    def variable_symbols(self) -> Tuple[str, ...]:
        return tuple(e.symbol for _, e in self.slots)

    def role_of(self, symbol: str) -> str:
        if symbol in {e.symbol for e in self.core}:
            return STATUS_CORE
        for role, e in self.slots:
            if e.symbol == symbol:
                return role
        raise KeyError(f"{symbol} tidak ada di tabel server ini")

    def role_counts(self) -> Dict[str, int]:
        return dict(Counter(role for role, _ in self.slots))


def build_periodic_table(seed: bytes) -> ServerPeriodicTable:
    """
    Bangun tabel server dari seed.  Per role (urutan ROLE_ORDER), untuk tiap
    slot: target = stream(seed,"periodic",i) mod Σw sisa kandidat, lalu scan
    kumulatif rarity_weight dalam urutan nomor atom; unsur terpilih keluar
    dari kandidat (tanpa pengembalian).
    """
    entropy = DomainStream(seed, DOMAIN_PERIODIC)
    slots = []
    for role in ROLE_ORDER:
        candidates = sorted((ELEMENT_BY_SYMBOL[s] for s in ROLE_MEMBERS[role]), key=lambda e: e.atomic_number)
        for _ in range(ROLE_SLOTS[role]):
            target = entropy.next_below(sum(e.rarity_weight for e in candidates))
            ceiling = 0
            for element in candidates:
                ceiling += element.rarity_weight
                if target < ceiling:
                    break
            slots.append((role, element))
            candidates.remove(element)
    return ServerPeriodicTable(
        seed_fingerprint=seed_fingerprint(seed),
        core=tuple(ELEMENT_BY_SYMBOL[s] for s in CORE_SET),
        slots=tuple(slots),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python world_periodic.py   (tes lengkap: tests/test_worldgen_langkah4.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from world_stream import test_seed

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))

    r = partition_report()
    print("\n[1] Rekonsiliasi status")
    check("118 unsur, Z 1..118 lengkap", (r["elements"], r["z_complete"]), (118, True))
    check("unik / dobel / hilang / tak-dikenal", (r["unique"], r["duplicates"], r["missing"], r["unknown"]), (118, [], [], []))
    check("per status", r["by_status"], {"core": 19, "role": 69, "excluded": 6, "synthetic": 24})

    print("\n[2] Tabel dari seed")
    t = build_periodic_table(test_seed("world_periodic:kat"))
    check("28 unsur, tanpa dobel", (len(t.symbols), len(set(t.symbols))), (28, 28))
    check("alokasi slot persis", t.role_counts(), dict(ROLE_SLOTS))
    check("deterministik", build_periodic_table(test_seed("world_periodic:kat")), t)
    check("KAT slot (test_seed('world_periodic:kat'))", t.variable_symbols,
          ("B", "Be", "As", "Tl", "Li", "Na", "Pd", "Sm", "Ac"))

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
