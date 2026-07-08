from decimal import Decimal
from datetime import date
from schema import Fatura, FaturaKalemi, HarcamaKategorisi,KDV_ORANI_MAP





# 1. Kimlik No Doğrulaması (VKN / TCKN)

def vkn_checksum_dogrula(vkn: str) -> bool:
    """10 haneli VKN'nin checksum algoritmasına uygunluğunu doğrular."""
    if len(vkn) != 10 or not vkn.isdigit():
        return False

    digits = [int(d) for d in vkn[:9]]
    total = 0
    for i in range(9):
        d = (digits[i] + (9 - i)) % 10
        if d != 0:
            d = (d * (2 ** (9 - i))) % 9
            if d == 0:
                d = 9
        total += d

    beklenen_check = (10 - (total % 10)) % 10
    return beklenen_check == int(vkn[9])


def tckn_checksum_dogrula(tckn: str) -> bool:
    """11 haneli TCKN'nin checksum algoritmasına uygunluğunu doğrular."""
    if len(tckn) != 11 or not tckn.isdigit():
        return False
    if tckn[0] == "0":
        return False

    digits = [int(d) for d in tckn[:9]]
    tek_toplam = sum(digits[0:9:2])
    cift_toplam = sum(digits[1:8:2])

    onuncu_beklenen = ((tek_toplam * 7) - cift_toplam) % 10
    on_birinci_beklenen = sum(digits + [onuncu_beklenen]) % 10

    return int(tckn[9]) == onuncu_beklenen and int(tckn[10]) == on_birinci_beklenen


def kimlik_no_dogrula(kimlik_no: str) -> bool:
    """Hane sayısına göre VKN ya da TCKN algoritmasını seçip doğrular."""
    if len(kimlik_no) == 10:
        return vkn_checksum_dogrula(kimlik_no)
    elif len(kimlik_no) == 11:
        return tckn_checksum_dogrula(kimlik_no)
    return False


# 2. Matematiksel Tutarlılık

def kalem_ara_toplam_dogrula(kalem: FaturaKalemi) -> bool:
    """ara_toplam = (miktar * birim_fiyat) * (1 - iskonto/100) doğru mu?"""
    beklenen = (
        Decimal(str(kalem.miktar)) * kalem.birim_fiyat
        * (Decimal("1") - Decimal(str(kalem.iskonto_orani)) / Decimal("100"))
    )
    return abs(kalem.ara_toplam - beklenen) < Decimal("0.01")


def kalem_satir_toplam_dogrula(kalem: FaturaKalemi) -> bool:
    """satir_toplam = ara_toplam + kdv_tutari doğru mu?"""
    beklenen = kalem.ara_toplam + kalem.kdv_tutari
    return abs(kalem.satir_toplam - beklenen) < Decimal("0.01")


def fatura_genel_toplam_dogrula(fatura: Fatura) -> bool:
    """genel_toplam = tüm kalemlerin satir_toplam toplamı mı?"""
    beklenen = sum((k.satir_toplam for k in fatura.kalemler), Decimal("0"))
    return abs(fatura.genel_toplam - beklenen) < Decimal("0.01")


#3. Fatura No Tekrar Kontrolü (Hepsi unique olmalı)

def fatura_no_tekrarlarini_bul(faturalar: list[Fatura]) -> list[str]:
    """Birden fazla kez geçen fatura no'ları döndürür (boşsa hiç tekrar yok demektir)."""
    gorulen: dict[str, int] = {}
    for fatura in faturalar:
        gorulen[fatura.fatura_no] = gorulen.get(fatura.fatura_no, 0) + 1
    return [no for no, sayi in gorulen.items() if sayi > 1]


#3. Tarih Kontrolü (Gelecek tarihli fatura olmamalı)
def tarih_gelecekte_mi(fatura_tarihi_str: str, bugun_str: str) -> bool:
    """Fatura tarihi bugünden ileri bir tarihse True döner (hata durumu)."""
    return fatura_tarihi_str > bugun_str  # ISO format (YYYY-MM-DD) string karşılaştırması güvenlidir


#4. KDV Oranı Kontrolü (Kategoriye göre sabit KDV oranı)
def kategori_kdv_dogrula(kalem: FaturaKalemi) -> bool:
    """Kalemin KDV oranı, kategorisi için tanımlı sabit orana eşit mi?"""
    beklenen_kdv = KDV_ORANI_MAP.get(kalem.harcama_kategorisi)
    return beklenen_kdv is not None and kalem.kdv_orani == beklenen_kdv

def vkn_firma_tutarlilik_hatalarini_bul(faturalar: list[Fatura]) -> dict:
    """
    Aynı VKN'nin farklı firma adlarıyla, ya da aynı firma adının
    farklı VKN'lerle eşleştiği durumları tespit eder.
    """
    vkn_to_adlar: dict[str, set[str]] = {}
    ad_to_vknler: dict[str, set[str]] = {}

    for fatura in faturalar:
        vkn_to_adlar.setdefault(fatura.satici_vkn, set()).add(fatura.satici_unvan)
        ad_to_vknler.setdefault(fatura.satici_unvan, set()).add(fatura.satici_vkn)

    celiskili_vkn = {vkn: adlar for vkn, adlar in vkn_to_adlar.items() if len(adlar) > 1}
    celiskili_ad = {ad: vknler for ad, vknler in ad_to_vknler.items() if len(vknler) > 1}

    return {
        "ayni_vkn_farkli_ad": celiskili_vkn,
        "ayni_ad_farkli_vkn": celiskili_ad,
    }


