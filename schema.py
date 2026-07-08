from pydantic import BaseModel, field_validator
from decimal import Decimal
from typing import List
from enum import Enum

class HarcamaKategorisi(str, Enum):
    YEMEK_HIZMETI = "yemek_hizmeti"      # restoran, catering, iş yemeği
    TEMEL_GIDA = "temel_gida"            # market, bakliyat, süt, et
    ULASIM_HIZMETI = "ulasim_hizmeti"      # B2B: nakliye, kargo, taşımacılık, depolama
    ULASIM_BIREYSEL = "ulasim_bireysel"    # taksi, otobüs/metro, yakıt, araç kiralama
    KONAKLAMA = "konaklama"
    OFIS_SARF_MALZEME = "ofis_sarf_malzeme"   # kırtasiye, toner, kağıt
    OFIS_MOBILYA = "ofis_mobilya"              # masa, sandalye, kahve makinesi
    TEKNOLOJI_EKIPMAN = "teknoloji_ekipman"    # yazıcı, monitör, bilgisayar           # bilgisayar, yazıcı, masa, sandalye, klima
    YAZILIM_LISANS = "yazilim_lisans"
    DANISMANLIK = "danismanlik"
    ALKOL = "alkol"
    EGLENCE = "eglence"
    DIGER = "diger"

KDV_ORANI_MAP = {
    HarcamaKategorisi.YEMEK_HIZMETI: 10.0,
    HarcamaKategorisi.TEMEL_GIDA: 1.0,
    HarcamaKategorisi.ULASIM_HIZMETI: 20.0,
    HarcamaKategorisi.ULASIM_BIREYSEL: 20.0,
    HarcamaKategorisi.KONAKLAMA: 10.0,
    HarcamaKategorisi.OFIS_SARF_MALZEME: 20.0,
    HarcamaKategorisi.OFIS_MOBILYA: 20.0,
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: 20.0,
    HarcamaKategorisi.YAZILIM_LISANS: 20.0,
    HarcamaKategorisi.DANISMANLIK: 20.0,
    HarcamaKategorisi.ALKOL: 20.0,
    HarcamaKategorisi.EGLENCE: 20.0,
    HarcamaKategorisi.DIGER: 20.0,
}

class IsKolu(str, Enum):
    RESTORAN = "restoran"
    MARKET = "market"
    OTEL = "otel"
    OFIS_TEDARIK = "ofis_tedarik"
    TEKNOLOJI = "teknoloji"
    DANISMANLIK_FIRMASI = "danismanlik_firmasi"
    LOJISTIK_FIRMASI = "lojistik_firmasi"       # eskiden ULASIM_FIRMASI ise adını netleştirdik
    ULASIM_SAGLAYICI = "ulasim_saglayici"        # taksi durağı, akaryakıt istasyonu, rent-a-car
    ORGANIZASYON = "organizasyon"


IS_KOLU_KATEGORILERI = {
    IsKolu.RESTORAN: [HarcamaKategorisi.YEMEK_HIZMETI, HarcamaKategorisi.ALKOL],
    IsKolu.OTEL: [HarcamaKategorisi.KONAKLAMA, HarcamaKategorisi.YEMEK_HIZMETI, HarcamaKategorisi.ALKOL],
    IsKolu.OFIS_TEDARIK: [
        HarcamaKategorisi.OFIS_SARF_MALZEME,
        HarcamaKategorisi.OFIS_MOBILYA,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    IsKolu.TEKNOLOJI: [
        HarcamaKategorisi.YAZILIM_LISANS,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    IsKolu.MARKET: [HarcamaKategorisi.TEMEL_GIDA],   # <- tek tanım kaldı
    IsKolu.DANISMANLIK_FIRMASI: [HarcamaKategorisi.DANISMANLIK],
    IsKolu.LOJISTIK_FIRMASI: [HarcamaKategorisi.ULASIM_HIZMETI],
    IsKolu.ULASIM_SAGLAYICI: [HarcamaKategorisi.ULASIM_BIREYSEL],
    IsKolu.ORGANIZASYON: [HarcamaKategorisi.EGLENCE, HarcamaKategorisi.YEMEK_HIZMETI, HarcamaKategorisi.ALKOL],
}


class FirmaTuru(str, Enum):
    KISA_UNVAN = "kisa_unvan"
    UZUN_UNVAN = "uzun_unvan"
    SAHIS_SIRKETI = "sahis_sirketi"
    YABANCI_ORTAKLI = "yabanci_ortakli"

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