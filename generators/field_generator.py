import uuid
import random
from decimal import Decimal
from faker import Faker
from datetime import date, timedelta
from schema import HarcamaKategorisi, FaturaKalemi, Fatura

fake = Faker("tr_TR")


# 2.1 — Kategoriye göre açiklama havuzu

ACIKLAMA_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: [          # YEMEK -> YEMEK_HIZMETI
        "Öğle Yemeği", "İş Yemeği", "Catering Hizmeti",
        "Personel Yemek Bedeli", "Toplanti İkrami",
    ],
    HarcamaKategorisi.TEMEL_GIDA: [               # YENİ EKLENDİ
        "Market Alişverişi", "Bakliyat", "Süt ve Süt Ürünleri",
        "Ekmek", "Et ve Tavuk Ürünleri",
    ],
    HarcamaKategorisi.ULASIM: [
        "Taksi Ücreti", "Uçak Bileti", "Otopark Ücreti",
        "Yakit Gideri", "Otobüs/Metro Karti Yükleme",
    ],
    HarcamaKategorisi.KONAKLAMA: [
        "Otel Konaklama", "Misafirhane Ücreti", "Apart Kiralama",
    ],
    HarcamaKategorisi.OFIS_MALZEME: [
        "Kirtasiye Malzemesi", "Toner/Kartuş", "Ofis Sarf Malzemesi",
        "Temizlik Malzemesi",
    ],
    HarcamaKategorisi.YAZILIM_LISANS: [
        "Yazilim Lisans Bedeli", "Bulut Depolama Aboneliği",
        "SaaS Hizmet Bedeli", "API Kullanim Ücreti",
    ],
    HarcamaKategorisi.DANISMANLIK: [
        "Danişmanlik Hizmeti", "Hukuki Danişmanlik", "Denetim Hizmeti",
    ],
    HarcamaKategorisi.ALKOL: [
        "Şarap Servisi", "Bira", "Kokteyl İkrami", "İçki Servisi",
    ],
    HarcamaKategorisi.EGLENCE: [
        "Organizasyon Bedeli", "Etkinlik Bileti", "Eğlence Hizmeti",
    ],
    HarcamaKategorisi.DIGER: [
        "Genel Gider", "Muhtelif Harcama", "Diğer Hizmet Bedeli",
    ],
}

# Kategoriye göre makul birim fiyat araliği (KDV hariç, TL)
FIYAT_ARALIGI = {
    HarcamaKategorisi.YEMEK_HIZMETI: (50, 800),
    HarcamaKategorisi.TEMEL_GIDA: (20, 1000),
    HarcamaKategorisi.ULASIM: (30, 5000),
    HarcamaKategorisi.KONAKLAMA: (800, 8000),
    HarcamaKategorisi.OFIS_MALZEME: (20, 1500),
    HarcamaKategorisi.YAZILIM_LISANS: (200, 15000),
    HarcamaKategorisi.DANISMANLIK: (1000, 50000),
    HarcamaKategorisi.ALKOL: (100, 2000),
    HarcamaKategorisi.EGLENCE: (200, 10000),
    HarcamaKategorisi.DIGER: (50, 3000),
}

# Kategoriye göre uygun birim
BIRIM_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: ["Adet", "Kişi"],
    HarcamaKategorisi.TEMEL_GIDA: ["Kg", "Adet", "Litre"],
    HarcamaKategorisi.ULASIM: ["Adet", "Km"],
    HarcamaKategorisi.KONAKLAMA: ["Gece", "Adet"],
    HarcamaKategorisi.OFIS_MALZEME: ["Adet", "Kutu"],
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
    HarcamaKategorisi.OFIS_MALZEME: ["Kirtasiye", "Ofis", "Büro Malzemeleri"],
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
    # sira: YEMEK_HIZMETI, TEMEL_GIDA, ULASIM, KONAKLAMA, OFIS_MALZEME,
    #       YAZILIM_LISANS, DANISMANLIK, ALKOL, EGLENCE, DIGER
    agirliklar =    [15, 15, 15, 8, 12, 10, 8, 5, 5, 7]
    return random.choices(kategoriler, weights=agirliklar, k=1)[0]


