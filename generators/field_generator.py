import uuid
import random
from decimal import Decimal
from faker import Faker
from datetime import date, timedelta
from schema import IS_KOLU_KATEGORILERI, HarcamaKategorisi, FaturaKalemi, Fatura, IsKolu, FirmaTuru, KDV_ORANI_MAP
import re, math
from pathlib import Path

fake = Faker("tr_TR")
SOYISIM_SQL_DOSYASI = Path(__file__).parent.parent / "data" / "soyisimler.sql"
MARKET_URUNLERI_CSV = Path(__file__).parent.parent / "data" / "urun_verileri" / "market_urunleri.csv"
TEMIZ_URUNLER_CSV = Path(__file__).parent.parent / "data" /   "urun_verileri" / "temiz_urunler.csv"
YEMEK_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "urun_verileri" /"restoran_urunleri.csv"
DANISMANLIK_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"danismanlik_urunleri.csv"
KONAKLAMA_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"konaklama_urunleri.csv"
ULASIM_URUNLERI_CSV = Path(__file__).parent.parent / "data" /  "hizmet_verileri" /"ulasim_urunleri.csv"
ANOMALI_URUNLERI_CSV = Path(__file__).parent.parent / "data" / "anomali_verileri" / "anomali_urunler.csv"

# CSV kategorisi -> HarcamaKategorisi eşlemesi. Karşılığı olmayan satırlar
# (KAĞIT/EV/BEBEK/PET gibi şu anki şemada kategorisi bulunmayanlar) BİLİNÇLİ
# olarak eşlemeye dahil EDİLMEDİ, aksi halde TEMEL_GIDA'ya sızarlardı.
# SİGARA de KASITLI olarak burada YOK: bu ürünler sadece anomali_urunleri_yukle()
# üzerinden TUTUN_URUNLERI anomali havuzuna girmeli, normal üretimde asla kullanılmamalı.
MARKET_KATEGORI_ESLESTIRME: dict[str, HarcamaKategorisi] = {
    "GIDA": HarcamaKategorisi.TEMEL_GIDA,
    "MEYVE SEBZE": HarcamaKategorisi.TEMEL_GIDA,
    "SÜT KAHVALTILIK": HarcamaKategorisi.TEMEL_GIDA,
    "İÇECEK": HarcamaKategorisi.TEMEL_GIDA,
    "ET TAVUK": HarcamaKategorisi.TEMEL_GIDA,
    "DETERJAN TEMİZLİK": HarcamaKategorisi.TEMIZLIK,
    "KOZMETİK": HarcamaKategorisi.KISISEL_BAKIM,
}

GIYIM_DAHIL_ANA_KATEGORI = "Giyim"

def market_urunleri_yukle(dosya_yolu: Path = MARKET_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    Market CSV'sinden (ITEMNAME, CATEGORY_NAME1) kategoriye göre GRUPLANMIŞ
    açıklama havuzu üretir. MARKET_KATEGORI_ESLESTIRME'de karşılığı olmayan
    satırlar (SİGARA dahil) elenir -- artık tek bir düz TEMEL_GIDA listesine
    karışmıyorlar. Dosya yoksa boş dict döner.
    """
    import csv as _csv
    if not dosya_yolu.exists():
        return {}
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("CATEGORY_NAME1") or "").strip().upper()
            hedef_kategori = MARKET_KATEGORI_ESLESTIRME.get(kategori_str)
            if hedef_kategori is None:
                continue
            isim = (satir.get("ITEMNAME") or "").strip()
            if isim:
                havuzlar.setdefault(hedef_kategori, []).append(isim.title())
    return havuzlar

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

# Firma adi belirli bir mutfagi isaret ettiginde SADECE o firmada gorunmesi
# gereken menu bolumleri. Genel YEMEK_HIZMETI havuzuna ALINMAZLAR: boylece
# notr adli bir restoran ("Mola Lokantasi") sushi/cig kofte satmaz.
# Ters yon (burgercinin tavuk sis satmamasi) mutfak kisitiyla saglanir.
DAR_MUTFAK_BOLUMLERI = {"cigkofte", "uzakdogu", "pastane_tatli", "balik", "pizza", "burger"}


def yemek_urunleri_yukle(dosya_yolu: Path = YEMEK_URUNLERI_CSV) -> list[str]:
    """
    Restoran/yemek harcamasi icin hazirlanan temiz CSV'yi (kategori, urun_adi
    sutunlari) okur ve GENEL havuzu dondurur. DAR_MUTFAK_BOLUMLERI atlanir --
    o urunler yalnizca adi eslesen firmada, mutfak kisiti uzerinden gorunur.
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
            bolum = (satir.get("kategori") or "").strip()
            if urun and bolum not in DAR_MUTFAK_BOLUMLERI:
                urunler.append(urun)
    return urunler


def yemek_urunleri_bolumlu_yukle(dosya_yolu: Path = YEMEK_URUNLERI_CSV) -> dict[str, list[str]]:
    """Ayni CSV'yi bolum -> [urun] olarak yukler (mutfak kisiti icin). Genel
    havuzun aksine DAR bolumler de dahildir."""
    import csv as _csv
    if not dosya_yolu.exists():
        return {}
    bolumler: dict[str, list[str]] = {}
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        for satir in _csv.DictReader(f):
            urun = (satir.get("urun_adi") or "").strip()
            bolum = (satir.get("kategori") or "").strip()
            if urun and bolum:
                bolumler.setdefault(bolum, []).append(urun)
    return bolumler

