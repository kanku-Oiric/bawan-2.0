"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLD_REGISTRY.PY  —  Stage 0: Server Registration & World Nonce     ║
║         Satu-satunya tempat randomness masuk ke pipeline worldgen            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ATURAN                                                                      ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  • world_nonce = randomness ronde drand quicknet PERTAMA yang waktunya      ║
║    > registered_at.  registered_at diisi server Postgres (now()), jadi      ║
║    ronde target selalu di masa depan saat registrasi → tidak ada pihak,     ║
║    termasuk operator, yang bisa tahu nonce sebelum server terdaftar.        ║
║  • Tidak ada fallback lokal.  Drand tidak bisa dihubungi / relay tidak      ║
║    sepakat → server tetap PENDING dan dicoba lagi.                          ║
║  • Insert-only: server_registry & world_nonces ditulis sekali, trigger DB  ║
║    menolak UPDATE / DELETE / TRUNCATE (lihat supabase/schema.sql v3).       ║
║  • RandomnessSource adalah satu-satunya interface ke sumber entropi;        ║
║    ganti sumber = implementasi baru + algo_version baru, stage lain tetap.  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  VERIFIKASI PUBLIK (siapa pun, tanpa bot)                                    ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1. round = floor((registered_at_unix − 1692803367) / 3) + 2                ║
║  2. GET https://api.drand.sh/<chain_hash>/public/<round>                    ║
║     → signature & randomness harus sama dengan /worldproof                  ║
║  3. randomness == SHA-256(signature)   (dan BLS signature valid terhadap    ║
║     public key quicknet — bisa dicek dengan tool resmi drand)               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

import httpx


log = logging.getLogger("world_registry")

# algo_version untuk server yang BARU mendaftar.  Server lama selamanya
# memakai algo_version yang tersimpan di barisnya sendiri.
ALGO_VERSION_CURRENT: str = "v1"

STATUS_PENDING: str = "pending"
STATUS_ACTIVE:  str = "active"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX96 = re.compile(r"^[0-9a-f]{96}$")


class BeaconUnavailable(Exception):
    """Beacon belum bisa dipakai (jaringan, relay tidak sepakat, dsb). Server tetap pending."""


class BeaconNotYet(BeaconUnavailable):
    """Ronde target belum terjadi."""


class RegistryIntegrityError(Exception):
    """Data tersimpan tidak konsisten dengan aturan Stage 0 — dunia TIDAK boleh dipakai."""


@dataclass(frozen=True)
class Beacon:
    source_id:  str
    round:      int
    randomness: str     # hex 64 — inilah world_nonce
    signature:  str     # hex 96 (BLS G1, 48 byte)


@dataclass(frozen=True)
class WorldRecord:
    guild_id:      int
    algo_version:  str
    source_id:     str
    registered_at: datetime
    target_round:  int
    beacon:        Optional[Beacon]

    @property
    def status(self) -> str:
        return STATUS_ACTIVE if self.beacon is not None else STATUS_PENDING

    @property
    def world_nonce(self) -> Optional[str]:
        return self.beacon.randomness if self.beacon else None


def parse_timestamptz(value: str) -> datetime:
    """Parse timestamptz PostgREST secara portable (Python 3.10 tidak terima pecahan ≠ 3/6 digit)."""
    text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", text)
    if m is None:
        raise ValueError(f"timestamp tidak dikenali: {value!r}")
    head, frac, tz = m.groups()
    frac = ((frac or "") + "000000")[:6]
    if re.fullmatch(r"[+-]\d{2}", tz):
        tz += ":00"
    dt = datetime.fromisoformat(f"{head}.{frac}{tz}")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# INTERFACE SUMBER RANDOMNESS
# ─────────────────────────────────────────────────────────────────────────────

class RandomnessSource(ABC):
    """Kontrak Stage 0.  Implementasi baru (beacon lain) cukup memenuhi ini."""

    source_id: str

    @abstractmethod
    def round_after(self, t: datetime) -> int:
        """Ronde pertama yang waktunya SETELAH t (strictly greater)."""

    @abstractmethod
    def round_time(self, round_number: int) -> datetime:
        """Waktu terjadinya ronde."""

    @abstractmethod
    def fetch(self, round_number: int) -> Beacon:
        """Ambil & validasi beacon.  Raise BeaconUnavailable / BeaconNotYet."""

    @abstractmethod
    def public_url(self, round_number: int) -> str:
        """URL publik tempat siapa pun bisa mencocokkan beacon ronde ini."""

    @abstractmethod
    def verification_steps(self) -> str:
        """Langkah verifikasi manual, untuk ditampilkan di /worldproof."""


