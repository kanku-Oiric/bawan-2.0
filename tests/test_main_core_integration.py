"""
Offline integration test for main_core: commands, voice, auto mine, Stage 0–2,
mining roll (D), worldgen_version, admin checks and the schema-v7 ledger flow.
Discord objects and Supabase are replaced with fakes (tests/fake_ledger.py for
the ledger RPCs).  Nothing touches the network or a real database.

    python tests/test_main_core_integration.py
"""
import asyncio
import json
import re
import threading
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
TEST_PEPPER_HEX = "11" * 32
os.environ["WORLD_SEED_PEPPER"] = TEST_PEPPER_HEX       # test-only pepper, set before import
os.environ.pop("WORLD_LOG_WEBHOOK", None)
import main_core as mc  # noqa: E402
from ore import OreItem  # noqa: E402
from crystal import CrystalItem  # noqa: E402
from economy_engine import EconomyEngine  # noqa: E402
from currency_engine import CurrencyEngine, preview_manifest  # noqa: E402
from world_registry import WorldRegistry, DrandQuicknet, WorldRecord, Beacon  # noqa: E402
from world_stream import seed_fingerprint  # noqa: E402
import hashlib  # noqa: E402
import httpx  # noqa: E402
from decimal import Decimal  # noqa: E402
from fake_ledger import LedgerModel  # noqa: E402

failures = 0


def check(label, got, expected):
    global failures
    ok = got == expected
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))


def embed_ok(em):
    d = em.to_dict()
    total = len(d.get("title", "")) + len(d.get("description", "")) + len(d.get("footer", {}).get("text", ""))
    for f in d.get("fields", []):
        if len(f["name"]) > 256 or len(f["value"]) > 1024 or not f["value"]:
            return False
        total += len(f["name"]) + len(f["value"])
    return total <= 6000 and len(d.get("description", "")) <= 4096


# ── Fakes ───────────────────────────────────────────────────────────────────

class FakeMember:
    def __init__(self, uid, bot=False, mute=False, deaf=False):
        self.id, self.bot = uid, bot
        self.voice = SimpleNamespace(self_mute=mute, self_deaf=deaf)
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png")


class FakeChannel:
    def __init__(self, cid, name, members=None):
        self.id, self.name, self.members = cid, name, members or []
        self.mention = f"<#{cid}>"


class FakeDB(LedgerModel):
    """Stands in for db_ekonomi_pusat.EconomyDatabase: schema-v7 ledger model + the other tables."""
    def __init__(self, currencies=None, configs=None):
        super().__init__(clock=lambda: self.now)
        self.now = 0.0                                   # DB clock (seconds), advanced by tests
        self.policy["max_swings_per_minute"] = None      # most tests swing a lot at t=0; [R] tests the limit
        self.currencies = currencies or {}
        self.configs = configs or {}
        self.upserted_configs = []
        self.upserted_currencies = []

    @property
    def mining_down(self): return self.swing_down
    @mining_down.setter
    def mining_down(self, value): self.swing_down = value

    def verify_schema(self): pass
    def load_currencies(self): return dict(self.currencies)
    def load_voice_configs(self): return dict(self.configs)

    # Stage 1 commitments + witness outbox (insert-only semantics)
    def get_commitment_row(self, v):
        rows = self.__dict__.setdefault("commit_rows", {})
        return dict(rows[v]) if v in rows else None
    def insert_commitment_row(self, v, c):
        self.__dict__.setdefault("commit_rows", {}).setdefault(
            v, {"algo_version": v, "pepper_commitment": c, "committed_at": "2026-09-25T00:00:00+00:00"})
    def load_commitment_rows(self): return list(self.__dict__.get("commit_rows", {}).values())
    def load_witness_keys(self): return set(self.__dict__.get("witness_rows", {}))

    def insert_witness_row(self, key, event_id):
        self.__dict__.setdefault("witness_rows", {}).setdefault(key, event_id)
    def upsert_voice_config(self, c): self.upserted_configs.append(c)
    def upsert_currency(self, m): self.upserted_currencies.append(m)

    # Stage 0 (insert-only semantics)
    registry_past = True
    def register_server_row(self, gid, algo, source, worldgen):
        self.__dict__.setdefault("registry", {})
        when = "2025-01-01T00:00:00.5+00:00" if self.registry_past else datetime.now(timezone.utc).isoformat()
        self.registry.setdefault(gid, {"guild_id": gid, "algo_version": algo,
                                       "randomness_source": source, "registered_at": when,
                                       "worldgen_version": worldgen})
        return dict(self.registry[gid])
    def get_world_nonce_row(self, gid):
        return dict(self.__dict__.get("nonces", {}).get(gid)) if gid in self.__dict__.get("nonces", {}) else None
    def insert_world_nonce_row(self, gid, rno, rnd, sig):
        self.__dict__.setdefault("nonces", {}).setdefault(
            gid, {"guild_id": gid, "drand_round": rno, "world_nonce": rnd, "drand_signature": sig})
    def load_registry_rows(self): return list(self.__dict__.get("registry", {}).values())
    def load_world_nonce_rows(self): return list(self.__dict__.get("nonces", {}).values())


class FakeResponse:
    def __init__(self): self.sent, self._done = [], False
    async def send_message(self, content=None, **kw): self.sent.append((content, kw)); self._done = True
    async def defer(self, **kw): self._done = True
    def is_done(self): return self._done


class FakeFollowup:
    def __init__(self): self.sent = []
    async def send(self, content=None, **kw): self.sent.append((content, kw))


def interaction_for(guild, user):
    return SimpleNamespace(guild=guild, guild_id=guild.id, user=user,
                           response=FakeResponse(), followup=FakeFollowup())


def install(fake, source=None):
    mc._economy_db = fake
    mc._world_registry = WorldRegistry(fake, [source or mc._drand], worldgen_version=mc.WORLDGEN_VERSION_CURRENT)
    return fake


def reset_state():
    mc._NODES_SYNCED.clear()
    for reg in (mc._GLOBAL_PLAYER_REGISTRY, mc._VOICE_UNSENT, mc._VOICE_OUTBOX, mc._GLOBAL_CURRENCY_REGISTRY,
                mc._GLOBAL_VOICE_CONFIG_REGISTRY, mc._GLOBAL_SPAWN_REGISTRY,
                mc._GLOBAL_CATALOG_REGISTRY, mc._GLOBAL_PROFILE_REGISTRY, mc._GLOBAL_WORLD_RECORDS,
                mc._WORLD_COMMITMENT_ROWS, mc._WITNESS_DELIVERED):
        reg.clear()
    mc._voice_tracker._sessions.clear()