def danismanlik_urunleri_yukle(dosya_yolu: Path = DANISMANLIK_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Danışmanlık harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar


def konaklama_urunleri_yukle(dosya_yolu: Path = KONAKLAMA_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Konaklama harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar


def ulasim_urunleri_yukle(dosya_yolu: Path = ULASIM_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Ulaşım harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar
def _csv_yok_uyar(dosya_yolu: Path, sonuc: str) -> None:
    """
    Eksik CSV'yi GURULTULU bildirir.

    SESSIZ DUSMEK PAHALIYA MAL OLDU (2026-07-29): `data/anomali_veri` dizini
    `data/anomali_verileri` olarak yeniden adlandirilinca (gitignore istisnasina
    uydurmak icin) bu dosya bulunamadi. Iki yukleyici de sessizce bos dondu:
      - ACIKLAMA_HAVUZU yasakli kategorileri 3-4 elemanli sabit listeye dustu
      - ANOMALI_URUN_MAKULLUGU bos kaldi -> satici-ekseni ayrimi devre disi
    Sonuc: 100k'lik bir uretim, yasakli kategoride 4 urun cesitliligiyle ve
    %100 etiket ortusmesiyle tamamlandi -- TEK BIR HATA MESAJI OLMADAN.

    Geri dusme davranisi KORUNUYOR (kirilgan import zinciri istemiyoruz) ama
    artik sessiz degil.
    """
    import warnings
    warnings.warn(
        f"\n  CSV BULUNAMADI: {dosya_yolu}\n  -> {sonuc}\n"
        "  -> uretime devam etmeden once yolu duzeltin.",
        RuntimeWarning, stacklevel=3,
    )


def anomali_urunleri_yukle(dosya_yolu: Path = ANOMALI_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    anomalili_veriler.csv'yi (kategori, urun_adi sutunlari) okur. kategori
    sutunundaki degerler (alkol, tutun_urunleri, eglence, kumar)
    HarcamaKategorisi enum degerleriyle birebir eslesiyor, ayri bir
    eslestirme tablosu gerekmiyor. Dosya yoksa bos dict doner, mevcut
    sabit havuzlar (ACIKLAMA_HAVUZU icindeki elle yazilmis 3-4 elemanli
    listeler) devrede kalir.
    """
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    if not dosya_yolu.exists():
        _csv_yok_uyar(dosya_yolu, "yasakli kategori havuzlari 3-4 elemanli sabit listeye DUSUYOR")
        return havuzlar
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue
            havuzlar.setdefault(kategori, []).append(urun)
    return havuzlar


def anomali_urun_makullugu_yukle(dosya_yolu: Path = ANOMALI_URUNLERI_CSV) -> dict[str, set[str]]:
    """
    Ayni CSV'nin ucuncu sutunu (`makul_is_kollari`, `;` ile ayrilmis is_kolu
    listesi) -> {urun_adi: {is_kolu, ...}}.

    NEDEN AYRI BIR EKSEN: yasakli kategoriler (alkol/tutun/eglence/kumar)
    hicbir is kolunun IS_KOLU_KATEGORILERI listesinde YOK -- olamaz da, olsaydi
    TEMIZ fise alkol/sigara duserdi. Bunun yan etkisi olarak yasakli bir kalem
    enjekte edilir edilmez `is_kolu_kategori_uyumsuzlugu` de yapisal olarak
    tetikleniyordu (olculdu: 2065/2065, %100). Oysa bunlar IKI AYRI eksendir:
      - POLITIKA ekseni  : sirket bu gideri odemez            -> yasakli_kategori
      - SATICI ekseni    : bu dukkan bunu zaten satmaz        -> is_kolu_kategori_uyumsuzlugu
    Restoranda alkol, markette sigara, akaryakit istasyonunda piyango bileti
    POLITIKA ihlalidir ama satici acisindan tamamen olagandir. Iki etiket %100
    korele oldugunda biri bilgi tasimaz; model aralarindaki farki ogrenemez.

    Makullik URUN bazindadir, kategori bazinda DEGIL: markette "Milli Piyango
    Bileti" makul, "Casino Cip Alimi" degil; restoranda "Nargile" makul, "Sarma
    Tutun (50gr)" degil. Kategori bazli bir muafiyet bu ayrimi silerdi.

    Sutun bos ise urun hicbir is koluna makul degildir (destinasyon biletleri,
    casino urunleri) -- bunlar uyumsuzlugu tetiklemeye DEVAM eder. Dosya ya da
    sutun yoksa bos dict doner: davranis eski haline (her yasakli kalem
    uyumsuzluk tetikler) guvenli sekilde geri duser.
    """
    import csv as _csv
    makullik: dict[str, set[str]] = {}
    if not dosya_yolu.exists():
        _csv_yok_uyar(dosya_yolu, "satici-ekseni ayrimi DEVRE DISI (yasakli kalem = otomatik is_kolu uyumsuzlugu)")
        return makullik
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            urun = (satir.get("urun_adi") or "").strip()
            ham = (satir.get("makul_is_kollari") or "").strip()
            if not urun or not ham:
                continue
            makullik[urun] = {p.strip() for p in ham.split(";") if p.strip()}
    return makullik


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
    HarcamaKategorisi.GIYIM: [
        "Giyim Ürünü",
        "Ayakkabı",
        "Mont",
        "Pantolon",
        "Gömlek",
        "Ceket",
        "Etek",
        "Kazak",
    ],
    HarcamaKategorisi.KISISEL_BAKIM: [
        "Şampuan",
        "Krem",
        "Makyaj Malzemesi",
        "Diş Macunu",
        "Parfüm",
        "Tıraş Malzemesi",
    ],
    HarcamaKategorisi.TEMIZLIK: [
        "Deterjan",
        "Temizlik Bezi",
        "Süpürge",
        "Temizlik Spreyi",
        "Bulaşık Makinesi Tableti",
    ],

}

# Ürün adı -> birim eşlemesi. Sadece birim bilgisi CSV'de VARSA doldurulur
# (danışmanlık/konaklama/ulaşım gibi el ile hazırlanmış küçük havuzlar).
# rastgele_kalem, bir aciklama seçtikten SONRA burada karşılığı var mı diye
# bakar; yoksa eski davranışa (kategori bazlı rastgele birim) düşer.
URUN_BIRIM_ESLEME: dict[str, str] = {}


def _urun_birim_kayitlarini_isle(
    urunler_by_kategori: dict[HarcamaKategorisi, list[tuple[str, str | None]]]
) -> dict[HarcamaKategorisi, list[str]]:
    """
    (urun_adi, birim) çiftlerinden oluşan yükleyici çıktısını ACIKLAMA_HAVUZU'nun
    beklediği düz isim listesine çevirir; birim bilgisi varsa global
    URUN_BIRIM_ESLEME sözlüğüne kaydeder.
    """
    sonuc: dict[HarcamaKategorisi, list[str]] = {}
    for kategori, kayitlar in urunler_by_kategori.items():
        isimler: list[str] = []
        for urun_adi, birim in kayitlar:
            isimler.append(urun_adi)
            if birim:
                URUN_BIRIM_ESLEME[urun_adi] = birim
        sonuc[kategori] = isimler
    return sonuc

# CSV'lerden gelen ürünlerle havuzu zenginleştir/değiştir (dosya yoksa sabit liste kalır)
_market_urunleri = market_urunleri_yukle()

TEMEL_GIDA_MARKET_HAVUZU = (
    _market_urunleri.get(HarcamaKategorisi.TEMEL_GIDA)
    or ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMEL_GIDA]
)
TEMEL_GIDA_MARKET_AGIRLIGI = 0.6

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

# 1. Danışmanlık Ürünleri
_danismanlik_urunleri = _urun_birim_kayitlarini_isle(danismanlik_urunleri_yukle())
for kategori, urunler in _danismanlik_urunleri.items():
    if urunler:
        ACIKLAMA_HAVUZU[kategori] = urunler  # Üzerine yaz (ez)

# 2. Konaklama Ürünleri
_konaklama_urunleri = _urun_birim_kayitlarini_isle(konaklama_urunleri_yukle())
for kategori, urunler in _konaklama_urunleri.items():
    if urunler:
        ACIKLAMA_HAVUZU[kategori] = urunler  # Üzerine yaz (ez)

# 3. Ulaşım Ürünleri
_ulasim_urunleri = _urun_birim_kayitlarini_isle(ulasim_urunleri_yukle())
for kategori, urunler in _ulasim_urunleri.items():
    if urunler:
        # Eğer ulasim.csv içinde kategori HIZMETI ve BIREYSEL diye ayrılmışsa
        # bu döngü direkt doğru yere yazacaktır.
        ACIKLAMA_HAVUZU[kategori] = urunler

_temiz_urunler = temiz_urunleri_yukle()

_anomali_urunleri = anomali_urunleri_yukle()

# Yasakli kalemin SATICI ekseninde makul olup olmadigi (bkz.
# anomali_urun_makullugu_yukle docstring'i). validators.py bunu okur.
ANOMALI_URUN_MAKULLUGU: dict[str, set[str]] = anomali_urun_makullugu_yukle()

# ALKOL/EGLENCE/TUTUN_URUNLERI/KUMAR: elle yazilmis sabit listeler sadece
# 3-4 elemanliydi, CSV çok daha zengin (100'lerce ürün) -- CSV varsa
# üzerine YAZIYORUZ (append degil), yoksa eski sabit liste fallback olarak kalir.
for _kategori in (
    HarcamaKategorisi.ALKOL,
    HarcamaKategorisi.EGLENCE,
    HarcamaKategorisi.TUTUN_URUNLERI,
    HarcamaKategorisi.KUMAR,
):
    _ekstra = _anomali_urunleri.get(_kategori)
    if _ekstra:
        ACIKLAMA_HAVUZU[_kategori] = _ekstra

# CSV'den geleni ata, boş/yoksa ACIKLAMA_HAVUZU'ndaki (artık zenginleştirilmiş)
# mevcut genel açıklama listesine düş -- ayrı, alakasız tek satırlık
# fallback listelerine artık gerek yok.
for _kategori in (HarcamaKategorisi.GIYIM, HarcamaKategorisi.KISISEL_BAKIM):
    ACIKLAMA_HAVUZU[_kategori] = _temiz_urunler.get(_kategori) or ACIKLAMA_HAVUZU[_kategori]

# Market CSV'sindeki KOZMETİK/DETERJAN TEMİZLİK satırlarıyla ilgili
# havuzları zenginleştir (üzerine yazma, ekle).
_market_kisisel_bakim = _market_urunleri.get(HarcamaKategorisi.KISISEL_BAKIM, [])
if _market_kisisel_bakim:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.KISISEL_BAKIM] = (
        ACIKLAMA_HAVUZU[HarcamaKategorisi.KISISEL_BAKIM] + _market_kisisel_bakim
    )

_market_temizlik = _market_urunleri.get(HarcamaKategorisi.TEMIZLIK, [])
if _market_temizlik:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] = (
        ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] + _market_temizlik
    )

# TEMIZLIK: artik ayri IsKolu yok, MARKET altinda kullaniliyor. CSV'den
# geleni ata, yoksa tek satirlik fallback.
ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] = _temiz_urunler.get(HarcamaKategorisi.TEMIZLIK) or ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK]

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

    (HarcamaKategorisi.ULASIM_BIREYSEL, "Km"): (150, 1750),
    (HarcamaKategorisi.ULASIM_HIZMETI, "Km"): (250, 3500),
    (HarcamaKategorisi.ULASIM_HIZMETI, "Ton"): (1000, 35000),
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Gün"): (800, 3000),
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Litre"): (50, 3750),
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Saat"): (100, 1000),
    (HarcamaKategorisi.KONAKLAMA, "Saat"): (300, 3000),

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
    HarcamaKategorisi.ULASIM_HIZMETI: ["Adet", "Km", "Kg", "Ton"],
    HarcamaKategorisi.ULASIM_BIREYSEL: ["Adet", "Km", "Litre", "Gün"],
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
# İş kolu bazlı SEÇİM ağırlığı: her iş kolunun izinli kategorilerindeki
# toplam açıklama/ürün sayısına göre (log ölçekli) hesaplanır. Amaç:
# market/teknoloji gibi CSV'den binlerce ürün çekebilen iş kollarının
# faturalarda AŞIRI baskın olmasını engellerken, danışmanlık/konaklama/
# ulaşım gibi havuzu çok küçük (20-40 ürün) iş kollarının da GERÇEKÇİ
# şekilde daha az üretilmesini sağlamak -- küçük havuzda çok sayıda fatura
# üretilirse ayni açıklamalar defalarca tekrar etmek zorunda kalıyor.
# log1p kullanılıyor çünkü ham havuz büyüklükleri arasında (23 ile 500.000+
# arası) devasa fark var; düz orantı diğer TÜM iş kollarını yok sayardı.
# TABAN_AGIRLIK, en küçük havuzlu iş kolunun bile sıfıra yakın olasılığa
# düşmemesini garantiler.
TABAN_AGIRLIK = 1.0

def _is_kolu_agirliklarini_hesapla() -> dict[IsKolu, float]:
    agirliklar: dict[IsKolu, float] = {}
    for is_kolu in IsKolu:
        izinli_kategoriler = IS_KOLU_KATEGORILERI.get(is_kolu, [])
        toplam_urun = sum(len(ACIKLAMA_HAVUZU.get(k, [])) for k in izinli_kategoriler)
        agirliklar[is_kolu] = TABAN_AGIRLIK + math.log1p(toplam_urun)
    return agirliklar


IS_KOLU_AGIRLIKLARI = _is_kolu_agirliklarini_hesapla()


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
    # Ulaşım sektör kelimeleri hizmete ÖZEL (Taksi/Akaryakit/Rent A Car) ya da
    # JENERİK (Turizm/Seyahat/Ulaşım). Özel olanlar yalnız fişte ilgili kalem varsa
    # firma adına girer (bkz. SEKTOR_KELIME_KALEM_KOSULU); jenerikler her zaman uygun.
    IsKolu.ULASIM_SAGLAYICI: ["Taksi", "Otogar", "Akaryakit", "Rent A Car",
                              "Turizm", "Seyahat", "Ulaşım"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
    IsKolu.GIYIM_MAGAZASI: ["Tekstil", "Giyim", "Moda"],   # yeni
    IsKolu.KISISEL_BAKIM: ["Kozmetik", "Bakım", "Güzellik"],
}

# Bir sektör kelimesi firma adına ancak fişteki kalemlerden biri ilgili anahtar
# kelimeyi içeriyorsa girebilir (ör. 'Taksi' yalnız faturada taksi kalemi varsa).
# Burada OLMAYAN sektör kelimeleri jeneriktir, her zaman uygundur.
SEKTOR_KELIME_KALEM_KOSULU = {
    "Taksi": ("taksi",),
    "Akaryakit": ("akaryakit", "yakit", "benzin", "motorin"),
    "Rent A Car": ("arac kiralama", "rent a car", "kiralama"),
    "Otogar": ("otobus", "otogar", "sehirlerarasi"),
}


def _ascii_kucuk(metin: str) -> str:
    """Türkçe metni ASCII küçük harfe indirger (kalem adı ile anahtar kelime
    eşleşmesini diakritik farkına takılmadan yapmak için)."""
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    return metin.translate(str.maketrans("ğüşıöç", "gusioc"))


# --- Mutfak uyumu -----------------------------------------------------------
# OSM'den gelen restoran adlarinin %44'u mutfak kelimesi tasiyor ("Tatlises Cig
# Kofte", "Duru Balik"). Ad dikkate alinmadan kalem secilirse cigkoftecidan
# sushi cikiyor; bu etiketleri bozmaz ama aciklama uretiminde ("X'ten aldik")
# tutarsiz metin dogurur. Burada firma adindan mutfak tespit edilip kalem havuzu
# ilgili menu bolumleriyle sinirlanir.
#
# NOT: Bu, emekliye ayrilan sektor-kelime / is_kolu-geri-okuma mekanizmasinin
# geri donusu DEGILDIR. O, ADDAN is_kolu CIKARMAYA calisiyordu; bu ise zaten
# bilinen is_kolu icinde yemek secimini daraltir.
#
# SIRA ONEMLI, ilk eslesen kazanir: "Tatlises Cig Kofte" hem "tatli" hem
# "cig kofte" iceriyor -- cigkofte once geldigi icin dogru siniflanir.
# Dar bolume ek olarak ilgili GENEL bolum de verilir: burgerci fast_food'daki
# klasik burgerleri, pizzaci hamur_isleri'ndeki pizzalari da satabilsin.
# Desenler DAR tutulur: "asya"/"deniz" gibi tek basina yaygin Turkce isimler
# (Asya Kasap, Deniz Restaurant) yanlis pozitif uretip kasaba sushi yazdiriyordu.
# Kural: kelime ancak mutfagi TEK BASINA belirtiyorsa desende yer alir.
MUTFAK_KISITLARI: list[tuple[str, set[str]]] = [
    (r"cig ?kofte",                        {"cigkofte", "icecekler"}),
    (r"sushi|susi|wok|japon|ramen|noodle|teriyaki|uzak ?dogu|asya mutfa|cin mutfa|cin lokanta",
                                           {"uzakdogu", "icecekler"}),
    (r"pastane|tatlici|baklava|dondurma|waffle|kahve|coffee|cafe|kafe|patisserie",
                                           {"pastane_tatli", "tatlilar", "icecekler"}),
    (r"balik|deniz urunleri|deniz mahsul", {"balik", "ara_sicaklar_mezeler", "corbalar", "icecekler"}),
    (r"pizza",                             {"pizza", "hamur_isleri", "icecekler", "ara_sicaklar_mezeler"}),
    (r"burger",                            {"burger", "fast_food", "icecekler"}),
]

YEMEK_MENU_BOLUMLERI: dict[str, list[str]] = yemek_urunleri_bolumlu_yukle()


def mutfak_anahtari(satici_unvan: str) -> str | None:
    """Firma adinin isaret ettigi dar mutfagin KIMLIGI. Ayni anahtar = ayni menu
    kisiti. Gruplama icin kullanilir (bkz. anomaly_injector.fatura_no_tekrari_uygula:
    ayni fatura_no'yu paylasan cift ayni mutfaktan secilmeli, yoksa devralinan
    unvan ile kalemler celisir). Eslesme yoksa None."""
    ad = _ascii_kucuk(satici_unvan)
    for desen, _ in MUTFAK_KISITLARI:
        if re.search(desen, ad):
            return desen
    return None


def mutfak_havuzu_sec(satici_unvan: str) -> list[str] | None:
    """Firma adindan dar mutfak tespit eder ve o mutfaga uygun kalem havuzunu
    dondurur. Eslesme yoksa None -> kisit uygulanmaz (genis mutfaklar: kebap,
    doner, lokanta, ocakbasi, pide serbest kalir)."""
    if not YEMEK_MENU_BOLUMLERI:
        return None
    anahtar = mutfak_anahtari(satici_unvan)
    if anahtar is None:
        return None
    bolumler = next(b for d, b in MUTFAK_KISITLARI if d == anahtar)
    havuz = [u for b in bolumler for u in YEMEK_MENU_BOLUMLERI.get(b, [])]
    return havuz or None


def _sektor_kelime_sec(is_kolu: IsKolu, kalemler=None) -> str:
    """İş koluna uygun sektör kelimesini, fişteki kalemlerle TUTARLI olacak şekilde
    seçer. Hizmete özel kelimeler (Taksi vb.) yalnız ilgili kalem varsa aday olur;
    eşleşen özel kelime, jeneriklere göre daha yüksek olasılıkla seçilir."""
    adaylar = IS_KOLU_SEKTOR_KELIME[is_kolu]
    kalem_metni = _ascii_kucuk(" ".join(getattr(k, "aciklama", "") for k in (kalemler or [])))
    uygun: list[str] = []
    agirlik: list[int] = []
    for kelime in adaylar:
        kosul = SEKTOR_KELIME_KALEM_KOSULU.get(kelime)
        if kosul is None:
            uygun.append(kelime)       # jenerik -> her zaman uygun
            agirlik.append(1)
        elif kalem_metni and any(a in kalem_metni for a in kosul):
            uygun.append(kelime)       # özel ama fişte gerçekten var
            agirlik.append(3)          # tutarlı özel kelimeyi öne çıkar
    if not uygun:
        # Hiç uygun yok (ör. ulaşım firması ama fişte yalnız 'Uçak Bileti'):
        # koşullu olmayan (jenerik) kelimelere düş; o da yoksa ilk adaya.
        jenerikler = [k for k in adaylar if k not in SEKTOR_KELIME_KALEM_KOSULU]
        return random.choice(jenerikler) if jenerikler else adaylar[0]
    return random.choices(uygun, weights=agirlik, k=1)[0]

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

# faker Türkçe adlara akademik/mesleki unvan ekleyebiliyor ('Dr. İşcan Gülen',
# 'Doç. Zinnure Bilir Durdu', 'Arş. Gör. Nazi Hekime Zengin'). Bir MARKET'in
# unvanı "Doç. ..." olamaz. Çok parçalı unvanlar için TEKRARLI eşleşme (`+`).
# Ölçüldü (6000 ad): Öğr. Dr. Doç. Okt. Av. 'Arş. Gör.' 'Yrd. Doç.' Çev. Prof. Uz.
_UNVAN_ONEKLERI = re.compile(
    r"^(?:(?:Dr|Doç|Prof|Op|Av|Uz|Uzm|Yrd|Müh|Öğr|Gör|Arş|Okt|Çev)\.\s*)+",
    re.IGNORECASE,
)

# Şahıs şirketinin TİCARİ UNVAN kelimesi. IS_KOLU_SEKTOR_KELIME'den AYRI tutulur
# çünkü oradaki kelimeler bir hukuki ekle birlikte kullanılmak üzere seçilmiş
# ('Lezzet' + 'Gida San. ve Tic. Ltd. Şti.'); tek başına bırakıldığında işletme
# adı gibi okunmuyor ('Oğuzman Çetin Lezzet'). Buradakiler TEK BAŞINA bir dükkân
# adı tamamlar ('Oğuzman Çetin Lokantası').
SAHIS_TICARI_UNVAN = {
    IsKolu.RESTORAN: ["Lokantası", "Restoran", "Kebap Salonu", "Kafe"],
    IsKolu.MARKET: ["Market", "Bakkaliyesi", "Gıda", "Şarküteri"],
    IsKolu.OTEL: ["Oteli", "Pansiyonu", "Konukevi"],
    IsKolu.OFIS_TEDARIK: ["Kırtasiye", "Büro Malzemeleri", "Ofis Market"],
    IsKolu.TEKNOLOJI: ["Bilişim", "Bilgisayar", "Teknoloji"],
    IsKolu.DANISMANLIK_FIRMASI: ["Danışmanlık", "Müşavirlik", "Denetim"],
    IsKolu.LOJISTIK_FIRMASI: ["Nakliyat", "Kargo", "Lojistik"],
    IsKolu.ULASIM_SAGLAYICI: ["Turizm", "Taşımacılık", "Oto Kiralama"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Davet Evi"],
    IsKolu.GIYIM_MAGAZASI: ["Giyim", "Tekstil", "Konfeksiyon"],
    IsKolu.KISISEL_BAKIM: ["Kuaför", "Güzellik Salonu", "Kozmetik"],
}


def rastgele_firma_adi(is_kolu: IsKolu, firma_turu: FirmaTuru, kalemler=None) -> str:
    if firma_turu == FirmaTuru.SAHIS_SIRKETI:
        # ŞAHIS ŞİRKETİ = kişi adı + TİCARİ UNVAN (2026-07-29).
        #
        # Eskiden çıplak `fake.name()` dönüyordu ve fiş "Özertem Alemdar" diye
        # bir satıcı gösteriyordu. Faz B'de bu doğrudan saçma cümle üretiyordu:
        # "Özertem Alemdar'dan karides güveç aldım", "Dr. İşcan Gülen'den
        # deterjan" -- model adı bir İŞLETME değil bir KİŞİ gibi okuyor, çünkü
        # metinde işletmeye dair hiçbir iz yok. (Ölçüldü: registry'nin %15'i,
        # pilotun %12'si.)
        #
        # Gerçek hayatta şahıs şirketi faturası da bir ticari unvan taşır
        # ('İşcan Gülen Market'). Hukuki ek (A.Ş./Ltd. Şti.) EKLENMEZ -- o
        # tüzel kişiliğe aittir, şahıs şirketinde bulunmaz.
        kisi = _UNVAN_ONEKLERI.sub("", fake.name()).strip()
        unvan = random.choice(SAHIS_TICARI_UNVAN[is_kolu])
        return f"{kisi} {unvan}"

    # Sektör kelimesi fişteki kalemlerle tutarlı seçilir (ör. 'Taksi' yalnız
    # faturada taksi kalemi varsa). kalemler verilmezse jeneriklere düşülür.
    sektor_kelime = _sektor_kelime_sec(is_kolu, kalemler)
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
ALICI_VKN_SABIT = "6463595880"
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

TAM_SAYI_BIRIMLERI = {"Adet", "Kutu", "Kişi", "Gece", "Gün", "Lisans", "Şişe", "Kullanici"}

def rastgele_miktar(birim: str) -> float:
    if birim in TAM_SAYI_BIRIMLERI:
        return float(random.randint(1, 10))
    else:
        return round(random.uniform(0.5, 10), 2)

def rastgele_kalem(
    kalem_no: int,
    izinli_kategoriler: list[HarcamaKategorisi],
    kullanilan_aciklamalar: set[str],
    yemek_havuzu: list[str] | None = None,) -> FaturaKalemi:
    """`yemek_havuzu` verilirse YEMEK_HIZMETI kalemleri genel havuz yerine ondan
    seçilir (firma adına göre mutfak kısıtı; bkz. mutfak_havuzu_sec)."""
    kategori = random.choice(izinli_kategoriler)

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
        if kategori == HarcamaKategorisi.YEMEK_HIZMETI and yemek_havuzu:
            havuz = yemek_havuzu   # mutfak kısıtı: firma adına uygun bölümler
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

    # Aciklama artik belli -- CSV'den gelen urun-birim eslemesinde varsa
    # onu kullan (gercekci birim), yoksa eski davranisa (kategori bazli
    # rastgele birim) dus.
    birim = URUN_BIRIM_ESLEME.get(aciklama) or rastgele_birim(kategori)

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

# Gerçek hayatta bir taksi ya da akaryakıt fişinde başka kalem OLMAZ -- fiş
# tek kalemlidir. rastgele_fatura() bu açiklamalardan biri kalem olarak
# seçildiğinde, o kalem faturanin TEK kalemi olacak şekilde döngüyü keser.
TEKIL_ZORUNLU_ACIKLAMALAR = {"Taksi Ücreti", "Yakit Gideri"}


# İş kolu başına kalem sayısı TAVANI.
#
# NEDEN: kalem sayısı eskiden `randint(1, 8)` ile HER iş kolunda aynıydı --
# ölçüldü, 100k'da her sektörün ortalaması 4,56-4,60 ve faturaların ~%63'ü
# 4+ kalemliydi. Bu, hizmet sektörlerinde gerçek dışı fişler üretiyordu:
# tek fişte "Feribot Bileti + İstanbulkart + LPG + Metro + Uçak Bileti" ya da
# bir danışmanlık faturasında altı ayrı hizmet kalemi. Gerçek hayatta feribota
# binersin, fişi ayrıdır; akbil yüklersin, fişi ayrıdır.
#
# Faz B'ye ETKİSİ (asıl gerekçe): açıklama üretiminde modelin tek cümlede
# uzlaştırması gereken heterojen kalem sayısı düşer -- halüsinasyon yüzeyi ve
# uzunluk baskısı azalır. Pilotta gözlenen "araç kiraladım" (4 farklı ulaşım
# kalemi varken) tipi eksik/yanlış özetlemelerin kaynağı buydu.
#
# TEKIL_ZORUNLU_ACIKLAMALAR bu mekanizmanın KALEM düzeyindeki dar hâliydi
# (yalnız taksi/yakıt); burası iş kolu düzeyine genelleştirir, ikisi birlikte
# çalışır (tekil zorunlu kalem seçilirse tavan ne olursa olsun fatura 1 kaleme
# iner).
#
# Tavanı YÜKSEK kalan üçlü bilinçlidir: market sepeti, grup yemeği ve
# kırtasiye toplu alımı gerçekten çok kalemlidir. Otel folyosu da doğal olarak
# birkaç satırdır (konaklama + kahvaltı + geç çıkış), o yüzden orta seviyede.
IS_KOLU_KALEM_TAVANI: dict[IsKolu, int] = {
    IsKolu.ULASIM_SAGLAYICI: 1,      # taksi/feribot/akbil/yakıt: tek olay, tek fiş
    IsKolu.DANISMANLIK_FIRMASI: 2,   # bir sözleşme = bir hizmet kalemi
    IsKolu.LOJISTIK_FIRMASI: 2,      # tek sevkiyat/kargo
    IsKolu.TEKNOLOJI: 3,             # cihaz + lisans makul
    IsKolu.GIYIM_MAGAZASI: 3,
    IsKolu.KISISEL_BAKIM: 3,
    IsKolu.ORGANIZASYON: 4,
    IsKolu.OTEL: 5,                  # folyo doğal olarak çok satırlı
    IsKolu.RESTORAN: 8,              # grup yemeği
    IsKolu.MARKET: 8,                # market sepeti
    IsKolu.OFIS_TEDARIK: 8,          # kırtasiye toplu alım
}

VARSAYILAN_KALEM_TAVANI = 8


# --- Firma Registry (tek kalıcı firma-kimliği kaynağı) ---
# Firma kişiliği artık FATURA bazlı DEĞİL, FIRMA bazlıdır: ad + kimlik no +
# is_kolu + firma_türü tek bir registry'de (data/firma_registry.csv) DONAR ve
# fatura üretimi bu registry'den bir firma SEÇER (icat etmez). Bu sayede aynı
# ad→hep aynı VKN, aynı VKN→hep aynı ad (tutarsızlık yapısal olarak imkânsız).
# Üretimi: firma_registry_olustur.py.
FIRMA_REGISTRY_CSV = Path(__file__).parent.parent / "data" / "firma_registry.csv"

# LAZY yükleme: import anında DEĞİL, ilk kullanımda bir kez okunur ve cache'lenir.
# (firma_registry_olustur.py bu modülü import ettiği için, CSV henüz YOKken
# import-anı yükleme tavuk-yumurta kilidine yol açardı.)
_FIRMA_REGISTRY: dict[IsKolu, list[dict]] | None = None


def firma_registry_yukle() -> dict[IsKolu, list[dict]]:
    """CSV'yi is_kolu -> [{'unvan':..., 'kimlik':...}, ...] olarak yükler."""
    import csv as _csv
    if not FIRMA_REGISTRY_CSV.exists():
        raise FileNotFoundError(
            f"Firma registry bulunamadı: {FIRMA_REGISTRY_CSV}\n"
            f"Önce registry'yi üret: python firma_registry_olustur.py"
        )
    gruplar: dict[IsKolu, list[dict]] = {}
    with open(FIRMA_REGISTRY_CSV, encoding="utf-8") as f:
        for satir in _csv.DictReader(f):
            is_kolu = IsKolu(satir["is_kolu"])
            gruplar.setdefault(is_kolu, []).append({
                "unvan": satir["satici_unvan"],
                "kimlik": satir["satici_kimlik"],
            })
    return gruplar


def registry_firma_sec(is_kolu: IsKolu) -> dict:
    """Verilen iş kolundaki registry firmalarından birini rastgele seçer."""
    global _FIRMA_REGISTRY
    if _FIRMA_REGISTRY is None:
        _FIRMA_REGISTRY = firma_registry_yukle()
    firmalar = _FIRMA_REGISTRY.get(is_kolu)
    if not firmalar:
        raise ValueError(f"Registry'de '{is_kolu.value}' iş kolu için firma yok.")
    return random.choice(firmalar)


def rastgele_fatura() -> Fatura:
    # 1. adim: iş kolunu (fatura dağılımını) ağırlıklı seç -- mevcut dağılım korunur.
    is_kolu = random.choices(
        list(IS_KOLU_AGIRLIKLARI.keys()),
        weights=list(IS_KOLU_AGIRLIKLARI.values()),
        k=1,
    )[0]
    izinli_kategoriler = IS_KOLU_KATEGORILERI[is_kolu]

    # 2. adim: firma KİMLİĞİNİ registry'den SEÇ (ad + kimlik no sabit, fatura başına
    # üretilmez). Sektör-kelime/geri-okuma mekanizması emekli oldu; is_kolu artık
    # Fatura'da açıkça saklanıyor.
    firma = registry_firma_sec(is_kolu)
    satici_kimlik = firma["kimlik"]
    satici_adi = firma["unvan"]

    # 2b. Mutfak kısıtı: firma adı dar bir mutfağı işaret ediyorsa (çiğköfteci,
    # balıkçı, pizzacı...) yemek kalemleri o mutfağın menüsünden seçilir.
    # Geniş mutfaklarda (kebap/lokanta/ocakbaşı) None döner, kısıt uygulanmaz.
    yemek_havuzu = mutfak_havuzu_sec(satici_adi)

    fatura_tarihi = rastgele_tarih()
    fatura_no = rastgele_fatura_no(fatura_tarihi)

    # Bu iş kolunda toplam kaç benzersiz açiklama üretilebilir? artık csv dosyalarından verileri çekildiği için bu gereksiz olabilir
    toplam_musait_aciklama = sum(
        len(ACIKLAMA_HAVUZU[kategori]) for kategori in izinli_kategoriler
    )

    # Kalem sayisi: iş koluna göre TAVAN (bkz. IS_KOLU_KALEM_TAVANI gerekçesi)
    # ve havuz çeşitliliği -- ikisinden küçüğü.
    is_kolu_tavani = IS_KOLU_KALEM_TAVANI.get(is_kolu, VARSAYILAN_KALEM_TAVANI)
    ust_sinir = min(is_kolu_tavani, toplam_musait_aciklama)
    kalem_sayisi = random.randint(1, max(1, ust_sinir))

    kullanilan_aciklamalar: set[str] = set()
    kalemler: list[FaturaKalemi] = []
    for i in range(kalem_sayisi):
        kalem = rastgele_kalem(i + 1, izinli_kategoriler, kullanilan_aciklamalar, yemek_havuzu)
        if kalem.aciklama in TEKIL_ZORUNLU_ACIKLAMALAR:
            # Bu kalem seçildiği an, öncekiler dahil hepsini atip faturayi
            # TEK kalemli yapiyoruz (taksi/yakit fişi başka kalemle gelmez).
            kalemler = [kalem.model_copy(update={"kalem_no": 1})]
            break
        kalemler.append(kalem)

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