class DrandQuicknet(RandomnessSource):
    """
    drand quicknet (League of Entropy), skema bls-unchained-g1-rfc9380.
    Parameter diverifikasi dari /info keempat relay publik resmi.

    Integritas per fetch:
      • randomness == SHA-256(signature) untuk setiap respons,
      • minimal MIN_AGREEING relay independen harus mengembalikan signature
        yang IDENTIK — satu relay yang dibajak tidak cukup untuk menyuntik nonce.
    """

    CHAIN_HASH: str   = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
    GENESIS_TIME: int = 1692803367
    PERIOD: int       = 3
    RELAYS: Sequence[str] = (
        "https://api.drand.sh",
        "https://api2.drand.sh",
        "https://api3.drand.sh",
        "https://drand.cloudflare.com",
    )
    MIN_AGREEING: int = 2
    TIMEOUT_SECONDS: float = 5.0

    source_id: str = f"drand-quicknet:{CHAIN_HASH}"

    def __init__(
        self,
        client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=DrandQuicknet.TIMEOUT_SECONDS),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client_factory = client_factory
        self._clock = clock

    def round_after(self, t: datetime) -> int:
        elapsed = t.timestamp() - self.GENESIS_TIME
        if elapsed < 0:
            return 1
        return math.floor(elapsed / self.PERIOD) + 2

    def round_time(self, round_number: int) -> datetime:
        return datetime.fromtimestamp(self.GENESIS_TIME + (round_number - 1) * self.PERIOD, tz=timezone.utc)

    def public_url(self, round_number: int, relay: Optional[str] = None) -> str:
        return f"{relay or self.RELAYS[0]}/{self.CHAIN_HASH}/public/{round_number}"

    def verification_steps(self) -> str:
        return (
            f"1. `ronde = floor((registered_at − {self.GENESIS_TIME}) / {self.PERIOD}) + 2`\n"
            f"2. Buka link relay → `signature` harus identik\n"
            f"3. `world_nonce = SHA-256(signature)`"
        )

    def fetch(self, round_number: int) -> Beacon:
        if self._clock() < self.round_time(round_number):
            raise BeaconNotYet(f"ronde {round_number} belum terjadi")

        votes: Dict[str, int] = {}
        errors: List[str] = []
        with self._client_factory() as client:
            for relay in self.RELAYS:
                try:
                    resp = client.get(self.public_url(round_number, relay))
                    resp.raise_for_status()
                    data = resp.json()
                    sig, rnd = str(data["signature"]).lower(), str(data["randomness"]).lower()
                    if int(data["round"]) != round_number:
                        raise ValueError(f"relay mengembalikan ronde {data['round']}")
                    if not (_HEX96.match(sig) and _HEX64.match(rnd)):
                        raise ValueError("format signature/randomness tidak valid")
                    if hashlib.sha256(bytes.fromhex(sig)).hexdigest() != rnd:
                        raise ValueError("randomness != SHA-256(signature)")
                except Exception as exc:          # one bad relay must not stop the others
                    errors.append(f"{relay}: {exc}")
                    continue
                votes[sig] = votes.get(sig, 0) + 1
                if votes[sig] >= self.MIN_AGREEING:
                    break

        if len(votes) > 1:
            # Two valid-looking but different signatures for one round = someone lies.
            log.error("drand relays DISAGREE on round %d: %s", round_number, list(votes))
            raise BeaconUnavailable(f"relay drand tidak sepakat untuk ronde {round_number}")
        if not votes or max(votes.values()) < self.MIN_AGREEING:
            raise BeaconUnavailable(
                f"kurang dari {self.MIN_AGREEING} relay drand yang menjawab valid: {errors}"
            )
        sig = next(iter(votes))
        return Beacon(self.source_id, round_number, hashlib.sha256(bytes.fromhex(sig)).hexdigest(), sig)


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 0 SERVICE
# ─────────────────────────────────────────────────────────────────────────────

