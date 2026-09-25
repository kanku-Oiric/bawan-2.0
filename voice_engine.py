"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         VOICE_ENGINE.PY  —  Voice Activity Tracker & Reward Calculator       ║
║         Layer murni: tidak menyentuh Discord API maupun Supabase            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ALUR                                                                        ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  main_core.py mengubah state voice Discord → List[MemberVoiceSnapshot],     ║
║  lalu memanggil VoiceSessionTracker.sync_guild() setiap ada event voice    ║
║  DAN setiap tick loop.  Detik aktif dihitung presisi antar-checkpoint      ║
║  (bukan sampling), jadi user yang join 10 detik sebelum tick tidak dapat   ║
║  reward penuh, dan user yang keluar di menit 4:59 tidak dapat apa-apa.     ║
║                                                                              ║
║  collect_payouts() mengembalikan jumlah interval PENUH yang sudah          ║
║  terkumpul; quote_reward() mengubahnya jadi coin + XP memakai audit        ║
║  Bank Sentral yang sama dengan /sell_ore (economy_engine.py).              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ANTI-ABUSE (masing-masing bisa dimatikan per server)                       ║
║  • AFK channel          → tidak dapat reward, waktu VC tidak dihitung      ║
║  • self-mute + deafen   → tidak dapat reward                               ║
║  • sendirian (<2 manusia; bot tidak dihitung) → tidak dapat reward         ║
║  XP tetap mengikuti aturan yang sama dengan coin; yang membedakan hanya    ║
║  coin butuh mata uang resmi + lolos audit, XP tidak.                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from economy_engine import SovereigntyReport


# ─────────────────────────────────────────────────────────────────────────────
# KONSTANTA SISTEM
# ─────────────────────────────────────────────────────────────────────────────

# Seberapa sering main_core.py menjalankan tick (sync + payout + flush DB).
TRACKER_TICK_SECONDS: int = 60

DEFAULT_REWARD_INTERVAL_MINUTES: int = 5
DEFAULT_REWARD_AMOUNT: float = 10.0
MIN_REWARD_INTERVAL_MINUTES: int = 1
MAX_REWARD_INTERVAL_MINUTES: int = 1440

XP_PER_INTERVAL: int = 10
MIN_HUMANS_FOR_REWARD: int = 2

# Level L butuh total XP = LEVEL_XP_BASE × (L-1)².
# Level 2 = 100 XP, level 3 = 400 XP, level 10 = 8.100 XP.
LEVEL_XP_BASE: int = 100

BLOCK_AFK_CHANNEL: str = "AFK_CHANNEL"
BLOCK_MUTE_DEAF: str = "MUTE_DEAF"
BLOCK_ALONE: str = "ALONE"

COIN_BLOCK_NO_CURRENCY: str = "NO_CURRENCY"
COIN_BLOCK_AUDIT_FAILED: str = "AUDIT_FAILED"


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VoiceConfig:
    """Konfigurasi voice tracker satu server. Disimpan di tabel `voice_config`."""
    guild_id: int
    notify_channel_id: Optional[int] = None
    reward_interval_minutes: int = DEFAULT_REWARD_INTERVAL_MINUTES
    reward_amount: float = DEFAULT_REWARD_AMOUNT
    block_self_mute_deaf: bool = True
    block_afk_channel: bool = True
    block_alone: bool = True

    @property
    def reward_interval_seconds(self) -> float:
        return self.reward_interval_minutes * 60.0


@dataclass(frozen=True)
class MemberVoiceSnapshot:
    """State voice satu member manusia pada satu saat (dibuat oleh main_core.py)."""
    user_id: int
    channel_id: int
    is_afk_channel: bool
    self_mute: bool
    self_deaf: bool
    human_count: int        # jumlah member non-bot di channel yang sama


@dataclass(frozen=True)
class VoicePayout:
    guild_id: int
    user_id: int
    intervals: int          # jumlah interval penuh yang dibayar


