"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         CURRENCY_ENGINE.PY  —  Sovereign Fiat Genesis Engine                 ║
║         Layer 3 — Meresmikan dan mengkalkulasi penerbitan mata uang baru     ║
║                                                                              ║
║         CHANGELOG dari blueprint awal:                                       ║
║         - Validasi ticker diperkuat (alphanumeric only, ASCII guard)         ║
║         - Ticker collision hook ditambahkan                                  ║
║         - Exchange rate genesis berbasis geological backing ratio            ║
║         - Genesis Market Cap berbobot (rich geology = higher cap)            ║
║         - CurrencyManifest diperluas: supply breakdown + metadata            ║
║         - CurrencyPolicy ditambahkan sebagai bridge ke mint_cap.py           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from material_gen import ServerMaterialCatalog
from economy_engine import SovereigntyReport


# ─────────────────────────────────────────────────────────────────────────────
# KONSTANTA SISTEM
# ─────────────────────────────────────────────────────────────────────────────

# Pegging ratio: seberapa "keras" cadangan geologi menjamin nilai mata uang.
# 1.0 = 100% backed (gold standard penuh), 0.5 = fractional reserve
GEOLOGICAL_BACKING_RATIO: float = 0.75

# Minimum dan maksimum exchange rate saat genesis (dalam UA)
# Server termiskin → 0.5 UA, terkaya → 5.0 UA per 1 Fiat Lokal
GENESIS_RATE_FLOOR: float = 0.5
GENESIS_RATE_CEILING: float = 5.0

# Score geologi referensi: server dengan skor ini dapat rate = 1.0 (par dengan UA)
PARITY_GEOLOGY_SCORE: float = 1500.0

# Rasio total supply terhadap genesis market cap
# Ini menentukan "denomination" — berapa koin yang dicetak untuk mencapai cap tersebut
DEFAULT_SUPPLY_DENOMINATOR: float = 1.0  # 1 koin = 1 unit cap di genesis

# Batas reserve yang harus selalu dijaga (tidak bisa dicetak habis)
RESERVE_RATIO: float = 0.20  # 20% dari total supply adalah reserve, tidak bisa beredar


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CurrencyPolicy:
    """
    Kebijakan moneter bawaan yang lahir bersama mata uang.
    Menjadi input utama untuk mint_cap.py dan sistem inflasi.
    
    Dibekukan saat genesis — perubahan kebijakan butuh governance vote di masa depan.
    """
    # Batas maksimum koin yang PERNAH bisa dicetak (hard cap, tidak bisa diubah)
    hard_cap_supply: float

    # Batas sirkulasi aktif (bisa berubah seiring waktu via mint_cap.py)
    max_circulating_supply: float

    # Persentase koin baru yang masuk reserve setiap mint event
    reserve_ratio: float

    # Skor geologi saat pendirian — dipakai untuk menghitung base mint rate
    founding_geology_score: float

    # Nilai backing geologi dalam UA (cadangan nyata yang menjamin mata uang)
    geological_backing_value_ua: float


@dataclass(frozen=True)
class CurrencyManifest:
    """
    Manifes resmi berdirinya sebuah mata uang fiat lokal server.
    Ini adalah 'akta kelahiran' mata uang — dibuat sekali, tidak bisa diubah.
    """
    # Identitas
    server_id: int
    currency_name: str
    ticker: str                         # Misal: "AMD", "IDR", "AMR"

    # Ekonomi genesis
    genesis_market_cap: float           # Total nilai pasar saat lahir (dalam UA)
    total_supply: float                 # Total koin yang dicetak saat genesis
    circulating_supply: float           # Koin yang bisa langsung beredar (non-reserve)
    reserve_supply: float               # Koin yang dikunci sebagai cadangan

    # Nilai tukar
    exchange_rate_to_ua: float          # 1 Fiat Lokal = X UA saat genesis
    geological_backing_value_ua: float  # Nilai backing nyata dalam UA

    # Kebijakan moneter
    policy: CurrencyPolicy

    # Metadata
    genesis_timestamp: str              # ISO 8601 UTC
    founding_geology_score: float       # Skor geologi saat pendirian
    is_active: bool


