"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLD_SEED.PY  —  Stage 1: Seed Derivation (deterministik)           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  seed = HMAC-SHA256(key = PEPPER[algo_version],                              ║
║                     msg = "BAWAN|{algo_version}|{guild_id}|{world_nonce}")   ║
║                                                                              ║
║  • guild_id WAJIB di pre-image: dua server yang mendaftar pada ronde drand  ║
║    yang sama mendapat world_nonce identik — guild_id yang membedakan.       ║
║  • algo_version diambil dari baris server (server_registry), bukan          ║
║    konstanta global.  Setiap versi punya pepper & commitment sendiri.       ║
║  • Pre-image kanonik & tervalidasi: algo_version ^v[0-9]+$, guild_id         ║
║    desimal, world_nonce hex-64 lowercase → tidak ada dua input berbeda      ║
║    yang menghasilkan string yang sama.                                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  COMMIT–REVEAL PEPPER                                                        ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  commitment = SHA-256(pepper_bytes)   (pepper_bytes = bytes.fromhex(env))   ║
║  • Dipublikasikan sekarang: tabel world_commitments (insert-only), README,  ║
║    dan webhook saksi.  Bot MENOLAK start kalau pepper di .env tidak cocok   ║
║    dengan commitment yang sudah tersimpan → pepper tidak bisa diganti diam- ║
║    diam di tengah eksperimen (mengganti pepper = mengganti semua dunia).   ║
║  • Reveal di akhir eksperimen: publikasikan pepper hex; siapa pun cek       ║
║    SHA-256(pepper) == commitment lalu hitung ulang seed tiap server.        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Dict, List, Mapping

SEED_TAG: str = "BAWAN"

# Satu env var per algo_version.  Versi baru = env var baru; env var versi
# lama TIDAK boleh dihapus selama masih ada server di versi itu.
PEPPER_ENV_BY_VERSION: Dict[str, str] = {
    "v1": "WORLD_SEED_PEPPER",
}
MIN_PEPPER_BYTES: int = 32
PEPPER_GENERATE_CMD: str = 'python -c "import secrets; print(secrets.token_hex(32))"'