def make_guild(gid, created=datetime(2020, 5, 17, tzinfo=timezone.utc)):
    g = SimpleNamespace(id=gid, name=f"Guild{gid}", created_at=created, voice_channels=[],
                        stage_channels=[], afk_channel=None)
    g.channels = {}
    g.get_channel = lambda cid: g.channels.get(cid)
    return g


def activate_world(gid, label=None, worldgen_version="dev"):
    """Install an ACTIVE Stage-0 record (deterministic fake beacon) for a test guild."""
    src = mc._drand
    sig = (hashlib.sha256(f"sig-a|{label or gid}".encode()).hexdigest()
           + hashlib.sha256(f"sig-b|{label or gid}".encode()).hexdigest())[:96]
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rno = src.round_after(t)
    rec = WorldRecord(gid, "v1", src.source_id, t, rno,
                      Beacon(src.source_id, rno, hashlib.sha256(bytes.fromhex(sig)).hexdigest(), sig),
                      worldgen_version)
    mc._GLOBAL_WORLD_RECORDS[gid] = rec
    return rec


def refill(fake, gid, uid):
    """Test fixture: set a player's stamina in the fake DB to the cap (there is no /rest any more)."""
    p = fake._player(gid, uid)
    p["stamina"], p["stamina_at"] = fake.policy["stamina_cap"], fake.now


def refill_node(fake, gid, node_id):
    """Test fixture: fill a node to max in the fake DB (reserves are authoritative there since v8)."""
    if (gid, node_id) in fake.nodes:
        n = fake.nodes[(gid, node_id)]
        n["reserve"], n["reserve_at"] = n["max_reserve"], fake.now


def forget_world(gid):
    for reg in (mc._GLOBAL_SPAWN_REGISTRY, mc._GLOBAL_CATALOG_REGISTRY, mc._GLOBAL_PROFILE_REGISTRY):
        reg.pop(gid, None)


def find_eligible_guild_id():
    for gid in range(10**17, 10**17 + 400):
        g = make_guild(gid)
        activate_world(gid)
        _, catalog, _ = mc._hydrate_server(g)
        if EconomyEngine.evaluate_server_capability(catalog, 0.0).is_eligible:
            return gid
    raise RuntimeError("no eligible guild id found")


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