# ─────────────────────────────────────────────────────────────────────────────
# CURRENCY ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CurrencyEngine:
    """
    Mesin Penerbitan Uang Sovereign.

    Tanggung jawab:
    - Memvalidasi kelayakan server sebelum genesis
    - Menghitung seluruh parameter ekonomi awal mata uang
    - Memproduksi CurrencyManifest yang immutable sebagai akta resmi

    Tidak menyentuh database. Tidak menyimpan state.
    Semua output adalah pure data — siap dilempar ke Supabase oleh main_core.py.
    """

    # ── Validasi ──────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_ticker(ticker: str) -> str:
        """
        Validasi dan normalisasi ticker mata uang.
        
        Rules:
        - 2–5 karakter
        - Hanya huruf ASCII latin (A–Z). Angka dan simbol dilarang.
        - Tidak mengandung spasi
        
        Returns:
        - Ticker dalam huruf kapital yang sudah bersih

        Raises:
        - ValueError jika format tidak valid
        """
        clean = ticker.strip().upper()

        if not clean:
            raise ValueError("Ticker tidak boleh kosong.")

        if len(clean) < 2 or len(clean) > 5:
            raise ValueError(
                f"Ticker '{clean}' tidak valid: harus 2–5 karakter "
                f"(sekarang {len(clean)} karakter)."
            )

        # Hanya izinkan huruf Latin ASCII (A-Z) — blokir angka, simbol, Cyrillic, dst.
        if not re.match(r'^[A-Z]+$', clean):
            raise ValueError(
                f"Ticker '{clean}' tidak valid: hanya boleh mengandung "
                f"huruf Latin A–Z (tanpa angka, simbol, atau karakter non-ASCII)."
            )

        return clean

    @staticmethod
    def _validate_currency_name(name: str) -> str:
        """
        Validasi nama mata uang.
        
        Rules:
        - 3–40 karakter setelah di-strip
        - Tidak boleh hanya whitespace
        """
        clean = name.strip()

        if len(clean) < 3:
            raise ValueError(
                f"Nama mata uang terlalu pendek: minimal 3 karakter (sekarang '{clean}')."
            )

        if len(clean) > 40:
            raise ValueError(
                f"Nama mata uang terlalu panjang: maksimal 40 karakter "
                f"(sekarang {len(clean)} karakter)."
            )

        return clean

    # ── Kalkulasi Ekonomi ─────────────────────────────────────────────────────

    @staticmethod
    def _calculate_genesis_exchange_rate(geology_score: float) -> float:
        """
        Menghitung nilai tukar awal berdasarkan kekuatan geologi server.

        Formula:
            rate = (geology_score / PARITY_SCORE) * backing_ratio
            lalu di-clamp antara FLOOR dan CEILING

        Logika:
        - Server dengan skor tepat di parity (1500) → rate = 0.75 UA
          (karena 75% backed, bukan 1:1)
        - Server dengan skor 2x parity (3000) → rate = 1.5 UA
        - Server sangat kaya → di-clamp di 5.0 UA
        - Server baru lolos minimum → di-clamp di 0.5 UA

        Ini mencegah mata uang semua server lahir dengan nilai sama (1:1),
        yang akan merusak hierarki ekonomi antar-server.
        """
        raw_rate = (geology_score / PARITY_GEOLOGY_SCORE) * GEOLOGICAL_BACKING_RATIO
        return max(GENESIS_RATE_FLOOR, min(GENESIS_RATE_CEILING, raw_rate))

    @staticmethod
    def _calculate_geological_backing_value(
        catalog: ServerMaterialCatalog,
        exchange_rate: float
    ) -> float:
        """
        Menghitung nilai backing geologi dalam UA.
        Ini adalah 'cadangan emas' virtual server — nilai aset nyata yang menjamin mata uang.

        Formula sederhana:
            backing_value = total_material_value_ua * GEOLOGICAL_BACKING_RATIO

        Catatan: total_material_value_ua diambil dari catalog.
        Ini menjadi angka yang muncul di 'laporan cadangan devisa' server.
        """
        # Asumsikan ServerMaterialCatalog punya property total_estimated_value_ua
        # Jika belum ada, kita fallback ke proxy dari exchange_rate
        try:
            raw_value = catalog.total_estimated_value_ua
        except AttributeError:
            # Fallback: estimasi kasar dari exchange rate
            # (ini sinyal ke material_gen.py bahwa perlu ditambah property tersebut)
            raw_value = exchange_rate * PARITY_GEOLOGY_SCORE

        return raw_value * GEOLOGICAL_BACKING_RATIO

    @staticmethod
    def _calculate_supply_breakdown(
        max_allowed_circulation: float,
    ) -> tuple[float, float, float]:
        """
        Menghitung breakdown supply saat genesis.

        Returns:
            (total_supply, circulating_supply, reserve_supply)

        Logika:
        - total_supply = max_allowed_circulation (dari audit geologi)
        - reserve = 20% dari total (dikunci, tidak beredar)
        - circulating = 80% dari total (bisa langsung dipakai pemain)
        """
        total_supply = max_allowed_circulation
        reserve_supply = total_supply * RESERVE_RATIO
        circulating_supply = total_supply - reserve_supply

        return total_supply, circulating_supply, reserve_supply

    # ── Genesis ───────────────────────────────────────────────────────────────

    @staticmethod
    def establish_sovereign_currency(
        catalog: ServerMaterialCatalog,
        audit: SovereigntyReport,
        currency_name: str,
        ticker: str,
        existing_tickers: Optional[set[str]] = None,
    ) -> CurrencyManifest:
        """
        Mengeksekusi Fiat Genesis — penerbitan mata uang perdana server.

        Args:
            catalog:          Data geologi server dari material_gen.py
            audit:            Laporan kelayakan dari economy_engine.py
            currency_name:    Nama lengkap mata uang (misal: "Amerta Dollar")
            ticker:           Kode pendek mata uang (misal: "AMD")
            existing_tickers: Set ticker yang sudah terpakai di seluruh ekosistem.
                              Diisi oleh main_core.py dari query Supabase.
                              Jika None, collision check dilewati (mode offline/dev).

        Returns:
            CurrencyManifest — akta resmi mata uang baru, siap disimpan ke Supabase.

        Raises:
            ValueError — jika ada kondisi ilegal (server gagal audit, ticker duplikat, dll.)
        """

        # ── GUARD 1: Audit kelayakan ──────────────────────────────────────────
        if not audit.is_eligible:
            # Karena sekarang formatnya list, kita join pakai koma atau garis vertikal
            reasons_str = " | ".join(audit.reasons)
            raise ValueError(
                f"ILLEGAL MINTING: Server '{catalog.server_id}' gagal audit Bank Sentral. "
                f"Alasan: {reasons_str}"
            )

        # ── GUARD 2: Validasi input ───────────────────────────────────────────
        clean_name = CurrencyEngine._validate_currency_name(currency_name)
        clean_ticker = CurrencyEngine._validate_ticker(ticker)

        # ── GUARD 3: Collision check ──────────────────────────────────────────
        if existing_tickers is not None:
            if clean_ticker in existing_tickers:
                raise ValueError(
                    f"TICKER CONFLICT: '{clean_ticker}' sudah digunakan server lain "
                    f"di ekosistem Bawan. Pilih ticker berbeda."
                )

        # ── KALKULASI 1: Exchange rate berbasis geologi ───────────────────────
        genesis_rate = CurrencyEngine._calculate_genesis_exchange_rate(
            audit.geology_score
        )

        # ── KALKULASI 2: Geological backing value ─────────────────────────────
        backing_value = CurrencyEngine._calculate_geological_backing_value(
            catalog, genesis_rate
        )

        # ── KALKULASI 3: Supply breakdown ─────────────────────────────────────
        total_supply, circulating_supply, reserve_supply = (
            CurrencyEngine._calculate_supply_breakdown(audit.max_allowed_circulation)
        )

        # ── KALKULASI 4: Genesis Market Cap ───────────────────────────────────
        # Market cap = circulating supply yang aktif * nilai per koin dalam UA
        # Reserve tidak dihitung karena belum beredar di pasar
        genesis_market_cap = circulating_supply * genesis_rate

        # ── RAKIT CURRENCY POLICY ─────────────────────────────────────────────
        policy = CurrencyPolicy(
            hard_cap_supply=total_supply * 2.0,         # Max ever = 2x supply awal
            max_circulating_supply=circulating_supply,
            reserve_ratio=RESERVE_RATIO,
            founding_geology_score=audit.geology_score,
            geological_backing_value_ua=backing_value,
        )

        # ── RAKIT CURRENCY MANIFEST ───────────────────────────────────────────
        return CurrencyManifest(
            server_id=catalog.server_id,
            currency_name=clean_name,
            ticker=clean_ticker,
            genesis_market_cap=genesis_market_cap,
            total_supply=total_supply,
            circulating_supply=circulating_supply,
            reserve_supply=reserve_supply,
            exchange_rate_to_ua=genesis_rate,
            geological_backing_value_ua=backing_value,
            policy=policy,
            genesis_timestamp=datetime.now(timezone.utc).isoformat(),
            founding_geology_score=audit.geology_score,
            is_active=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG / PREVIEW HELPER
# ─────────────────────────────────────────────────────────────────────────────

def preview_manifest(manifest: CurrencyManifest) -> str:
    """
    Menghasilkan teks ringkasan CurrencyManifest untuk logging atau Discord embed.
    Dipanggil oleh main_core.py setelah genesis berhasil.
    """
    p = manifest.policy
    lines = [
        f"╔══ CURRENCY GENESIS REPORT ══════════════════════════",
        f"║  Nama        : {manifest.currency_name} ({manifest.ticker})",
        f"║  Server ID   : {manifest.server_id}",
        f"║  Geology Score: {manifest.founding_geology_score:.1f}",
        f"╠══ NILAI & CADANGAN ══════════════════════════════════",
        f"║  Kurs Genesis : 1 {manifest.ticker} = {manifest.exchange_rate_to_ua:.4f} UA",
        f"║  Backing Value: {manifest.geological_backing_value_ua:,.2f} UA",
        f"║  Market Cap   : {manifest.genesis_market_cap:,.2f} UA",
        f"╠══ SUPPLY ════════════════════════════════════════════",
        f"║  Total Supply : {manifest.total_supply:,.0f} {manifest.ticker}",
        f"║  Beredar      : {manifest.circulating_supply:,.0f} {manifest.ticker} (80%)",
        f"║  Reserve      : {manifest.reserve_supply:,.0f} {manifest.ticker} (20%)",
        f"╠══ KEBIJAKAN MONETER ═════════════════════════════════",
        f"║  Hard Cap     : {p.hard_cap_supply:,.0f} {manifest.ticker} (max ever)",
        f"║  Reserve Ratio: {p.reserve_ratio * 100:.0f}%",
        f"╠══ METADATA ══════════════════════════════════════════",
        f"║  Genesis UTC  : {manifest.genesis_timestamp}",
        f"║  Status       : {'✅ AKTIF' if manifest.is_active else '❌ NONAKTIF'}",
        f"╚═════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)