"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         MINT_CAP.PY  —  Central Bank Operations Engine                       ║
║         Layer 4 — Mengontrol dan memvalidasi pencetakan uang (QE)            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from dataclasses import dataclass
from currency_engine import CurrencyManifest

@dataclass(frozen=True)
class MintingReport:
    """Laporan hasil evaluasi request pencetakan uang."""
    success: bool
    amount_requested: float
    amount_to_circulate: float
    amount_to_reserve: float
    new_total_supply: float
    reason: str

class CentralBankEngine:
    """
    Mesin Operasional Bank Sentral.
    Bertugas sebagai gatekeeper (penjaga gawang) untuk mencegah admin 
    melakukan pencetakan fiat tak terbatas yang bisa merusak ekonomi game.
    """

    @staticmethod
    def evaluate_minting_request(
        manifest: CurrencyManifest, 
        current_geology_score: float, 
        mint_amount: float
    ) -> MintingReport:
        
        # ── GUARD 1: Status Mata Uang ──
        if not manifest.is_active:
            return MintingReport(False, mint_amount, 0.0, 0.0, manifest.total_supply, "Mata uang sedang dibekukan / tidak aktif.")

        # ── GUARD 2: Batas Kewajaran Input ──
        if mint_amount <= 0:
            return MintingReport(False, mint_amount, 0.0, 0.0, manifest.total_supply, "Jumlah cetak harus lebih dari 0.")

        policy = manifest.policy

        # ── GUARD 3: Cek Stabilitas Geologi (Underlying Asset) ──
        # Jika skor geologi hancur (kurang dari 50% saat pendirian) karena diover-mining,
        # maka mencetak uang baru adalah tindakan bunuh diri ekonomi (Hiperinflasi).
        CRITICAL_GEOLOGY_DROP_RATIO = 0.50
        minimum_safe_score = policy.founding_geology_score * CRITICAL_GEOLOGY_DROP_RATIO
        
        if current_geology_score < minimum_safe_score:
            return MintingReport(
                success=False,
                amount_requested=mint_amount,
                amount_to_circulate=0.0,
                amount_to_reserve=0.0,
                new_total_supply=manifest.total_supply,
                reason=f"Krisis Geologi! Skor kerak bumi saat ini ({current_geology_score:.1f}) anjlok di bawah batas aman ({minimum_safe_score:.1f}). Cadangan tidak kuat menopang fiat baru."
            )

        # ── GUARD 4: Cek Hard Cap Supply ──
        projected_total_supply = manifest.total_supply + mint_amount
        if projected_total_supply > policy.hard_cap_supply:
            sisa_kuota = policy.hard_cap_supply - manifest.total_supply
            return MintingReport(
                success=False,
                amount_requested=mint_amount,
                amount_to_circulate=0.0,
                amount_to_reserve=0.0,
                new_total_supply=manifest.total_supply,
                reason=f"Melewati Hard Cap! Sisa kuota cetak server ini hanya {sisa_kuota:.2f} {manifest.ticker}."
            )

        # ── KALKULASI PEMBAGIAN SUPPLY ──
        # Jika lolos semua guard, potong pajak cadangan (reserve) sesuai policy
        reserve_cut = mint_amount * policy.reserve_ratio
        circulating_cut = mint_amount - reserve_cut

        return MintingReport(
            success=True,
            amount_requested=mint_amount,
            amount_to_circulate=circulating_cut,
            amount_to_reserve=reserve_cut,
            new_total_supply=projected_total_supply,
            reason="Pencetakan disetujui. Distribusi sirkulasi dan reserve telah dihitung."
        )