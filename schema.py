from pydantic import BaseModel, Field, field_validator
from decimal import ROUND_HALF_UP, Decimal
from typing import List
from enum import Enum


def paraya_yuvarla(deger: Decimal) -> Decimal:
        """Herhangi bir Decimal değeri 2 ondalik basamağa (kuruş hassasiyeti) yuvarlar."""
        return deger.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class HarcamaKategorisi(str, Enum):
    YEMEK_HIZMETI = "yemek_hizmeti"      # restoran, catering, iş yemeği
    TEMEL_GIDA = "temel_gida"            # market, bakliyat, süt, et
    ULASIM_HIZMETI = "ulasim_hizmeti"      # B2B: nakliye, kargo, taşimacilik, depolama
    ULASIM_BIREYSEL = "ulasim_bireysel"    # taksi, otobüs/metro, yakit, araç kiralama
    KONAKLAMA = "konaklama"
    OFIS_SARF_MALZEME = "ofis_sarf_malzeme"   # kirtasiye, toner, kağit
    OFIS_MOBILYA = "ofis_mobilya"              # masa, sandalye, kahve makinesi
    TEKNOLOJI_EKIPMAN = "teknoloji_ekipman"    # yazici, monitör, bilgisayar           # bilgisayar, yazici, masa, sandalye, klima
    YAZILIM_LISANS = "yazilim_lisans"
    DANISMANLIK = "danismanlik"
    ALKOL = "alkol"
    EGLENCE = "eglence"
    TUTUN_URUNLERI = "tutun_urunleri"   # yeni
    KUMAR = "kumar"                     # yeni
    DIGER = "diger"                     # ölü kod
    GIYIM = "giyim"                     # yeni
    KISISEL_BAKIM = "kisisel_bakim"      # yeni
    TEMIZLIK = "temizlik"                # yeni

# Genel geçer, şirketten bağimsiz yasakli kategoriler (kanunen kabul edilmeyen gider mantiği)
POLICY_YASAKLI_KATEGORILER: set[HarcamaKategorisi] = {
    HarcamaKategorisi.ALKOL,
    HarcamaKategorisi.EGLENCE,
    HarcamaKategorisi.TUTUN_URUNLERI,
    HarcamaKategorisi.KUMAR,
}

# Kategori bazli üst tutar limitleri (aşilirsa şüpheli sayilir)
POLICY_TUTAR_LIMITLERI: dict[HarcamaKategorisi, float] = {
    HarcamaKategorisi.YEMEK_HIZMETI: 2500,
    HarcamaKategorisi.KONAKLAMA: 20000,
    HarcamaKategorisi.ULASIM_BIREYSEL: 5000,
}

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
    HarcamaKategorisi.TUTUN_URUNLERI: 20.0,
    HarcamaKategorisi.KUMAR: 20.0,
    HarcamaKategorisi.EGLENCE: 20.0,
    HarcamaKategorisi.DIGER: 20.0,
    HarcamaKategorisi.GIYIM: 10.0,   # yeni — tekstil/hazır giyim indirimli KDV
    HarcamaKategorisi.KISISEL_BAKIM: 20.0,   # yeni — varsayım, teyit gerekebilir
    HarcamaKategorisi.TEMIZLIK: 20.0,        # yeni — varsayım
}

class IsKolu(str, Enum):
    RESTORAN = "restoran"
    MARKET = "market"
    OTEL = "otel"
    OFIS_TEDARIK = "ofis_tedarik"
    TEKNOLOJI = "teknoloji"
    DANISMANLIK_FIRMASI = "danismanlik_firmasi"
    LOJISTIK_FIRMASI = "lojistik_firmasi"       # eskiden ULASIM_FIRMASI ise adini netleştirdik
    ULASIM_SAGLAYICI = "ulasim_saglayici"        # taksi duraği, akaryakit istasyonu, rent-a-car
    ORGANIZASYON = "organizasyon"
    GIYIM_MAGAZASI = "giyim_magazasi"   # yeni

    # Gerçekçi Sentetik Veri İçin Eklenmesi Gerekenler
    KISISEL_BAKIM = "kisisel_bakim"      # Şampuan, krem, makyaj


