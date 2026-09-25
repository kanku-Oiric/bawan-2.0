"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLD_STREAM.PY  —  Stage 2: Per-Domain Entropy Streams              ║
║         Deterministik, tak terbatas, independen antar-domain                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  stream(seed, domain, i) = int.from_bytes(HMAC-SHA256(seed, "{domain}|{i}")) ║
║  unit(h)                 = (h >> 203) / 2**53        → [0, 1), 53-bit        ║
║                                                                              ║
║  • Setiap domain adalah fungsi terpisah dari seed: menambah, menghapus,     ║
║    atau mengubah pemakaian domain "wood" TIDAK mengubah satu bit pun di     ║
║    domain "genetic" / "material" / "spawner" / "periodic".                  ║
║  • Domain hanya boleh berasal dari konstanta di bawah — nama domain yang    ║
║    tidak terdaftar ditolak, supaya salah ketik tidak diam-diam membuat      ║
║    dunia yang berbeda.                                                       ║
║  • Tidak ada state global, tidak ada modul `random`.                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import hmac

# ── Domain registry ──────────────────────────────────────────────────────────
DOMAIN_GENETIC:  str = "genetic"    # identitas_genetik.GeneticEngine
DOMAIN_MATERIAL: str = "material"   # material_gen.MaterialEngine
DOMAIN_SPAWNER:  str = "spawner"    # resource_spawner.ResourceSpawner
DOMAIN_PERIODIC: str = "periodic"   # tabel periodik per server (LANGKAH 4)
DOMAIN_MINING:   str = "mining"     # roll per ayunan — HANYA lewat mining_roll()
# Reserved — belum dipakai, tapi namanya sudah dikunci.
DOMAIN_WOOD:     str = "wood"
DOMAIN_FLORA:    str = "flora"
DOMAIN_MOB:      str = "mob"
DOMAIN_SEASON:   str = "season"

# Domains read with an integer index i via stream() / DomainStream.
ALL_DOMAINS = frozenset({
    DOMAIN_GENETIC, DOMAIN_MATERIAL, DOMAIN_SPAWNER, DOMAIN_PERIODIC,
    DOMAIN_WOOD, DOMAIN_FLORA, DOMAIN_MOB, DOMAIN_SEASON,
})
# DOMAIN_MINING is deliberately NOT in ALL_DOMAINS: its pre-image is a composite
# key, so stream(seed, "mining", i) is rejected and mining_roll() is the only way in.

SEED_BYTES: int = 32
_UNIT_SHIFT: int = 256 - 53
_UNIT_SCALE: float = float(2 ** 53)


def stream(seed: bytes, domain: str, i: int) -> int:
    """256-bit unsigned integer ke-i dari stream `domain`."""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != SEED_BYTES:
        raise ValueError(f"seed harus {SEED_BYTES} byte")
    if domain not in ALL_DOMAINS:
        raise ValueError(f"domain tidak terdaftar: {domain!r}")
    if not isinstance(i, int) or isinstance(i, bool) or i < 0:
        raise ValueError(f"index stream harus int ≥ 0, dapat {i!r}")
    digest = hmac.new(bytes(seed), f"{domain}|{i}".encode("ascii"), hashlib.sha256).digest()
    return int.from_bytes(digest, "big")


def unit(h: int) -> float:
    """53 bit teratas dari h → float di [0, 1)."""
    return (h >> _UNIT_SHIFT) / _UNIT_SCALE


def mining_roll(seed: bytes, guild_id: int, user_id: int, node_id: str, attempt: int) -> int:
    """
    Roll 256-bit untuk SATU ayunan:
        HMAC-SHA256(seed, "mining|{guild_id}|{user_id}|{node_id}|{attempt}")

    `attempt` (n) adalah nomor percobaan ke-n milik (guild, user), di-increment
    atomik di Supabase SEBELUM roll dihitung.  Semua input kecuali seed tampil
    ke pemain, jadi setelah pepper di-reveal setiap roll bisa dihitung ulang.
    """
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != SEED_BYTES:
        raise ValueError(f"seed harus {SEED_BYTES} byte")
    for name, value in (("guild_id", guild_id), ("user_id", user_id), ("attempt", attempt)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} harus int, dapat {type(value).__name__}")
    if attempt < 1:
        raise ValueError("attempt harus ≥ 1")
    if not isinstance(node_id, str) or not node_id or "|" in node_id:
        raise ValueError("node_id harus string non-kosong tanpa '|'")
    msg = f"{DOMAIN_MINING}|{guild_id}|{user_id}|{node_id}|{attempt}".encode("utf-8")
    return int.from_bytes(hmac.new(bytes(seed), msg, hashlib.sha256).digest(), "big")


def seed_fingerprint(seed: bytes) -> str:
    """
    Sidik jari PUBLIK sebuah seed (untuk ditampilkan).  Satu arah: tidak bisa
    dipakai merekonstruksi seed, jadi aman walau seed bergantung pada pepper.
    """
    return hashlib.sha256(b"BAWAN-FINGERPRINT|" + bytes(seed)).hexdigest()


def test_seed(label: str) -> bytes:
    """Seed KHUSUS self-test/unit-test.  Jangan pernah dipakai untuk dunia sungguhan."""
    return hashlib.sha256(b"BAWAN-TEST-SEED|" + label.encode("utf-8")).digest()


