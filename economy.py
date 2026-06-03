from dataclasses import dataclass
from material_gen import ServerMaterialCatalog
from ore import OreItem

@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    price_per_tonne: float
    total_value: float
    purity_modifier: float
    scarcity_multiplier: float

class EconomyOracle:
    """
    Oracle penilai harga item lokal. 
    Murni menghitung VALUE barang ke dalam bentuk fiat lokal server.
    """
    
    @staticmethod
    def calculate_ore_price(item: OreItem, catalog: ServerMaterialCatalog) -> PriceQuote:
        # JIKA ITEM ADALAH UA (UANG ANJING / JANGKAR EMAS)
        if item.element_symbol in ["Au", "UA"]:
            base_multiplier = 500.0  # Dikunci tetap sebagai standar moneter global
            scarcity_mult = 1.0     # Tidak terpengaruh kelangkaan lokal
        else:
            # Komoditas biasa harganya didikte oleh kesiapan makro geologi server
            if item.origin_type == "GLOBAL_CORE":
                base_multiplier = catalog.industrial_resource_score * 0.1
            else:
                base_multiplier = catalog.strategic_resource_score * 0.2
            
            # Semakin dominan suatu unsur, harganya di pasar lokal makin murah
            scarcity_mult = 2.0 - catalog.dominance_ratio

        # Pengali berdasarkan tingkat kemurnian ore saat ditambang
        purity_map = {"Crude": 1.0, "Enriched": 1.5, "Flawless": 2.5}
        purity_mod = purity_map.get(item.purity, 1.0)

        # Kalkulasi nilai akhir
        price_per_tonne = base_multiplier * scarcity_mult * purity_mod
        total_value = round(price_per_tonne * item.weight_tonnes, 4)

        return PriceQuote(
            symbol=item.element_symbol,
            price_per_tonne=round(price_per_tonne, 4),
            total_value=total_value,
            purity_modifier=purity_mod,
            scarcity_multiplier=round(scarcity_mult, 4)
        )