_ALGO_RE  = re.compile(r"^v[0-9]+$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


class PepperError(RuntimeError):
    """Pepper tidak ada / tidak valid — bot wajib menolak start."""


class CommitmentMismatch(RuntimeError):
    """Pepper di .env tidak cocok dengan commitment yang sudah dipublikasikan."""


def parse_pepper(value: str, env_name: str = "WORLD_SEED_PEPPER") -> bytes:
    text = (value or "").strip()
    if not text:
        raise PepperError(
            f"{env_name} kosong. Bot menolak start — pepper kosong akan membuat seed bisa ditebak.\n"
            f"Buat sendiri dengan:  {PEPPER_GENERATE_CMD}\n"
            f"lalu isi {env_name}=<hasilnya> di .env (JANGAN dibagikan sebelum reveal)."
        )
    if not re.fullmatch(r"[0-9a-fA-F]+", text) or len(text) % 2:
        raise PepperError(f"{env_name} harus berupa hex. Buat ulang dengan:  {PEPPER_GENERATE_CMD}")
    raw = bytes.fromhex(text)
    if len(raw) < MIN_PEPPER_BYTES:
        raise PepperError(
            f"{env_name} terlalu pendek ({len(raw)} byte, minimal {MIN_PEPPER_BYTES}). "
            f"Buat ulang dengan:  {PEPPER_GENERATE_CMD}"
        )
    return raw


def load_peppers(env: Mapping[str, str]) -> Dict[str, bytes]:
    """Pepper untuk SEMUA algo_version yang dikenal.  Satu saja hilang → PepperError."""
    return {version: parse_pepper(env.get(name, ""), name) for version, name in PEPPER_ENV_BY_VERSION.items()}


def pepper_commitment(pepper: bytes) -> str:
    return hashlib.sha256(pepper).hexdigest()


def seed_preimage(algo_version: str, guild_id: int, world_nonce: str) -> str:
    if not _ALGO_RE.match(algo_version):
        raise ValueError(f"algo_version tidak kanonik: {algo_version!r}")
    if not isinstance(guild_id, int) or isinstance(guild_id, bool):
        raise ValueError(f"guild_id harus int, dapat {type(guild_id).__name__}")
    if not _NONCE_RE.match(world_nonce):
        raise ValueError("world_nonce harus hex-64 lowercase")
    return f"{SEED_TAG}|{algo_version}|{guild_id}|{world_nonce}"


def derive_seed(pepper: bytes, algo_version: str, guild_id: int, world_nonce: str) -> bytes:
    msg = seed_preimage(algo_version, guild_id, world_nonce).encode("ascii")
    return hmac.new(pepper, msg, hashlib.sha256).digest()


def reconcile_commitments(db, peppers: Mapping[str, bytes]) -> List[str]:
    """
    Pastikan setiap pepper cocok dengan commitment tersimpan (insert-only).
    Versi yang belum punya commitment → di-commit sekarang (ON CONFLICT DO
    NOTHING, lalu dibaca ulang: baris tersimpan yang menang).

    Returns: daftar algo_version yang BARU di-commit pada panggilan ini.
    Raises : CommitmentMismatch — bot wajib menolak start.
    """
    newly: List[str] = []
    for version, pepper in sorted(peppers.items()):
        mine = pepper_commitment(pepper)
        row = db.get_commitment_row(version)
        if row is None:
            db.insert_commitment_row(version, mine)
            row = db.get_commitment_row(version)
            newly.append(version)
        if row is None or row["pepper_commitment"] != mine:
            raise CommitmentMismatch(
                f"Pepper {PEPPER_ENV_BY_VERSION[version]} TIDAK cocok dengan commitment {version} yang sudah "
                f"dipublikasikan ({row['pepper_commitment'] if row else '∅'}). Bot menolak start: mengganti pepper "
                f"= mengganti dunia semua server {version}. Kembalikan pepper yang asli."
            )
    return newly


class SeedService:
    """Stage 1 untuk main_core: seed per server dari WorldRecord yang sudah aktif."""

    def __init__(self, peppers: Mapping[str, bytes]) -> None:
        self._peppers = dict(peppers)

    def commitments(self) -> Dict[str, str]:
        return {v: pepper_commitment(p) for v, p in sorted(self._peppers.items())}

    def seed_for(self, algo_version: str, guild_id: int, world_nonce: str) -> bytes:
        try:
            pepper = self._peppers[algo_version]
        except KeyError:
            raise PepperError(f"tidak ada pepper untuk {algo_version}") from None
        return derive_seed(pepper, algo_version, guild_id, world_nonce)


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python world_seed.py
#   python world_seed.py --commitment   → cetak commitment dari pepper di .env
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    if "--commitment" in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        try:
            peppers = load_peppers(os.environ)
        except PepperError as exc:
            print(f"\n✗ {exc}\n")
            raise SystemExit(1)
        for version, pepper in peppers.items():
            print(f"{version}  SHA-256(pepper) = {pepper_commitment(pepper)}")
        raise SystemExit(0)

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))

    P1 = bytes(range(32))
    P2 = bytes(range(1, 33))
    N  = "22162721bc8e0f82667dce0e161d9d4f844faf09b2a5b0cab77419a3481de339"
    G  = 1234567890123456789

    print("\n[1] Determinisme & known-answer (membekukan algoritma v1)")
    check("dua kali panggil identik", derive_seed(P1, "v1", G, N), derive_seed(P1, "v1", G, N))
    check("KAT v1", derive_seed(P1, "v1", G, N).hex(),
          "beff4d0ee37a0f2c784783b6f4d3c84c7b6b87614f2097170ba4082db7b71ad1")
    check("pre-image kanonik", seed_preimage("v1", G, N), f"BAWAN|v1|{G}|{N}")

    print("\n[2] Setiap komponen pre-image mengubah seed")
    base = derive_seed(P1, "v1", G, N)
    check("guild_id beda, nonce SAMA → seed beda", base != derive_seed(P1, "v1", G + 1, N), True)
    check("algo_version beda → seed beda", base != derive_seed(P1, "v2", G, N), True)
    check("pepper beda → seed beda", base != derive_seed(P2, "v1", G, N), True)
    check("nonce beda → seed beda", base != derive_seed(P1, "v1", G, "0" * 64), True)

    print("\n[3] Input tidak kanonik ditolak")
    for label, args in [
        ("algo_version dengan '|'", ("v1|x", G, N)),
        ("nonce huruf besar", ("v1", G, N.upper())),
        ("nonce pendek", ("v1", G, N[:-2])),
        ("guild_id string", ("v1", str(G), N)),
    ]:
        try:
            seed_preimage(*args)
            check(label, "diterima", "ValueError")
        except ValueError:
            check(label, "ValueError", "ValueError")

    print("\n[4] Pepper: kosong / pendek / bukan hex → bot menolak start")
    for label, env in [("tidak ada", {}), ("kosong", {"WORLD_SEED_PEPPER": "  "}),
                       ("pendek", {"WORLD_SEED_PEPPER": "ab" * 16}), ("bukan hex", {"WORLD_SEED_PEPPER": "zz" * 32})]:
        try:
            load_peppers(env)
            check(label, "diterima", "PepperError")
        except PepperError:
            check(label, "PepperError", "PepperError")
    check("pepper valid diterima", load_peppers({"WORLD_SEED_PEPPER": P1.hex()}), {"v1": P1})

    print("\n[5] Commit–reveal: commitment insert-only & pepper tidak bisa diganti")

    class FakeDB:
        def __init__(self):
            self.rows: Dict[str, dict] = {}
        def get_commitment_row(self, v):
            return dict(self.rows[v]) if v in self.rows else None
        def insert_commitment_row(self, v, c):
            self.rows.setdefault(v, {"algo_version": v, "pepper_commitment": c})

    db = FakeDB()
    check("start pertama → v1 di-commit", reconcile_commitments(db, {"v1": P1}), ["v1"])
    check("commitment = SHA-256(pepper)", db.rows["v1"]["pepper_commitment"], hashlib.sha256(P1).hexdigest())
    check("start berikutnya, pepper sama → tidak commit ulang", reconcile_commitments(db, {"v1": P1}), [])
    try:
        reconcile_commitments(db, {"v1": P2})
        check("pepper diganti → bot menolak start", "jalan", "CommitmentMismatch")
    except CommitmentMismatch:
        check("pepper diganti → bot menolak start", "CommitmentMismatch", "CommitmentMismatch")
    check("commitment lama tetap utuh", db.rows["v1"]["pepper_commitment"], hashlib.sha256(P1).hexdigest())

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
