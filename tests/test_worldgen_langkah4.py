"""
LANGKAH 4 — tabel periodik per server: tes wajib.

    python tests/test_worldgen_langkah4.py              # 10.000 seed
    python tests/test_worldgen_langkah4.py --seeds 20000

Seed tes = world_stream.test_seed("langkah4:<i>") — HMAC, bisa diulang, tanpa
`random`.  Uji statistik memakai skor-z per unsur terhadap ekspektasi eksak
per seed; batas |z| < 5 (dengan ~90 unsur, peluang alarm palsu ≈ 5·10⁻⁵).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import hmac
import inspect
import math
import os
import sys
import time
from collections import Counter, defaultdict
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import identitas_genetik as ig                                        # noqa: E402
import world_periodic as wp                                           # noqa: E402
from identitas_genetik import GeneticEngine, dominant_weight          # noqa: E402
from material_gen import MaterialEngine                               # noqa: E402
from world_periodic import (                                          # noqa: E402
    CORE_SET, ELEMENT_BY_SYMBOL, EXCLUDED, ROLE_MEMBERS, ROLE_ORDER, ROLE_SLOTS, SYNTHETIC,
    PeriodicPartitionError, ServerPeriodicTable, build_periodic_table, element_status,
)
from world_stream import DOMAIN_GENETIC, seed_fingerprint, stream, test_seed   # noqa: E402

N_SEEDS = int(sys.argv[sys.argv.index("--seeds") + 1]) if "--seeds" in sys.argv else 10_000
Z_LIMIT = 5.0

failures = 0


def check(label: str, got, expected) -> None:
    global failures
    ok = got == expected
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))


def raises(label: str, exc_type, fn) -> None:
    try:
        fn()
        check(label, "tidak ada error", exc_type.__name__)
    except exc_type:
        check(label, exc_type.__name__, exc_type.__name__)


def max_abs_z(observed: Counter, expected: dict, variance: dict) -> tuple:
    worst = (0.0, None)
    for key in set(expected) | set(observed):
        var = variance.get(key, 0.0)
        diff = observed.get(key, 0) - expected.get(key, 0.0)
        z = diff / math.sqrt(var) if var > 0 else (0.0 if diff == 0 else math.inf)
        if abs(z) > abs(worst[0]):
            worst = (z, key)
    return worst


W = lambda e: dominant_weight(e.rarity_weight)                         # noqa: E731
FORBIDDEN = frozenset(EXCLUDED) | frozenset(SYNTHETIC)
SEEDS = [test_seed(f"langkah4:{i}") for i in range(N_SEEDS)]
t0 = time.time()

# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] Rekonsiliasi 118 unsur — tepat satu status")
report = wp.partition_report()
check("Z 1..118 lengkap di data", (report["elements"], report["z_complete"]), (118, True))
check("unik 118, dobel 0, hilang 0, tak-dikenal 0",
      (report["unique"], report["duplicates"], report["missing"], report["unknown"]), (118, [], [], []))
check("per status: core 19 · role 69 · excluded 6 · synthetic 24", report["by_status"],
      {"core": 19, "role": 69, "excluded": 6, "synthetic": 24})
check("setiap unsur punya status", sorted({element_status(e.symbol) for e in ig.ELEMENTS}),
      sorted({"core", "excluded", "synthetic", *ROLE_ORDER}))
check("synthetic = Z 95..118 (termasuk Ts)", sorted(ELEMENT_BY_SYMBOL[s].atomic_number for s in SYNTHETIC),
      list(range(95, 119)))
check("excluded = gas mulia He Ne Ar Kr Xe Rn", EXCLUDED, ("He", "Ne", "Ar", "Kr", "Xe", "Rn"))
check("Co di structural, catalytic tanpa Co/La/Ce",
      ("Co" in ROLE_MEMBERS["structural"], sorted(ROLE_MEMBERS["catalytic"])),
      (True, sorted(["Ru", "Rh", "Pd", "Os", "Ir", "Pt", "Re"])))
with mock.patch.object(wp, "CORE_SET", CORE_SET + ("Co",)):
    raises("penjaga partisi: unsur dobel → PeriodicPartitionError", PeriodicPartitionError, wp._verify_partition)
with mock.patch.object(wp, "EXCLUDED", EXCLUDED[:-1]):
    raises("penjaga partisi: unsur hilang (Rn) → PeriodicPartitionError", PeriodicPartitionError, wp._verify_partition)
with mock.patch.object(wp, "ROLE_SLOTS", {**ROLE_SLOTS, "catalytic": 8}):
    raises("penjaga partisi: slot > anggota role → PeriodicPartitionError", PeriodicPartitionError, wp._verify_partition)

# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] dominant_weight = round(10·√rarity_weight), integer eksak")
check("sama dengan round(10·√w) untuk w = 1..100.000",
      [w for w in range(1, 100_001) if dominant_weight(w) != round(10 * math.sqrt(w))], [])
check("contoh: Fe 1000→316, Au 22→47, Ts 1→10",
      (dominant_weight(1000), dominant_weight(22), dominant_weight(1)), (316, 47, 10))
for bad in (0, -5, True, 1.5, "9"):
    raises(f"dominant_weight({bad!r}) ditolak", ValueError, lambda b=bad: dominant_weight(b))


# ─────────────────────────────────────────────────────────────────────────────
def reference_slots(seed: bytes) -> tuple:
    """Independent re-implementation: raw HMAC, no DomainStream."""
    i, out = 0, []
    for role in ROLE_ORDER:
        pool = sorted(ROLE_MEMBERS[role], key=lambda s: ELEMENT_BY_SYMBOL[s].atomic_number)
        for _ in range(ROLE_SLOTS[role]):
            h = int.from_bytes(hmac.new(seed, f"periodic|{i}".encode(), hashlib.sha256).digest(), "big")
            i += 1
            target, acc = h % sum(ELEMENT_BY_SYMBOL[s].rarity_weight for s in pool), 0
            for s in pool:
                acc += ELEMENT_BY_SYMBOL[s].rarity_weight
                if target < acc:
                    break
            out.append(s)
            pool.remove(s)
    return tuple(out)


def p_in_table(role: str, symbol: str) -> float:
    ws = {s: ELEMENT_BY_SYMBOL[s].rarity_weight for s in ROLE_MEMBERS[role]}
    total = sum(ws.values())
    if ROLE_SLOTS[role] == 1:
        return ws[symbol] / total
    assert ROLE_SLOTS[role] == 2
    return ws[symbol] / total + sum(w / total * ws[symbol] / (total - w) for s, w in ws.items() if s != symbol)


print(f"\n[3] Stratifikasi — {N_SEEDS} tabel")
TABLES = [build_periodic_table(s) for s in SEEDS]
bad = Counter()
slot_counts = Counter()
for seed, t in zip(SEEDS, TABLES):
    syms = t.symbols
    bad["len≠28 / dobel"] += len(syms) != 28 or len(set(syms)) != 28
    bad["core tidak lengkap / urutan salah"] += tuple(e.symbol for e in t.core) != CORE_SET
    bad["alokasi slot ≠ ROLE_SLOTS"] += t.role_counts() != dict(ROLE_SLOTS)
    bad["unsur slot bukan anggota role-nya"] += any(e.symbol not in ROLE_MEMBERS[r] for r, e in t.slots)
    bad["status bukan core/role"] += any(element_status(s) not in ("core", *ROLE_ORDER) for s in syms)
    bad["excluded/synthetic muncul"] += bool(FORBIDDEN & set(syms))
    bad["seed_fingerprint salah"] += t.seed_fingerprint != seed_fingerprint(seed)
    bad["≠ implementasi referensi"] += t.variable_symbols != reference_slots(seed)
    for r, e in t.slots:
        slot_counts[(r, e.symbol)] += 1
for label in ("len≠28 / dobel", "core tidak lengkap / urutan salah", "alokasi slot ≠ ROLE_SLOTS",
              "unsur slot bukan anggota role-nya", "status bukan core/role", "excluded/synthetic muncul",
              "seed_fingerprint salah", "≠ implementasi referensi"):
    check(f"{label}: 0 tabel", bad[label], 0)
check("deterministik (500 tabel dibangun ulang)", [build_periodic_table(s) for s in SEEDS[:500]], TABLES[:500])
exp_slot = {(r, s): N_SEEDS * p_in_table(r, s) for r in ROLE_ORDER for s in ROLE_MEMBERS[r]}
var_slot = {k: v * (1 - v / N_SEEDS) for k, v in exp_slot.items()}
z, worst = max_abs_z(slot_counts, exp_slot, var_slot)
check(f"frekuensi slot sesuai rarity_weight (|z| terbesar {abs(z):.2f} di {worst})", abs(z) < Z_LIMIT, True)
check("KAT tabel test_seed('world_periodic:kat')",
      build_periodic_table(test_seed("world_periodic:kat")).variable_symbols,
      ("B", "Be", "As", "Tl", "Li", "Na", "Pd", "Sm", "Ac"))

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n[4] Isolasi — {N_SEEDS} seed: GeneticEngine & material_gen ⊆ tabel server")
g_engine, m_engine = GeneticEngine(), MaterialEngine()
PROFILES = []
iso = Counter()
seen_profile, seen_nodes = set(), set()
for seed, t in zip(SEEDS, TABLES):
    p = g_engine.generate_profile(0, seed, table=t)
    PROFILES.append(p)
    allowed = set(t.symbols)
    four = (p.dominant_metal_element, p.secondary_metal_element,
            p.dominant_nonmetal_element, p.secondary_nonmetal_element)
    iso["4 output profil ⊄ tabel"] += not {e.symbol for e in four} <= allowed
    iso["kelas salah (logam/non-logam)"] += not (
        ig._METAL_CATEGORIES >= {four[0].category, four[1].category}
        and ig._NONMETAL_CATEGORIES >= {four[2].category, four[3].category})
    iso["sekunder = dominan"] += four[0].symbol == four[1].symbol or four[2].symbol == four[3].symbol
    iso["profile.periodic_table ≠ tabel"] += p.periodic_table != t.symbols
    nodes = {n.element_symbol for n in m_engine.generate_geology(p, seed).ore_nodes}
    iso["node material_gen ⊄ tabel"] += not nodes <= allowed
    seen_profile |= {e.symbol for e in four}
    seen_nodes |= nodes
for label in ("4 output profil ⊄ tabel", "kelas salah (logam/non-logam)", "sekunder = dominan",
              "profile.periodic_table ≠ tabel", "node material_gen ⊄ tabel"):
    check(f"{label}: 0 seed", iso[label], 0)
check("excluded/synthetic TIDAK PERNAH muncul (gabungan semua seed, profil + node)",
      sorted(FORBIDDEN & (seen_profile | seen_nodes)), [])
print(f"  ·  unsur berbeda yang pernah muncul: profil {len(seen_profile)}, node {len(seen_nodes)}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n[5] Tanpa fallback ke daftar global")
s0, t_0 = SEEDS[0], TABLES[0]
raises("generate_profile tanpa tabel → TypeError", TypeError, lambda: g_engine.generate_profile(0, s0))
raises("tabel milik seed lain → ValueError", ValueError, lambda: g_engine.generate_profile(0, s0, table=TABLES[1]))
nonmetal_only = ServerPeriodicTable(seed_fingerprint(s0),
                                    tuple(e for e in t_0.core if GeneticEngine.is_nonmetal(e)), ())
raises("tabel tanpa logam → ValueError", ValueError, lambda: g_engine.generate_profile(0, s0, table=nonmetal_only))
one_nonmetal = ServerPeriodicTable(seed_fingerprint(s0),
                                   tuple(e for e in t_0.core if GeneticEngine.is_metal(e)) + (ELEMENT_BY_SYMBOL["O"],), ())
raises("tabel dengan 1 non-logam → ValueError", ValueError, lambda: g_engine.generate_profile(0, s0, table=one_nonmetal))
p0 = PROFILES[0]
outside = dataclasses.replace(p0, periodic_table=tuple(s for s in p0.periodic_table
                                                       if s != p0.dominant_metal_element.symbol))
raises("material_gen: node di luar tabel → ValueError", ValueError, lambda: m_engine.generate_geology(outside, s0))

K = 300
with mock.patch.object(ig, "_METAL_ELEMENTS", []), mock.patch.object(ig, "_NONMETAL_ELEMENTS", []), \
        mock.patch.object(ig, "_ELEMENT_BY_ATOMIC", {}), mock.patch.object(ig, "ELEMENTS", ()), \
        mock.patch.object(wp, "ELEMENT_BY_SYMBOL", {}):
    emptied = [GeneticEngine().generate_profile(0, s, table=t) for s, t in zip(SEEDS[:K], TABLES[:K])]
check(f"katalog global dikosongkan → {K} profil identik (tidak ada yang membaca daftar global)",
      emptied, PROFILES[:K])
engine_src = inspect.getsource(GeneticEngine)
check("kode GeneticEngine tidak menyebut daftar unsur global",
      [n for n in ("_METAL_ELEMENTS", "_NONMETAL_ELEMENTS", "_ELEMENT_BY_ATOMIC", "ELEMENTS)", "ELEMENTS,")
       if n in engine_src], [])
check("GeneticEngine tidak punya pool kelas lagi",
      [a for a in ("_METALS", "_NONMETALS", "_METAL_CW", "_NONMETAL_CW", "metal_pool", "nonmetal_pool", "_cws_element")
       if hasattr(GeneticEngine, a)], [])


def imported_modules(path: str) -> set:
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


WORLDGEN_FILES = ("world_stream.py", "world_periodic.py", "identitas_genetik.py", "material_gen.py",
                  "resource_spawner.py", "worldgen.py")
check("tidak ada `random`/`secrets`/`numpy` di jalur worldgen",
      {f: sorted(imported_modules(os.path.join(ROOT, f)) & {"random", "secrets", "numpy"}) for f in WORLDGEN_FILES},
      {f: [] for f in WORLDGEN_FILES})

# ─────────────────────────────────────────────────────────────────────────────
print("\n[6] Sekunder: dominan dikeluarkan, bobot dinormalisasi ulang, satu draw")
# 6a — unit: fixed pool, many draws
pool = tuple(sorted((e for e in t_0.elements if GeneticEngine.is_metal(e)), key=lambda e: e.atomic_number))
dom = max(pool, key=W)
rest = GeneticEngine._without(pool, dom)
H = [stream(test_seed("langkah4:secondary-unit"), DOMAIN_GENETIC, i) for i in range(40_000)]
obs = Counter(GeneticEngine._pick(h, rest).symbol for h in H)
w_rest = sum(W(e) for e in rest)
exp = {e.symbol: len(H) * W(e) / w_rest for e in rest}
z, worst = max_abs_z(obs, exp, {k: v * (1 - v / len(H)) for k, v in exp.items()})
check(f"unit: frekuensi ≈ w/(W − w_dominan) di pool tetap (|z| terbesar {abs(z):.2f})", abs(z) < Z_LIMIT, True)
check("unit: dominan tidak pernah terpilih", obs[dom.symbol], 0)


def by_interval(h: int, candidates) -> str:
    total = sum(W(e) for e in candidates)
    target, lo = h % total, 0
    for e in candidates:
        if lo <= target < lo + W(e):
            return e.symbol
        lo += W(e)


check("unit: hasil = definisi interval kumulatif (40.000 draw)",
      sum(GeneticEngine._pick(h, rest).symbol != by_interval(h, rest) for h in H), 0)

# 6b/6c/6d — end-to-end over all seeds, expectation computed per seed
exp_dom, var_dom, obs_dom = defaultdict(float), defaultdict(float), Counter()
exp_sec, var_sec, obs_sec = defaultdict(float), defaultdict(float), Counter()
nb_obs, nb_new, nb_new_var, nb_old = 0, 0.0, 0.0, 0.0
for t, p in zip(TABLES, PROFILES):
    for cls, d_el, s_el, kind in ((GeneticEngine.is_metal, p.dominant_metal_element, p.secondary_metal_element, "m"),
                                  (GeneticEngine.is_nonmetal, p.dominant_nonmetal_element,
                                   p.secondary_nonmetal_element, "n")):
        cand = sorted((e for e in t.elements if cls(e)), key=lambda e: e.atomic_number)
        total = sum(W(e) for e in cand)
        w_dom = dominant_weight(d_el.rarity_weight)
        for e in cand:
            pd = W(e) / total
            exp_dom[(kind, e.symbol)] += pd
            var_dom[(kind, e.symbol)] += pd * (1 - pd)
            if e.symbol != d_el.symbol:
                ps = W(e) / (total - w_dom)
                exp_sec[(kind, e.symbol)] += ps
                var_sec[(kind, e.symbol)] += ps * (1 - ps)
        obs_dom[(kind, d_el.symbol)] += 1
        obs_sec[(kind, s_el.symbol)] += 1
        # Z-neighbour of the dominant inside this pool (the old "next by Z" rule)
        idx = [e.symbol for e in cand].index(d_el.symbol)
        nb = cand[(idx + 1) % len(cand)]
        p_new = W(nb) / (total - w_dom)
        nb_new += p_new
        nb_new_var += p_new * (1 - p_new)
        nb_old += (W(nb) + w_dom) / total
        nb_obs += s_el.symbol == nb.symbol
z, worst = max_abs_z(obs_dom, exp_dom, var_dom)
check(f"dominan: frekuensi ≈ w/W per tabel, {N_SEEDS} seed (|z| terbesar {abs(z):.2f} di {worst})",
      abs(z) < Z_LIMIT, True)
z, worst = max_abs_z(obs_sec, exp_sec, var_sec)
check(f"sekunder: frekuensi ≈ w/(W − w_dominan) per tabel (|z| terbesar {abs(z):.2f} di {worst})",
      abs(z) < Z_LIMIT, True)
z_new = (nb_obs - nb_new) / math.sqrt(nb_new_var)
z_old = (nb_obs - nb_old) / math.sqrt(nb_new_var)
print(f"  ·  sekunder = tetangga-Z dominan: teramati {nb_obs}, aturan baru {nb_new:.1f}, aturan lama {nb_old:.1f}")
check(f"tidak ada bonus tetangga-Z: cocok aturan baru (z={z_new:+.2f})", abs(z_new) < Z_LIMIT, True)
check(f"… dan jauh dari aturan lama 'unsur berikutnya menurut Z' (z={z_old:+.1f})", z_old < -10, True)
check("KAT profil test_seed('langkah4:0'): dominan/sekunder logam & non-logam",
      tuple(e.symbol for e in (PROFILES[0].dominant_metal_element, PROFILES[0].secondary_metal_element,
                               PROFILES[0].dominant_nonmetal_element, PROFILES[0].secondary_nonmetal_element)),
      ("Mg", "Pb", "S", "O"))

print(f"\n  ·  {N_SEEDS} seed dalam {time.time() - t0:.1f}s")
print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
raise SystemExit(1 if failures else 0)
