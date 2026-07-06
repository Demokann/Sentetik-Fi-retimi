from pydantic import BaseModel, field_validator
from decimal import Decimal
from typing import List
from enum import Enum

class HarcamaKategorisi(str, Enum):
    YEMEK_HIZMETI = "yemek_hizmeti"      # restoran, catering, iş yemeği
    TEMEL_GIDA = "temel_gida"            # market, bakliyat, süt, et
    ULASIM = "ulasim"
    KONAKLAMA = "konaklama"
    OFIS_MALZEME = "ofis_malzeme"
    YAZILIM_LISANS = "yazilim_lisans"
    DANISMANLIK = "danismanlik"
    ALKOL = "alkol"
    EGLENCE = "eglence"
    DIGER = "diger"




class FaturaKalemi(BaseModel):
    kalem_no: int
    aciklama: str
    harcama_kategorisi: HarcamaKategorisi
    miktar: float
    birim: str  # "Adet", "Saat", "Kg", "Ay"
    birim_fiyat: Decimal
    iskonto_orani: float = 0.0
    kdv_orani: float  # 1.0, 10.0, 20.0

    @property
    def ara_toplam(self) -> Decimal:
        brut = Decimal(str(self.miktar)) * self.birim_fiyat
        iskonto = brut * Decimal(str(self.iskonto_orani)) / 100
        return brut - iskonto

    @property
    def kdv_tutari(self) -> Decimal:
        return self.ara_toplam * Decimal(str(self.kdv_orani)) / 100

    @property
    def satir_toplam(self) -> Decimal:
        return self.ara_toplam + self.kdv_tutari


class Fatura(BaseModel):
    fatura_no: str
    fatura_tarihi: str
    satici_vkn: str  # 10 haneli
    satici_unvan: str
    alici_vkn: str
    alici_unvan: str
    kalemler: List[FaturaKalemi]

    @property
    def genel_toplam(self) -> Decimal:
        return sum((k.satir_toplam for k in self.kalemler), Decimal("0"))