@dataclass(frozen=True)
class RewardQuote:
    coin: float
    xp: int
    coin_blocked_reason: Optional[str]   # None = coin dibayar normal


@dataclass
class _Session:
    channel_id: int
    last_checkpoint: float
    block_reason: Optional[str]
    counts_voice_time: bool
    eligible_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# ATURAN MURNI
# ─────────────────────────────────────────────────────────────────────────────

def reward_block_reason(snap: MemberVoiceSnapshot, config: VoiceConfig) -> Optional[str]:
    """Alasan member TIDAK layak reward saat ini, atau None kalau layak."""
    if config.block_afk_channel and snap.is_afk_channel:
        return BLOCK_AFK_CHANNEL
    if config.block_self_mute_deaf and snap.self_mute and snap.self_deaf:
        return BLOCK_MUTE_DEAF
    if config.block_alone and snap.human_count < MIN_HUMANS_FOR_REWARD:
        return BLOCK_ALONE
    return None


def level_for_xp(xp: int) -> int:
    """Level dari total XP (level 1 dimulai di 0 XP)."""
    return math.isqrt(max(0, int(xp)) // LEVEL_XP_BASE) + 1


def xp_for_level(level: int) -> int:
    """Total XP minimum untuk mencapai `level`."""
    return LEVEL_XP_BASE * (max(1, level) - 1) ** 2


def quote_reward(
    config: VoiceConfig,
    intervals: int,
    *,
    has_currency: bool,
    audit: Optional[SovereigntyReport],
) -> RewardQuote:
    """
    Hitung reward untuk `intervals` interval penuh.

    Coin mengikuti aturan yang sama dengan /sell_ore: server harus lolos audit
    Bank Sentral, dan nominal dikali seigniorage_modifier.  Tambahannya, coin
    hanya dibayar kalau server sudah punya mata uang resmi (/found_currency).
    XP selalu dibayar.
    """
    xp = XP_PER_INTERVAL * intervals
    if not has_currency:
        return RewardQuote(0.0, xp, COIN_BLOCK_NO_CURRENCY)
    if audit is None or not audit.is_eligible:
        return RewardQuote(0.0, xp, COIN_BLOCK_AUDIT_FAILED)
    coin = round(config.reward_amount * intervals * audit.seigniorage_modifier, 4)
    return RewardQuote(coin, xp, None)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class VoiceSessionTracker:
    """
    Menyimpan sesi voice aktif di memori: guild_id → user_id → _Session.

    Sesi sengaja TIDAK dipersist: setelah restart, semua member yang sedang
    di VC mulai sesi baru.  Progres interval yang belum penuh hilang saat
    member keluar VC — reward hanya untuk interval yang benar-benar selesai.
    Pindah channel TIDAK mereset progres.
    """

    def __init__(self) -> None:
        self._sessions: Dict[int, Dict[int, _Session]] = {}

    def sync_guild(
        self,
        guild_id: int,
        snapshots: Iterable[MemberVoiceSnapshot],
        config: VoiceConfig,
        now: float,
    ) -> Dict[int, float]:
        """
        Checkpoint semua sesi guild ini lalu terapkan snapshot terbaru.

        Detik sejak checkpoint terakhir dikreditkan memakai state SEBELUMNYA
        (state itulah yang berlaku selama rentang waktu tersebut).

        Returns: user_id → detik waktu VC (non-AFK) sejak sync terakhir.
        """
        sessions = self._sessions.setdefault(guild_id, {})
        voice_delta: Dict[int, float] = {}

        for user_id, s in sessions.items():
            elapsed = max(0.0, now - s.last_checkpoint)
            if s.counts_voice_time and elapsed > 0:
                voice_delta[user_id] = elapsed
            if s.block_reason is None:
                s.eligible_seconds += elapsed
            s.last_checkpoint = now

        present = {snap.user_id: snap for snap in snapshots}
        for user_id in [u for u in sessions if u not in present]:
            del sessions[user_id]

        for user_id, snap in present.items():
            reason = reward_block_reason(snap, config)
            s = sessions.get(user_id)
            if s is None:
                sessions[user_id] = _Session(
                    channel_id        = snap.channel_id,
                    last_checkpoint   = now,
                    block_reason      = reason,
                    counts_voice_time = not snap.is_afk_channel,
                )
            else:
                s.channel_id        = snap.channel_id
                s.block_reason      = reason
                s.counts_voice_time = not snap.is_afk_channel

        if not sessions:
            del self._sessions[guild_id]
        return voice_delta

    def collect_payouts(self, guild_id: int, config: VoiceConfig) -> List[VoicePayout]:
        """Ambil interval penuh yang sudah terkumpul; sisanya tetap di akumulator."""
        interval = config.reward_interval_seconds
        payouts: List[VoicePayout] = []
        for user_id, s in self._sessions.get(guild_id, {}).items():
            if s.eligible_seconds >= interval:
                n = int(s.eligible_seconds // interval)
                s.eligible_seconds -= n * interval
                payouts.append(VoicePayout(guild_id, user_id, n))
        return payouts

    def progress(self, guild_id: int, user_id: int) -> Optional[Tuple[float, Optional[str]]]:
        """(detik terkumpul menuju reward berikutnya, alasan blokir) atau None kalau tidak di VC."""
        s = self._sessions.get(guild_id, {}).get(user_id)
        if s is None:
            return None
        return s.eligible_seconds, s.block_reason

    def retain_guilds(self, guild_ids: Iterable[int]) -> None:
        """Buang sesi milik server yang sudah tidak berisi bot."""
        keep = set(guild_ids)
        for gid in [g for g in self._sessions if g not in keep]:
            del self._sessions[gid]


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python voice_engine.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    G = 111
    CFG = VoiceConfig(guild_id=G)              # interval 5 menit, semua blokir aktif
    AFK = 999
    failures = 0

    def snap(uid: int, ch: int = 1, humans: int = 2, mute: bool = False,
             deaf: bool = False) -> MemberVoiceSnapshot:
        return MemberVoiceSnapshot(uid, ch, ch == AFK, mute, deaf, humans)

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    def paid(tracker: VoiceSessionTracker, cfg: VoiceConfig = CFG) -> Dict[int, int]:
        return {p.user_id: p.intervals for p in tracker.collect_payouts(G, cfg)}

    print("\n[1] Dua user ngobrol 5 menit → masing-masing 1 interval")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1), snap(2)], CFG, 0.0)
    delta = t.sync_guild(G, [snap(1), snap(2)], CFG, 300.0)
    check("voice delta", delta, {1: 300.0, 2: 300.0})
    check("payout", paid(t), {1: 1, 2: 1})
    check("payout kedua langsung (akumulator sudah terpakai)", paid(t), {})

    print("\n[2] Sendirian 10 menit → tanpa reward, tapi waktu VC tetap dihitung")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1, humans=1)], CFG, 0.0)
    delta = t.sync_guild(G, [snap(1, humans=1)], CFG, 600.0)
    check("voice delta", delta, {1: 600.0})
    check("payout", paid(t), {})
    check("progress", t.progress(G, 1), (0.0, BLOCK_ALONE))

    print("\n[3] Self-mute + deafen diblokir; mute saja tetap dapat")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1, mute=True, deaf=True), snap(2, mute=True)], CFG, 0.0)
    t.sync_guild(G, [snap(1, mute=True, deaf=True), snap(2, mute=True)], CFG, 300.0)
    check("payout", paid(t), {2: 1})

    print("\n[4] Toggle blok dimatikan → mute+deaf & sendirian dapat reward")
    loose = VoiceConfig(guild_id=G, block_self_mute_deaf=False, block_alone=False)
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1, humans=1, mute=True, deaf=True)], loose, 0.0)
    t.sync_guild(G, [snap(1, humans=1, mute=True, deaf=True)], loose, 300.0)
    check("payout", paid(t, loose), {1: 1})

    print("\n[5] AFK channel → tanpa reward dan waktu VC tidak dihitung")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1, ch=AFK), snap(2, ch=AFK)], CFG, 0.0)
    delta = t.sync_guild(G, [snap(1, ch=AFK), snap(2, ch=AFK)], CFG, 300.0)
    check("voice delta", delta, {})
    check("payout", paid(t), {})

    print("\n[6] Keluar di detik 299 → progres hilang, tidak dibayar")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1), snap(2)], CFG, 0.0)
    t.sync_guild(G, [], CFG, 299.0)
    check("payout", paid(t), {})
    check("progress setelah keluar", t.progress(G, 1), None)

    print("\n[7] Transisi di tengah interval dihitung presisi")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1), snap(2)], CFG, 0.0)          # A & B masuk
    t.sync_guild(G, [snap(1, humans=1)], CFG, 200.0)       # B keluar → A sendirian
    t.sync_guild(G, [snap(1), snap(2)], CFG, 500.0)        # B masuk lagi
    check("A di t=500 (200 detik layak)", t.progress(G, 1), (200.0, None))
    t.sync_guild(G, [snap(1), snap(2)], CFG, 600.0)
    check("payout di t=600", paid(t), {1: 1})
    check("sisa A", t.progress(G, 1), (0.0, None))

    print("\n[8] Pindah channel tidak mereset progres")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1, ch=1), snap(2, ch=1)], CFG, 0.0)
    t.sync_guild(G, [snap(1, ch=2), snap(2, ch=2)], CFG, 150.0)
    t.sync_guild(G, [snap(1, ch=2), snap(2, ch=2)], CFG, 300.0)
    check("payout", paid(t), {1: 1, 2: 1})

    print("\n[9] Tick terlewat lama → beberapa interval sekaligus + sisa")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1), snap(2)], CFG, 0.0)
    t.sync_guild(G, [snap(1), snap(2)], CFG, 1000.0)
    check("payout", paid(t), {1: 3, 2: 3})
    check("sisa", t.progress(G, 1), (100.0, None))

    print("\n[10] retain_guilds membuang server yang ditinggal bot")
    t = VoiceSessionTracker()
    t.sync_guild(G, [snap(1), snap(2)], CFG, 0.0)
    t.retain_guilds([222])
    check("progress", t.progress(G, 1), None)

    print("\n[11] Kurva level")
    for xp, lvl in [(0, 1), (99, 1), (100, 2), (399, 2), (400, 3), (8100, 10), (-5, 1)]:
        check(f"level_for_xp({xp})", level_for_xp(xp), lvl)
    check("xp_for_level(3)", xp_for_level(3), 400)

    print("\n[12] quote_reward mengikuti audit Bank Sentral")
    healthy = SovereigntyReport(G, True, 1e6, 1.0, ["ok"], 900.0)
    warning = SovereigntyReport(G, True, 1e6, 0.65, ["warn"], 900.0)
    failed  = SovereigntyReport(G, False, 0.0, 0.0, ["gagal"], 100.0)
    check("tanpa mata uang", quote_reward(CFG, 2, has_currency=False, audit=healthy),
          RewardQuote(0.0, 20, COIN_BLOCK_NO_CURRENCY))
    check("audit gagal", quote_reward(CFG, 1, has_currency=True, audit=failed),
          RewardQuote(0.0, 10, COIN_BLOCK_AUDIT_FAILED))
    check("sehat", quote_reward(CFG, 2, has_currency=True, audit=healthy),
          RewardQuote(20.0, 20, None))
    check("warning inflasi (×0.65)", quote_reward(CFG, 1, has_currency=True, audit=warning),
          RewardQuote(6.5, 10, None))

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
