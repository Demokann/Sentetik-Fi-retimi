"""
BİR KEZ çalışır (Ollama kapalıyken, RAM sorunu yok): faturalar.json +
faturalar_etiketler.json'dan ~20k'lik bir alt küme örnekler, 1000'lik batch
dosyalarına böler ve bir durum.json manifesti yazar. Toplu üretim
(aciklama_toplu_uret.py) 155 MB'lık asıl dosyaya bir daha dokunmaz -- yalnızca
bu batch dosyalarını okur.

Varsayılan seçim modu ("kota"): ham anomali_turleri'ne göre tür başına
taban/tavan kotası uygular (bkz. anomali_turu_kotali_sec) -- nadir anomali
türlerinin rastgele örneklemede ezilmesini önler. Eski aciklama_kategorisi
bazlı orantılı seçim --secim-modu kategori ile hâlâ kullanılabilir.

Kullanım:
    python batch_hazirla.py --toplam 20000 --batch-size 1000 --tur-taban 300 --tur-tavan 600
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from generators.aciklama_uretici import (
    ONCELIK_SIRASI, ACIKLAMA_KATEGORILERI, ACIKLAMA_KATEGORI_ORANLARI,
    VARSAYILAN_TEKNIK_ORAN, _belirleyici_turu_sec,
)

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"

# Bu batch'in aciklama_kategorisi kompozisyonu için hedef -- faturalar_etiketler.
# json'daki popülasyon-geneli kalibre dağılımdan (ör. yeterli %56) BAĞIMSIZ bir
# hedef; bkz. _kategori_kotali_yeniden_ata.
VARSAYILAN_KATEGORI_HEDEF_ORANLARI: dict[str, float] = {
    "yeterli": 0.50,
    "yetersiz": 0.20,
    "manipulatif": 0.20,
    "ai_uretimi": 0.10,
}


def batch_kaydi_olustur(fatura: dict, etiket: dict, aciklama_kategorisi_override: str | None = None) -> dict:
    """Runner'ın prompt kurmak ve ihlal denetlemek için ihtiyaç duyduğu
    MİNİMUM alanları taşıyan kompakt kayıt. Asıl dosyaya bir daha dönülmez.
    aciklama_kategorisi_override verilirse, etiketteki (popülasyon geneli
    kalibre) değer yerine bu batch'e özel yeniden atanmış kategori yazılır --
    etiket dosyası DEĞİŞMEZ, sadece bu kayıtta override edilir."""
    return {
        "fatura_no": fatura["fatura_no"],
        "satici_unvan": fatura["satici_unvan"],
        "kalemler": fatura["kalemler"],
        "aciklama_kategorisi": aciklama_kategorisi_override if aciklama_kategorisi_override is not None else etiket["aciklama_kategorisi"],
        "is_anomali": etiket["is_anomali"],
        "anomali_turleri": etiket["anomali_turleri"],
    }


def dengeli_ornekle(
    kategori_havuzlari: dict[str, list[dict]],
    toplam: int,
    min_per_kategori: int,
) -> dict[str, list[dict]]:
    """
    Kategori oranlarını koruyan stratified downsample; nadir sınıflar için
    (manipulatif/ai_uretimi) bir taban (min_per_kategori) garanti eder.
    Havuzda yeterli örnek yoksa mevcut kadarını alır.
    """
    genel_toplam = sum(len(v) for v in kategori_havuzlari.values())
    secilen: dict[str, list[dict]] = {}

    # 1. adım: her kategoriye orantılı pay, ama en az min_per_kategori (havuz elverdiğince)
    for kategori, havuz in kategori_havuzlari.items():
        orantili = round(toplam * len(havuz) / genel_toplam)
        hedef = max(orantili, min_per_kategori)
        hedef = min(hedef, len(havuz))  # havuzda olandan fazlasını isteyemeyiz
        secilen[kategori] = random.sample(havuz, hedef)

    return secilen


# ---------------------------------------------------------------------------
# Kota bazlı (anomali_turleri) seçim -- 75k'lik geçerli havuzdan ~20k'lik alt
# küme çıkarırken rastgele örneklemenin nadir türleri (ör. fatura_no_tekrari,
# ~1000 mevcut) ezmesini önler. dengeli_ornekle (yukarıda) aciklama_kategorisi
# (4 kova) bazında dengeler; bu fonksiyon bir kat daha temelde, ham
# anomali_turleri (11 tür, union etiketleme sonucu) bazında dengeler --
# ikisi farklı eksenler, biri diğerinin yerine geçmez ama bu, "hangi 20k
# fatura seçilsin" sorusunun asıl cevabıdır.
# ---------------------------------------------------------------------------

# is_kolu, fatura_to_dict() tarafından faturalar.json'a export edilmiyor
# (main.py:fatura_to_dict) -- faturalar.json'a DOKUNMADAN (§5) çeşitlilik
# ölçmek için baskın kalem kategorisini iş kolu yerine proxy olarak kullanıyoruz.
def _baskin_kategori(fatura: dict) -> str:
    if not fatura["kalemler"]:
        return "bilinmeyen"
    sayac = Counter(k["harcama_kategorisi"] for k in fatura["kalemler"])
    return sayac.most_common(1)[0][0]


def _tutar_bucket(genel_toplam: float, sinirlar: tuple[float, float, float] = (500, 2000, 8000)) -> str:
    if genel_toplam < sinirlar[0]:
        return "dusuk"
    if genel_toplam < sinirlar[1]:
        return "orta"
    if genel_toplam < sinirlar[2]:
        return "yuksek"
    return "cok_yuksek"


def _cesitli_ornekle(fatura_no_listesi: list[str], fatura_map: dict[str, dict], n: int, rnd: random.Random) -> list[str]:
    """
    (baskin_kategori, tutar_bucket) katmanlarina round-robin dağıtarak n
    kayıt seçer -- düz random.sample yerine, bol havuzlarda dar bir örnek
    deseni (ör. hep ayni iş kolundan/tutar araligindan) öğrenilmesin diye.
    n >= havuz büyüklüğü ise hepsini döner.
    """
    if n <= 0:
        return []
    if n >= len(fatura_no_listesi):
        return list(fatura_no_listesi)

    katmanlar: dict[tuple[str, str], list[str]] = {}
    for fno in fatura_no_listesi:
        f = fatura_map[fno]
        anahtar = (_baskin_kategori(f), _tutar_bucket(f["genel_toplam"]))
        katmanlar.setdefault(anahtar, []).append(fno)
    for grup in katmanlar.values():
        rnd.shuffle(grup)

    secilen: list[str] = []
    gruplar = list(katmanlar.values())
    idx = 0
    while len(secilen) < n and any(gruplar):
        grup = gruplar[idx % len(gruplar)]
        if grup:
            secilen.append(grup.pop())
        idx += 1
    return secilen


def _cesitli_sira(fatura_no_listesi: list[str], fatura_map: dict[str, dict], rnd: random.Random) -> list[str]:
    """_cesitli_ornekle ile ayni katman/round-robin mantığı, ama bir alt küme
    değil TÜM listenin çeşitlilik-gözeten bir sırasını döner -- çağıran taraf
    bu sırada tek tek ilerleyip her adayı ayrı ayrı kabul/red edebilsin diye
    (tavan taşma kontrolü tek tek yapılmak zorunda, bkz. anomali_turu_kotali_sec)."""
    katmanlar: dict[tuple[str, str], list[str]] = {}
    for fno in fatura_no_listesi:
        f = fatura_map[fno]
        anahtar = (_baskin_kategori(f), _tutar_bucket(f["genel_toplam"]))
        katmanlar.setdefault(anahtar, []).append(fno)
    for grup in katmanlar.values():
        rnd.shuffle(grup)

    sira: list[str] = []
    gruplar = list(katmanlar.values())
    idx = 0
    while any(gruplar):
        grup = gruplar[idx % len(gruplar)]
        if grup:
            sira.append(grup.pop())
        idx += 1
    return sira


def _kategori_kotali_yeniden_ata(
    secilen_no_listesi: list[str],
    etiket_map: dict[str, dict],
    hedef_oranlar: dict[str, float],
    rnd: random.Random,
) -> tuple[dict[str, str], int]:
    """
    Seçilen batch için aciklama_kategorisi'ni hedef orana (varsayılan
    %50/20/20/10) göre atar -- faturalar_etiketler.json'daki atama hiç
    DEĞİŞTİRİLMEZ, yalnızca bu batch kaydı için bir değer üretilir.

    İlk tercih HER ZAMAN faturanın etikette zaten sahip olduğu kategoridir
    (kota müsaitse hiçbir şey override edilmez). Kota doluysa, rastgele bir
    kategoriye DEĞİL, faturanın belirleyici anomali türünün (ya da temizse
    "temiz"in) ACIKLAMA_KATEGORI_ORANLARI ağırlık vektöründe büyükten
    küçüğe sıralı bir sonraki uygun kategoriye deterministik olarak düşer
    (ör. yasakli_kategori'de manipulatif dolu ise ikinci en yüksek ağırlıklı
    kategori olan yeterli'ye). Böylece anomali türü <-> açıklama kategorisi
    arasındaki araştırma-kalibreli korelasyon (ör. yasakli_kategori'nin
    "gizleme" davranışını simüle etmek için manipulatife en yatkın olması)
    override durumunda bile mümkün olduğunca korunur -- rastgele/alakasız
    bir kategoriye asla düşürülmez.
    """
    toplam = len(secilen_no_listesi)
    kalan_kota = {kategori: round(toplam * oran) for kategori, oran in hedef_oranlar.items()}
    # Yuvarlama farkını en büyük paydan telafi et (toplam kota == toplam fatura olsun)
    fark = toplam - sum(kalan_kota.values())
    if fark and kalan_kota:
        en_buyuk = max(kalan_kota, key=lambda k: kalan_kota[k])
        kalan_kota[en_buyuk] += fark

    sira = list(secilen_no_listesi)
    rnd.shuffle(sira)

    yeni_kategoriler: dict[str, str] = {}
    override_sayisi = 0

    for fno in sira:
        etiket = etiket_map[fno]
        mevcut = etiket["aciklama_kategorisi"]
        if kalan_kota.get(mevcut, 0) > 0:
            secilen_kategori = mevcut
        else:
            belirleyici_tur = _belirleyici_turu_sec(etiket["anomali_turleri"])
            agirliklar = ACIKLAMA_KATEGORI_ORANLARI.get(belirleyici_tur, VARSAYILAN_TEKNIK_ORAN)
            siralama = sorted(zip(ACIKLAMA_KATEGORILERI, agirliklar), key=lambda x: -x[1])
            secilen_kategori = next(
                (kategori for kategori, _agirlik in siralama if kalan_kota.get(kategori, 0) > 0),
                None,
            )
            if secilen_kategori is None:
                # Tüm kotalar dolu (yuvarlama kenar durumu) -- mevcut kategoride kalsin
                secilen_kategori = mevcut
            else:
                override_sayisi += 1
        yeni_kategoriler[fno] = secilen_kategori
        kalan_kota[secilen_kategori] = kalan_kota.get(secilen_kategori, 0) - 1

    return yeni_kategoriler, override_sayisi


def _konteyner_tavanlarini_hesapla(
    tur_havuzlari: dict[str, list[str]],
    tur_taban: int,
    tur_tavan: int,
    esik: float = 0.9,
) -> dict[str, int]:
    """
    Bazı anomali türleri, başka bir (çok daha büyük) türün NEREDEYSE TAM ALT
    KÜMESİDİR -- ör. genel_toplam/satir_toplami havuzlarının %100'ü aynı
    zamanda footer_kismi'ye de sahip (injector yan etkisi: toplam/satır
    tutarını bozmak doğal olarak footer tutarlılığını da bozuyor). Böyle bir
    durumda "konteyner" türün (footer_kismi) tavanı normal (tur_tavan) kalırsa,
    içindeki bağımlı türlerin taban hedeflerini AYNI ANDA karşılamak
    matematiksel olarak imkânsız olur (ör. genel_toplam(300)+satir_toplami(300)
    = 600, footer_kismi'nin tavanı da 600 ise footer'a kendi payına HİÇ yer
    kalmaz, üstelik ikisi ayrı ayrı 300'e çıkmaya çalışırken birbirini bloklar).

    Bu fonksiyon, hangi türün hangi türün konteyneri olduğunu (overlap oranı
    >= esik) veriden OTOMATİK tespit eder ve konteyner türe, içerdiği her
    bağımlı tür için tur_taban kadar EK bütçe tanır -- böylece konteyner hem
    kendi normal payını (tur_tavan) korur hem de bağımlı türlerin tabanını
    bloklamaz.
    """
    turler = list(tur_havuzlari.keys())
    havuz_seti = {tur: set(fnolar) for tur, fnolar in tur_havuzlari.items()}
    ekstra_butce: dict[str, int] = {tur: 0 for tur in turler}

    for a in turler:
        if not havuz_seti[a]:
            continue
        for b in turler:
            if a == b or len(tur_havuzlari[b]) <= len(tur_havuzlari[a]):
                continue
            oran = len(havuz_seti[a] & havuz_seti[b]) / len(havuz_seti[a])
            if oran >= esik:
                # a, (daha büyük) b'nin neredeyse tam alt kümesi -- b konteyner
                ekstra_butce[b] += tur_taban

    return {tur: tur_tavan + ekstra_butce[tur] for tur in turler}


def anomali_turu_kotali_sec(
    faturalar: list[dict],
    etiketler: list[dict],
    hedef_toplam: int = 20000,
    tur_taban: int = 300,
    tur_tavan: int = 600,
    temiz_orani_min: float = 0.70,
    temiz_orani_max: float = 0.75,
    hedef_kategori_oranlari: dict[str, float] | None = None,
    seed: int = 42,
) -> tuple[list[dict], dict]:
    """
    75k'lik geçerli fatura havuzundan, her anomali türünün (anomali_turleri
    içindeki ham türler) alt kümede tabana (tur_taban) kadar temsil edilmesini,
    bol türlerin ise tavani (tur_tavan) aşmamasini garanti eden kota bazlı bir
    seçim yapar. Union/çoklu-etiket faturalar (bir fatura birden fazla türe
    aynı anda sahip olabilir) seçildiğinde sahip oldukları TÜM türlerin
    kotasından düşülür -- bir faturayı iki kez saymadan kotaları da
    şişirmeden. Kalan hedef, temiz faturalarla (~%70-75) doldurulur.

    Bu seçim tamamlandıktan SONRA, seçilen batch üzerinde aciklama_kategorisi
    için de ayrı bir kota (varsayılan %50/20/20/10) uygulanır -- bkz.
    _kategori_kotali_yeniden_ata. Bu, anomali_turleri kotasından bağımsız
    ikinci bir eksendir; faturanın etiketteki mevcut kategorisi öncelikli
    denenir, kota doluysa türün ağırlık sıralamasında bir sonraki uygun
    kategoriye düşülür (rastgele/alakasız bir kategoriye değil).

    Döndürdüğü (secilenler, rapor) ikilisinde rapor: tür bazında kaç fatura
    alındığı, hedefin altında kalan türler (varsa), nihai anomali oranı --
    bandın (temiz_orani_min/max'ın tümleyeni) dışına çıkarsa bunu işaretler
    (elle zorlamaz, sadece raporlar) -- ve aciklama_kategorisi dağılımı +
    kaç faturanın kategorisinin override edildiği.
    """
    if hedef_kategori_oranlari is None:
        hedef_kategori_oranlari = VARSAYILAN_KATEGORI_HEDEF_ORANLARI
    rnd = random.Random(seed)

    etiket_map = {e["fatura_no"]: e for e in etiketler}
    fatura_map = {f["fatura_no"]: f for f in faturalar}

    anomalili_no = [fno for fno, e in etiket_map.items() if e["is_anomali"]]
    temiz_no = [fno for fno, e in etiket_map.items() if not e["is_anomali"]]

    # Tür havuzları: ONCELIK_SIRASI gibi sabit bir tür listesi icat etmek
    # yerine, etiketlerde fiilen görülen türlerden dinamik kurulur (yeni bir
    # anomali türü eklenirse otomatik dahil olur).
    tur_havuzlari: dict[str, list[str]] = {}
    for fno in anomalili_no:
        for tur in etiket_map[fno]["anomali_turleri"]:
            tur_havuzlari.setdefault(tur, []).append(fno)

    # İşlem sırası ONCELIK_SIRASI'nı (aciklama_uretici) yeniden kullanır --
    # bazı türler bir üst türün NEREDEYSE TAM ALT KÜMESİ (ör. yasakli_kategori
    # -> is_kolu_kategori_uyumsuzlugu %100, genel_toplam/satir_toplami ->
    # footer_kismi %100 örtüşüyor, injector yan etkisi). Bu durumda hangi tür
    # önce işlenirse ortak tavan bütçesini o alır; ONCELIK_SIRASI zaten "en
    # insan-bilinçli/kasıtlı"yı önce sıralıyor, o yüzden aynı mantık burada da
    # kullanılır (yasakli_kategori limit_asimi/is_kolu'dan önce bütçeyi
    # kapabilsin diye). Listede olmayan yeni bir tür çıkarsa (havuzu en küçük
    # olan önce işlenecek şekilde) sona eklenir.
    bilinen_turler = [t for t in ONCELIK_SIRASI if t in tur_havuzlari]
    yeni_turler = sorted(
        (t for t in tur_havuzlari if t not in ONCELIK_SIRASI),
        key=lambda t: len(tur_havuzlari[t]),
    )
    tur_sirasi = bilinen_turler + yeni_turler

    # Konteyner türlere (ör. footer_kismi, is_kolu_kategori_uyumsuzlugu) veriden
    # otomatik tespitle ek tavan bütçesi tanı -- bkz. _konteyner_tavanlarini_hesapla.
    tavan_efektif = _konteyner_tavanlarini_hesapla(tur_havuzlari, tur_taban, tur_tavan)

    # tur_sayaci: bir türden GERÇEKTE kaç fatura seçildiği (hangi türün
    # "sırasında" seçildiğine bakılmaksızın, union nedeniyle bir fatura başka
    # bir türün turunda seçilse bile bu türden sayılır). Tavan bunun üzerinden
    # KATI şekilde uygulanır -- aksi halde (yalnızca "kendi sırasında kaç tane
    # seçildi" sayılırsa) yapısal türler (ör. footer_kismi) diğer sayısal
    # anomalilerin yan etkisiyle union'a sızıp tavanı fazlasıyla aşabilir.
    tur_sayaci: dict[str, int] = {tur: 0 for tur in tur_havuzlari}
    secilen_anomalili: set[str] = set()

    for tur in tur_sirasi:
        ihtiyac = tavan_efektif[tur] - tur_sayaci[tur]
        if ihtiyac <= 0:
            continue
        adaylar = [fno for fno in tur_havuzlari[tur] if fno not in secilen_anomalili]
        sira = _cesitli_sira(adaylar, fatura_map, rnd)
        # Münhasırlık önceliği: bu tur içindeki adaylar arasında, BAŞKA
        # (tur_havuzlari'nda izlenen) türlere daha az sahip olanlar önce
        # denenir -- böylece bir türü doldururken paylaşımlı/kıt bütçeli
        # başka bir türün (ör. is_kolu_kategori_uyumsuzlugu) kotası gereksiz
        # tüketilmez (bkz. limit_asimi örneği). sort() stabil olduğu için
        # aynı münhasırlık skorundakiler arasında çeşitlilik sırası korunur.
        sira.sort(key=lambda fno: sum(
            1 for t2 in etiket_map[fno]["anomali_turleri"] if t2 != tur and t2 in tur_sayaci
        ))
        alinan = 0
        for fno in sira:
            if alinan >= ihtiyac:
                break
            turleri = etiket_map[fno]["anomali_turleri"]
            # Bu faturayi almak, sahip olduğu BAŞKA bir türü kendi (efektif)
            # tavanının üzerine taşıyorsa vazgeç -- kota her tür için
            # bağımsız bir tavan, bir türü doldururken başkasını taşırmamalı.
            if any(tur_sayaci[t2] >= tavan_efektif[t2] for t2 in turleri if t2 != tur and t2 in tur_sayaci):
                continue
            secilen_anomalili.add(fno)
            for t2 in turleri:
                if t2 in tur_sayaci:
                    tur_sayaci[t2] += 1
            alinan += 1

    # Küçük bir hedef_toplam (ör. kalibrasyon denemesi) verilirse, tür
    # kotalarının bağımsız toplamı hedefin tamamını (hatta üstünü) doldurup
    # temiz faturaya hiç yer bırakmayabilir. Bu durumda anomalili kümeyi
    # hedef bandın (temiz_orani_min/max'ın tümleyeni) tavanına göre kırpar,
    # çeşitliliği koruyarak -- kırpma olduysa rapor bunu açıkça işaretler.
    anomali_ust_sinir = int(hedef_toplam * (1 - temiz_orani_min))
    anomalili_kirpildi = len(secilen_anomalili) > anomali_ust_sinir
    if anomalili_kirpildi:
        secilen_anomalili = set(_cesitli_sira(list(secilen_anomalili), fatura_map, rnd)[:max(anomali_ust_sinir, 0)])

    anomalili_sayisi = len(secilen_anomalili)
    temiz_hedef = max(0, min(hedef_toplam - anomalili_sayisi, len(temiz_no)))
    secilen_temiz = _cesitli_ornekle(temiz_no, fatura_map, temiz_hedef, rnd)

    tum_secilen_no = list(secilen_anomalili) + secilen_temiz
    rnd.shuffle(tum_secilen_no)

    yeni_kategoriler, kategori_override_sayisi = _kategori_kotali_yeniden_ata(
        tum_secilen_no, etiket_map, hedef_kategori_oranlari, rnd
    )
    secilenler = [
        batch_kaydi_olustur(fatura_map[fno], etiket_map[fno], aciklama_kategorisi_override=yeni_kategoriler[fno])
        for fno in tum_secilen_no
    ]

    toplam = len(tum_secilen_no)
    anomali_orani = anomalili_sayisi / toplam if toplam else 0.0
    hedef_araligi = (round(1 - temiz_orani_max, 4), round(1 - temiz_orani_min, 4))

    tur_raporu: dict[str, dict] = {}
    hedefin_altinda_kalanlar: list[str] = []
    for tur, havuz in tur_havuzlari.items():
        secilen_bu_tur = sum(1 for fno in secilen_anomalili if tur in etiket_map[fno]["anomali_turleri"])
        yetersiz = secilen_bu_tur < tur_taban
        if yetersiz:
            hedefin_altinda_kalanlar.append(tur)
        tur_raporu[tur] = {
            "mevcut_havuzda": len(havuz),
            "secilen": secilen_bu_tur,
            "taban": tur_taban,
            "tavan": tavan_efektif[tur],
            "konteyner": tavan_efektif[tur] > tur_tavan,
            "hedefin_altinda": yetersiz,
        }

    # aciklama_kategorisi (yeterli/yetersiz/manipulatif/ai_uretimi) dağılımı --
    # _kategori_kotali_yeniden_ata sonrası (override uygulanmış) SONUÇ.
    kategori_sayaci = Counter(s["aciklama_kategorisi"] for s in secilenler)
    kategori_raporu = {
        kategori: {"adet": adet, "oran": round(adet / toplam, 4) if toplam else 0.0}
        for kategori, adet in kategori_sayaci.items()
    }

    rapor = {
        "toplam_secilen": toplam,
        "anomalili_sayisi": anomalili_sayisi,
        "anomalili_kirpildi": anomalili_kirpildi,
        "temiz_sayisi": len(secilen_temiz),
        "anomali_orani": anomali_orani,
        "hedef_anomali_orani_araligi": hedef_araligi,
        "aciklama_kategorisi_hedef_oranlari": hedef_kategori_oranlari,
        "aciklama_kategorisi_override_sayisi": kategori_override_sayisi,
        "anomali_orani_bandin_disinda": not (hedef_araligi[0] <= anomali_orani <= hedef_araligi[1]),
        "tur_bazli": tur_raporu,
        "hedefin_altinda_kalan_turler": hedefin_altinda_kalanlar,
        "aciklama_kategorisi_dagilimi": kategori_raporu,
    }
    return secilenler, rapor


def _kota_raporu_yazdir(rapor: dict) -> None:
    print("\n[+] Tür bazlı kota raporu:")
    for tur, bilgi in sorted(rapor["tur_bazli"].items(), key=lambda x: -x[1]["mevcut_havuzda"]):
        isaret = "  [HEDEF ALTINDA]" if bilgi["hedefin_altinda"] else ""
        konteyner_etiketi = "  [KONTEYNER]" if bilgi["konteyner"] else ""
        print(
            f"      {tur:32s}: secilen={bilgi['secilen']:4d}  "
            f"(havuz={bilgi['mevcut_havuzda']:5d}, taban={bilgi['taban']}, tavan={bilgi['tavan']}){konteyner_etiketi}{isaret}"
        )
    if rapor["hedefin_altinda_kalan_turler"]:
        print(f"\n[!] Tabanin altinda kalan turler: {', '.join(rapor['hedefin_altinda_kalan_turler'])}")
    if rapor["anomalili_kirpildi"]:
        print("\n[!] hedef_toplam kucuk kaldigi icin anomalili kume temiz oranini korumak adina kirpildi.")
    print(
        f"\n[+] Toplam secilen: {rapor['toplam_secilen']}  "
        f"(anomalili={rapor['anomalili_sayisi']}, temiz={rapor['temiz_sayisi']})"
    )
    bant = rapor["hedef_anomali_orani_araligi"]
    uyari = " [!] BANDIN DISINDA" if rapor["anomali_orani_bandin_disinda"] else ""
    print(f"[+] Anomali orani: {rapor['anomali_orani']:.3f}  (hedef bant: {bant[0]:.2f}-{bant[1]:.2f}){uyari}")

    hedef_kat = rapor["aciklama_kategorisi_hedef_oranlari"]
    print("\n[+] aciklama_kategorisi dagilimi (secilen alt kumede, hedefle kiyasla):")
    for kategori, bilgi in sorted(rapor["aciklama_kategorisi_dagilimi"].items(), key=lambda x: -x[1]["adet"]):
        hedef_oran = hedef_kat.get(kategori, 0.0)
        print(
            f"      {kategori:12s}: {bilgi['adet']:5d}  (%{bilgi['oran']*100:5.1f}, hedef %{hedef_oran*100:.0f})"
        )
    toplam_secilen = rapor["toplam_secilen"]
    override_orani = rapor["aciklama_kategorisi_override_sayisi"] / toplam_secilen if toplam_secilen else 0.0
    print(
        f"\n[+] Kategori override: {rapor['aciklama_kategorisi_override_sayisi']} fatura "
        f"(%{override_orani*100:.1f}) -- kendi kategorisinin kotasi dolu oldugu icin "
        f"turunun agirlik sirasindaki bir sonraki kategoriye dustu."
    )


def main():
    parser = argparse.ArgumentParser(description="Dengeli alt küme örnekle ve batch dosyalarına böl")
    parser.add_argument("--input-json", default="data/faturalar.json", help="faturalar.json yolu")
    parser.add_argument("--etiket-json", default="data/faturalar_etiketler.json", help="etiketler.json yolu")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="Batch dosyalarının yazılacağı dizin")
    parser.add_argument("--toplam", type=int, default=22000, help="Hedef toplam fatura sayısı (yaklaşık)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Her batch dosyasındaki fatura sayısı")
    parser.add_argument(
        "--secim-modu", choices=["kota", "kategori"], default="kota",
        help="kota: anomali_turleri bazli tur-kotali secim (varsayilan, nadir turleri korur); "
             "kategori: eski aciklama_kategorisi bazli orantili secim",
    )
    parser.add_argument("--min-per-kategori", type=int, default=2500, help="[kategori modu] Nadir sınıflar için taban örnek sayısı")
    parser.add_argument("--tur-taban", type=int, default=300, help="[kota modu] Anomali türü başına hedeflenen taban")
    parser.add_argument("--tur-tavan", type=int, default=600, help="[kota modu] Anomali türü başına izin verilen tavan")
    parser.add_argument("--temiz-orani-min", type=float, default=0.70, help="[kota modu] Alt kümede hedeflenen minimum temiz oranı")
    parser.add_argument("--temiz-orani-max", type=float, default=0.75, help="[kota modu] Alt kümede hedeflenen maksimum temiz oranı")
    parser.add_argument("--kategori-oran-yeterli", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yeterli"], help="[kota modu] aciklama_kategorisi hedef oranı: yeterli")
    parser.add_argument("--kategori-oran-yetersiz", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yetersiz"], help="[kota modu] aciklama_kategorisi hedef oranı: yetersiz")
    parser.add_argument("--kategori-oran-manipulatif", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["manipulatif"], help="[kota modu] aciklama_kategorisi hedef oranı: manipulatif")
    parser.add_argument("--kategori-oran-ai-uretimi", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["ai_uretimi"], help="[kota modu] aciklama_kategorisi hedef oranı: ai_uretimi")
    parser.add_argument("--seed", type=int, default=42, help="Tekrarlanabilirlik için rastgelelik tohumu")
    args = parser.parse_args()

    random.seed(args.seed)

    cikti_dizini = Path(args.cikti_dizini)
    cikti_dizini.mkdir(parents=True, exist_ok=True)

    print(f"[+] {args.input_json} okunuyor (büyük dosya, biraz sürebilir)...")
    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    etiket_map = {e["fatura_no"]: e for e in etiketler}
    if "aciklama_kategorisi" not in etiketler[0]:
        print("HATA: etiketler.json'da aciklama_kategorisi yok.")
        return

    if args.secim_modu == "kota":
        hedef_kategori_oranlari = {
            "yeterli": args.kategori_oran_yeterli,
            "yetersiz": args.kategori_oran_yetersiz,
            "manipulatif": args.kategori_oran_manipulatif,
            "ai_uretimi": args.kategori_oran_ai_uretimi,
        }
        secilenler, rapor = anomali_turu_kotali_sec(
            faturalar, etiketler,
            hedef_toplam=args.toplam,
            tur_taban=args.tur_taban,
            tur_tavan=args.tur_tavan,
            temiz_orani_min=args.temiz_orani_min,
            temiz_orani_max=args.temiz_orani_max,
            hedef_kategori_oranlari=hedef_kategori_oranlari,
            seed=args.seed,
        )
        _kota_raporu_yazdir(rapor)
    else:
        # Kategori bazında havuzları kur (pilot'taki join mantığının aynısı)
        kategori_havuzlari: dict[str, list[dict]] = {}
        for fatura in faturalar:
            etiket = etiket_map.get(fatura["fatura_no"])
            if etiket is None:
                continue
            kategori_havuzlari.setdefault(etiket["aciklama_kategorisi"], []).append(fatura)

        print("[+] Havuz dağılımı (tüm veri):")
        for k, v in sorted(kategori_havuzlari.items(), key=lambda x: -len(x[1])):
            print(f"      {k:12s}: {len(v)}")

        secilen_kategori = dengeli_ornekle(kategori_havuzlari, args.toplam, args.min_per_kategori)

        print("\n[+] Seçilen alt küme dağılımı:")
        secilenler = []
        for kategori, faturalar_alt in secilen_kategori.items():
            print(f"      {kategori:12s}: {len(faturalar_alt)}")
            for fatura in faturalar_alt:
                secilenler.append(batch_kaydi_olustur(fatura, etiket_map[fatura["fatura_no"]]))

        random.shuffle(secilenler)

    print(f"\n[+] Toplam seçilen: {len(secilenler)} fatura")

    # Batch dosyalarına böl
    batch_manifest = []
    batch_no = 0
    for i in range(0, len(secilenler), args.batch_size):
        batch_no += 1
        dilim = secilenler[i : i + args.batch_size]
        batch_dosya = f"batch_{batch_no:04d}.json"
        cikti_dosya = f"batch_{batch_no:04d}_ciktilar.json"
        with open(cikti_dizini / batch_dosya, "w", encoding="utf-8") as f:
            # indent=2: faturalar.json ile aynı biçim (main.py). Tek satırlık JSON
            # editörde inceleme sırasında donduruyordu -- açıklama üretimini teşhis
            # ederken aynı faturaya bakmak yaygın bir iş, okunabilirlik önemli.
            json.dump(dilim, f, ensure_ascii=False, indent=2)
        batch_manifest.append({
            "dosya": batch_dosya,
            "cikti_dosyasi": cikti_dosya,
            "adet": len(dilim),
            "tamam": False,
        })

    durum = {
        "config": {
            "toplam_hedef": args.toplam,
            "batch_size": args.batch_size,
            "secim_modu": args.secim_modu,
            "min_per_kategori": args.min_per_kategori,
            "tur_taban": args.tur_taban,
            "tur_tavan": args.tur_tavan,
            "temiz_orani_min": args.temiz_orani_min,
            "temiz_orani_max": args.temiz_orani_max,
            "kategori_oran_yeterli": args.kategori_oran_yeterli,
            "kategori_oran_yetersiz": args.kategori_oran_yetersiz,
            "kategori_oran_manipulatif": args.kategori_oran_manipulatif,
            "kategori_oran_ai_uretimi": args.kategori_oran_ai_uretimi,
            "seed": args.seed,
        },
        "toplam_secilen": len(secilenler),
        "batch_sayisi": len(batch_manifest),
        "batchler": batch_manifest,
    }
    if args.secim_modu == "kota":
        durum["kota_raporu"] = rapor
    with open(cikti_dizini / "durum.json", "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=2)

    print(f"[+] {len(batch_manifest)} batch dosyası + durum.json yazıldı -> {cikti_dizini}/")
    print(f"[+] Sonraki adım: python aciklama_toplu_uret.py")


if __name__ == "__main__":
    main()
