import uuid
import random
from decimal import Decimal
from faker import Faker
from datetime import date, timedelta
from schema import IS_KOLU_KATEGORILERI, HarcamaKategorisi, FaturaKalemi, Fatura, IsKolu, FirmaTuru, KDV_ORANI_MAP
import re
from pathlib import Path

fake = Faker("tr_TR")
SOYISIM_SQL_DOSYASI = Path(__file__).parent.parent / "data" / "soyisimler.sql"
MARKET_URUNLERI_CSV = Path(__file__).parent.parent / "data" / "urun_verileri" / "market_urunleri.csv"
TEMIZ_URUNLER_CSV = Path(__file__).parent.parent / "data" /   "urun_verileri" / "temiz_urunler.csv"
YEMEK_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "urun_verileri" /"restoran_urunleri.csv"
DANISMANLIK_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"danismanlik_urunleri.csv"
KONAKLAMA_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"konaklama_urunleri.csv"
ULASIM_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"ulasim_urunleri.csv"

# CSV'deki CATEGORY_NAME1 sütununun TÜM benzersiz değerlerine bakıp
# burayı güncelleyin (örn. pandas ile: df['CATEGORY_NAME1'].unique())
MARKET_DAHIL_KATEGORILER =  {
                            "GIDA", "MEYVE SEBZE", "SÜT KAHVALTILIK", "İÇECEK", 
                            "DETERJAN TEMİZLİK", "KAĞIT", "KOZMETİK", "ET TAVUK", 
                            "SİGARA", "EV", "BEBEK", "PET"}

GIYIM_DAHIL_ANA_KATEGORI = "Giyim"

#kategoriye göre uygun csv dosyasından ürünleri yükler.
def market_urunleri_yukle(dosya_yolu: Path = MARKET_URUNLERI_CSV) -> list[str]:
    """
    Market CSV'sinden (ITEMNAME, CATEGORY_NAME1) TEMEL_GIDA açıklama
    havuzunu üretir. Sadece MARKET_DAHIL_KATEGORILER içindeki kategoriler
    alınır (temizlik/kozmetik gibi gıda-dışı satırlar elenir).
    Dosya yoksa boş liste döner, mevcut sabit havuz devrede kalır.
    """
    import csv as _csv
    if not dosya_yolu.exists():
        return []
    urunler: list[str] = []
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        # NOT: dosyanız virgülle ayrılmışsa delimiter="," yapın
        reader = _csv.DictReader(f)   # varsayılan delimiter=","
        for satir in reader:
            kategori = (satir.get("CATEGORY_NAME1") or "").strip().upper()
            if kategori in MARKET_DAHIL_KATEGORILER:
                isim = (satir.get("ITEMNAME") or "").strip()
                if isim:
                    urunler.append(isim.title())
    return urunler

#helper method ürün adında gereksiz şeyleri temizler.
def _urun_kodu_temizle(baslik: str) -> str:
    """
    Başlık sonundaki SKU/ürün kodu benzeri token'ları temizler.
    Heuristik: sondan başlayarak, içinde rakam geçen kelimeleri siler.
    Örn: 'Kruvaze Yaka Trençkot 20mtegk1955trn00' -> 'Kruvaze Yaka Trençkot'
         'Külot TYC0051198584' -> 'Külot'
    Not: bazen 'PNR 002' gibi durumlarda tek harfli kısaltma (PNR) rakamsız
    olduğu için silinmeden kalabilir — mükemmel değil ama çoğu durumda işi görür.
    """
    kelimeler = baslik.split()
    while kelimeler and re.search(r"\d", kelimeler[-1]) and len(kelimeler[-1]) >= 3:
        kelimeler.pop()
    temiz = " ".join(kelimeler).strip(" -/")
    return temiz if temiz else baslik


