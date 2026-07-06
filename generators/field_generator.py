import uuid
import random
from decimal import Decimal
from faker import Faker
from datetime import date, timedelta
from schema import HarcamaKategorisi, FaturaKalemi, Fatura

fake = Faker("tr_TR")


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
        "Toplanti İkrami",
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
    HarcamaKategorisi.ULASIM: [
        "Taksi Ücreti",
        "Uçak Bileti",
        "Otopark Ücreti",
        "Yakit Gideri",
        "Otobüs/Metro Karti Yükleme",
        "Araç Kiralama",
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
    HarcamaKategorisi.OFIS_DEMIRBAS: [
        "Ofis Masasi", "Ofis Sandalyesi", "Kahve Makinesi",
        "Yazici/Tarayici", "Monitör",
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
        "Kokteyl İkrami",
        "İçki Servisi",
    ],
    HarcamaKategorisi.EGLENCE: [
        "Organizasyon Bedeli",
        "Etkinlik Bileti",
        "Eğlence Hizmeti",
        "Sinema/Tiyatro Bileti",
    ],
    HarcamaKategorisi.DIGER: [
        "Genel Gider",
        "Muhtelif Harcama",
        "Diğer Hizmet Bedeli",
    ],
}

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

    (HarcamaKategorisi.ULASIM, "Km"): (25, 60), # Taksi km ücretleri baz alinarak
    (HarcamaKategorisi.ULASIM, "Adet"): (30, 8000), # Toplu taşimadan uçak biletine

    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Adet"): (50, 500),
    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Kutu"): (300, 2500),
    (HarcamaKategorisi.OFIS_DEMIRBAS, "Adet"): (3000, 30000),

    (HarcamaKategorisi.DANISMANLIK, "Saat"): (1000, 5000),
    (HarcamaKategorisi.DANISMANLIK, "Ay"): (10000, 100000),
    (HarcamaKategorisi.DANISMANLIK, "Adet"): (2000, 50000),

    (HarcamaKategorisi.YAZILIM_LISANS, "Ay"): (300, 5000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Lisans"): (1000, 25000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Adet"): (500, 15000),
}

# (kategori, birim) sözlükte yoksa geri düşülecek genel aralik (fallback)
FIYAT_ARALIGI_GENEL = {
    HarcamaKategorisi.YEMEK_HIZMETI: (150, 2000),
    HarcamaKategorisi.TEMEL_GIDA: (20, 1000),
    HarcamaKategorisi.ULASIM: (30, 8000),
    HarcamaKategorisi.KONAKLAMA: (1500, 15000),
    HarcamaKategorisi.OFIS_SARF_MALZEME: (50, 2500),   # kirtasiye + sarf paket birleşik fallback
    HarcamaKategorisi.OFIS_DEMIRBAS: (3000, 30000),
    HarcamaKategorisi.YAZILIM_LISANS: (300, 25000),
    HarcamaKategorisi.DANISMANLIK: (2000, 100000),
    HarcamaKategorisi.ALKOL: (150, 4000),
    HarcamaKategorisi.EGLENCE: (300, 15000),
    HarcamaKategorisi.DIGER: (100, 5000),
}

# Kategoriye göre uygun birim
BIRIM_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: ["Adet", "Kişi"],
    HarcamaKategorisi.TEMEL_GIDA: ["Kg", "Adet", "Litre"],
    HarcamaKategorisi.ULASIM: ["Adet", "Km"],
    HarcamaKategorisi.KONAKLAMA: ["Gece", "Adet"],
    HarcamaKategorisi.OFIS_SARF_MALZEME: ["Adet", "Kutu"],
    HarcamaKategorisi.OFIS_DEMIRBAS: ["Adet"],
    HarcamaKategorisi.YAZILIM_LISANS: ["Ay", "Lisans", "Adet"],
    HarcamaKategorisi.DANISMANLIK: ["Saat", "Ay", "Adet"],
    HarcamaKategorisi.ALKOL: ["Adet", "Şişe"],
    HarcamaKategorisi.EGLENCE: ["Adet", "Kişi"],
    HarcamaKategorisi.DIGER: ["Adet"],
}

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

SUFFIX_HAVUZU = ["Ltd. Şti.", "A.Ş.", "Tic. Ltd. Şti."]


# 2.2 — Alan üretici fonksiyonlar

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




def rastgele_firma_adi(kategori: HarcamaKategorisi) -> str:
    sektor_kelime = random.choice(SEKTOR_KELIME_HAVUZU[kategori])
    ozel_isim = fake.last_name()  # Faker'dan sadece soyisim çekiyoruz, iş kolunu biz belirliyoruz
    suffix = random.choice(SUFFIX_HAVUZU)
    return f"{ozel_isim} {sektor_kelime} {suffix}"