async def main():
    clock = Clock()
    with mock.patch.object(mc.time, "monotonic", clock):

        print("\n[A] Command tree")
        names = sorted(c.name for c in mc.bot.tree.get_commands())
        for n in ("balance", "leaderboard", "voiceconfig"):
            check(f"/{n} terdaftar", n in names, True)
        payloads = {c.name: c.to_dict(mc.bot.tree) for c in mc.bot.tree.get_commands()}
        vc = payloads["voiceconfig"]
        check("/voiceconfig default_member_permissions = administrator (8)", int(vc["default_member_permissions"]), 8)
        opts = {o["name"]: o for o in vc["options"]}
        check("interval_menit range", (opts["interval_menit"].get("min_value"), opts["interval_menit"].get("max_value")), (1, 1440))
        lb = {o["name"]: o for o in payloads["leaderboard"]["options"]}
        check("leaderboard choices", [c["value"] for c in lb["kategori"]["choices"]], ["coin", "xp", "voicetime"])
        check("/inventory guild-only", payloads["inventory"].get("contexts"), [0])

        print("\n[B] Load dari DB: pemain + barang yang masih dipegang")
        reset_state()
        fake = install(FakeDB())
        ore = OreItem("u1", 42, 7, "Fe", "Standard Iron Ore", "GLOBAL_CORE", "Crude", 1.25, 0.6)
        cry = CrystalItem("u2", 42, 7, "Pyro Quartz", "CRYSTAL_GEM", "Igneous", "POWER",
                          "Prismatic", 0.5, 0.33, "CRYSTAL_CAVERN", 2.0)
        fake.counters[(7, 42)] = 2
        fake._player(7, 42)
        fake.register_nodes(7, [{"node_id": "x", "max_reserve": 1000.0, "regen_per_second": 0.0}])
        for n, it, kind in ((1, ore, "ore"), (2, cry, "crystal")):
            fake.record_swing(7, 42, n, node_id="x", success=True, critical_hit=False, amount_extracted=1.0,
                              stamina_consumed=10.0, item_kind=kind, item_uuid=it.item_uuid, item_payload=it.to_dict())
        fake.apply_voice_tick(7, "voice:seed", [{"user_id": 42, "coin": "12.5", "xp": 150, "voice_seconds": 61.2}])
        fake.apply_voice_tick(8, "voice:seed8", [{"user_id": 1, "coin": "1", "xp": 0, "voice_seconds": 0}])
        await mc._load_persistent_state()
        p = mc._get_player(7, 42)
        check("pemain dimuat dari DB (stamina, wallet, xp, level, detik)",
              (p.stamina, p.wallet, p.xp, p.level, p.voice_seconds), (80.0, 12.5, 150, 2, 61.2))
        check("barang dimuat sebagai item asli, kunci = item_id DB",
              (p.ore_bag, p.crystal_bag, sorted(p.held)), ([ore], [cry], ["swing:7:42:1", "swing:7:42:2"]))

        print("\n[C] Tidak ada flush: uang beredar dibaca dari DB")
        check("mesin flush lama sudah tidak ada",
              [n for n in ("_flush_players", "_PLAYER_SAVED_ROWS", "_save_players_quietly", "_get_server_circulation")
               if hasattr(mc, n)], [])
        check("wallet per-server terpisah", (mc._get_player(7, 42).wallet, mc._get_player(8, 1).wallet), (12.5, 1.0))
        check("sirkulasi per-server dari DB", (await mc._money_supply(7), await mc._money_supply(8)), (12.5, 1.0))

        print("\n[D] Voice: tanpa mata uang → hanya XP")
        reset_state()
        install(FakeDB())
        g = make_guild(555)
        a, b, bot_m = FakeMember(1), FakeMember(2), FakeMember(3, bot=True)
        vc1 = FakeChannel(10, "Ngobrol", [a, b, bot_m])
        g.voice_channels = [vc1]
        with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[g]):
            await mc._voice_tick.coro()                      # t=1000: sesi mulai
            clock.t += 300
            await mc._voice_tick.coro()                      # t=1300: 1 interval
        pa = mc._get_player(555, 1)
        check("A: xp/level/wallet", (pa.xp, pa.level, pa.wallet), (10, 1, 0.0))
        check("A: voice_seconds", pa.voice_seconds, 300.0)
        check("bot tidak dapat profil", (555, 3) in mc._GLOBAL_PLAYER_REGISTRY, False)
        check("tick tersimpan di DB lewat RPC (bukan flush)",
              (mc._economy_db.players[(555, 1)]["xp"], mc._economy_db.players[(555, 1)]["voice_seconds"]), (10, 300.0))

        print("\n[E] Voice: dengan mata uang resmi → coin pakai ticker server")
        reset_state()
        install(FakeDB())
        gid = find_eligible_guild_id()
        g = make_guild(gid)
        _, catalog, _ = mc._hydrate_server(g)
        audit = EconomyEngine.evaluate_server_capability(catalog, 0.0)
        manifest = CurrencyEngine.establish_sovereign_currency(catalog, audit, "Amerta Dollar", "AMD")
        mc._GLOBAL_CURRENCY_REGISTRY[gid] = manifest
        mc._GLOBAL_VOICE_CONFIG_REGISTRY[gid] = mc.VoiceConfig(guild_id=gid, reward_amount=25.0)
        a, b = FakeMember(1), FakeMember(2, mute=True, deaf=True)
        g.voice_channels = [FakeChannel(10, "Ngobrol", [a, b])]
        with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[g]):
            await mc._voice_tick.coro()
            clock.t += 600
            await mc._voice_tick.coro()
        check("A (aktif) dapat 2×25 AMD + 20 XP", (mc._get_player(gid, 1).wallet, mc._get_player(gid, 1).xp), (50.0, 20))
        check("B (mute+deaf) tidak dapat apa-apa", (mc._get_player(gid, 2).wallet, mc._get_player(gid, 2).xp), (0.0, 0))
        check("B tetap tercatat waktu VC", mc._get_player(gid, 2).voice_seconds, 600.0)

        print("\n[F] /balance, /leaderboard, /voiceconfig")
        g.me = FakeMember(999, bot=True)
        inter = interaction_for(g, a)
        await mc.balance.callback(inter, None)
        em = inter.response.sent[0][1]["embed"]
        check("balance embed valid", embed_ok(em), True)
        check("balance pakai ticker", "AMD" in em.fields[0].value and "Amerta Dollar" in em.fields[0].value, True)
        check("balance ada progress reward", any("Reward Voice" in f.name for f in em.fields), True)

        for kind in ("coin", "xp", "voicetime"):
            inter = interaction_for(g, a)
            await mc.leaderboard.callback(inter, SimpleNamespace(value=kind))
            em = inter.response.sent[0][1]["embed"]
            check(f"leaderboard {kind}: A di #1", ("<@1>" in em.description.splitlines()[0], embed_ok(em)), (True, True))

        text = FakeChannel(77, "log-voice")
        text.permissions_for = lambda m: SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        with mock.patch.object(mc.discord, "TextChannel", FakeChannel):
            inter = interaction_for(g, a)
            await mc.voiceconfig.callback(inter, channel=text, interval_menit=3, blok_sendirian=False)
        check("voiceconfig tersimpan ke DB", mc._economy_db.upserted_configs[-1].notify_channel_id, 77)
        cfg = mc._get_voice_config(gid)
        check("voiceconfig di memori", (cfg.reward_interval_minutes, cfg.block_alone, cfg.reward_amount), (3, False, 25.0))
        check("voiceconfig embed valid", embed_ok(inter.followup.sent[0][1]["embed"]), True)

        inter = interaction_for(g, a)
        await mc.voiceconfig.callback(inter, channel=text, matikan_notifikasi=True)
        check("channel + matikan ditolak", "Pilih salah satu" in inter.response.sent[0][0], True)

        print("\n[G] Notifikasi voice (join / leave / pindah)")
        g.channels[77] = text
        sent = []
        async def fake_send(**kw): sent.append(kw["embed"])
        text.send = fake_send
        a.guild = g
        vc_a, vc_b = FakeChannel(10, "Ngobrol", [a]), FakeChannel(11, "Gaming", [])
        with mock.patch.object(mc.discord, "TextChannel", FakeChannel):
            await mc._send_voice_notification(a, None, vc_a)
            await mc._send_voice_notification(a, vc_a, None)
            await mc._send_voice_notification(a, vc_a, vc_b)
        check("3 embed terkirim", len(sent), 3)
        check("warna join/leave/pindah", [e.colour for e in sent],
              [mc.discord.Colour.green(), mc.discord.Colour.red(), mc.discord.Colour.blurple()])
        check("teks join", sent[0].description, "<@1> masuk ke <#10>")
        check("teks leave", sent[1].description, "<@1> meninggalkan <#10>")
        check("semua embed valid", all(embed_ok(e) for e in sent), True)

        print("\n[H] Shutdown: tidak ada yang perlu di-flush")
        reset_state()
        fake = install(FakeDB())
        fake.apply_voice_tick(1, "voice:h", [{"user_id": 1, "coin": "3", "xp": 0, "voice_seconds": 0}])
        before_calls = len(fake.calls)
        with mock.patch.object(mc.commands.Bot, "close", mock.AsyncMock()):
            await mc.bot.close()
        check("close() tidak menulis apa pun ke DB (semua sudah tersimpan)", len(fake.calls), before_calls)

        print("\n[J] Auto mine: pendaftaran, ayunan per interval voice, regenerasi")
        reset_state()
        mc._AUTOMINE_LAST.clear()
        install(FakeDB())
        g = make_guild(4242)
        activate_world(4242)
        a, b = FakeMember(1), FakeMember(2)
        g.voice_channels = [FakeChannel(10, "Tambang", [a, b])]
        names = {c.name: c for c in mc.bot.tree.get_commands()}
        grp = names["automine"].to_dict(mc.bot.tree)
        check("/automine punya daftar/berhenti/status", sorted(o["name"] for o in grp["options"]), ["berhenti", "daftar", "status"])
        check("/automine guild-only", grp.get("contexts"), [0])

        inter = interaction_for(g, a)
        await mc.automine_daftar.callback(inter)
        check("daftar → automine=True", mc._get_player(4242, 1).automine, True)
        check("embed daftar valid", embed_ok(inter.response.sent[0][1]["embed"]), True)
        check("daftar langsung disimpan ke DB", mc._economy_db.players[(4242, 1)]["automine"], True)

        with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[g]):
            await mc._voice_tick.coro()
            clock.t += 300
            await mc._voice_tick.coro()
        pa, pb = mc._get_player(4242, 1), mc._get_player(4242, 2)
        check("A (terdaftar) dapat 1 item", len(pa.ore_bag) + len(pa.crystal_bag), 1)
        check("A stamina berkurang", pa.stamina < 100.0, True)
        check("B (tidak terdaftar) tidak nambang", len(pb.ore_bag) + len(pb.crystal_bag), 0)
        check("B tetap dapat XP voice", pb.xp, 10)
        check("hasil terakhir tercatat", (4242, 1) in mc._AUTOMINE_LAST, True)

        inter = interaction_for(g, a)
        await mc.automine_status.callback(inter)
        em = inter.response.sent[0][1]["embed"]
        check("status embed valid", embed_ok(em), True)
        check("status ada ayunan terakhir", any("Ayunan terakhir" in f.name for f in em.fields), True)

        state = mc._GLOBAL_SPAWN_REGISTRY[4242]
        fake_j = mc._economy_db
        for (gid_n, nid), row in fake_j.nodes.items():
            if gid_n == 4242:
                row["reserve"], row["reserve_at"] = 0.0, fake_j.now      # habis di DB
        with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[g]):
            await mc._voice_tick.coro()
            check("node habis di DB → tick menampilkan habis (bukan penuh dari memori)",
                  all(n.is_depleted for n in state.active_ores.values()), True)
            fake_j.now += 600                                              # 10 menit jam DB
            await mc._voice_tick.coro()
        check("regenerasi dari jam DB: setelah 10 menit node terisi lagi",
              all(0 < n.current_reserve <= n.max_reserve for n in state.active_ores.values()), True)

        inter = interaction_for(g, a)
        await mc.automine_berhenti.callback(inter)
        check("berhenti → automine=False (DB & cache)",
              (mc._economy_db.players[(4242, 1)]["automine"], mc._get_player(4242, 1).automine), (False, False))
        inter = interaction_for(g, b)
        await mc.automine_berhenti.callback(inter)
        check("berhenti tanpa daftar → pesan info", "belum terdaftar" in inter.response.sent[0][0], True)

        print("\n[K] Stage 0: registrasi & /worldproof")
        reset_state()
        fake = install(FakeDB())
        g = make_guild(8080)
        records = await asyncio.gather(*[mc._ensure_registered(g, wait_for_round=False) for _ in range(50)])
        check("50× _ensure_registered paralel → 1 baris registry", len(fake.registry), 1)
        check("semua dapat target round yang sama", len({r.target_round for r in records}), 1)
        check("nonce belum ada (pending)", records[0].status, "pending")

        sig = "a1" * 48
        def relay_ok(request):
            rno = int(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(200, json={"round": rno, "signature": sig,
                                             "randomness": hashlib.sha256(bytes.fromhex(sig)).hexdigest()})
        good_src = DrandQuicknet(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(relay_ok)))
        install(fake, good_src)
        inter = interaction_for(g, FakeMember(1))
        await mc.worldproof.callback(inter)
        em = inter.followup.sent[0][1]["embed"]
        check("/worldproof embed valid", embed_ok(em), True)
        check("/worldproof status aktif", mc._GLOBAL_WORLD_RECORDS[8080].status, "active")
        check("nonce yang tampil = SHA-256(signature)",
              hashlib.sha256(bytes.fromhex(sig)).hexdigest() in "".join(f.value for f in em.fields), True)
        check("nonce tersimpan di DB", fake.nonces[8080]["world_nonce"], mc._GLOBAL_WORLD_RECORDS[8080].world_nonce)

        def relay_down(request):
            raise httpx.ConnectError("down", request=request)
        down_src = DrandQuicknet(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(relay_down)))
        g2 = make_guild(9090)
        install(fake, down_src)
        inter = interaction_for(g2, FakeMember(1))
        await mc.worldproof.callback(inter)
        check("drand mati → tetap pending", mc._GLOBAL_WORLD_RECORDS[9090].status, "pending")
        check("drand mati → TIDAK ada nonce fallback", 9090 in fake.nonces, False)
        check("embed pending valid", embed_ok(inter.followup.sent[0][1]["embed"]), True)
        await mc._resolve_pending_worlds.coro()
        check("retry saat drand masih mati → tetap pending", 9090 in fake.nonces, False)
        install(fake, good_src)
        await mc._resolve_pending_worlds.coro()
        check("retry setelah drand hidup → aktif", mc._GLOBAL_WORLD_RECORDS[9090].status, "active")

        mc._GLOBAL_WORLD_RECORDS.clear()
        await mc._load_persistent_state()
        check("restart: dunia dimuat ulang dari DB, nonce sama",
              mc._GLOBAL_WORLD_RECORDS[8080].world_nonce, hashlib.sha256(bytes.fromhex(sig)).hexdigest())

        print("\n[L] Stage 1 (pepper commit–reveal, seed) + saksi eksternal")
        import hmac
        import subprocess
        from world_seed import CommitmentMismatch
        want = hashlib.sha256(bytes.fromhex(TEST_PEPPER_HEX)).hexdigest()

        reset_state()
        fake = install(FakeDB())
        await mc._load_persistent_state()
        check("startup pertama → commitment v1 = SHA-256(pepper)", fake.commit_rows["v1"]["pepper_commitment"], want)
        await mc._load_persistent_state()
        check("startup kedua dengan pepper sama → tetap jalan", fake.commit_rows["v1"]["pepper_commitment"], want)
        bad = install(FakeDB())
        bad.insert_commitment_row("v1", "0" * 64)
        try:
            await mc._load_persistent_state()
            check("pepper ≠ commitment tersimpan → bot menolak start", "jalan", "CommitmentMismatch")
        except CommitmentMismatch:
            check("pepper ≠ commitment tersimpan → bot menolak start", "CommitmentMismatch", "CommitmentMismatch")

        proc = subprocess.run([sys.executable, "-c", "import main_core"], cwd=os.getcwd(),
                              env={**os.environ, "WORLD_SEED_PEPPER": ""}, capture_output=True,
                              text=True, encoding="utf-8")
        check("pepper kosong → main_core menolak start dengan pesan jelas",
              (proc.returncode != 0, "WORLD_SEED_PEPPER kosong" in proc.stdout + proc.stderr), (True, True))

        reset_state()
        fake = install(FakeDB(), good_src)
        ra = await mc._ensure_registered(make_guild(1111), wait_for_round=True)
        rb = await mc._ensure_registered(make_guild(2222), wait_for_round=True)
        check("dua server di ronde drand sama → world_nonce SAMA", ra.world_nonce == rb.world_nonce, True)
        check("…tapi seed BEDA karena guild_id di pre-image", mc._world_seed(ra) != mc._world_seed(rb), True)
        check("seed = HMAC-SHA256(pepper, 'BAWAN|v1|guild|nonce')", mc._world_seed(ra),
              hmac.new(bytes.fromhex(TEST_PEPPER_HEX), f"BAWAN|v1|1111|{ra.world_nonce}".encode(), hashlib.sha256).digest())

        posts, hook_state = [], {"up": False}
        def hook(request):
            if not hook_state["up"]:
                return httpx.Response(503)
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": str(len(posts))})
        mc._witness = mc.WebhookWitness("https://discord.test/api/webhooks/1/x",
                                        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(hook)))
        mc._WITNESS_SEND_GAP_SECONDS = 0
        reset_state()
        fake = install(FakeDB(), good_src)
        await mc._load_persistent_state()
        gw = make_guild(3333)
        rec = await mc._ensure_registered(gw, wait_for_round=True)
        await asyncio.gather(*list(mc._BACKGROUND_TASKS))
        check("webhook mati → registrasi tetap sukses & aktif", rec.status, "active")
        check("webhook mati → belum ada event tercatat terkirim", len(fake.__dict__.get("witness_rows", {})), 0)
        hook_state["up"] = True
        check("webhook hidup → antrean terkirim (commitment, registered, activated)",
              await mc._deliver_witness_events(), 3)
        bodies = [json.loads(p["content"].split("```json\n")[1].split("\n```")[0]) for p in posts]
        check("urutan kronologis", [b["event"] for b in bodies], ["commitment", "registered", "activated"])
        check("activated memuat guild_id, registered_at, drand_round, world_nonce",
              all(k in bodies[2] for k in ("guild_id", "registered_at", "drand_round", "world_nonce")), True)
        check("pass berikutnya tidak kirim ulang", await mc._deliver_witness_events(), 0)
        check("tercatat di world_witness_log", sorted(fake.witness_rows),
              ["activated:3333", "commitment:v1", "registered:3333"])
        mc._WITNESS_DELIVERED.clear()
        await mc._load_persistent_state()
        check("setelah restart tidak kirim ulang", await mc._deliver_witness_events(), 0)

        inter = interaction_for(gw, FakeMember(1))
        await mc.worldproof.callback(inter)
        shown = "".join(f.value for f in inter.followup.sent[0][1]["embed"].fields)
        check("/worldproof menampilkan pepper commitment", want in shown, True)
        check("/worldproof TIDAK membocorkan seed", mc._world_seed(rec).hex() in shown, False)
        mc._witness = mc.WebhookWitness(None)

        print("\n[M] Stage 2: dunia dari seed (pending, determinisme, input terlarang)")
        reset_state()
        install(FakeDB(), down_src)             # drand unreachable → world stays pending
        gp = make_guild(5151)
        gp.me = FakeMember(999, bot=True)
        inter = interaction_for(gp, FakeMember(1))
        await mc.explore_mines.callback(inter)
        check("world pending → /explore_mines menolak dengan pesan", mc._WORLD_PENDING_TEXT in inter.followup.sent[0][0], True)
        check("world pending → TIDAK ada dunia yang dibangun", 5151 in mc._GLOBAL_SPAWN_REGISTRY, False)

        rec = activate_world(5151)
        inter = interaction_for(gp, FakeMember(1))
        await mc.explore_mines.callback(inter)
        check("world aktif → /explore_mines kirim embed geologi", "embed" in inter.followup.sent[0][1], True)
        prof, cat, st = mc._hydrate_server(gp)
        seed = mc._world_seed(rec)
        check("signature = sidik jari publik seed, bukan seed", (prof.genetic_signature == seed_fingerprint(seed),
                                                                 prof.genetic_signature != seed.hex()), (True, True))
        snap = (cat.to_json(), st.to_json())
        forget_world(5151)
        _, cat2, st2 = mc._hydrate_server(gp)
        check("hydrate ulang → dunia identik byte per byte", (cat2.to_json(), st2.to_json()), snap)

        forget_world(5151)
        g_other_meta = make_guild(5151, created=datetime(2001, 1, 1, tzinfo=timezone.utc))
        g_other_meta.name = "Nama Server Lain"
        _, cat3, st3 = mc._hydrate_server(g_other_meta)
        check("created_at & nama server beda → dunia tetap identik", (cat3.to_json(), st3.to_json()), snap)

        activate_world(6262, label="5151")      # SAME fake beacon/nonce as guild 5151
        check("nonce sama dengan guild 5151", mc._GLOBAL_WORLD_RECORDS[6262].world_nonce, rec.world_nonce)
        _, cat4, _ = mc._hydrate_server(make_guild(6262))
        check("guild beda, nonce sama → dunia beda (guild_id di seed)", cat4.to_json() != snap[0], True)

        print("\n[N] D: roll mining = stream(seed,'mining',guild|user|node|n), counter atomik")
        from world_stream import mining_roll
        reset_state()
        fake = install(FakeDB())
        GID, UID = 7070, 555
        gm = make_guild(GID)
        rec = activate_world(GID)
        _, cat_m, st_m = mc._hydrate_server(gm)
        node = max(st_m.active_ores, key=lambda nid: st_m.active_ores[nid].max_reserve)
        seed_m = mc._world_seed(rec)
        miner_user = FakeMember(UID)
        miner_user.guild = gm

        calls = []                                  # records when execute_swing runs
        real_execute = mc.execute_swing
        def spy_execute(*a, **k):
            calls.append(dict(fake.__dict__.get("counters", {})))
            return real_execute(*a, **k)

        async def manual_swing(pick="abyss_resonator"):
            inter = interaction_for(gm, miner_user)
            inter.data = {"values": [pick]}
            await mc.ToolSelectView(GID, node, UID)._on_tool_selected(inter)
            content, kw = inter.followup.sent[0]
            text = kw["embed"].footer.text if "embed" in kw else content
            m = re.search(r"#(\d+)", text or "")
            return (int(m.group(1)) if m else None), kw.get("embed"), content

        with mock.patch.object(mc, "execute_swing", spy_execute):
            # (a) /rest, then swing the SAME node 100× with full stamina every time
            ns, crits = [], []
            for _ in range(100):
                refill_node(fake, GID, node)                                             # keep node full (DB)
                refill(fake, GID, UID)
                n, em, _ = await manual_swing()
                ns.append(n)
                crits.append("CRIT" in em.footer.text)
            rolls = {mining_roll(seed_m, GID, UID, node, n) for n in ns}
            check("(a) footer embed menampilkan nomor percobaan #1..#100", ns, list(range(1, 101)))
            check("(a) 100 ayunan dengan stamina penuh → 100 roll berbeda", len(rolls), 100)
            check("(a) crit TIDAK berulang (bukan selalu / bukan tidak pernah)", 0 < sum(crits) < 100, True)
            check("(a) roll dihitung SETELAH counter naik", all(c.get((GID, UID)) == i + 1 for i, c in enumerate(calls[:100])), True)

            # (b) 50 parallel swings of the same user → 50 unique n
            fake.policy["stamina_cap"] = 1e9                               # 50 swings need 50× stamina
            refill(fake, GID, UID)
            before = fake.counters[(GID, UID)]
            par = await asyncio.gather(*[manual_swing() for _ in range(50)])
            par_ns = [n for n, _, _ in par]
            check("(b) 50 ayunan paralel → 50 n unik", len(set(par_ns)), 50)
            check("(b) n lanjut tanpa celah", sorted(par_ns), list(range(before + 1, before + 51)))

            # (c) restart → n continues from the DB, never from 1
            last_n = fake.counters[(GID, UID)]
            reset_state()
            mc._GLOBAL_WORLD_RECORDS[GID] = rec
            await mc._load_persistent_state()
            _, cat_m, st_m = mc._hydrate_server(gm)
            check("(c) setelah restart stamina = nilai DB (bukan default memori)",
                  mc._get_player(GID, UID).stamina, fake.players[(GID, UID)]["stamina"])
            refill(fake, GID, UID)
            n_after, _, _ = await manual_swing()
            check("(c) setelah restart n lanjut dari DB", n_after, last_n + 1)

            # single path: auto mine uses the SAME counter as manual mining
            player = mc._get_player(GID, UID)
            player.automine = True
            mate = FakeMember(UID + 1)
            gm.voice_channels = [FakeChannel(1, "Tambang", [miner_user, mate])]
            with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[gm]):
                await mc._voice_tick.coro()
                clock.t += 300
                await mc._voice_tick.coro()
            check("auto mine lewat jalur & counter yang sama (n berikutnya)",
                  mc._AUTOMINE_LAST[(GID, UID)].attempt, n_after + 1)

            # (d) DB down → swing refused, nothing changes, no roll computed
            fake.mining_down = True
            player.stamina = 100.0
            snap = (fake.counters[(GID, UID)], st_m.active_ores[node].current_reserve, player.stamina,
                    len(player.ore_bag), len(player.crystal_bag), len(calls))
            _, em_d, content_d = await manual_swing()
            check("(d) DB mati → ayunan ditolak dengan pesan", (em_d is None, content_d == mc._SWING_REJECTED_TEXT), (True, True))
            check("(d) counter, cadangan, stamina, tas TIDAK berubah; roll tidak dihitung",
                  (fake.counters[(GID, UID)], st_m.active_ores[node].current_reserve, player.stamina,
                   len(player.ore_bag), len(player.crystal_bag), len(calls)), snap)
            last_auto = mc._AUTOMINE_LAST[(GID, UID)]
            with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[gm]):
                clock.t += 300
                await mc._voice_tick.coro()
            check("(d) DB mati → auto mine juga tidak mengayun", mc._AUTOMINE_LAST[(GID, UID)] is last_auto, True)
            fake.mining_down = False

        print("\n[O] LANGKAH 4: worldgen_version terkunci & tabel periodik server")
        from worldgen import UnsupportedWorldgen, generate_world
        reset_state()
        install(FakeDB())
        GW = 4242
        gw = make_guild(GW)
        rec_w = activate_world(GW)
        prof_w, cat_w, _ = mc._hydrate_server(gw)
        ref = generate_world("dev", GW, mc._world_seed(rec_w), int(gw.created_at.timestamp()))
        check("hydrate = generate_world(worldgen_version tersimpan)", (prof_w, cat_w), (ref.profile, ref.catalog))
        check("profil membawa tabel 28 unsur server", prof_w.periodic_table, ref.table.symbols)
        check("semua node ore ⊆ tabel server",
              {n.element_symbol for n in cat_w.ore_nodes} <= set(prof_w.periodic_table), True)
        inter = interaction_for(gw, FakeMember(1))
        await mc.worldproof.callback(inter)
        em_w = inter.followup.sent[-1][1]["embed"]
        check("/worldproof embed valid & menampilkan worldgen_version",
              (embed_ok(em_w), "`dev`" in "".join(f.value for f in em_w.fields)), (True, True))
        reset_state()
        activate_world(GW, worldgen_version="v7")
        try:
            mc._hydrate_server(gw)
            check("worldgen_version tak dikenal → dunia TIDAK dibangun", "dibangun", "UnsupportedWorldgen")
        except UnsupportedWorldgen:
            check("worldgen_version tak dikenal → dunia TIDAK dibangun", "UnsupportedWorldgen", "UnsupportedWorldgen")
        check("tidak ada state yang ter-cache untuk versi tak dikenal", GW in mc._GLOBAL_SPAWN_REGISTRY, False)
        fk = install(FakeDB())
        await mc._ensure_registered(make_guild(5151), wait_for_round=False)
        check("registrasi baru mencatat WORLDGEN_VERSION_CURRENT", fk.registry[5151]["worldgen_version"],
              mc.WORLDGEN_VERSION_CURRENT)

        print("\n[P] Izin admin /found_currency & /mint_fiat")
        import discord
        from discord import app_commands
        gp = make_guild(6161)
        for name in ("found_currency", "mint_fiat"):
            check(f"/{name} default_member_permissions = administrator (8)",
                  int(payloads[name]["default_member_permissions"]), 8)
            check(f"/{name} guild-only", payloads[name].get("contexts"), [0])
            cmd = mc.bot.tree.get_command(name)
            member = interaction_for(gp, FakeMember(1))
            member.permissions, member.command = discord.Permissions.none(), cmd
            try:
                await cmd._check_can_run(member)
                outcome = "lolos"
            except app_commands.MissingPermissions as exc:
                outcome = "MissingPermissions"
                await mc._admin_command_error(member, exc)
            check(f"/{name}: member biasa DITOLAK di sisi server", outcome, "MissingPermissions")
            sent = member.response.sent[-1] if member.response.sent else (None, {})
            check(f"/{name}: pesan penolakan ephemeral", (sent[0], sent[1].get("ephemeral")),
                  ("⛔ Command ini khusus admin server.", True))
            admin = interaction_for(gp, FakeMember(2))
            admin.permissions, admin.command = discord.Permissions(administrator=True), cmd
            check(f"/{name}: admin lolos pengecekan", await cmd._check_can_run(admin), True)

        print("\n[Q] Saldo otoritatif di DB (v7)")
        reset_state()
        fake = install(FakeDB())
        GQ, UQ = find_eligible_guild_id(), 777
        reset_state()
        install(fake)
        gq = make_guild(GQ)
        rec_q = activate_world(GQ)
        _, cat_q, st_q = mc._hydrate_server(gq)
        miner_q = FakeMember(UQ)
        node_q = max(st_q.active_ores, key=lambda nid: st_q.active_ores[nid].max_reserve)

        async def swing_q(pick="abyss_resonator"):
            inter = interaction_for(gq, miner_q)
            inter.data = {"values": [pick]}
            await mc.ToolSelectView(GQ, node_q, UQ)._on_tool_selected(inter)
            return inter.followup.sent[0]

        async def mine_one_ore():
            for _ in range(20):
                refill_node(fake, GQ, node_q)
                refill(fake, GQ, UQ)
                await swing_q()
                ores = [(iid, it) for iid, it in mc._get_player(GQ, UQ).held.items() if isinstance(it, OreItem)]
                if ores:
                    return ores[-1]
            raise RuntimeError("no ore mined")

        async def sell_q(item):
            inter = interaction_for(gq, miner_q)
            await mc.sell_ore.callback(inter, item.element_symbol)
            return inter.followup.sent[-1]

        item_id, item = await mine_one_ore()
        check("ayunan: barang ada di DB dengan item_id = swing:g:u:n", item_id in fake.items, True)
        check("ayunan: pickaxe tersimpan di DB", fake.players[(GQ, UQ)]["pickaxe_key"], "abyss_resonator")
        content, kw = await sell_q(item)
        player_q = mc._get_player(GQ, UQ)
        check("jual: sukses → barang keluar dari tas, saldo = saldo DB",
              ("embed" in kw, item_id in player_q.held, Decimal(str(player_q.wallet)) == fake.players[(GQ, UQ)]["wallet"]),
              (True, False, True))
        check("jual: tepat 1 baris ledger + 1 pelepasan", (sum(r["ref"] == f"sell:{item_id}" for r in fake.ledger),
                                                        item_id in fake.disposals), (1, True))

        wallet_before = player_q.wallet
        player_q.held[item_id] = item                       # cache basi: barang "muncul lagi"
        content, _ = await sell_q(item)
        check("jual ulang barang yang sama (cache basi) → DB bilang sudah terjual, saldo tetap",
              ("sudah terjual" in content, player_q.wallet, item_id in player_q.held,
               sum(r["ref"] == f"sell:{item_id}" for r in fake.ledger)), (True, wallet_before, False, 1))

        item2_id, item2 = await mine_one_ore()
        fake.lose_response("sell_item")                     # DB commit, respons hilang
        content, _ = await sell_q(item2)
        check("respons hilang setelah commit → user diberi tahu gagal, cache belum berubah",
              (content == mc._DB_UNAVAILABLE_TEXT, item2_id in player_q.held), (True, True))
        content, _ = await sell_q(item2)
        check("retry dengan ref yang sama → tidak dobel; cache menyusul DB",
              ("sudah terjual" in content, item2_id in player_q.held,
               sum(r["ref"] == f"sell:{item2_id}" for r in fake.ledger),
               Decimal(str(player_q.wallet)) == fake.players[(GQ, UQ)]["wallet"]), (True, False, 1, True))

        item3_id, item3 = await mine_one_ore()
        fake.lose_response("sell_item")
        await sell_q(item3)                                 # "crash" tepat setelah DB commit
        reset_state()                                       # restart: cache kosong
        mc._GLOBAL_WORLD_RECORDS[GQ] = rec_q
        await mc._load_persistent_state()
        player_q = mc._get_player(GQ, UQ)
        check("crash di antara DB dan memori → setelah restart konsisten dengan DB",
              (item3_id in player_q.held, Decimal(str(player_q.wallet)) == fake.players[(GQ, UQ)]["wallet"]),
              (False, True))
        _, cat_q, st_q = mc._hydrate_server(gq)

        item4_id, item4 = await mine_one_ore()
        fake.down = True
        snap_db = (len(fake.ledger), len(fake.disposals), fake.players[(GQ, UQ)]["wallet"])
        snap_cache = (player_q.wallet, dict(player_q.held))
        content, _ = await sell_q(item4)
        check("DB mati → jual ditolak, DB & cache tidak berubah",
              (content == mc._DB_UNAVAILABLE_TEXT, (len(fake.ledger), len(fake.disposals), fake.players[(GQ, UQ)]["wallet"]),
               (player_q.wallet, dict(player_q.held))), (True, snap_db, snap_cache))
        n_before = fake.counters[(GQ, UQ)]
        content, _ = await swing_q()
        check("DB mati → ayunan ditolak, counter tidak naik", (content == mc._SWING_REJECTED_TEXT, fake.counters[(GQ, UQ)]),
              (True, n_before))
        fake.down = False

        # voice: DB mati → interval TIDAK hilang; respons hilang → retry ref sama tidak dobel
        mc._GLOBAL_VOICE_CONFIG_REGISTRY[GQ] = mc.VoiceConfig(guild_id=GQ, reward_amount=10.0)
        mate_q = FakeMember(UQ + 1)
        gq.voice_channels = [FakeChannel(5, "VC", [miner_q, mate_q])]
        xp0 = fake.players.get((GQ, UQ + 1), {}).get("xp", 0)
        with mock.patch.object(type(mc.bot), "guilds", new_callable=mock.PropertyMock, return_value=[gq]):
            await mc._voice_tick.coro()                     # sesi mulai
            fake.down = True
            clock.t += 300
            await mc._voice_tick.coro()                     # 1 interval selesai, DB mati
            check("DB mati → XP belum tercatat, batch menunggu di outbox",
                  (fake.players.get((GQ, UQ + 1), {}).get("xp", 0), len(mc._VOICE_OUTBOX.get(GQ, []))), (xp0, 1))
            fake.down = False
            fake.lose_response("apply_voice_tick")
            clock.t += 300
            await mc._voice_tick.coro()                     # commit tapi respons hilang
            clock.t += 60
            await mc._voice_tick.coro()                     # retry ref yang sama
        check("2 interval = tepat +20 XP (tidak hilang, tidak dobel)",
              fake.players[(GQ, UQ + 1)]["xp"] - xp0, 20)
        check("outbox kosong setelah DB mengonfirmasi", mc._VOICE_OUTBOX.get(GQ, []), [])
        check("detik VC di cache = DB", mc._get_player(GQ, UQ + 1).voice_seconds,
              fake.players[(GQ, UQ + 1)]["voice_seconds"])

        inter = interaction_for(gq, FakeMember(1))
        await mc.mint_fiat.callback(inter, 5000.0)
        check("/mint_fiat nonaktif (pesan, tidak ada perubahan)",
              (inter.response.sent[0][0] == mc._MINT_DISABLED_TEXT, fake.upserted_currencies), (True, []))

        audit = fake.ledger_audit()
        check("audit: wallet = Σ ledger untuk semua pemain, rantai saldo utuh",
              (audit["wallet_mismatch"], audit["balance_chain_broken"], audit["sale_without_disposal"],
               audit["results_beyond_counter"]), ([], 0, 0, 0))

        print("\n[R] v8: cadangan node di DB + batas produksi (tanpa /rest)")
        names_r = {c.name for c in mc.bot.tree.get_commands()}
        check("/rest sudah tidak ada", "rest" in names_r, False)
        reset_state()
        mc._NODES_SYNCED.clear()
        fake = install(FakeDB())
        mc._PRODUCTION_POLICY.update(fake.load_production_policy())
        GR, UR = 7171, 42
        gr = make_guild(GR)
        gr.me = FakeMember(999, bot=True)
        rec_r = activate_world(GR)
        inter = interaction_for(gr, FakeMember(UR))
        await mc.explore_mines.callback(inter)
        view_r = inter.followup.sent[0][1].get("view")
        check("embed /explore_mines tanpa tombol Rest",
              any("rest" in (getattr(c, "custom_id", "") or "") for c in view_r.children), False)
        _, cat_r, st_r = mc._hydrate_server(gr)
        node_r = max(st_r.active_ores, key=lambda nid: st_r.active_ores[nid].max_reserve)
        check("/explore_mines mendaftarkan semua node di DB",
              {nid for (g_, nid) in fake.nodes if g_ == GR}, {*st_r.active_ores, *st_r.active_crystals})
        miner_r = FakeMember(UR)

        async def swing_r(pick="abyss_resonator"):
            inter = interaction_for(gr, miner_r)
            inter.data = {"values": [pick]}
            await mc.ToolSelectView(GR, node_r, UR)._on_tool_selected(inter)
            return inter.followup.sent[0]

        content, kw = await swing_r()
        db_reserve = fake._node_now(fake.nodes[(GR, node_r)])
        check("ayunan mengurangi cadangan node di DB; memori = DB",
              (db_reserve < st_r.active_ores[node_r].max_reserve,
               abs(st_r.active_ores[node_r].current_reserve - db_reserve) < 1e-6), (True, True))

        reset_state()                                          # restart
        mc._NODES_SYNCED.clear()
        mc._GLOBAL_WORLD_RECORDS[GR] = rec_r
        await mc._load_persistent_state()
        _, cat_r, st_r = mc._hydrate_server(gr)
        check("restart tanpa sync: dunia baru di memori masih penuh (inilah celah lama)",
              st_r.active_ores[node_r].current_reserve, st_r.active_ores[node_r].max_reserve)
        await mc._sync_nodes(GR)
        check("setelah sync: cadangan = DB (restart TIDAK mengisi ulang node)",
              abs(st_r.active_ores[node_r].current_reserve - db_reserve) < 1e-6, True)

        p = fake._player(GR, UR)
        p["stamina"], p["stamina_at"] = 1.0, fake.now
        mc._get_player(GR, UR).stamina = 1.0
        n0 = fake.counters[(GR, UR)]
        content, _ = await swing_r()
        check("stamina kurang → ditolak sebelum counter naik, pesan menyebut laju pulih",
              ("Stamina nggak cukup" in content, "Pulih 50/jam" in content, fake.counters[(GR, UR)]), (True, True, n0))
        fake.now += 3600                                       # 1 jam jam DB → +50 stamina
        content, kw = await swing_r()
        check("setelah 1 jam stamina pulih dari waktu → ayunan jalan", fake.counters[(GR, UR)], n0 + 1)

        fake.policy["max_swings_per_minute"] = 6
        refill(fake, GR, UR)
        fake.policy["stamina_cap"] = 1e9
        refill(fake, GR, UR)
        fake.now += 120                                        # jendela 1 menit baru
        results = []
        for _ in range(7):
            content, kw = await swing_r()
            results.append("Terlalu cepat" in (content or ""))
        check("batas 6 ayunan/menit: ayunan ke-7 ditolak", (results[:6], results[6]), ([False] * 6, True))
        fake.policy["max_swings_per_minute"] = None

        real_begin = fake.begin_swing
        def begin_then_someone_else_mines(*a, **k):
            out = real_begin(*a, **k)
            node = fake.nodes[(GR, node_r)]
            node["reserve"], node["reserve_at"] = 0.0, fake.now   # pemain lain menghabiskan node di antaranya
            return out
        fake.now += 10_000
        with mock.patch.object(fake, "begin_swing", begin_then_someone_else_mines):
            held_before = len(mc._get_player(GR, UR).held)
            content, _ = await swing_r()
        check("node dihabiskan orang lain di tengah ayunan → DB menolak (node_depleted), tas tidak berubah",
              ("Node ini sudah habis" in content, len(mc._get_player(GR, UR).held)), (True, held_before))

        fake.now += 10_000
        fake.lose_response("record_swing")                     # commit, respons hilang
        content, _ = await swing_r()
        await mc._sync_nodes(GR)
        check("respons record_swing hilang → sync berikutnya menampilkan cadangan DB",
              abs(st_r.active_ores[node_r].current_reserve - fake._node_now(fake.nodes[(GR, node_r)])) < 1e-6, True)

        mc._get_player(GR, UR).automine = True
        p = fake._player(GR, UR)
        fake.policy["stamina_cap"] = 100.0
        p["stamina"], p["stamina_at"] = 0.0, fake.now
        mc._get_player(GR, UR).stamina, mc._get_player(GR, UR).stamina_at = 0.0, mc.time.time()
        n0 = fake.counters[(GR, UR)]
        await mc._run_auto_mine(gr, UR, mc._get_player(GR, UR), 3)
        check("auto mine dengan stamina habis → melewati interval, tidak ada counter terbuang",
              fake.counters[(GR, UR)], n0)

        print("\n[I] Cek bawaan: embed /found_currency (kode lama) muat di batas 1024")
        field = f"```text\n{preview_manifest(manifest)}\n```"
        print(f"  ·  panjang field manifest = {len(field)} karakter")


asyncio.run(main())
print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
sys.exit(1 if failures else 0)
