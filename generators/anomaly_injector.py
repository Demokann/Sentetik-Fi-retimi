import random
from datetime import date, timedelta
from decimal import Decimal

from schema import (
    Fatura, FaturaKalemi, HarcamaKategorisi,
    IS_KOLU_KATEGORILERI, KDV_ORANI_MAP,
    POLICY_YASAKLI_KATEGORILER, POLICY_TUTAR_LIMITLERI,
)
from generators.field_generator import (
    ACIKLAMA_HAVUZU, rastgele_birim, rastgele_miktar, rastgele_birim_fiyat,rastgele_fatura
)



# 1. Gelecek Tarihli Fatura

def gelecek_tarihli_anomali_uret(fatura: Fatura) -> Fatura:
    """Fatura tarihini kasitli olarak gelecekteki bir tarihe çeker."""
    gelecek_tarih = (date.today() + timedelta(days=random.randint(30, 365))).isoformat()
    fatura.fatura_tarihi = gelecek_tarih
    return fatura



# 2. Geçersiz Kimlik No

def gecersiz_kimlik_no_anomali_uret(fatura: Fatura) -> Fatura:
    """Satici kimlik numarasini checksum'i tutmayan rastgele bir sayiyla değiştirir."""
    hane_sayisi = len(fatura.satici_vkn)
    yanlis_kimlik = "".join(str(random.randint(0, 9)) for _ in range(hane_sayisi))
    fatura.satici_vkn = yanlis_kimlik
    return fatura



# 3. KDV-Kategori Uyumsuzluğu

def kdv_kategori_uyumsuzlugu_anomali_uret(fatura: Fatura) -> Fatura:
    """Rastgele bir kalemin KDV oranini, kategorisi için doğru olmayan bir oranla değiştirir."""
    kalem = random.choice(fatura.kalemler)
    dogru_oran = KDV_ORANI_MAP[kalem.harcama_kategorisi]
    olasi_yanlis_oranlar = [o for o in {1.0, 10.0, 20.0} if o != dogru_oran]
    kalem.kdv_orani = random.choice(olasi_yanlis_oranlar)
    return fatura



# 4. İş Kolu - Kategori Uyumsuzluğu

def is_kolu_kategori_uyumsuzlugu_anomali_uret(fatura: Fatura) -> Fatura:
    """Faturaya, mevcut kalemlerin kategorisiyle hiç ilgisi olmayan bir kalem ekler."""
    mevcut_kategoriler = {k.harcama_kategorisi for k in fatura.kalemler}
    tum_kategoriler = set(HarcamaKategorisi)
    yabanci_kategoriler = list(tum_kategoriler - mevcut_kategoriler - POLICY_YASAKLI_KATEGORILER)
    yabanci_kategori = random.choice(yabanci_kategoriler)

    yeni_kalem_no = len(fatura.kalemler) + 1
    birim = rastgele_birim(yabanci_kategori)
    yeni_kalem = FaturaKalemi(
        kalem_no=yeni_kalem_no,
        aciklama=random.choice(ACIKLAMA_HAVUZU[yabanci_kategori]),
        harcama_kategorisi=yabanci_kategori,
        miktar=rastgele_miktar(birim),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(yabanci_kategori, birim),
        iskonto_orani=0.0,
        kdv_orani=KDV_ORANI_MAP[yabanci_kategori],
    )
    fatura.kalemler.append(yeni_kalem)
    return fatura



# 5. Politika İhlali — Yasakli Kategori

def yasakli_kategori_anomali_uret(fatura: Fatura) -> Fatura:
    """Faturaya, kategori ne olursa olsun her zaman reddedilen bir kalem ekler."""
    yasakli_kategori = random.choice(list(POLICY_YASAKLI_KATEGORILER))

    yeni_kalem_no = len(fatura.kalemler) + 1
    birim = rastgele_birim(yasakli_kategori)
    yeni_kalem = FaturaKalemi(
        kalem_no=yeni_kalem_no,
        aciklama=random.choice(ACIKLAMA_HAVUZU[yasakli_kategori]),
        harcama_kategorisi=yasakli_kategori,
        miktar=rastgele_miktar(birim),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(yasakli_kategori, birim),
        iskonto_orani=0.0,
        kdv_orani=KDV_ORANI_MAP[yasakli_kategori],
    )
    fatura.kalemler.append(yeni_kalem)
    return fatura



# 6. Politika İhlali — Limit Aşimi