def rastgele_kategori() -> HarcamaKategorisi:
    kategoriler = list(HarcamaKategorisi)
    # sira: YEMEK_HIZMETI, TEMEL_GIDA, ULASIM, KONAKLAMA, OFIS_SARF_MALZEME, OFIS_DEMIRBAS,
    #       YAZILIM_LISANS, DANISMANLIK, ALKOL, EGLENCE, DIGER
    agirliklar = [15, 15, 15, 8, 9, 3, 10, 8, 5, 5, 7]
    #             YH  TG  UL  KO OS OD YL  DA AL EG DI
    return random.choices(kategoriler, weights=agirliklar, k=1)[0]


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


KDV_ORANI_MAP = {
    HarcamaKategorisi.YEMEK_HIZMETI: 10.0,
    HarcamaKategorisi.TEMEL_GIDA: 1.0,
    HarcamaKategorisi.ULASIM: 20.0,
    HarcamaKategorisi.KONAKLAMA: 10.0,
    HarcamaKategorisi.OFIS_SARF_MALZEME: 20.0,
    HarcamaKategorisi.OFIS_DEMIRBAS: 20.0,
    HarcamaKategorisi.YAZILIM_LISANS: 20.0,
    HarcamaKategorisi.DANISMANLIK: 20.0,
    HarcamaKategorisi.ALKOL: 20.0,
    HarcamaKategorisi.EGLENCE: 20.0,
    HarcamaKategorisi.DIGER: 20.0,
}

def kdv_orani_belirle(kategori: HarcamaKategorisi) -> float:
    return KDV_ORANI_MAP[kategori]

TAM_SAYI_BIRIMLERI = {"Adet", "Kutu", "Kişi", "Gece", "Lisans", "Şişe"}

def rastgele_miktar(birim: str) -> float:
    if birim in TAM_SAYI_BIRIMLERI:
        return float(random.randint(1, 10))
    else:
        return round(random.uniform(0.5, 10), 2)

def rastgele_kalem(kalem_no: int) -> FaturaKalemi:
    kategori = rastgele_kategori()
    birim = rastgele_birim(kategori)          # önce birim seçiliyor

    return FaturaKalemi(
        kalem_no=kalem_no,
        aciklama=rastgele_aciklama(kategori),
        harcama_kategorisi=kategori,
        miktar=rastgele_miktar(birim),  # miktar da birime bağlı olarak rastgele üretiliyor
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(kategori, birim),   # birim parametre olarak geçiyor
        iskonto_orani=random.choices([0.0, 5.0, 10.0], weights=[70, 20, 10])[0],
        kdv_orani=kdv_orani_belirle(kategori),
    )


def rastgele_tarih(gun_araligi: int = 90) -> str:
    """Son `gun_araligi` gün içinde rastgele bir tarih üretir (gelecek tarih yok)."""
    bugun = date.today()
    rastgele_gun = random.randint(0, gun_araligi)
    tarih = bugun - timedelta(days=rastgele_gun)
    return tarih.isoformat()  # "2026-06-15" formatinda


def rastgele_fatura() -> Fatura:
    """Tam bir Fatura nesnesi üretir: header + kalemler."""
    kalem_sayisi = random.randint(1, 8)

    # Faturadaki tüm kalemler ayni ana kategoriye ait olmak zorunda değil,
    # her kalem bağimsiz kategoriye sahip olabilir (gerçek faturalar böyledir).
    kalemler = [rastgele_kalem(kalem_no=i + 1) for i in range(kalem_sayisi)]

    # Satici firma adini, faturadaki EN BASKIN kategoriye göre belirliyoruz
    # (örn. çoğunlukla market kalemi varsa satici da market/gida firmasi olsun)
    baskin_kategori = kalemler[0].harcama_kategorisi
    satici_adi = rastgele_firma_adi(baskin_kategori)

    return Fatura(
        fatura_no=f"FTR{random.randint(100000, 999999)}",
        fatura_tarihi=rastgele_tarih(),
        satici_vkn=rastgele_vkn(),
        satici_unvan=satici_adi,
        alici_vkn=rastgele_vkn(),
        alici_unvan="SOA People", 
        kalemler=kalemler,
    )

if __name__ == "__main__":
    fatura = rastgele_fatura()

    print("=" * 60)
    print(f"  FATURA NO   : {fatura.fatura_no}")
    print(f"  TARİH       : {fatura.fatura_tarihi}")
    print(f"  SATICI      : {fatura.satici_unvan}")
    print(f"  SATICI VKN  : {fatura.satici_vkn}")
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
    print("=" * 60)