class WorldRegistry:
    """
    Stage 0.  Semua method SINKRON.  Pemisahan sengaja:
      • register() / store_beacon() / load_all()  → hanya DB
      • fetch_beacon()                            → hanya jaringan drand
    supaya main_core bisa menjalankan DB di thread DB-nya sendiri dan fetch
    drand di thread lain (timeout drand tidak menahan flush data player).

    `db` wajib menyediakan: register_server_row, get_world_nonce_row,
    insert_world_nonce_row, load_registry_rows, load_world_nonce_rows.
    """

    def __init__(self, db, sources: Sequence[RandomnessSource]) -> None:
        self._db = db
        self._sources = {s.source_id: s for s in sources}
        self._default = sources[0]

    def source_for(self, record: WorldRecord) -> RandomnessSource:
        try:
            return self._sources[record.source_id]
        except KeyError:
            raise RegistryIntegrityError(f"sumber randomness tidak dikenal: {record.source_id}") from None

    # ── DB side ───────────────────────────────────────────────────────────────

    def register(self, guild_id: int) -> WorldRecord:
        """Insert-or-get atomik di DB.  Baris yang sudah ada TIDAK pernah dibuat ulang."""
        row = self._db.register_server_row(guild_id, ALGO_VERSION_CURRENT, self._default.source_id)
        return self._build(row, self._db.get_world_nonce_row(guild_id))

    def store_beacon(self, record: WorldRecord, beacon: Beacon) -> WorldRecord:
        """Simpan beacon (insert-or-ignore) lalu baca ulang: baris tersimpan yang menang."""
        if beacon.round != record.target_round or beacon.source_id != record.source_id:
            raise RegistryIntegrityError(
                f"beacon ronde {beacon.round} ({beacon.source_id}) bukan milik guild {record.guild_id}"
            )
        self._db.insert_world_nonce_row(record.guild_id, beacon.round, beacon.randomness, beacon.signature)
        return self._build(self._row_of(record), self._db.get_world_nonce_row(record.guild_id))

    def load_all(self) -> Dict[int, WorldRecord]:
        nonces = {int(r["guild_id"]): r for r in self._db.load_world_nonce_rows()}
        return {
            int(row["guild_id"]): self._build(row, nonces.get(int(row["guild_id"])))
            for row in self._db.load_registry_rows()
        }

    # ── Network side ──────────────────────────────────────────────────────────

    def fetch_beacon(self, record: WorldRecord) -> Beacon:
        return self.source_for(record).fetch(record.target_round)

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_of(record: WorldRecord) -> dict:
        return {
            "guild_id": record.guild_id, "algo_version": record.algo_version,
            "randomness_source": record.source_id, "registered_at": record.registered_at.isoformat(),
        }

    def _build(self, row: dict, nonce_row: Optional[dict]) -> WorldRecord:
        registered_at = (
            row["registered_at"] if isinstance(row["registered_at"], datetime)
            else parse_timestamptz(row["registered_at"])
        )
        record = WorldRecord(
            guild_id      = int(row["guild_id"]),
            algo_version  = row["algo_version"],
            source_id     = row["randomness_source"],
            registered_at = registered_at,
            target_round  = 0,
            beacon        = None,
        )
        source = self.source_for(record)
        record = WorldRecord(**{**record.__dict__, "target_round": source.round_after(registered_at)})
        if nonce_row is None:
            return record

        # Re-check every stored nonce: a tampered row must stop the world, not be used.
        sig, rnd, rno = nonce_row["drand_signature"], nonce_row["world_nonce"], int(nonce_row["drand_round"])
        if rno != record.target_round:
            raise RegistryIntegrityError(
                f"guild {record.guild_id}: ronde tersimpan {rno} ≠ ronde aturan {record.target_round}"
            )
        if hashlib.sha256(bytes.fromhex(sig)).hexdigest() != rnd:
            raise RegistryIntegrityError(f"guild {record.guild_id}: world_nonce ≠ SHA-256(signature)")
        return WorldRecord(**{**record.__dict__, "beacon": Beacon(record.source_id, rno, rnd, sig)})


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST
#   python world_registry.py          → offline (transport palsu, DB palsu)
#   python world_registry.py --live   → + ambil beacon asli dari relay drand (read-only)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys
    import threading
    from datetime import timedelta

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    G, P = DrandQuicknet.GENESIS_TIME, DrandQuicknet.PERIOD
    at = lambda s: datetime.fromtimestamp(s, tz=timezone.utc)   # noqa: E731
    src = DrandQuicknet()

    print("\n[1] Aturan 'ronde pertama SETELAH registered_at'")
    for t, want in [(G, 2), (G + 0.001, 2), (G + 2.999, 2), (G + 3, 3), (G + 3.5, 3)]:
        check(f"round_after(genesis+{t - G:g}s)", src.round_after(at(t)), want)
    bad = 0
    for k in range(20000):
        t = at(G + k * 0.37 + 1_000_000)
        r = src.round_after(t)
        bad += not (src.round_time(r) > t >= src.round_time(r - 1))
    check("20.000 titik waktu: round_time(r) > t ≥ round_time(r−1)", bad, 0)

    print("\n[2] Fetch dengan relay palsu")
    log.disabled = True     # the disagreement test logs an ERROR on purpose
    SIG = "ab" * 48
    RND = hashlib.sha256(bytes.fromhex(SIG)).hexdigest()

    def fake_source(responses: Dict[str, object], now: float = G + 10_000) -> DrandQuicknet:
        def handler(request: httpx.Request) -> httpx.Response:
            host = f"{request.url.scheme}://{request.url.host}"
            body = responses.get(host)
            if isinstance(body, int):
                return httpx.Response(body)
            if body is None:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json=body)
        return DrandQuicknet(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
                             clock=lambda: at(now))

    good = {"round": 5, "signature": SIG, "randomness": RND}
    relays = list(DrandQuicknet.RELAYS)
    b = fake_source({relays[0]: good, relays[1]: good}).fetch(5)
    check("2 relay sepakat → beacon", (b.round, b.randomness), (5, RND))
    b = fake_source({relays[0]: 500, relays[1]: None, relays[2]: good, relays[3]: good}).fetch(5)
    check("2 relay mati, 2 sepakat → tetap dapat", b.randomness, RND)
    forged = {"round": 5, "signature": "cd" * 48, "randomness": RND}
    try:
        fake_source({relays[0]: forged, relays[1]: good, relays[2]: good}).fetch(5)
        check("randomness ≠ SHA-256(sig) diabaikan, sisanya sepakat", True, True)
    except BeaconUnavailable:
        check("randomness ≠ SHA-256(sig) diabaikan, sisanya sepakat", False, True)
    other = {"round": 5, "signature": "cd" * 48, "randomness": hashlib.sha256(bytes.fromhex("cd" * 48)).hexdigest()}
    for label, resp in [
        ("relay tidak sepakat → pending", {relays[0]: good, relays[1]: other, relays[2]: None, relays[3]: None}),
        ("cuma 1 relay hidup → pending", {relays[0]: good}),
        ("semua relay mati → pending", {}),
    ]:
        try:
            fake_source(resp).fetch(5)
            check(label, "beacon", "BeaconUnavailable")
        except BeaconUnavailable:
            check(label, "BeaconUnavailable", "BeaconUnavailable")
    try:
        fake_source({relays[0]: good, relays[1]: good}, now=G).fetch(5)
        check("ronde belum terjadi → BeaconNotYet", "beacon", "BeaconNotYet")
    except BeaconNotYet:
        check("ronde belum terjadi → BeaconNotYet", "BeaconNotYet", "BeaconNotYet")

    log.disabled = False
    print("\n[3] WorldRegistry dengan DB palsu (meniru ON CONFLICT DO NOTHING)")

    class FakeDB:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.registry: Dict[int, dict] = {}
            self.nonces: Dict[int, dict] = {}
            self.clock = G + 1_000_000.0

        def register_server_row(self, gid, algo, source):
            with self.lock:                        # the DB's unique index serialises this
                self.clock += 0.01
                self.registry.setdefault(gid, {"guild_id": gid, "algo_version": algo, "randomness_source": source,
                                               "registered_at": at(self.clock).isoformat()})
                return dict(self.registry[gid])

        def get_world_nonce_row(self, gid):
            return dict(self.nonces[gid]) if gid in self.nonces else None

        def insert_world_nonce_row(self, gid, rno, rnd, sig):
            with self.lock:
                self.nonces.setdefault(gid, {"guild_id": gid, "drand_round": rno, "world_nonce": rnd,
                                             "drand_signature": sig})

        def load_registry_rows(self):
            return list(self.registry.values())

        def load_world_nonce_rows(self):
            return list(self.nonces.values())

    db = FakeDB()
    reg = WorldRegistry(db, [src])
    results: List[WorldRecord] = []
    threads = [threading.Thread(target=lambda: results.append(reg.register(777))) for _ in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("50× register paralel → 1 baris", len(db.registry), 1)
    check("50× register paralel → registered_at sama semua", len({r.registered_at for r in results}), 1)
    check("status awal pending", results[0].status, STATUS_PENDING)

    rec = results[0]
    rsig = "ef" * 48
    beacon = Beacon(src.source_id, rec.target_round, hashlib.sha256(bytes.fromhex(rsig)).hexdigest(), rsig)
    rec2 = reg.store_beacon(rec, beacon)
    check("setelah beacon → active", rec2.status, STATUS_ACTIVE)
    again = reg.register(777)
    check("register ulang TIDAK membuat nonce baru", again.world_nonce, rec2.world_nonce)
    try:
        reg.store_beacon(rec, Beacon(src.source_id, rec.target_round + 1, beacon.randomness, rsig))
        check("beacon ronde lain ditolak", "diterima", "RegistryIntegrityError")
    except RegistryIntegrityError:
        check("beacon ronde lain ditolak", "RegistryIntegrityError", "RegistryIntegrityError")

    db.nonces[777]["drand_round"] += 1          # simulate an operator tampering with the table
    try:
        reg.load_all()
        check("baris nonce yang diutak-atik → dunia ditolak", "dipakai", "RegistryIntegrityError")
    except RegistryIntegrityError:
        check("baris nonce yang diutak-atik → dunia ditolak", "RegistryIntegrityError", "RegistryIntegrityError")

    check("timestamptz 5 digit pecahan (Py3.10-safe)",
          parse_timestamptz("2026-09-25 11:30:01.12345+00").microsecond, 123450)

    if "--live" in sys.argv:
        print("\n[live] Beacon asli dari relay drand (read-only)")
        live = DrandQuicknet()
        r = live.round_after(datetime.now(timezone.utc) - timedelta(minutes=5))
        b = live.fetch(r)
        check(f"ronde {r}: SHA-256(signature) == randomness",
              hashlib.sha256(bytes.fromhex(b.signature)).hexdigest(), b.randomness)
        print(f"  ·  verifikasi manual: {live.public_url(r)}")

    if "--live-db" in sys.argv:
        # Writes PERMANENT rows for sentinel guild_id -1 (insert-only by design).
        # Negative ids can never collide with real Discord snowflakes; metrics
        # must filter guild_id > 0.
        import os
        import time as _time
        from concurrent.futures import ThreadPoolExecutor
        from dotenv import load_dotenv
        from db_ekonomi_pusat import EconomyDatabase, connect_test_database

        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        # Refuses to run unless SUPABASE_TEST_* is set AND points to another project.
        try:
            client = connect_test_database(os.environ)
        except RuntimeError as exc:
            print(f"\n✗ {exc}\n")
            raise SystemExit(1)
        gw = EconomyDatabase(client)
        live_reg = WorldRegistry(gw, [DrandQuicknet()])
        TEST_GID = -1

        print("\n[live-db] Supabase TES: idempotensi & insert-only (sentinel guild_id -1, permanen)")
        with ThreadPoolExecutor(max_workers=50) as pool:
            recs = list(pool.map(lambda _: live_reg.register(TEST_GID), range(50)))
        check("50× register paralel → registered_at identik", len({r.registered_at for r in recs}), 1)
        check("tepat 1 baris di server_registry",
              len(client.table("server_registry").select("guild_id").eq("guild_id", TEST_GID).execute().data), 1)

        rec = recs[0]
        if rec.status != STATUS_ACTIVE:
            wait = (live_reg.source_for(rec).round_time(rec.target_round) - datetime.now(timezone.utc)).total_seconds()
            _time.sleep(max(0.0, wait) + 1.0)
            rec = live_reg.store_beacon(rec, live_reg.fetch_beacon(rec))
        check("nonce tersimpan & lolos cek ulang", live_reg.register(TEST_GID).world_nonce, rec.world_nonce)

        for label, action in [
            ("UPDATE server_registry ditolak",
             lambda: client.table("server_registry").update({"algo_version": "hack"}).eq("guild_id", TEST_GID).execute()),
            ("DELETE server_registry ditolak",
             lambda: client.table("server_registry").delete().eq("guild_id", TEST_GID).execute()),
            ("UPDATE world_nonces ditolak",
             lambda: client.table("world_nonces").update({"drand_round": 1}).eq("guild_id", TEST_GID).execute()),
            ("DELETE world_nonces ditolak",
             lambda: client.table("world_nonces").delete().eq("guild_id", TEST_GID).execute()),
        ]:
            try:
                action()
                check(label, "diterima", "ditolak")
            except Exception as exc:
                check(label, "ditolak" if "insert-only" in str(exc) else f"error lain: {exc}", "ditolak")
        check("nonce masih utuh setelah semua percobaan", live_reg.register(TEST_GID).world_nonce, rec.world_nonce)
        print(f"  ·  verifikasi: {DrandQuicknet().public_url(rec.target_round)}")

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