def limit_asimi_anomali_uret(fatura: Fatura) -> Fatura:
    """Limitli bir kategorideki kalemin birim fiyatini, limitin üstüne kasitli olarak çeker."""
    limitli_kalemler = [
        k for k in fatura.kalemler if k.harcama_kategorisi in POLICY_TUTAR_LIMITLERI
    ]

    if not limitli_kalemler:
        kalem = fatura.kalemler[0]
        kalem.birim_fiyat = kalem.birim_fiyat * Decimal("10")
        return fatura

    kalem = random.choice(limitli_kalemler)
    limit = POLICY_TUTAR_LIMITLERI[kalem.harcama_kategorisi]
    kalem.birim_fiyat = Decimal(str(limit * random.uniform(1.5, 3.0)))
    return fatura



# 7. Fatura No Tekrari (İki Fatura Arasi — Farkli İmza, Ayri Kategori)

def fatura_no_tekrari_anomali_uret(fatura: Fatura, diger_fatura_no: str) -> Fatura:
    """
    Faturanin no'sunu, başka (var olan) bir faturanin no'suyla kasitli
    olarak çakiştirir. DİKKAT: Gerçek bir anomali üretmesi için bu
    fonksiyon, ayni satici_vkn'e sahip iki fatura arasinda kullanilmali —
    farkli VKN'li faturalarin ayni no'yu paylaşmasi artik anomali
    sayilmiyor (validators.py, fatura_no_tekrarlarini_bul).
    """
    fatura.fatura_no = diger_fatura_no
    return fatura


from schema import AnomaliliFaturaKalemi, AnomaliliFatura

def satir_toplami_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin satir_toplam'ini kasitli olarak saptirir.
    model_dump() yerine alanlar doğrudan geçirilir; böylece kalem zaten
    başka bir anomaliyle (ör. sahte_ara_toplam) işaretlenmişse o bilgi
    kaybolmaz.
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    gercek_toplam = orijinal_kalem.satir_toplam
    carpan = random.choice([
        random.uniform(1.2, 2.0),   # gerçekten fazla gösterilmiş
        random.uniform(0.3, 0.7),   # gerçekten az gösterilmiş
    ])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(
        kalem_no=orijinal_kalem.kalem_no,
        aciklama=orijinal_kalem.aciklama,
        harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
        miktar=orijinal_kalem.miktar,
        birim=orijinal_kalem.birim,
        birim_fiyat=orijinal_kalem.birim_fiyat,
        iskonto_orani=orijinal_kalem.iskonto_orani,
        kdv_orani=orijinal_kalem.kdv_orani,
        sahte_ara_toplam=getattr(orijinal_kalem, "sahte_ara_toplam", None),
        sahte_satir_toplam=sahte_toplam,
    )
    fatura.kalemler[hedef_index] = anomalili_kalem
    return fatura


