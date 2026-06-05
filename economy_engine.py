"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         ECONOMY_ENGINE.PY  —  Sovereign Capability Detector                  ║
║         Layer 2 — Mengakumulasikan List Alasan Ketidaklayakan Ekonomi        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from dataclasses import dataclass
from typing import List
from material_gen import ServerMaterialCatalog

@dataclass(frozen=True)
class SovereigntyReport:
    server_id: int
    is_eligible: bool             
    max_allowed_circulation: float 
    seigniorage_modifier: float    
    reasons: List[str]            
    geology_score: float           

class EconomyEngine:
    @staticmethod
    def evaluate_server_capability(catalog: ServerMaterialCatalog, current_circulation: float) -> SovereigntyReport:
        # Papan tampungan list alasan kegagalan
        failed_reasons = []
        
        # ── SELEKSI 1: STANDAR KELAYAKAN GEOLOGI TOTAL ──
        MINIMUM_GEO_SCORE = 500.0
        if catalog.total_resource_score < MINIMUM_GEO_SCORE:
            failed_reasons.append(
                f"❌ [Kapasitas Geologi Rendah]: Total skor kerak bumi hanya {catalog.total_resource_score:.2f}, "
                f"sedangkan standar minimum untuk merdeka adalah {MINIMUM_GEO_SCORE:.1f}."
            )

        # ── SELEKSI 2: STANDAR KEKUATAN KOMODITAS INDUSTRI ──
        MINIMUM_INDUSTRIAL_SCORE = 200.0
        if catalog.industrial_resource_score < MINIMUM_INDUSTRIAL_SCORE:
            failed_reasons.append(
                f"❌ [Infrastruktur Industri Lemah]: Skor resource common-metal (Fe, Cu) hanya "
                f"{catalog.industrial_resource_score:.2f}/{MINIMUM_INDUSTRIAL_SCORE:.1f}. "
                f"Sovereign currency wajib dibacking oleh pondasi industri komoditas yang kuat."
            )

        # ── SELEKSI 3: STANDAR BATAS SIRKULASI AWAL ──
        max_allowed_circulation = round(catalog.total_resource_score * 5000.0, 2)
        if current_circulation > max_allowed_circulation and max_allowed_circulation > 0:
            failed_reasons.append(
                f"❌ [Hiperinflasi Awal]: Jumlah uang beredar saat ini ({current_circulation:.2f}) "
                f"sudah melampaui batas maksimum geologi server ({max_allowed_circulation:.2f})."
            )

        # ── EVALUASI AKHIR DARI MESIN EKONOMI ──
        is_eligible = len(failed_reasons) == 0

        if not is_eligible:
            return SovereigntyReport(
                server_id=catalog.server_id,
                is_eligible=False,
                max_allowed_circulation=0.0,
                seigniorage_modifier=0.0, 
                reasons=failed_reasons, 
                geology_score=catalog.total_resource_score 
            )

        # Jika lolos semua syarat, hitung modifier pasar normal
        inflation_rate = current_circulation / max_allowed_circulation if max_allowed_circulation > 0 else 0.0
        if inflation_rate >= 0.75:
            seigniorage_modifier = 0.65
            success_msg = ["🟢 Lulus Uji Kelayakan, namun sirkulasi M2 terpantau padat (Warning Inflasi)."]
        else:
            seigniorage_modifier = 1.0
            success_msg = ["🟢 Lulus Uji Kelayakan! Seluruh indikator makroekonomi server dalam kondisi prima."]

        return SovereigntyReport(
            server_id=catalog.server_id,
            is_eligible=True,
            max_allowed_circulation=max_allowed_circulation,
            seigniorage_modifier=seigniorage_modifier,
            reasons=success_msg,
            geology_score=catalog.total_resource_score 
        )