def fatura_dogrula(fatura: Fatura, bugun_str: str) -> list[str]:
    """
    Tek bir faturayı tüm kurallara göre kontrol eder.
    Dönüş: ihlal edilen kuralların isim listesi (boşsa fatura tamamen geçerli demektir).
    """
    hatalar: list[str] = []

    if not kimlik_no_dogrula(fatura.satici_vkn):
        hatalar.append(f"Geçersiz satıcı kimlik no: {fatura.satici_vkn}")

    if not kimlik_no_dogrula(fatura.alici_vkn):
        hatalar.append(f"Geçersiz alıcı kimlik no: {fatura.alici_vkn}")

    if tarih_gelecekte_mi(fatura.fatura_tarihi, bugun_str):
        hatalar.append(f"Gelecek tarihli fatura: {fatura.fatura_tarihi}")

    if not fatura_genel_toplam_dogrula(fatura):
        hatalar.append("Genel toplam, kalemlerin toplamıyla uyuşmuyor")

    for kalem in fatura.kalemler:
        if not kalem_ara_toplam_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: ara_toplam hesabı hatalı")
        if not kalem_satir_toplam_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: satir_toplam hesabı hatalı")
        if not kategori_kdv_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: KDV oranı kategoriyle uyuşmuyor")

    return hatalar


def dogrulama_raporu_olustur(faturalar: list[Fatura]) -> dict:
    bugun_str = date.today().isoformat()

    tekrar_eden_no = set(fatura_no_tekrarlarini_bul(faturalar))   # önce hesapla
    vkn_firma_hatalari = vkn_firma_tutarlilik_hatalarini_bul(faturalar)
    ayni_vkn_farkli_ad = vkn_firma_hatalari["ayni_vkn_farkli_ad"] 

    fatura_hatalari: dict[int, dict] = {}   # artık indeks bazlı önceden fatura no key di tekrari halinde eski fatura nonun üstüne yazılacaktı, bu yüzden dict[int, dict] tipinde
    for i, fatura in enumerate(faturalar):
        hatalar = fatura_dogrula(fatura, bugun_str)
        if fatura.fatura_no in tekrar_eden_no:
            hatalar.append(f"Fatura no birden fazla kez kullanılmış: {fatura.fatura_no}")
        
        if fatura.satici_vkn in ayni_vkn_farkli_ad:   # sadece VKN çelişkisi geçersizlik sayılır
            hatalar.append(f"Satıcı VKN'si farklı unvanlarla eşleşmiş: {fatura.satici_vkn}")
            
        if hatalar:
            fatura_hatalari[i] = {"fatura_no": fatura.fatura_no, "hatalar": hatalar}

    return {
        "toplam_fatura": len(faturalar),
        "hatali_fatura_sayisi": len(fatura_hatalari),
        "gecerli_fatura_sayisi": len(faturalar) - len(fatura_hatalari),
        "fatura_no_tekrarlari": list(tekrar_eden_no),
        "vkn_firma_tutarsizliklari": vkn_firma_hatalari,
        "hata_detaylari": fatura_hatalari,
    }


def raporu_yazdir(rapor: dict) -> None:
    print("=" * 60)
    print("  DOĞRULAMA RAPORU")
    print("=" * 60)
    print(f"  Toplam Fatura       : {rapor['toplam_fatura']}")
    print(f"  Geçerli Fatura      : {rapor['gecerli_fatura_sayisi']}")
    print(f"  Hatalı Fatura       : {rapor['hatali_fatura_sayisi']}")
    print(f"  Tekrar Eden Fatura No: {len(rapor['fatura_no_tekrarlari'])}")

    if rapor["fatura_no_tekrarlari"]:
        print(f"\n  Tekrar eden no'lar: {rapor['fatura_no_tekrarlari']}")


    vkn_firma = rapor.get("vkn_firma_tutarsizliklari", {})
    ayni_vkn_farkli_ad = vkn_firma.get("ayni_vkn_farkli_ad", {})
    ayni_ad_farkli_vkn = vkn_firma.get("ayni_ad_farkli_vkn", {})

    print(f"\n  Aynı VKN/Farklı Ad Sayısı : {len(ayni_vkn_farkli_ad)}")
    print(f"  Aynı Ad/Farklı VKN Sayısı : {len(ayni_ad_farkli_vkn)}")

    if ayni_vkn_farkli_ad:
        print("\n  ⚠️  Aynı VKN farklı firma adı ile eşleşmiş (daha sıkıntı)")
        for vkn, adlar in list(ayni_vkn_farkli_ad.items())[:5]:
            print(f"    VKN {vkn}: {adlar}")

    if ayni_ad_farkli_vkn:
        print("\n  Aynı firma adı farklı VKN ile üretilmiş (isim havuzu çakışması, düşük öncelikli):")
        for ad, vknler in list(ayni_ad_farkli_vkn.items())[:5]:  #aynı ada sahip farklı vknleri yazdır, 5 ten fazlasını yazdırma.
            print(f"    {ad}: {vknler}")
   

    if rapor["hata_detaylari"]:
        print("\n  İlk 5 hatalı faturanın detayı:")
        for i, (indeks, detay) in enumerate(rapor["hata_detaylari"].items()):
            if i >= 5:
                break
            print(f"\n    [{indeks}] Fatura No: {detay['fatura_no']}")
            for hata in detay["hatalar"]:
                print(f"      - {hata}")

    print("=" * 60)