import random
from datetime import date, timedelta
from decimal import Decimal

from schema import (
    Fatura, FaturaKalemi, HarcamaKategorisi,
    IS_KOLU_KATEGORILERI, KDV_ORANI_MAP,
    POLICY_YASAKLI_KATEGORILER, POLICY_TUTAR_LIMITLERI,
)
from generators.field_generator import (
    ACIKLAMA_HAVUZU, rastgele_birim, rastgele_miktar, rastgele_birim_fiyat,
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
    """Faturanin no'sunu, başka (var olan) bir faturanin no'suyla kasitli olarak çakiştirir."""
    fatura.fatura_no = diger_fatura_no
    return fatura

from schema import AnomaliliFaturaKalemi


def satir_toplami_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin satir_toplam'ini, gerçek hesaplanan değerden
    kasitli olarak saptirir (satir toplami ile ara_toplam+kdv_tutari
    tutarsiz hale gelir).
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    kalem_verisi = orijinal_kalem.model_dump(exclude={"sahte_satir_toplam"}) 

    gercek_toplam = orijinal_kalem.satir_toplam
    carpan = random.choice([
        random.uniform(1.2, 2.0),   # gerçekten fazla gösterilmiş
        random.uniform(0.3, 0.7),   # gerçekten az gösterilmiş
    ])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(**kalem_verisi, sahte_satir_toplam=sahte_toplam)
    fatura.kalemler[hedef_index] = anomalili_kalem

    return fatura


from schema import AnomaliliFatura

def genel_toplam_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Faturanin genel_toplam'ini, kalemlerin gerçek toplamindan
    kasitli olarak saptirir (footer toplami ile kalem toplamlari tutarsiz olur).
    """
    fatura_verisi = fatura.model_dump(exclude={"sahte_genel_toplam"})

    gercek_toplam = fatura.genel_toplam
    carpan = random.choice([random.uniform(1.1, 1.5), random.uniform(0.5, 0.9)])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    return AnomaliliFatura(**fatura_verisi, sahte_genel_toplam=sahte_toplam)


if __name__ == "__main__":
    from generators.field_generator import rastgele_fatura
    from validators import kalem_satir_toplam_dogrula

    fatura = rastgele_fatura()
    fatura = satir_toplami_anomali_uret(fatura)

    for k in fatura.kalemler:
        gecerli = kalem_satir_toplam_dogrula(k)
        print(f"[{k.kalem_no}] {k.aciklama}: satir_toplam={k.satir_toplam}, doğrulama={'✓' if gecerli else '✗ (beklenen anomali)'}")