IS_KOLU_KATEGORILERI = {
    IsKolu.RESTORAN: [HarcamaKategorisi.YEMEK_HIZMETI],
    IsKolu.OTEL: [HarcamaKategorisi.KONAKLAMA, HarcamaKategorisi.YEMEK_HIZMETI],
    IsKolu.OFIS_TEDARIK: [
        HarcamaKategorisi.OFIS_SARF_MALZEME,
        HarcamaKategorisi.OFIS_MOBILYA,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    IsKolu.TEKNOLOJI: [
        HarcamaKategorisi.YAZILIM_LISANS,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    IsKolu.MARKET: [HarcamaKategorisi.TEMEL_GIDA, HarcamaKategorisi.TEMIZLIK],   # temizlik urunleri de markette satilir
    IsKolu.DANISMANLIK_FIRMASI: [HarcamaKategorisi.DANISMANLIK],
    IsKolu.LOJISTIK_FIRMASI: [HarcamaKategorisi.ULASIM_HIZMETI],
    IsKolu.ULASIM_SAGLAYICI: [HarcamaKategorisi.ULASIM_BIREYSEL],
    IsKolu.ORGANIZASYON: [HarcamaKategorisi.YEMEK_HIZMETI],
    IsKolu.GIYIM_MAGAZASI: [HarcamaKategorisi.GIYIM],   # yeni
    IsKolu.KISISEL_BAKIM: [HarcamaKategorisi.KISISEL_BAKIM],
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
        return paraya_yuvarla(brut - iskonto)

    @property
    def kdv_tutari(self) -> Decimal:
        return paraya_yuvarla(self.ara_toplam * Decimal(str(self.kdv_orani)) / 100)

    @property
    def satir_toplam(self) -> Decimal:
        return paraya_yuvarla(self.ara_toplam + self.kdv_tutari)


class Fatura(BaseModel):
    fatura_no: str
    fatura_tarihi: str
    satici_vkn: str  # 10 haneli
    satici_unvan: str
    alici_vkn: str
    alici_unvan: str
    kalemler: List[FaturaKalemi]
    is_kolu: IsKolu   # yeni — iş kolu/kategori uyumsuzluğu validasyonu için şart
    is_anomali: bool = False
    anomali_turleri: List[str] = Field(default_factory=list) 

    @property
    def toplam_vergisiz_tutar(self) -> Decimal:
        """Tüm kalemlerin KDV hariç (iskonto sonrasi) toplami."""
        toplam = sum((k.ara_toplam for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)

    @property
    def toplam_kdv_tutari(self) -> Decimal:
        """Tüm kalemlerin KDV tutarlarinin toplami."""
        toplam = sum((k.kdv_tutari for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)

    @property
    def toplam_iskonto(self) -> Decimal:
        """Tüm kalemlerdeki iskonto tutarlarinin toplami (brüt - ara_toplam farki)."""
        toplam = Decimal("0")
        for k in self.kalemler:
            brut = Decimal(str(k.miktar)) * k.birim_fiyat
            iskonto_tutari = brut * Decimal(str(k.iskonto_orani)) / 100
            toplam += iskonto_tutari
        return paraya_yuvarla(toplam)

    @property
    def genel_toplam(self) -> Decimal:
        toplam = sum((k.satir_toplam for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)
    
class AnomaliliFaturaKalemi(FaturaKalemi):
    """
    FaturaKalemi'nin matematiksel anomali üretmek için kullanilan alt sinifi.
    sahte_satir_toplam verilirse, satir_toplam property'si; sahte_ara_toplam
    verilirse ara_toplam property'si gerçek hesaplama yerine bu sahte
    değerleri döndürür.
    """
    sahte_satir_toplam: Decimal | None = None
    sahte_ara_toplam: Decimal | None = None
    sahte_kdv_tutari: Decimal | None = None

    @property
    def ara_toplam(self) -> Decimal:
        if self.sahte_ara_toplam is not None:
            return self.sahte_ara_toplam
        return super().ara_toplam
    
    @property
    def kdv_tutari(self) -> Decimal:
        if self.sahte_kdv_tutari is not None:
            return self.sahte_kdv_tutari
        return super().kdv_tutari

    @property
    def satir_toplam(self) -> Decimal:
        if self.sahte_satir_toplam is not None:
            return self.sahte_satir_toplam
        return super().satir_toplam
    
class AnomaliliFatura(Fatura):
    """
    Fatura'nin genel_toplam'ini ya da footer kirilimlarindan
    (toplam_vergisiz_tutar / toplam_kdv_tutari) birini kasitli olarak
    bozmak için kullanilan alt sinifi.
    """
    sahte_genel_toplam: Decimal | None = None
    sahte_toplam_vergisiz_tutar: Decimal | None = None
    sahte_toplam_kdv_tutari: Decimal | None = None

    @property
    def toplam_vergisiz_tutar(self) -> Decimal:
        if self.sahte_toplam_vergisiz_tutar is not None:
            return self.sahte_toplam_vergisiz_tutar
        return super().toplam_vergisiz_tutar

    @property
    def toplam_kdv_tutari(self) -> Decimal:
        if self.sahte_toplam_kdv_tutari is not None:
            return self.sahte_toplam_kdv_tutari
        return super().toplam_kdv_tutari

    @property
    def genel_toplam(self) -> Decimal:
        if self.sahte_genel_toplam is not None:
            return self.sahte_genel_toplam
        return super().genel_toplam