def temiz_urunleri_yukle(dosya_yolu: Path = TEMIZ_URUNLER_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    veri_temizle.py tarafından üretilen, her satırı zaten tek bir
    HarcamaKategorisi'ne atanmış temiz CSV'yi okur. Burada ARTIK keyword
    matching / regex YOK — sınıflandırma kararı offline'da (veri_temizle.py)
    verildi, bu fonksiyon sadece harcama_kategorisi sütununa göre gruplayarak okuyor.
    """
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    if not dosya_yolu.exists():
        return havuzlar
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            baslik = (satir.get("title") or "").strip()
            kategori_str = (satir.get("harcama_kategorisi") or "").strip()
            if not baslik or not kategori_str:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue
            havuzlar.setdefault(kategori, []).append(_urun_kodu_temizle(baslik))
    return havuzlar

def yemek_urunleri_yukle(dosya_yolu: Path = YEMEK_URUNLERI_CSV) -> list[str]:
    """
    Restoran/yemek harcamasi icin hazirlanan temiz CSV'yi (kategori, urun_adi
    sutunlari) okur. kategori sutunu (corbalar, ana_yemekler vb.) burada
    ayrica kullanilmiyor -- tum satirlar zaten YEMEK_HIZMETI harcama
    kategorisine ait, sadece urun_adi aciklama havuzuna alinir.
    Dosya yoksa bos liste doner, sabit havuz tek basina devrede kalir.
    """
    import csv as _csv
    if not dosya_yolu.exists():
        return []
    urunler: list[str] = []
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            urun = (satir.get("urun_adi") or "").strip()
            if urun:
                urunler.append(urun)
    return urunler

# 2.1 — Kategoriye göre açiklama havuzu

ACIKLAMA_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: [
        "Öğle Yemeği Servis",
        "İş Yemeği",
        "Catering Hizmeti",
        "Tavuk Şiş Menü",
        "Izgara Tabaği",
        "Restoran Faturasi",
        "Personel Yemek Bedeli",
        "Toplanti Yemek Servisi",
        "Kahvalti Servisi",
    ],
    HarcamaKategorisi.TEMEL_GIDA: [
        "Tavuk But (Kg)",
        "Dana Kiyma",
        "Kuşbaşi Et",
        "Market Alişverişi",
        "Bakliyat",
        "Süt ve Süt Ürünleri",
        "Ekmek",
        "Gida Ürünleri",
        "Sebze ve Meyve",
    ],
    HarcamaKategorisi.ULASIM_HIZMETI: [
    "Nakliye Ücreti", "Kargo Hizmeti", "Taşimacilik Bedeli",
    "Depolama Hizmeti", "Lojistik Hizmet Bedeli",
    ],
    HarcamaKategorisi.ULASIM_BIREYSEL: [
        "Taksi Ücreti", "Uçak Bileti", "Otopark Ücreti",
        "Yakit Gideri", "Otobüs/Metro Karti Yükleme", "Araç Kiralama",
    ],
    HarcamaKategorisi.KONAKLAMA: [
        "Otel Konaklama",
        "Misafirhane Ücreti",
        "Apart Kiralama",
        "Konaklama Hizmeti",
    ],
    HarcamaKategorisi.OFIS_SARF_MALZEME: [
        "Kirtasiye Malzemesi", "Toner/Kartuş", "Yazici Kağidi",
        "Temizlik Malzemesi", "A4 Kağit Kolisi",
    ],
    HarcamaKategorisi.OFIS_MOBILYA: [
    "Ofis Masasi", "Ofis Sandalyesi", "Kahve Makinesi",
    "Dolap", "Toplanti Masasi",
    ],
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: [
        "Yazici/Tarayici", "Monitör", "Bilgisayar",
        "Klavye/Mouse Seti", "Sunucu Ekipmani",
    ],
    HarcamaKategorisi.YAZILIM_LISANS: [
        "Yazilim Lisans Bedeli",
        "Bulut Depolama Aboneliği",
        "SaaS Hizmet Bedeli",
        "API Kullanim Ücreti",
        "Yillik Lisans Yenileme",
    ],
    HarcamaKategorisi.DANISMANLIK: [
        "Danişmanlik Hizmeti",
        "Hukuki Danişmanlik",
        "Denetim Hizmeti",
        "Proje Danişmanliği",
    ],
    HarcamaKategorisi.ALKOL: [
        "Şarap Servisi",
        "Bira",
        "Kokteyl Servisi",
        "İçki Servisi",
    ],
    HarcamaKategorisi.EGLENCE: [
        "Organizasyon Bedeli",
        "Etkinlik Bileti",
        "Eğlence Hizmeti",
        "Sinema/Tiyatro Bileti",
    ],
    HarcamaKategorisi.TUTUN_URUNLERI: [
        "Sigara", "Puro", "Elektronik Sigara", "Nargile Hizmeti",
    ],
    HarcamaKategorisi.KUMAR: [
        "Piyango Bileti", "Bahis Ödemesi", "Casino Harcamasi",
    ],
    HarcamaKategorisi.DIGER: [
        "Genel Gider",
        "Muhtelif Harcama",
        "Diğer Hizmet Bedeli",
        "Diğer Market Ürünleri",
    ],
}

# CSV'lerden gelen ürünlerle havuzu zenginleştir/değiştir (dosya yoksa sabit liste kalır)
_market_urunleri = market_urunleri_yukle()

TEMEL_GIDA_MARKET_HAVUZU = _market_urunleri or ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMEL_GIDA]
TEMEL_GIDA_MARKET_AGIRLIGI = 0.6   # rastgele_kalem icinde agirlikli secim icin

# YEMEK_HIZMETI: CSV'deki ~100 kusur urun havuzun ONUNE eklenir, sabit
# el-yazimi liste SILINMEZ -- ayni faturada kalem sayisi CSV'yi tuketirse
# (BUYUK_HAVUZ_ESIGI altinda oldugu icin rastgele_kalem'deki mevcut dedup
# filtresi zaten devrede), filtreleme otomatik olarak eski sabit listeye
# duser. Ayri bir agirlik/fallback kodu gerekmez.
_yemek_urunleri = yemek_urunleri_yukle()
if _yemek_urunleri:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.YEMEK_HIZMETI] = (
        _yemek_urunleri + ACIKLAMA_HAVUZU[HarcamaKategorisi.YEMEK_HIZMETI]
    )

_temiz_urunler = temiz_urunleri_yukle()

# Bu kategorilerin elle yazılmış havuzu yok / azınlıkta — CSV'den geleni
# doğrudan ata, yoksa tek satırlık fallback'e düş.
_SIFIRDAN_HAVUZ_FALLBACK = {
    HarcamaKategorisi.GIYIM: ["Giyim Ürünü"],
    HarcamaKategorisi.KISISEL_BAKIM: ["Kişisel Bakım Ürünü"],
    HarcamaKategorisi.YAZILIM_LISANS: None,   # asagida ayrica ekleniyor (mevcut liste var)
}
for _kategori in (HarcamaKategorisi.GIYIM, HarcamaKategorisi.KISISEL_BAKIM):
    ACIKLAMA_HAVUZU[_kategori] = _temiz_urunler.get(_kategori) or _SIFIRDAN_HAVUZ_FALLBACK[_kategori]

# TEMIZLIK: artik ayri IsKolu yok, MARKET altinda kullaniliyor. CSV'den
# geleni ata, yoksa tek satirlik fallback.
ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] = _temiz_urunler.get(HarcamaKategorisi.TEMIZLIK) or ["Temizlik Ürünü"]

# Supermarket etiketiyle gelen TEMEL_GIDA urunleri (temiz_urunler.csv) —
# market_urunleri.csv'den AYRI tutuluyor, cunku rastgele_kalem'de ikisi
# arasinda %60/%40 agirlikli secim yapilacak (cesitlilik icin).
TEMEL_GIDA_SUPERMARKET_HAVUZU = _temiz_urunler.get(HarcamaKategorisi.TEMEL_GIDA, [])

# ACIKLAMA_HAVUZU[TEMEL_GIDA]: sadece uzunluk hesaplari (rastgele_fatura
# icindeki toplam_musait_aciklama) icin birlesik liste olarak tutuluyor;
# asil SECIM rastgele_kalem icinde iki ayri havuzdan agirlikli yapiliyor.
ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMEL_GIDA] = TEMEL_GIDA_MARKET_HAVUZU + TEMEL_GIDA_SUPERMARKET_HAVUZU

# Elle yazılmış havuzu ZATEN OLAN kategorilerde CSV'den geleni ÜZERİNE
# YAZMA, mevcut listeye EKLE.
for _kategori in (
    HarcamaKategorisi.OFIS_MOBILYA,
    HarcamaKategorisi.OFIS_SARF_MALZEME,
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    HarcamaKategorisi.YAZILIM_LISANS,
):
    _ekstra = _temiz_urunler.get(_kategori)
    if _ekstra:
        ACIKLAMA_HAVUZU[_kategori] = ACIKLAMA_HAVUZU[_kategori] + _ekstra

# Kategoriye göre makul birim fiyat araliği (KDV hariç, TL)
# (kategori, birim) -> (min, max) fiyat araliği
FIYAT_ARALIGI_DETAYLI = {
    (HarcamaKategorisi.EGLENCE, "Kişi"): (300, 4000),
    (HarcamaKategorisi.EGLENCE, "Adet"): (500, 15000),

    (HarcamaKategorisi.YEMEK_HIZMETI, "Kişi"): (200, 2000),
    (HarcamaKategorisi.YEMEK_HIZMETI, "Adet"): (100, 1000),

    (HarcamaKategorisi.KONAKLAMA, "Gece"): (1500, 15000),
    (HarcamaKategorisi.KONAKLAMA, "Adet"): (1500, 15000),

    (HarcamaKategorisi.TEMEL_GIDA, "Kg"): (50, 800), # Sebzeden kirmizi ete uzanan aralik
    (HarcamaKategorisi.TEMEL_GIDA, "Litre"): (20, 500), # Su/Sütten Zeytinyağina uzanan aralik
    (HarcamaKategorisi.TEMEL_GIDA, "Adet"): (20, 500),

    (HarcamaKategorisi.ULASIM_BIREYSEL, "Km"): (10, 50),
    (HarcamaKategorisi.ULASIM_HIZMETI, "Km"): (50, 500),

    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Adet"): (50, 500),
    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Kutu"): (300, 2500),
    (HarcamaKategorisi.OFIS_MOBILYA, "Adet"): (2000, 20000),
    (HarcamaKategorisi.TEKNOLOJI_EKIPMAN, "Adet"): (3000, 40000),   # bilgisayar/sunucu daha pahali olabilir
    

    (HarcamaKategorisi.DANISMANLIK, "Saat"): (1000, 5000),
    (HarcamaKategorisi.DANISMANLIK, "Ay"): (10000, 100000),
    (HarcamaKategorisi.DANISMANLIK, "Adet"): (2000, 50000),

    (HarcamaKategorisi.YAZILIM_LISANS, "Ay"): (300, 5000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Kullanici"): (1000, 25000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Adet"): (500, 15000),
    
    (HarcamaKategorisi.GIYIM, "Adet"): (150, 3000),
}

# (kategori, birim) sözlükte yoksa geri düşülecek genel aralik (fallback)
FIYAT_ARALIGI_GENEL = {
    HarcamaKategorisi.YEMEK_HIZMETI: (150, 2000),
    HarcamaKategorisi.TEMEL_GIDA: (20, 1000),
    HarcamaKategorisi.ULASIM_HIZMETI: (500, 20000),    # B2B nakliye/kargo genelde daha yüksek tutarli
    HarcamaKategorisi.ULASIM_BIREYSEL: (30, 3000),      # taksi/yakit/otopark daha küçük tutarli
    HarcamaKategorisi.KONAKLAMA: (1500, 15000),
    HarcamaKategorisi.OFIS_SARF_MALZEME: (50, 2500),   # kirtasiye + sarf paket birleşik fallback
    HarcamaKategorisi.OFIS_MOBILYA: (2000, 20000),        # masa/sandalye/kahve makinesi
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: (3000, 40000),    # bilgisayar/sunucu daha pahali olabilir
    HarcamaKategorisi.YAZILIM_LISANS: (300, 25000),
    HarcamaKategorisi.DANISMANLIK: (2000, 100000),
    HarcamaKategorisi.ALKOL: (150, 4000),
    HarcamaKategorisi.EGLENCE: (300, 15000),
    HarcamaKategorisi.TUTUN_URUNLERI: (50, 500),
    HarcamaKategorisi.KUMAR: (100, 5000),
    HarcamaKategorisi.DIGER: (100, 5000),
    HarcamaKategorisi.DIGER: (100, 5000),
    HarcamaKategorisi.GIYIM: (150, 3000),   # yeni, kaba varsayım — isterseniz ayarlayın
    HarcamaKategorisi.KISISEL_BAKIM: (50, 800),
    HarcamaKategorisi.TEMIZLIK: (30, 500),
}

# Kategoriye göre uygun birim
BIRIM_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: ["Adet", "Kişi"],
    HarcamaKategorisi.TEMEL_GIDA: ["Kg", "Adet", "Litre"],
    HarcamaKategorisi.ULASIM_HIZMETI: ["Adet", "Km", "Kg"],
    HarcamaKategorisi.ULASIM_BIREYSEL: ["Adet", "Km"],
    HarcamaKategorisi.KONAKLAMA: ["Gece", "Adet"],
    HarcamaKategorisi.OFIS_SARF_MALZEME: ["Adet", "Kutu"],
    HarcamaKategorisi.OFIS_MOBILYA: ["Adet"],
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: ["Adet"],
    HarcamaKategorisi.YAZILIM_LISANS: ["Ay", "Kullanici", "Adet"],
    HarcamaKategorisi.DANISMANLIK: ["Saat", "Ay", "Adet"],
    HarcamaKategorisi.ALKOL: ["Adet", "Şişe"],
    HarcamaKategorisi.EGLENCE: ["Adet", "Kişi"],
    HarcamaKategorisi.TUTUN_URUNLERI: ["Adet", "Paket"],
    HarcamaKategorisi.KUMAR: ["Adet"],
    HarcamaKategorisi.DIGER: ["Adet"],
    HarcamaKategorisi.GIYIM: ["Adet"],
    HarcamaKategorisi.KISISEL_BAKIM: ["Adet"],
    HarcamaKategorisi.TEMIZLIK: ["Adet"],
}
"""
SEKTOR_KELIME_HAVUZU = {
    HarcamaKategorisi.TEMEL_GIDA: ["Bereket", "Öz", "Tarim", "Gida Pazarlama", "Market"],
    HarcamaKategorisi.YEMEK_HIZMETI: ["Lezzet", "Sofra", "Mutfak", "Catering"],
    HarcamaKategorisi.ULASIM: ["Hizli", "Güven", "Transfer", "Lojistik"],
    HarcamaKategorisi.KONAKLAMA: ["Grand", "Otel", "Konaklama", "Suite"],
    HarcamaKategorisi.OFIS_SARF_MALZEME: ["Kirtasiye", "Ofis Sarf", "Büro Malzemeleri"],
    HarcamaKategorisi.OFIS_DEMIRBAS: ["Mobilya", "Ofis Ekipmanlari", "Demirbaş"],
    HarcamaKategorisi.YAZILIM_LISANS: ["Yazilim", "Teknoloji", "Bilişim", "Tech"],
    HarcamaKategorisi.DANISMANLIK: ["Danişmanlik", "Consulting", "Denetim"],
    HarcamaKategorisi.ALKOL: ["İçki Pazarlama", "Şarap Evi", "Meyhane"],
    HarcamaKategorisi.EGLENCE: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
    HarcamaKategorisi.DIGER: ["Ticaret", "Genel", "Pazarlama"],
}
"""

NITELIK_KELIME_HAVUZU = [
    "Modern", "Global", "Anadolu", "Ege", "Merkez", "Yildiz",
    "Başkent", "Marmara", "Kardeş", "Öncü", "Doğa", "Vizyon",
    "Akdeniz", "Karadeniz", "Toros", "Zirve", "Ufuk", "Bereket",
    "Değer", "Fener", "Umut", "Bariş", "Güven", "Sinir",
    "Doruk", "Ata", "Yeni", "Bati", "Doğu", "Kuzey",
]


IS_KOLU_SEKTOR_KELIME = {
    IsKolu.RESTORAN: ["Sofra", "Lezzet", "Mutfak"],
    IsKolu.MARKET: ["Market", "Gida Pazarlama", "Tarim"],
    IsKolu.OTEL: ["Grand", "Otel", "Resort"],
    IsKolu.OFIS_TEDARIK: ["Kirtasiye", "Ofis Sarf", "Büro Malzemeleri"],
    IsKolu.TEKNOLOJI: ["Yazilim", "Teknoloji", "Bilişim"],
    IsKolu.DANISMANLIK_FIRMASI: ["Danişmanlik", "Consulting", "Denetim"],
    IsKolu.LOJISTIK_FIRMASI: ["Lojistik", "Nakliyat", "Kargo"],
    IsKolu.ULASIM_SAGLAYICI: ["Taksi", "Otogar", "Akaryakit", "Rent A Car"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
    IsKolu.GIYIM_MAGAZASI: ["Tekstil", "Giyim", "Moda"],   # yeni
    IsKolu.KISISEL_BAKIM: ["Kozmetik", "Bakım", "Güzellik"],
}
"""SUFFIX_HAVUZU = ["Ltd. Şti.", "A.Ş.", "Tic. Ltd. Şti."] """

IS_KOLU_SUFFIX = {
    IsKolu.RESTORAN: ["Gida San. ve Tic. Ltd. Şti.", "Ltd. Şti."],
    IsKolu.MARKET: ["Gida Paz. Tic. Ltd. Şti.", "Tic. A.Ş."],
    IsKolu.OTEL: ["Turizm A.Ş.", "Otelcilik Ltd. Şti."],
    IsKolu.TEKNOLOJI: ["A.Ş.", "Ltd. Şti."],
    IsKolu.DANISMANLIK_FIRMASI: ["Danişmanlik A.Ş.", "Ltd. Şti."],
    IsKolu.LOJISTIK_FIRMASI: ["A.Ş.", "Nak. Tic. Ltd. Şti."],
    IsKolu.ULASIM_SAGLAYICI: ["Ltd. Şti.", "Turizm Taş. Ltd. Şti."],
    IsKolu.OFIS_TEDARIK: ["Tic. Ltd. Şti.", "A.Ş."],
    IsKolu.ORGANIZASYON: ["Ltd. Şti.", "Prodüksiyon A.Ş."],
    IsKolu.ORGANIZASYON: ["Ltd. Şti.", "Prodüksiyon A.Ş."],
    IsKolu.GIYIM_MAGAZASI: ["Tekstil Tic. Ltd. Şti.", "Konfeksiyon A.Ş."],   # yeni
    IsKolu.KISISEL_BAKIM: ["Kozmetik Tic. Ltd. Şti.", "A.Ş."],
}

# Uzun unvan varyasyonlari için ek kelime havuzu
UZUN_UNVAN_EKLERI = ["Global", "İç ve Diş Ticaret", "Sanayi ve Ticaret"]

FIRMA_TURU_AGIRLIK = {
    FirmaTuru.KISA_UNVAN: 55,
    FirmaTuru.UZUN_UNVAN: 25,
    FirmaTuru.SAHIS_SIRKETI: 15,
    FirmaTuru.YABANCI_ORTAKLI: 5,
}


# 2.2 — Alan üretici fonksiyonlar

def soyisimleri_yukle() -> list[str]:
    """SQL dosyasindaki INSERT satirlarindan soyisimleri regex ile çeker."""
    with open(SOYISIM_SQL_DOSYASI, "r", encoding="utf-8") as f:
        icerik = f.read()

    # 'ABAT' gibi tek tirnak içindeki değerleri yakalar
    bulunanlar = re.findall(
    r"VALUES\s*\('([^']+)'\)",
    icerik,
    flags=re.IGNORECASE
)
    return bulunanlar


TUM_SOYISIMLER = soyisimleri_yukle()   # modül yüklenince bir kere okunur


def rastgele_soyisim() -> str:
    return random.choice(TUM_SOYISIMLER)

def rastgele_vkn() -> str:
    """
    Gerçek Türkiye VKN algoritmasina uygun 10 haneli vergi kimlik no üretir.
    Algoritma: ilk 9 hane rastgele, 10. hane bir checksum formülüyle hesaplanir.
    """
    digits = [random.randint(0, 9) for _ in range(9)]

    total = 0
    for i in range(9):
        d = (digits[i] + (9 - i)) % 10
        if d != 0:
            d = (d * (2 ** (9 - i))) % 9
            if d == 0:
                d = 9
        total += d

    check_digit = (10 - (total % 10)) % 10
    digits.append(check_digit)

    return "".join(str(d) for d in digits)

def rastgele_tckn() -> str:
    """
    Gerçek TCKN algoritmasina uygun 11 haneli TC Kimlik No üretir.
    İlk hane 0 olamaz. Son iki hane checksum'dir.
    """
    ilk_dokuz = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]

    tek_toplam = sum(ilk_dokuz[0:9:2])   # 1,3,5,7,9. haneler (index 0,2,4,6,8)
    cift_toplam = sum(ilk_dokuz[1:8:2])  # 2,4,6,8. haneler (index 1,3,5,7)

    onuncu_hane = ((tek_toplam * 7) - cift_toplam) % 10
    ilk_on = ilk_dokuz + [onuncu_hane]

    on_birinci_hane = sum(ilk_on) % 10

    return "".join(str(d) for d in ilk_on + [on_birinci_hane])

def rastgele_firma_turu() -> FirmaTuru:
    """Sadece firma türünü ağirlikli olarak seçer."""
    turler = list(FIRMA_TURU_AGIRLIK.keys())
    agirliklar = list(FIRMA_TURU_AGIRLIK.values())
    return random.choices(turler, weights=agirliklar, k=1)[0]


def rastgele_kimlik_no(firma_turu: FirmaTuru) -> str:
    if firma_turu == FirmaTuru.SAHIS_SIRKETI:
        return rastgele_tckn()
    return rastgele_vkn()

def rastgele_firma_adi(is_kolu: IsKolu, firma_turu: FirmaTuru) -> str:
    if firma_turu == FirmaTuru.SAHIS_SIRKETI:
        return fake.name()

    sektor_kelime = random.choice(IS_KOLU_SEKTOR_KELIME[is_kolu])
    suffix = random.choice(IS_KOLU_SUFFIX[is_kolu])
    ozel_isim = rastgele_soyisim()

    sablon = random.choices(
        [
            "isim_once",       # Yilmaz Gida A.Ş.
            "isim_yok",        # Anadolu Gida A.Ş. (nitelik kelimesi sektörün yerini dolduruyor)
            "isim_nitelikli",  # Yilmaz Anadolu Gida A.Ş.
        ],
        weights=[40, 20, 40],
        k=1,
    )[0]

    if sablon == "isim_yok":
        nitelik = random.choice(NITELIK_KELIME_HAVUZU)
        return f"{nitelik} {sektor_kelime} {suffix}"

    if sablon == "isim_nitelikli":
        nitelik = random.choice(NITELIK_KELIME_HAVUZU)
        return f"{ozel_isim} {nitelik} {sektor_kelime} {suffix}"

    return f"{ozel_isim} {sektor_kelime} {suffix}"   # isim_once (varsayilan)

# Alici (bizim şirketimiz) sabit kimlik bilgileri — her faturada ayni olmali
ALICI_VKN_SABIT = rastgele_vkn()
ALICI_UNVAN_SABIT = "SOA People"



""" fatura kategorisi tutarlılık için random.choices(izinli_kategoriler) ile oluşturuluyor bu kod bloğu şuanlık ölü.
def rastgele_kategori() -> HarcamaKategorisi:
    kategoriler = list(HarcamaKategorisi)
    # Sira: YEMEK_HIZMETI, TEMEL_GIDA, ULASIM_HIZMETI, ULASIM_BIREYSEL, KONAKLAMA,
    #       OFIS_SARF_MALZEME, OFIS_DEMIRBAS, YAZILIM_LISANS, DANISMANLIK, ALKOL, EGLENCE, DIGER
    agirliklar = [15, 15, 7, 8, 8, 9, 3, 10, 8, 5, 5, 7, 1 ,1] #sondaki 1, 1 sonradan eklelenen tütün ürünleri ve kumar için
    # iskoluna dahil edilip normal fatura üretiminde hiç kullanilmayacaklar 
    # yalnizca anomali kisminda dahil olacak ama liste uzunluğu enumerator ile eşleşmeli bundan dolayi eklendi
    return random.choices(kategoriler, weights=agirliklar, k=1)[0]