def ara_toplam_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin ara_toplam'ini kasitli olarak saptirir. KDV ve
    satir_toplam bu sahte ara_toplam üzerinden tutarli hesaplanmaya devam
    eder (satir_toplam anomalisiyle çakişmadiği sürece).
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    gercek_ara_toplam = orijinal_kalem.ara_toplam
    carpan = random.choice([
        random.uniform(1.15, 1.8),
        random.uniform(0.4, 0.8),
    ])
    sahte_ara_toplam = Decimal(str(round(float(gercek_ara_toplam) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(
        kalem_no=orijinal_kalem.kalem_no,
        aciklama=orijinal_kalem.aciklama,
        harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
        miktar=orijinal_kalem.miktar,
        birim=orijinal_kalem.birim,
        birim_fiyat=orijinal_kalem.birim_fiyat,
        iskonto_orani=orijinal_kalem.iskonto_orani,
        kdv_orani=orijinal_kalem.kdv_orani,
        sahte_ara_toplam=sahte_ara_toplam,
        sahte_satir_toplam=getattr(orijinal_kalem, "sahte_satir_toplam", None),
    )
    fatura.kalemler[hedef_index] = anomalili_kalem
    return fatura


def genel_toplam_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Faturanin genel_toplam'ini kasitli olarak saptirir. kalemler listesi
    dump edilmeden doğrudan geçirilir; bu sayede kalemlerdeki mevcut
    anomaliler (AnomaliliFaturaKalemi tipleri) korunur.
    """
    gercek_toplam = fatura.genel_toplam
    carpan = random.choice([random.uniform(1.1, 1.5), random.uniform(0.5, 0.9)])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    return AnomaliliFatura(
        fatura_no=fatura.fatura_no,
        fatura_tarihi=fatura.fatura_tarihi,
        satici_vkn=fatura.satici_vkn,
        satici_unvan=fatura.satici_unvan,
        alici_vkn=fatura.alici_vkn,
        alici_unvan=fatura.alici_unvan,
        kalemler=fatura.kalemler,
        sahte_toplam_vergisiz_tutar=getattr(fatura, "sahte_toplam_vergisiz_tutar", None),
        sahte_toplam_kdv_tutari=getattr(fatura, "sahte_toplam_kdv_tutari", None),
        sahte_genel_toplam=sahte_toplam,
    )


def footer_kismi_anomali_uret(fatura: Fatura) -> Fatura:
    """
    toplam_vergisiz_tutar veya toplam_kdv_tutari'nden SADECE birini
    kasitli olarak saptirir; genel_toplam kalemlerin gerçek toplamindan
    hesaplanmaya devam eder (zaten sahte_genel_toplam ayarlanmadiysa).
    """
    hangi_alan = random.choice(["vergisiz", "kdv"])
    carpan = random.choice([random.uniform(1.1, 1.4), random.uniform(0.6, 0.9)])

    mevcut_sahte_genel_toplam = getattr(fatura, "sahte_genel_toplam", None)
    mevcut_sahte_vergisiz = getattr(fatura, "sahte_toplam_vergisiz_tutar", None)
    mevcut_sahte_kdv = getattr(fatura, "sahte_toplam_kdv_tutari", None)

    sahte_vergisiz = mevcut_sahte_vergisiz
    sahte_kdv = mevcut_sahte_kdv

    if hangi_alan == "vergisiz":
        gercek = fatura.toplam_vergisiz_tutar
        sahte_vergisiz = Decimal(str(round(float(gercek) * carpan, 2)))
    else:
        gercek = fatura.toplam_kdv_tutari
        sahte_kdv = Decimal(str(round(float(gercek) * carpan, 2)))

    return AnomaliliFatura(
        fatura_no=fatura.fatura_no,
        fatura_tarihi=fatura.fatura_tarihi,
        satici_vkn=fatura.satici_vkn,
        satici_unvan=fatura.satici_unvan,
        alici_vkn=fatura.alici_vkn,
        alici_unvan=fatura.alici_unvan,
        kalemler=fatura.kalemler,
        sahte_genel_toplam=mevcut_sahte_genel_toplam,
        sahte_toplam_vergisiz_tutar=sahte_vergisiz,
        sahte_toplam_kdv_tutari=sahte_kdv,
    )

def kdv_tutari_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin kdv_tutari'ni, ara_toplam*kdv_orani/100 formülünden
    kasitli olarak saptirir. kdv_orani DOĞRU kalir (kategoriyle uyumlu) —
    yani kategori_kdv_dogrula geçer ama kalem_kdv_tutari_dogrula patlar.
    satir_toplam (fake edilmediği sürece) bu sahte kdv_tutari üzerinden
    tutarli hesaplanir.
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    gercek_kdv_tutari = orijinal_kalem.kdv_tutari
    carpan = random.choice([
        random.uniform(1.3, 2.5),   # olmasi gerekenden çok fazla KDV
        random.uniform(0.1, 0.6),   # olmasi gerekenden az KDV (kaçirma şüphesi)
    ])
    sahte_kdv_tutari = Decimal(str(round(float(gercek_kdv_tutari) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(
        kalem_no=orijinal_kalem.kalem_no,
        aciklama=orijinal_kalem.aciklama,
        harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
        miktar=orijinal_kalem.miktar,
        birim=orijinal_kalem.birim,
        birim_fiyat=orijinal_kalem.birim_fiyat,
        iskonto_orani=orijinal_kalem.iskonto_orani,
        kdv_orani=orijinal_kalem.kdv_orani,
        sahte_ara_toplam=getattr(orijinal_kalem, "sahte_ara_toplam", None),
        sahte_kdv_tutari=sahte_kdv_tutari,
        sahte_satir_toplam=getattr(orijinal_kalem, "sahte_satir_toplam", None),
    )
    fatura.kalemler[hedef_index] = anomalili_kalem
    return fatura


def sistematik_yuvarlama_anomali_uret(fatura: Fatura) -> Fatura:
    """
    'Skimming' deseni: faturadaki BİRDEN FAZLA kalemin satir_toplam'indan
    ayni yönde, küçük (kuruş seviyesinde) tutarlar kirpar. Tek kalemde
    büyük sapma yerine, her kalemde fark edilmesi zor küçük sapmalar
    birikir — gerçek hayattaki sistematik hile / yuvarlama suistimalini
    simüle eder.
    """
    if len(fatura.kalemler) < 2:
        # tek kalemli faturada "sistematik" (çoklu) sapma anlamsiz, atla
        return fatura

    etkilenecek_sayisi = random.randint(2, len(fatura.kalemler))
    hedef_indexler = random.sample(range(len(fatura.kalemler)), etkilenecek_sayisi)

    # tüm kalemlerde ayni yönde kirpma (hep az gösterme ya da hep fazla gösterme)
    yon = random.choice([Decimal("0.97"), Decimal("0.98"), Decimal("1.02"), Decimal("1.03")])

    for idx in hedef_indexler:
        orijinal_kalem = fatura.kalemler[idx]
        gercek_satir_toplam = orijinal_kalem.satir_toplam
        # kuruş seviyesinde küçük sapma + hafif rastgelelik
        carpan = yon + Decimal(str(round(random.uniform(-0.005, 0.005), 4)))
        sahte_satir_toplam = Decimal(str(round(float(gercek_satir_toplam) * float(carpan), 2)))

        anomalili_kalem = AnomaliliFaturaKalemi(
            kalem_no=orijinal_kalem.kalem_no,
            aciklama=orijinal_kalem.aciklama,
            harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
            miktar=orijinal_kalem.miktar,
            birim=orijinal_kalem.birim,
            birim_fiyat=orijinal_kalem.birim_fiyat,
            iskonto_orani=orijinal_kalem.iskonto_orani,
            kdv_orani=orijinal_kalem.kdv_orani,
            sahte_ara_toplam=getattr(orijinal_kalem, "sahte_ara_toplam", None),
            sahte_kdv_tutari=getattr(orijinal_kalem, "sahte_kdv_tutari", None),
            sahte_satir_toplam=sahte_satir_toplam,
        )
        fatura.kalemler[idx] = anomalili_kalem

    return fatura


def ondalik_kaymasi_anomali_uret(fatura: Fatura) -> Fatura:  #Tespiti ile ilgili validator.py'de ayrıca bir iş kuralı yazılmalı
    """
    Veri girişi hatasi (fat-finger) simülasyonu: rastgele bir kalemin
    birim_fiyat'ini GERÇEKTEN 10x ya da 100x kaydirir. Bu, sahte_* alanlari
    kullanan diğer anomalilerden farkli — burada 'gerçek' birim_fiyat
    değişiyor, dolayisiyla ara_toplam/kdv_tutari/satir_toplam hep kendi
    içinde matematiksel olarak TUTARLI kalir (validators.py'deki hesap
    kontrollerinin hiçbirini tetiklemez). Yakalanmasi gereken yer, işin
    doğasi gereği fahiş/anormal birim fiyat tespiti (ayri bir iş kurali/
    istatistiksel anomali tespiti gerektirir, matematiksel doğrulama değil).
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    kalem = fatura.kalemler[hedef_index]

    kayma_carpani = random.choice([Decimal("10"), Decimal("100")])
    kalem.birim_fiyat = kalem.birim_fiyat * kayma_carpani

    return fatura

ANOMALI_FONKSIYONLARI = {
    "gelecek_tarihli": gelecek_tarihli_anomali_uret,
    "gecersiz_kimlik_no": gecersiz_kimlik_no_anomali_uret,
    "kdv_kategori_uyumsuzlugu": kdv_kategori_uyumsuzlugu_anomali_uret,
    "is_kolu_kategori_uyumsuzlugu": is_kolu_kategori_uyumsuzlugu_anomali_uret,
    "yasakli_kategori": yasakli_kategori_anomali_uret,
    "limit_asimi": limit_asimi_anomali_uret,
    "ara_toplam": ara_toplam_anomali_uret,
    "kdv_tutari": kdv_tutari_anomali_uret,
    "satir_toplami": satir_toplami_anomali_uret,
    "sistematik_yuvarlama": sistematik_yuvarlama_anomali_uret,
    "ondalik_kaymasi": ondalik_kaymasi_anomali_uret,
    "genel_toplam": genel_toplam_anomali_uret,
    "footer_kismi": footer_kismi_anomali_uret,
    # fatura_no_tekrari burada YOK: iki fatura arasinda çalişiyor, ayri ele aliniyor
}


def fatura_no_tekrari_uygula(faturalar: list[Fatura]) -> None:
    """
    fatura_no_tekrari anomalisi, tek fatura yerine İKİ fatura arasinda
    çalişir. Validator'in (fatura_no_tekrarlarini_bul) bunu gerçek bir
    anomali sayabilmesi için iki faturanin satici_vkn'i AYNI olmali —
    farkli VKN'li faturalarin ayni no'yu paylaşmasi artik anomali
    sayilmiyor (bkz. validators.py). Bu yüzden ikinci faturanin VKN'si
    kasitli olarak birinciyle eşitleniyor, sonra no çakiştiriliyor.
    Liste yerinde (in-place) değiştirilir, dönüş değeri yok.
    """
    if len(faturalar) < 2:
        return

    idx1, idx2 = random.sample(range(len(faturalar)), 2)
    f1, f2 = faturalar[idx1], faturalar[idx2]

    f2.satici_vkn = f1.satici_vkn  # anomalinin gerçek sayilmasi için şart!
    faturalar[idx2] = fatura_no_tekrari_anomali_uret(f2, f1.fatura_no)
    faturalar[idx2].is_anomali = True
    faturalar[idx2].anomali_turleri = faturalar[idx2].anomali_turleri + ["fatura_no_tekrari"]


def karisik_veri_seti_uret(adet: int, anomali_orani: float = 0.2) -> list[Fatura]:
    """
    `adet` kadar fatura üretir, bunlarin `anomali_orani` kadarina rastgele
    bir anomali uygulayip etiketler (is_anomali, anomali_turleri). Etiket
    bilgisi sadece Pydantic modelinde tutulur; JSON export'a (fatura_to_dict)
    dahil edilmez — model bu alanlari GÖRMEMELİ, sadece değerlendirme
    (ground truth) amaçli kullanilmali.
    """
    faturalar = [rastgele_fatura() for _ in range(adet)]

    anomali_sayisi = int(adet * anomali_orani)
    anomalili_indexler = random.sample(range(adet), min(anomali_sayisi, adet))

    fonksiyon_havuzu = list(ANOMALI_FONKSIYONLARI.items())

    for idx in anomalili_indexler:
        isim, fonksiyon = random.choice(fonksiyon_havuzu)
        fatura = fonksiyon(faturalar[idx])
        fatura.is_anomali = True
        fatura.anomali_turleri = fatura.anomali_turleri + [isim]
        faturalar[idx] = fatura

    fatura_no_tekrar_oranı = 0.02  # tüm faturalarin %5'inde fatura_no_tekrari anomalisi

    # fatura_no_tekrari havuzda olmadigi için ayrica, düşük bir olasilikla uygula
    if anomali_orani > 0 and random.random() < fatura_no_tekrar_oranı:
        fatura_no_tekrari_uygula(faturalar)

    return faturalar

if __name__ == "__main__":
    from generators.field_generator import rastgele_fatura
    from validators import (
        kalem_ara_toplam_dogrula, kalem_kdv_tutari_dogrula,
        kalem_satir_toplam_dogrula, fatura_footer_tutarlilik_dogrula,
    )

    testler = [
        ("ara_toplam", ara_toplam_anomali_uret),
        ("kdv_tutari", kdv_tutari_anomali_uret),
        ("satir_toplami", satir_toplami_anomali_uret),
        ("sistematik_yuvarlama", sistematik_yuvarlama_anomali_uret),
        ("ondalik_kaymasi", ondalik_kaymasi_anomali_uret),
        ("genel_toplam", genel_toplam_anomali_uret),
        ("footer_kismi", footer_kismi_anomali_uret),
    ]

    for isim, fonksiyon in testler:
        fatura = rastgele_fatura()
        fatura = fonksiyon(fatura)

        print(f"\n--- {isim} ---")
        for k in fatura.kalemler:
            print(f"  ara_toplam:{'✓' if kalem_ara_toplam_dogrula(k) else '✗'} "
                  f"kdv_tutari:{'✓' if kalem_kdv_tutari_dogrula(k) else '✗'} "
                  f"satir_toplam:{'✓' if kalem_satir_toplam_dogrula(k) else '✗'}")
        print(f"  footer:{'✓' if fatura_footer_tutarlilik_dogrula(fatura) else '✗'}")