def rastgele_aciklama(kategori: HarcamaKategorisi) -> str:
    return random.choice(ACIKLAMA_HAVUZU[kategori])


def rastgele_birim(kategori: HarcamaKategorisi) -> str:
    return random.choice(BIRIM_HAVUZU[kategori])


def rastgele_birim_fiyat(kategori: HarcamaKategorisi) -> Decimal:
    low, high = FIYAT_ARALIGI[kategori]
    # log-normal benzeri dağilim: çoğu değer alt banda yakin, nadiren yüksek
    fiyat = random.triangular(low, high, low + (high - low) * 0.2)
    return Decimal(str(round(fiyat, 2)))


KDV_ORANI_MAP = {
    HarcamaKategorisi.YEMEK_HIZMETI: 10.0,
    HarcamaKategorisi.TEMEL_GIDA: 1.0,
    HarcamaKategorisi.ULASIM: 20.0,
    HarcamaKategorisi.KONAKLAMA: 10.0,
    HarcamaKategorisi.OFIS_MALZEME: 20.0,
    HarcamaKategorisi.YAZILIM_LISANS: 20.0,
    HarcamaKategorisi.DANISMANLIK: 20.0,
    HarcamaKategorisi.ALKOL: 20.0,
    HarcamaKategorisi.EGLENCE: 20.0,
    HarcamaKategorisi.DIGER: 20.0,
}

def kdv_orani_belirle(kategori: HarcamaKategorisi) -> float:
    return KDV_ORANI_MAP[kategori]

def rastgele_kalem(kalem_no: int) -> FaturaKalemi:
    """Tek bir fatura kalemi üretir — kategori tutarlılığını korur."""
    kategori = rastgele_kategori()

    return FaturaKalemi(
        kalem_no=kalem_no,
        aciklama=rastgele_aciklama(kategori),
        harcama_kategorisi=kategori,
        miktar=round(random.uniform(1, 10), 2),
        birim=rastgele_birim(kategori),
        birim_fiyat=rastgele_birim_fiyat(kategori),
        iskonto_orani=random.choices([0.0, 5.0, 10.0], weights=[70, 20, 10])[0],
        kdv_orani=kdv_orani_belirle(kategori),
    )


def rastgele_tarih(gun_araligi: int = 90) -> str:
    """Son `gun_araligi` gün içinde rastgele bir tarih üretir (gelecek tarih yok)."""
    bugun = date.today()
    rastgele_gun = random.randint(0, gun_araligi)
    tarih = bugun - timedelta(days=rastgele_gun)
    return tarih.isoformat()  # "2026-06-15" formatında


def rastgele_fatura() -> Fatura:
    """Tam bir Fatura nesnesi üretir: header + kalemler."""
    kalem_sayisi = random.randint(1, 8)

    # Faturadaki tüm kalemler aynı ana kategoriye ait olmak zorunda değil,
    # her kalem bağımsız kategoriye sahip olabilir (gerçek faturalar böyledir).
    kalemler = [rastgele_kalem(kalem_no=i + 1) for i in range(kalem_sayisi)]

    # Satıcı firma adını, faturadaki EN BASKIN kategoriye göre belirliyoruz
    # (örn. çoğunlukla market kalemi varsa satıcı da market/gıda firması olsun)
    baskin_kategori = kalemler[0].harcama_kategorisi
    satici_adi = rastgele_firma_adi(baskin_kategori)

    return Fatura(
        fatura_no=f"FTR{random.randint(100000, 999999)}",
        fatura_tarihi=rastgele_tarih(),
        satici_vkn=rastgele_vkn(),
        satici_unvan=satici_adi,
        alici_vkn=rastgele_vkn(),
        alici_unvan="Tessera Lab & Oktavis",  # sabit alıcı, senin şirketin
        kalemler=kalemler,
    )

if __name__ == "__main__":
    # Hizli manuel test
    print("VKN:", rastgele_vkn())

    kategori = rastgele_kategori()
    print("Firma:", rastgele_firma_adi(kategori))
    print("Kategori:", kategori)
    print("Açiklama:", rastgele_aciklama(kategori))
    print("Birim:", rastgele_birim(kategori))
    print("Birim Fiyat:", rastgele_birim_fiyat(kategori))
    print("KDV Orani:", kdv_orani_belirle(kategori))