"""

def rastgele_aciklama(kategori: HarcamaKategorisi) -> str:
    return random.choice(ACIKLAMA_HAVUZU[kategori])


def rastgele_birim(kategori: HarcamaKategorisi) -> str:
    return random.choice(BIRIM_HAVUZU[kategori])


def rastgele_birim_fiyat(kategori: HarcamaKategorisi, birim: str) -> Decimal:
    # Önce (kategori, birim) özel araliğina bak, yoksa genel kategori araliğina düş
    aralik = FIYAT_ARALIGI_DETAYLI.get(
        (kategori, birim),
        FIYAT_ARALIGI_GENEL[kategori]
    )
    low, high = aralik

    # %90 ihtimalle normal aralikta, %10 ihtimalle aralik dişina taşan
    # "gürültülü" bir değer üret (gerçek hayatin kusurlu dağilimini simüle eder)
    if random.random() < 0.90:
        fiyat = random.triangular(low, high, low + (high - low) * 0.2)
    else:
        # Araliğin biraz dişina taşan aykiri (ama fahiş olmayan) bir değer
        disari_tasma = (high - low) * 0.3
        fiyat = random.uniform(max(0, low - disari_tasma), high + disari_tasma)

    return Decimal(str(round(fiyat, 2)))



def kdv_orani_belirle(kategori: HarcamaKategorisi) -> float:
    return KDV_ORANI_MAP[kategori]

TAM_SAYI_BIRIMLERI = {"Adet", "Kutu", "Kişi", "Gece", "Lisans", "Şişe"}

def rastgele_miktar(birim: str) -> float:
    if birim in TAM_SAYI_BIRIMLERI:
        return float(random.randint(1, 10))
    else:
        return round(random.uniform(0.5, 10), 2)

def rastgele_kalem(
    kalem_no: int,
    izinli_kategoriler: list[HarcamaKategorisi],
    kullanilan_aciklamalar: set[str],) -> FaturaKalemi:
    kategori = random.choice(izinli_kategoriler)
    birim = rastgele_birim(kategori)

    # Büyük havuzlarda (CSV kaynaklı, binlerce eleman) tekrar filtresi hem
    # gereksiz maliyetli hem de anlamsız (çakışma ihtimali zaten ~0),
    # o yüzden sadece küçük (elle yazılmış) havuzlarda filtreleme yapılır.
    BUYUK_HAVUZ_ESIGI = 500

    if kategori == HarcamaKategorisi.TEMEL_GIDA:
        # Cesitlilik icin iki ayri kaynaktan agirlikli secim:
        # %60 market_urunleri.csv, %40 temiz_urunler.csv (Supermarket etiketi).
        # Ikisi de buyuk havuz oldugu icin dedup filtresi uygulanmiyor.
        if random.random() < TEMEL_GIDA_MARKET_AGIRLIGI and TEMEL_GIDA_MARKET_HAVUZU:
            aciklama = random.choice(TEMEL_GIDA_MARKET_HAVUZU)
        elif TEMEL_GIDA_SUPERMARKET_HAVUZU:
            aciklama = random.choice(TEMEL_GIDA_SUPERMARKET_HAVUZU)
        else:
            aciklama = random.choice(TEMEL_GIDA_MARKET_HAVUZU)
    else:
        havuz = ACIKLAMA_HAVUZU[kategori]
        if len(havuz) > BUYUK_HAVUZ_ESIGI:
            aciklama = random.choice(havuz)
        else:
            musait_aciklamalar = [a for a in havuz if a not in kullanilan_aciklamalar]
            if not musait_aciklamalar:
                musait_aciklamalar = havuz
            aciklama = random.choice(musait_aciklamalar)
            kullanilan_aciklamalar.add(aciklama)

    return FaturaKalemi(
        kalem_no=kalem_no,
        aciklama=aciklama,
        harcama_kategorisi=kategori,
        miktar=rastgele_miktar(birim),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(kategori, birim),
        iskonto_orani=random.choices([0.0, 5.0, 10.0], weights=[70, 20, 10])[0],
        kdv_orani=kdv_orani_belirle(kategori),
    )


def rastgele_tarih(gun_araligi: int = 90) -> str:
    """Son `gun_araligi` gün içinde rastgele bir tarih üretir (gelecek tarih yok)."""
    bugun = date.today()
    rastgele_gun = random.randint(0, gun_araligi)
    tarih = bugun - timedelta(days=rastgele_gun)
    return tarih.isoformat()  # "2026-06-15" formatinda


def rastgele_fatura_no(fatura_tarihi: str) -> str:
    """
    GİB e-fatura standardina uygun 16 haneli fatura no üretir:
    3 harf (seri) + 4 haneli yil + 9 haneli sira no
    """
    yil = fatura_tarihi[:4]   # "2026-04-20" -> "2026", tarihle tutarli olsun
    sira_no = random.randint(1, 999999999)
    return f"FTR{yil}{sira_no:09d}"   # 09d -> 9 haneye sifirla tamamla


def rastgele_fatura() -> Fatura:
    is_kolu = random.choice(list(IsKolu))
    izinli_kategoriler = IS_KOLU_KATEGORILERI[is_kolu]

    firma_turu = rastgele_firma_turu()             # 1. adim: tür seç (taksonomi)
    satici_adi = rastgele_firma_adi(is_kolu, firma_turu)   # 2. adim: isim üret
    satici_kimlik = rastgele_kimlik_no(firma_turu)         #3. adim: kimlik no üret

    fatura_tarihi = rastgele_tarih()        
    fatura_no = rastgele_fatura_no(fatura_tarihi)
    
    # Bu iş kolunda toplam kaç benzersiz açiklama üretilebilir? artık csv dosyalarından verileri çekildiği için bu gereksiz olabilir
    toplam_musait_aciklama = sum(
        len(ACIKLAMA_HAVUZU[kategori]) for kategori in izinli_kategoriler
    )

    # Kalem sayisi, mevcut çeşitliliği aşmasin (en fazla 8, ama havuz küçükse ona göre kisitla)
    ust_sinir = min(8, toplam_musait_aciklama)
    kalem_sayisi = random.randint(1, max(1, ust_sinir))      

    kullanilan_aciklamalar: set[str] = set()
    kalemler = [
        rastgele_kalem(i + 1, izinli_kategoriler, kullanilan_aciklamalar)
        for i in range(kalem_sayisi)
    ]

    return Fatura(
        fatura_no=fatura_no,
        fatura_tarihi=fatura_tarihi,
        satici_vkn=satici_kimlik,
        satici_unvan=satici_adi,
        alici_vkn=ALICI_VKN_SABIT,
        alici_unvan=ALICI_UNVAN_SABIT,
        is_kolu=is_kolu,   # yeni
        kalemler=kalemler,
    )

def kimlik_etiketi(kimlik_no: str) -> str:
    """10 haneliyse VKN, 11 haneliyse TCKN etiketi döndürür."""
    if len(kimlik_no) == 11:
        return "TCKN"
    return "VKN"


if __name__ == "__main__":
    fatura = rastgele_fatura()

    print("=" * 60)
    print(f"  FATURA NO   : {fatura.fatura_no}")
    print(f"  TARİH       : {fatura.fatura_tarihi}")
    print(f"  SATICI      : {fatura.satici_unvan}")
    etiket = kimlik_etiketi(fatura.satici_vkn)
    print(f"  SATICI {etiket:<4} : {fatura.satici_vkn}")
    print(f"  ALICI       : {fatura.alici_unvan}")
    print(f"  ALICI VKN   : {fatura.alici_vkn}")
    print("=" * 60)

    for k in fatura.kalemler:
        print(f"\n  [{k.kalem_no}] {k.aciklama}  ({k.harcama_kategorisi.value})")
        print(f"      Miktar       : {k.miktar} {k.birim}")
        print(f"      Birim Fiyat  : {k.birim_fiyat} TL")
        print(f"      İskonto      : %{k.iskonto_orani}")
        print(f"      KDV Orani    : %{k.kdv_orani}")
        print(f"      Ara Toplam   : {k.ara_toplam:.2f} TL")
        print(f"      KDV Tutari   : {k.kdv_tutari:.2f} TL")
        print(f"      Satir Toplam : {k.satir_toplam:.2f} TL")

    print("\n" + "=" * 60)
    print(f"  GENEL TOPLAM: {fatura.genel_toplam:.2f} TL")
    print(f"  VERGİSİZ TOPLAM: {fatura.toplam_vergisiz_tutar:.2f} TL")
    print(f"  KDV TOPLAM: {fatura.toplam_kdv_tutari:.2f} TL")
    print(f"  İSKONTO TOPLAM: {fatura.toplam_iskonto:.2f} TL")
    print("=" * 60)