class DomainStream:
    """Kursor berurutan atas stream(seed, domain, i): setiap draw memakai satu index."""

    def __init__(self, seed: bytes, domain: str, start: int = 0) -> None:
        stream(seed, domain, 0)                  # validate eagerly
        self._seed = bytes(seed)
        self._domain = domain
        self._cursor = start

    @property
    def cursor(self) -> int:
        return self._cursor

    def next_int(self) -> int:
        value = stream(self._seed, self._domain, self._cursor)
        self._cursor += 1
        return value

    def next_unit(self) -> float:
        return unit(self.next_int())

    def next_below(self, n: int) -> int:
        """Integer di [0, n).  Bias modulo ≤ n / 2**256 — dapat diabaikan."""
        if n <= 0:
            raise ValueError("n harus > 0")
        return self.next_int() % n

    def next_in_range(self, lo: float, hi: float) -> float:
        return lo + self.next_unit() * (hi - lo)


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python world_stream.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))

    S = bytes(range(32))

    print("\n[1] Known-answer (membekukan Stage 2)")
    check("stream(S, genetic, 0) == HMAC langsung",
          stream(S, DOMAIN_GENETIC, 0),
          int.from_bytes(hmac.new(S, b"genetic|0", hashlib.sha256).digest(), "big"))
    check("KAT hex stream(S, material, 7)", f"{stream(S, DOMAIN_MATERIAL, 7):064x}", "bb2224d5ad9261b129a7164af2b000fa3a7a80738089fce7d73e3b9838ac7b30")

    print("\n[2] unit()")
    check("unit(0) = 0", unit(0), 0.0)
    check("unit(2**256 − 1) < 1", unit(2 ** 256 - 1) < 1.0, True)
    check("unit pakai 53 bit teratas", unit(1 << 255), 0.5)
    ds = DomainStream(S, DOMAIN_SPAWNER)
    vals = [ds.next_unit() for _ in range(20000)]
    check("20.000 draw semua di [0,1)", all(0.0 <= v < 1.0 for v in vals), True)
    check("rata-rata ≈ 0.5 (±0.01)", abs(sum(vals) / len(vals) - 0.5) < 0.01, True)

    print("\n[3] Independensi domain")
    before = [stream(S, DOMAIN_GENETIC, i) for i in range(50)]
    _ = [stream(S, DOMAIN_WOOD, i) for i in range(1000)]    # "memakai" domain lain
    check("pakai domain wood tidak mengubah genetic", [stream(S, DOMAIN_GENETIC, i) for i in range(50)], before)
    check("domain berbeda → nilai berbeda", stream(S, DOMAIN_GENETIC, 3) != stream(S, DOMAIN_MATERIAL, 3), True)
    check("seed berbeda → nilai berbeda", stream(S, DOMAIN_GENETIC, 0) != stream(test_seed("x"), DOMAIN_GENETIC, 0), True)

    print("\n[4] Input ilegal ditolak")
    for label, args in [("domain salah ketik", (S, "genetik", 0)), ("index negatif", (S, DOMAIN_GENETIC, -1)),
                        ("seed pendek", (S[:16], DOMAIN_GENETIC, 0)), ("seed string", ("aa" * 32, DOMAIN_GENETIC, 0))]:
        try:
            stream(*args)
            check(label, "diterima", "ValueError")
        except ValueError:
            check(label, "ValueError", "ValueError")

    print("\n[4b] mining_roll")
    NODE = "123:Fe:hematite:0"
    check("mining_roll == HMAC langsung", mining_roll(S, 123, 456, NODE, 7),
          int.from_bytes(hmac.new(S, f"mining|123|456|{NODE}|7".encode(), hashlib.sha256).digest(), "big"))
    rolls = {mining_roll(S, 123, 456, NODE, n) for n in range(1, 1001)}
    check("1000 attempt berurutan → 1000 roll unik", len(rolls), 1000)
    check("user beda → roll beda", mining_roll(S, 123, 456, NODE, 1) != mining_roll(S, 123, 457, NODE, 1), True)
    check("node beda → roll beda", mining_roll(S, 123, 456, NODE, 1) != mining_roll(S, 123, 456, NODE + "x", 1), True)
    for label, args in [("stream(seed,'mining',i) ditolak", None), ("attempt 0", (S, 1, 1, NODE, 0)),
                        ("node_id dengan '|'", (S, 1, 1, "a|b", 1)), ("attempt bool", (S, 1, 1, NODE, True))]:
        try:
            stream(S, DOMAIN_MINING, 0) if args is None else mining_roll(*args)
            check(label, "diterima", "ValueError")
        except ValueError:
            check(label, "ValueError", "ValueError")

    print("\n[5] DomainStream = stream berurutan")
    ds = DomainStream(S, DOMAIN_PERIODIC)
    check("draw ke-k memakai index k", [ds.next_int() for _ in range(5)], [stream(S, DOMAIN_PERIODIC, i) for i in range(5)])
    check("cursor maju", ds.cursor, 5)
    check("next_below dalam rentang", all(0 <= DomainStream(S, DOMAIN_MOB, k).next_below(7) < 7 for k in range(500)), True)
    check("fingerprint ≠ seed & deterministik",
          (seed_fingerprint(S) != S.hex(), seed_fingerprint(S) == seed_fingerprint(S)), (True, True))

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
