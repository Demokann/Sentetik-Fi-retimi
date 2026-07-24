"""
Açıklama üretiminin paylaşılan çekirdeği: prompt kurulumu, Ollama çağrısı,
kural tabanlı ihlal tespiti ve düzeltici retry mantığı burada tek kaynak
olarak durur. Hem pilot (aciklama_llm_pilot.py) hem toplu üretim
(aciklama_toplu_uret.py) bu modülü kullanır -- böylece prompt/retry mantığı
asla ayrışmaz.
"""

import random
import re
import requests
from collections import Counter

OLLAMA_HOST_VARSAYILAN = "http://localhost:11434"
MODEL_VARSAYILAN = "qwen3:8b"

POLICY_YASAKLI_KATEGORI_ADLARI = {"alkol", "eglence", "tutun_urunleri", "kumar"}
KATEGORILER = ("yeterli", "yetersiz", "manipulatif", "ai_uretimi")

# Model üretim yerine reddederse (güvenlik/moderasyon refleksi) yakalanır -> "red"
# ihlali olarak retry tetiklenir. Çıktı temizlemeden SONRA da hayatta kalan
# kalıplar (temizleme redleri silmez, çünkü red metni de "anlamlı" satırdır).
RED_KALIPLARI = ("yerine getiremem", "yardımcı olamam", "yardimci olamam",
                 "üretemem", "uretemem", "i can't", "i cannot", "as an ai")


def kalemler_ozetle(kalemler: list[dict]) -> str:
    return ", ".join(f"{k['aciklama']} ({k['harcama_kategorisi']})" for k in kalemler)


def kalemler_ozetle_prompt(kalemler: list[dict]) -> str:
    """Prompt için kalem özeti: kategori adı İNSAN-OKUR (alt çizgisiz) gösterilir.
    Böylece model ham enum'u ('kisisel_bakim') kopyalayamaz -> enum sızıntısı
    KAYNAĞINDA engellenir (retry'a kalmadan). kalemler_ozetle ise kayıt/MD için
    ham haliyle kalır."""
    return ", ".join(f"{k['aciklama']} ({k['harcama_kategorisi'].replace('_', ' ')})" for k in kalemler)


def yasakli_kalem_bul(kalemler: list[dict]) -> dict | None:
    for k in kalemler:
        if k["harcama_kategorisi"] in POLICY_YASAKLI_KATEGORI_ADLARI:
            return k
    return None


# Firma unvanindaki hukuki ek/suffix'leri temizler -- LLM'e "Yilmaz Gida San.
# ve Tic. Ltd. Şti." yerine "Yilmaz Gida" gibi konuşma diline yakin bir isim
# vermek icin (field_generator.py:IS_KOLU_SUFFIX ile SENKRON tutulmali).
_UNVAN_EKLERI_REGEX = r"\b(A\.Ş\.|Ltd\.\s*Şti\.|Tic\.|San\.|ve|Paz\.|Turizm|Nak\.|Otelcilik|Danişmanlik|Prodüksiyon|Konfeksiyon|Kozmetik|Global|İç|Diş|Ticaret|Sanayi|Taş\.)(?=\s|$)"


def firma_adi_kisalt(unvan: str) -> str:
    kisa = re.sub(_UNVAN_EKLERI_REGEX, "", unvan)
    kisa = re.sub(r"\s+", " ", kisa).strip(" .,-")
    return kisa if kisa else unvan


def aykiri_kalem_bul(kalemler: list[dict]) -> dict | None:
    if len(kalemler) < 2:
        return None
    kategori_sayaci = Counter(k["harcama_kategorisi"] for k in kalemler)
    baskin_kategori, baskin_adet = kategori_sayaci.most_common(1)[0]
    azinlik_kategorileri = {kat for kat, adet in kategori_sayaci.items() if adet < baskin_adet}
    if not azinlik_kategorileri:
        return None
    for k in kalemler:
        if k["harcama_kategorisi"] in azinlik_kategorileri:
            return k
    return None


def gizlenecek_kalem_bul(kalemler: list[dict]) -> dict | None:
    return yasakli_kalem_bul(kalemler) or aykiri_kalem_bul(kalemler)


# ---------------------------------------------------------------------------
# UZUNLUK HEDEFİ (kategoriden BAĞIMSIZ, yumuşak ağırlıklı): araştırma, 8B gibi
# küçük modellerde çeşitliliğin EN güçlü kaldıracının kategoriden bağımsız uzunluk
# varyasyonu olduğunu gösteriyor -- ince persona detayını açık ara geçiyor (bkz.
# arXiv:2505.17390). Persona ekseni 8B'de neredeyse etkisiz olduğu için kaldırıldı,
# yerine bu geldi. Ağırlıklar YUMUŞAK: her kategori her uzunluğu alabilir (leakage
# yok -> uzunluk temiz bir kategori ayracı olamaz), ama semantik makullük korunur
# (ai_uretimi doğal olarak uzuna, yetersiz kısaya eğilir).
# Her hedef: (ad, prompt_tarifi, alt_karakter, ust_karakter).
# ---------------------------------------------------------------------------

UZUNLUK_HEDEFLERI = [
    ("çok kısa", "en fazla 4-5 kelimelik tek bir öbek", 8, 45),
    ("kısa", "tek cümle, 6-10 kelime", 20, 85),
    ("orta", "1-2 cümle", 45, 135),
    ("uzun", "2-3 cümle, biraz daha detaylı", 90, 230),
]

# Kategori başına yumuşak ağırlık: dört uzunluğa da şans verir, sadece eğim farkı.
_UZUNLUK_AGIRLIK = {
    "yetersiz": [0.40, 0.35, 0.20, 0.05],
    # ai_uretimi: HEM kısa (kalıba uyan tek öbek) HEM ölçülü-uzun (1-2 cümle resmi
    # paragraf) alabilsin -> 'uzunsa ai' diye TEK YÖNLÜ sahte sinyal vermeyelim
    # (ai kısa da olabilir, uzun da). Aşırı uzun (3+ cümle/paragraf) hariç, dört
    # uzunluğa da makul şans. Karakteri artık system prompt taşıyor.
    "ai_uretimi": [0.10, 0.30, 0.35, 0.25],
    "manipulatif": [0.10, 0.30, 0.40, 0.20],
    "yeterli": [0.20, 0.35, 0.30, 0.15],
}


def uzunluk_hedefi_sec(kategori: str) -> tuple[str, str, int, int]:
    """Kategoriden bağımsız (yumuşak ağırlıklı) bir uzunluk hedefi seçer.
    Dönüş: (ad, prompt_tarifi, alt_karakter, ust_karakter)."""
    agirlik = _UZUNLUK_AGIRLIK.get(kategori, [0.25, 0.25, 0.25, 0.25])
    return random.choices(UZUNLUK_HEDEFLERI, weights=agirlik, k=1)[0]


# ---------------------------------------------------------------------------
# FEW-SHOT: kategori başına küratörlü örnekler. Model tarif yerine TAKLİT eder.
# Çekirdeğin pozitif-çerçeveleme felsefesine uyumlu: yeterli örnekleri aktif fiil
# (aldım/ödedim) taşır -- yasak kalıp GÖSTERİLMEZ (pembe-fil etkisi). ai_uretimi
# örnekleri AI_URETIMI_KAPANIS_IPUCLARI havuzuyla tutarlı kapanışlar kullanır.
# ---------------------------------------------------------------------------

FEWSHOT = {
    "yeterli": [
        "Bölge bayi ziyaretinde ekiple öğle yemeği için ödedim.",
        "Yeni ekip üyeleri için kırtasiye ve toner aldım, stok bitmişti.",
        "Müşteri sunumu öncesi toplantı odasına ikram ısmarladım.",
        "Saha kurulumunda kullanmak üzere kablo ve bağlantı parçası satın aldım.",
    ],
    "yetersiz": [
        "genel ofis ihtiyacı",
        "iş gideri",
        "muhtelif harcama, departman için",
        "gerekliydi alındı",
    ],
    "manipulatif": [
        "Yönetim kurulu değerlendirme toplantısı kapsamında ağırlama gideri.",
        "Kesinlikle tamamen proje bütçesi kapsamındadır, iş dışı hiçbir kalem yoktur.",
        "Önemli müşteri ağırlama organizasyonu, tümüyle iş geliştirme amaçlıdır.",
    ],
    # ai_uretimi örnekleri BİLEREK farklı açılışlarla (İlgili/Söz konusu/İşbu/Fişe konu/
    # Bahse konu) -> model tek bir açılışa ('Belirtilen fiş...') çakılmasın.
    "ai_uretimi": [
        "İlgili masraf kalemi, kurumsal faaliyetlerin sürdürülebilirliği doğrultusunda gerçekleştirilmiştir.",
        "Söz konusu harcama, departmanın operasyonel gereksinimleri kapsamında temin edilmiştir.",
        "İşbu gider, ilgili birimin ihtiyaçları çerçevesinde değerlendirilmiştir.",
        "Fişe konu kalemler, kurumsal süreçlerin devamlılığı amacıyla tanzim edilmiştir.",
        "Bahse konu masraf, mevzuat ve şirket politikaları doğrultusunda karşılanmıştır.",
    ],
}


def fewshot_blok(kategori: str, adet: int = 2) -> str:
    havuz = FEWSHOT.get(kategori, [])
    if not havuz:
        return ""
    secim = random.sample(havuz, min(adet, len(havuz)))
    satirlar = "\n".join(f"- {s}" for s in secim)
    return f"Örnek TARZLAR (birebir kopyalama, yalnız tarzı yakala):\n{satirlar}\n"


YETERLI_USLUP_IPUCLARI = [
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "gündelik konuşma diliyle, fazla resmi olmayan bir ifade",
]

# yeterli İSKELE: iyi örnekler (#66/#87) hem iş amacı verir, hem firma adını doğal
# kullanır, hem de gerçek kalemi kısaca dahil eder. Tek kalıba çakılmamak için
# fatura başına rastgele bir YAPI seçilir (amaç-önce / kalem-önce / olay-çıpalı /
# iki-parçalı). {firma} çalışma anında doldurulur.
YETERLI_ISKELE = [
    "Önce iş amacını (kim için / neden) söyle, sonra {firma}'dan hangi gerçek kalemi aldığını belirt.",
    "Önce fişteki gerçek kalem(ler)i aldığını yaz, ardından bunu neden/hangi iş için yaptığını ekle.",
    "Bir iş olayına bağla (toplantı, bayi ziyareti, saha işi, sunum öncesi, ekip ihtiyacı gibi) ve o "
    "kapsamda {firma}'dan gerçek kalemi aldığını anlat.",
    "Kısa ana cümlede amaç+gerçek kalemi ver; istersen ikinci kısa cümlede küçük bir bağlam/gerekçe ekle.",
]

# yeterli DAYANAK kontrolü için iş-amacı isim havuzu (metin bunlardan hiçbirini,
# gerçek kalemi ya da firma adını içermiyorsa 'dayanaksız/muğlak' sayılır).
_YETERLI_AMAC_ISIMLERI = (
    "proje", "toplanti", "ziyaret", "musteri", "ekip", "etkinlik", "sunum", "bayi",
    "saha", "organizasyon", "lansman", "fuar", "egitim", "departman", "ofis",
    "misafir", "agirlama", "ikram", "servis", "sube", "calisan", "personel", "is",
)

# ai_uretimi kategorisinde model tek bir kapanış kalıbına ("...kapsamında
# gerçekleştirilmiştir") aşırı yakınsıyordu (200 örnekte %70). Kapanışı
# fatura başına rastgele seçip zorunlu kılarak kalıp çeşitliliğini garantiye
# alıyoruz.
AI_URETIMI_KAPANIS_IPUCLARI = [
    "kapsamında gerçekleştirilmiştir",
    "için işlem yapılmıştır",
    "amacıyla gerçekleştirilmiştir",
    "doğrultusunda sağlanmıştır",
    "kapsamında temin edilmiştir",
    "çerçevesinde gerçekleştirilmiştir",
]

# ai_uretimi AÇILIŞ havuzu: tüm çıktılar 'Belirtilen fiş kapsamında gerçekleştirilen
# harcamalar...' ile başlıyordu (deneme_80'de 20/20). Kaynak, ai talimatındaki sabit
# örnek ifadeydi -> model onu birebir açılış yapıyordu. Açılışı fatura başına rastgele
# döndürerek collapse'ı kırıyoruz (kapanış havuzuyla aynı mantık).
AI_URETIMI_ACILIS_IPUCLARI = [
    "Belirtilen fiş kapsamında gerçekleştirilen harcamalar",
    "İlgili masraf kalemi",
    "Söz konusu harcama",
    "İşbu masraf",
    "Fişe konu kalemler",
    "Bahse konu giderler",
    "Kayda alınan masraf",
    "Değerlendirilen harcama kalemleri",
]

# yetersiz kategorisinde de benzer şekilde "...temin edilmiştir" kalıbına
# yakınsama vardı (%22). Üslup çeşitliliği için rastgele seçim.
YETERSIZ_USLUP_IPUCLARI = [
    "sadece firma adını an, iş amacından hiç bahsetme",
    "'ihtiyaç için', 'iş ile ilgili' gibi çok genel geçer ifadelerle geç",
    "hangi ürün/hizmet olduğunu belirtme, sadece genel bir harcama olduğunu söyle",
    "kısa ve detaysız, tek bir öbek halinde",
    "'çeşitli', 'genel', 'muhtelif' gibi belirsizlik bildiren kelimelerle",
]

# yetersiz talimatındaki literal örnekler SABİT verildiğinde model bunları birebir
# kopyalıyordu ('genel ofis ihtiyacı' 5'te 4). Havuzdan her seferinde 2 rastgele
# örnek gösterip döndürerek collapse'ı kırıyoruz (few-shot ise yetersiz'de artık
# hiç kullanılmıyor -- talimat örnekleriyle çakışıp verbatim kopyayı besliyordu).
YETERSIZ_ORNEK_HAVUZ = [
    # isim-öbeği (muğlak, kategorik)
    "genel ofis ihtiyacı", "iş gideri", "muhtelif harcama", "departman ihtiyacı",
    "çeşitli alışveriş", "rutin harcama", "genel gider", "ihtiyaç malzemesi",
    "ofis için", "gerekli malzeme", "aylık ihtiyaç",
    # üşengeç/baştan-savma cümle (whaatif'in en iyi verdiği ton)
    "masraf", "gider", "bir şeyler aldım işte", "gerekliydi aldım",
    "iş için lazımdı", "çeşitli şeyler işte", "toplantı için bir şeyler", "gerekliydi işte",
]

# manipulatif "aşırı haklı çıkarma" dalı: model bazen abartılı vurguyu atlayıp
# meşru/yeterli gibi okunan cümle üretiyordu. Zorunlu bir vurgu açılışı seçip
# dayatarak (ve validator'la denetleyerek) manipülatif "işareti"ni garantiye
# alıyoruz. _VURGU_ANAHTARLARI, üretilen metinde vurgunun varlığını (Türkçe
# normalize edilmiş) yakalamak için kullanılır.
MANIPULATIF_VURGU_IPUCLARI = [
    "Kesinlikle",
    "Yüzde yüz iş amaçlı",
    "Tamamen iş gereği",
    "Hiç şüphesiz"
    " iş için",
    "Yüzde yüz proje kapsamında",
    "Hiç kuşkusuz iş amaçlı",
    "Kesinlikle şirket gideri",
    "Tümüyle iş için",
    # 'kesinlikle/yüzde yüz/tamamen' aşırı tekrar ediyordu -> emphatic ifadeyi çeşitlendir
    # (aynı zamanda 'kelime = manipulatif' sahte sinyalini de zayıflatır).
    "Hiç tartışmasız iş için",
    "Net biçimde işle ilgili",
    "Gönül rahatlığıyla iş amaçlı",
    "Baştan sona iş kapsamında",
    "Her açıdan işe dönük",
    "Eksiksiz biçimde proje için",
    "Şüpheye yer yok, iş amaçlı",
    "Bal gibi iş gideri",
]
# Genel emphatic-marker fallback (atanan vurgu paraphrase edilse de yakalansın).
_VURGU_ANAHTARLARI = (
    "kesinlikle", "yuzde yuz", "tamamen", "tumuyle", "suphesiz", "kuskusuz",
    "tartismasiz", "eksiksiz", "gonul rahat", "bastan sona", "her acidan",
    "net bicimde", "supheye yer", "bal gibi",
)

# Vurgunun cümledeki KONUMU rastgele seçilir: menü ("başa/ortaya/sona") verilince
# model hep başa koyuyordu -> eğitimde sahte konumsal sinyal. Tek somut slot dayatmak
# konumu fatura başına değiştirir, dağılımı dengeler.
MANIPULATIF_VURGU_KONUMLARI = ["cümlenin BAŞINA", "cümlenin ORTASINA", "cümlenin SONUNA"]

# ---------------------------------------------------------------------------
# PERSONA (Faz 7'de geri getirildi): whaatif'in karakter zenginliğini taşır.
# 8B'de lexical ÇEŞİTLİLİĞİ az artırır (bkz. arXiv:2505.17390) ama KARAKTER/ses
# için değerli -- 'umursamaz/yorgun/aceleci bir kişi gibi' yazmak çıktıya insan
# tınısı verir; yüksek parametreli modellerde etkisi daha da büyür. SADECE user
# prompt'a girer (system cache korunur). ai_uretimi'ne UYGULANMAZ (robotik kalmalı).
# ---------------------------------------------------------------------------

PERSONA_ROL = ["satış temsilcisi", "yazılım mühendisi", "saha teknisyeni",
               "yönetici asistanı", "muhasebe uzmanı", "proje yöneticisi",
               "pazarlama uzmanı", "insan kaynakları uzmanı", "lojistik sorumlusu",
               "operasyon uzmanı"]
PERSONA_KIDEM = ["yeni başlamış", "orta seviye", "kıdemli", "departman yöneticisi"]
PERSONA_RUH = ["aceleci", "sakin", "yorgun", "dikkatli", "umursamaz", "stresli"]
PERSONA_YAZIM = [
    "düzgün yazım",
    "hep küçük harf, noktalama az",
    "arada ufak yazım/daktilo hatası olan",
    "kısa ve devrik cümleli",
    "resmi ve düzgün noktalamalı",
]


def persona_uret() -> dict:
    return {
        "rol": random.choice(PERSONA_ROL),
        "kidem": random.choice(PERSONA_KIDEM),
        "ruh": random.choice(PERSONA_RUH),
        "yazim": random.choice(PERSONA_YAZIM),
    }


def persona_metni(p: dict) -> str:
    return (f"{p['kidem']} bir {p['rol']}sın, şu an {p['ruh']} bir ruh halindesin; "
            f"yazım tarzın: {p['yazim']}")

# Kategoriye özel örnekleme sıcaklığı. yeterli fişteki gerçek kalemlere
# dayanmalı -> düşük temp uydurma/halüsinasyonu azaltır. Diğerleri çeşitlilik için
# 1.1'e çekildi: min_p (0.1) güvenlik ağı sayesinde yüksek sıcaklıkta bile
# tutarlılık korunur, çeşitlilik artar (bkz. min-p sampling, arXiv:2407.01082).
KATEGORI_SICAKLIK = {
    "yeterli": 0.6,
    "yetersiz": 1.1,
    "manipulatif": 1.1,
    "ai_uretimi": 1.1,
}


def baskin_kategori(kalemler: list[dict]) -> str:
    """Fişteki kalemlerin en sık geçen harcama kategorisi (baskın tema).
    yeterli prompt'unu çıpalamak için kullanılır."""
    sayac = Counter(k["harcama_kategorisi"] for k in kalemler)
    return sayac.most_common(1)[0][0]


def prompt_olustur(fatura: dict, kategori: str, anomali_turleri: list[str] | None = None) -> tuple[str, str, dict]:
    """
    (system_prompt, user_prompt, meta) döndürür. `meta`, kategoriye özel
    beklentiyi taşır (ai_uretimi için seçilen `kapanis`, manipulatif için
    `gizlenecek` kalem) -- böylece ihlalleri_bul() üretilen metni gerçek
    beklentiye karşı denetleyebilir.

    `anomali_turleri`: faturanın etiketindeki anomali listesi. manipulatif
    dalının hangi alt-üslubu seçeceğini belirler (bkz. aşağıdaki yorum).
    """
    anomali_turleri = anomali_turleri or []
    kalem_ozeti = kalemler_ozetle_prompt(fatura["kalemler"])  # insan-okur (enum sızıntısını keser)
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    meta: dict = {}

    # Kategoriden bağımsız uzunluk hedefi seç; hem prompt'a yaz hem meta'ya koy
    # (ihlalleri_bul uzunluk denetimini bu hedefe göre yapsın -- kategori-uzunluk
    # sahte korelasyonunu kırar).
    uz_ad, uz_tarif, uz_alt, uz_ust = uzunluk_hedefi_sec(kategori)
    meta["uzunluk"] = (uz_ad, uz_alt, uz_ust)

    # SISTEM PROMPTU (Sabit -> Ollama önbelleğe alır). whaatif.py'nin karakter-
    # daldırma çerçevesi esas alındı: dört karakterin KONTRASTLI tanımı, modele her
    # kategorinin sesini VE diğerlerinden farkını öğretir (kural listesi tek başına
    # bunu vermiyordu). Bizim kurallarımız (fişteki kalemler, rakam yok, pasif kalıp
    # yasağı) korunur. Kategoriye özel ayrıntı user prompt'taki 'Talimat'ta.
    system_prompt = (
        # "Sen bir yapay zeka DEĞİLSİN. 
        "Sen, ay sonu geldiği için masraf sistemine fiş giren "
        "gerçek bir şirket çalışanısın. Sana verilen KARAKTER tipine tam olarak bürüneceksin; "
        "bu dört karakter birbirinden kesinlikle farklı insanlar gibi konuşur:\n"
        "1) YETERLİ çalışan → işini düzgün yapan, ne için harcadığını rahatça söyleyen, kendinden emin biri.\n"
        "2) YETERSİZ çalışan → tembel, aceleci, detay vermeyi dert etmeyen, umursamaz biri (yalan söylemez, sadece anlatmaya üşenir).\n"
        "3) MANİPÜLATİF çalışan → bir şeyi bilerek gizleyen ya da abartıyla haklı çıkaran, sahte kurumsal gerekçe uyduran kurnaz biri.\n"
        "4) AI_URETIMI → tek istisna: insan değil, resmi ve robotik bir yapay zeka gibi yazacaksın.\n"
        "Sana hangi karakter verildiyse SADECE onun gibi yaz; diğer üç karakterin üslubuna asla kayma "
        "(yetersiz asla amaç açıklamaz, yeterli asla amacı gizlemez, manipülatif asla gerçek/aykırı kalemi açıkça söylemez).\n"
        "KURALLAR:\n"
        "- Yalnızca fişte gerçekten bulunan kalemlerden söz et; tutar/rakam yazma.\n"
        "- Açıklama HARİCİNDE hiçbir şey yazma ('İşte açıklama:', 'Tabii', 'Açıklama:' YASAK).\n"
        "- Tamamen Türkçe konuş; İngilizce kelime kullanma.\n"
        "- (AI_URETIMI hariç) edilgen/resmi kalıplar (edildi, edilmiştir, sağlanmıştır, karşılanmıştır) KULLANMA; "
        "birinci tekil şahıs, doğal ve konuşma diline yakın yaz.\n"
        "- İki karaktere birden benzeyen belirsiz cümle kurma; net bir karaktere gir.\n"
        "- Firma/satıcı adını kaynak olarak kullan, doğru '-dan/-den/-tan/-ten' ekiyle: "
        "'ABC Yazılım'dan lisans aldık', 'XYZ Market'ten malzeme aldım' gibi.\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        baskin = baskin_kategori(fatura["kalemler"]).replace("_", " ")
        talimat = (
            f"KARAKTER: YETERLİ çalışan. Harcamanın İŞ AMACINI (kiminle, ne için) net ve kendinden emin "
            f"söyle - saklayacak bir şeyin yok. Fişteki gerçek kalemi ÇALIŞAN gibi SADELEŞTİREREK an "
            f"('F Saff Sıvı El Sabunu 500Ml' -> 'el sabunu'; 'Samsung Galaxy S23 256GB Siyah' -> 'Samsung S23'); "
            f"boyut/miktar/model detayı yazma, kategori adını ('{baskin}') amaç yerine kullanma. "
            f"Birinci tekil şahıs, doğal çalışan notu; edilgen/resmi kalıp kullanma. Üslup: {uslup}. "
            f"Bu bir YETERSİZ not DEĞİL: amacı belirsiz bırakma. Bu bir MANİPÜLATİF not da DEĞİL: gerçek "
            f"amacı savunmaya geçmeden söyle. Firma: {firma_kisa}."
        )

    elif kategori == "yetersiz":
        uslup = random.choice(YETERSIZ_USLUP_IPUCLARI)
        ornekler = ", ".join(f"'{o}'" for o in random.sample(YETERSIZ_ORNEK_HAVUZ, 2))
        talimat = (
            f"KARAKTER: YETERSİZ çalışan. Baştan savma, muğlak, geçiştirmelik bir not yaz. İşi kiminle/neden "
            f"yaptığını ASLA söyleme - ama gizlemeye de uğraşma, sadece yazmaya üşen. Üslup: {uslup}. "
            f"Kuru bir öbek ('genel gider') ya da umursamaz bir söz ('gerekliydi aldım işte') olabilir, ör. {ornekler} "
            f"(birebir kopyalama, tarzını yakala). Bu bir MANİPÜLATİF not DEĞİL: kurnaz gerekçe uydurma, sahte "
            f"kılıf yok. Firma: {firma_kisa} (istersen kullanma)."
        )
    elif kategori == "manipulatif":
        # Gizleme dalı SADECE gerçekten gizlenecek bir şey varsa çalışmalı:
        #   (a) politika yasağı olan kalem (alkol/kumar/tütün/eğlence), veya
        #   (b) is_kolu_kategori_uyumsuzlugu -> faturaya aykırı düşen kalem.
        # Aksi halde (temiz fatura ya da salt sayısal anomali) "aykırı kalem"
        # meşru bir kategori olur; onu gizlemek manipülatif değil, sıradan bir
        # açıklama üretir (bkz. #13). O durumda "aşırı haklı çıkarma" dalına git.
        yasakli = yasakli_kalem_bul(fatura["kalemler"])
        if yasakli:
            gizlenecek = yasakli
        elif "is_kolu_kategori_uyumsuzlugu" in anomali_turleri:
            gizlenecek = aykiri_kalem_bul(fatura["kalemler"])
        else:
            gizlenecek = None
        meta["gizlenecek"] = gizlenecek

        if gizlenecek:
            gizli_kat = gizlenecek["harcama_kategorisi"].replace("_", " ")
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu fişte '{gizlenecek['aciklama']}' ({gizli_kat}) var ve "
                f"şirket bunu ödemez/uygun görmez. Görevin bunu kurnazca GİZLEMEK! 'Müşteri ağırlama', "
                f"'temsil gideri', 'toplantı gideri' gibi aşırı kurumsal kılıflar uydur. Gerçek kalemin adını "
                f"ya da türünü ('{gizli_kat}') hiçbir formda geçirme. Bu bir YETERLİ not DEĞİL: gerçek amacı "
                f"gizliyorsun. Bu bir YETERSİZ not da DEĞİL: kısa geçiştirmiyorsun, iddialı bir kılıf kuruyorsun. "
                f"Kendinden aşırı emin, sahte kurumsal dille yaz ama pasif 'edilmiştir' kalıbı kullanma. "
                f"Satıcı: {firma_kisa}."
            )
        elif random.random() < 0.5:
            # BARİZ dal: abartılı savunmacı + zorunlu vurgu (konum meta ile denetlenir).
            vurgu = random.choice(MANIPULATIF_VURGU_IPUCLARI)
            konum = random.choice(MANIPULATIF_VURGU_KONUMLARI)
            meta["vurgu"] = vurgu
            meta["vurgu_konum"] = konum
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Masraf aslında normal ama sen bunu abartılı ve gereksiz "
                f"savunmacı yaz; sanki suçluluk duyuyormuşsun gibi ikna etmeye çalış. '{vurgu}' gibi abartılı "
                f"bir vurguyu {konum} yerleştir ama KENDİ cümlenle kur, ezber kalıbı ('yüzde yüz işle "
                f"alakalıdır' gibi) aynen tekrarlama. Bu bir YETERLİ not DEĞİL: sakin değilsin, gereğinden "
                f"fazla ısrarcısın. Kusur/aykırılık/prosedürden bahsetme. Sadece açıklama metnini yaz."
            )
        else:
            # KURNAZ dal: abartılı vurgu YOK; sıradan masrafı gereksiz-kurumsal bir kılıfla önemli
            # gösteren, sakin/şişirilmiş üslup. Sınırda (yeterliye yakın) -> ayırt etmeyi öğretir.
            talimat = (
                "KARAKTER: MANİPÜLATİF çalışan. Masraf aslında sorunsuz. Onu SAKİN ama kurnazca, sıradan bir "
                "alışverişi ÖNEMLİ bir iş kararıymış gibi gösteren gereksiz-kurumsal bir kılıfla haklı çıkar "
                "(ör. 'stratejik değerlendirme toplantısı kapsamında', 'temsil gideri', 'iş geliştirme amaçlı "
                "ağırlama'). Abartılı ünlem/vurgu KULLANMA; masrafı gereğinden büyük ve resmi göster. Birinci "
                "tekil şahıs ya da kısa kurumsal öbek; 'edilmiştir' gibi pasif kalıp YAZMA. Bu bir YETERLİ not "
                "DEĞİL: sıradan gerekçe değil, şişirilmiş kurumsal kılıf. Kusur/prosedürden bahsetme. "
                "Sadece açıklama metnini yaz."
            )
    else:  # ai_uretimi
        kapanis = random.choice(AI_URETIMI_KAPANIS_IPUCLARI)
        acilis = random.choice(AI_URETIMI_ACILIS_IPUCLARI)
        meta["kapanis"] = kapanis
        talimat = (
            "KARAKTER: AI_URETIMI (tek istisna). İnsan değil, ChatGPT gibi bir yapay zeka gibi yaz: duygusuz, "
            "aşırı resmi, kalıpsal/şablon. Diğer üçünün aksine doğallık ARANMIYOR; bariz yapay/robotik dursun. "
            f"Cümleye '{acilis}' gibi bir açılışla başla ve '...{kapanis}' ifadesiyle bitir; verilen açılış/"
            f"kapanışı KULLAN, hep aynı kalıbı ('Belirtilen fiş...') tekrarlama. Kısa tek cümle de olur, resmi "
            f"1-2 cümle de -- 3+ cümlelik paragraf yazma. Satıcı/firma adını ASLA kullanma."
        )

    # Few-shot SADECE user prompt'a girer (system prompt sabit -> Ollama önbelleği
    # korunur). yetersiz'de few-shot KULLANILMAZ: talimattaki dönüşümlü örneklerle
    # çakışıp verbatim kopyayı besliyordu ('genel ofis ihtiyacı' collapse'ı).
    fs_blok = "" if kategori == "yetersiz" else fewshot_blok(kategori, adet=2)

    # Persona da user prompt'ta; ai_uretimi hariç (robotik/kişiliksiz kalmalı).
    persona_notu = "" if kategori == "ai_uretimi" else f"Yazan kişi: {persona_metni(persona_uret())}.\n"

    # USER PROMPTU (Sadece değişen kısım) + persona + kategoriden bağımsız uzunluk hedefi.
    user_prompt = (
        f"Satıcı/Firma: {firma_kisa}\nFiş kalemleri: {kalem_ozeti}\n{fs_blok}{persona_notu}"
        f"Talimat: {talimat}\nUzunluk hedefi: {uz_tarif}."
    )

    return system_prompt, user_prompt, meta


# Session oluştur (Global seviyede) -- keep-alive bağlantı havuzu için
http_session = requests.Session()

# ---------------------------------------------------------------------------
# Çıktı temizleme: model bazen <think>, düşünce önsözü ('Tamam, ...'), ön-ek
# ('Açıklama:') ya da tırnak/madde işareti sarması ekliyor. Bunları ayıklamak
# bozuk çıktı kaynaklı gereksiz retry'ı azaltır (hem kalite hem hız kazancı).
# ---------------------------------------------------------------------------

_THINK_REGEX = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_DUSUNCE_ONSOZ = re.compile(
    r"^\s*(okay|alright|let me|let's|sure|first|tamam|bakalım|öncelikle|"
    r"hmm|well|i need to|i should|i'll|the user|kullanıcı)\b.*",
    re.IGNORECASE,
)
_ON_EK_TEMIZLE = re.compile(r"^(Açıklama|Not|Masraf Açıklaması|İşte.*?|Cevap)[\s:]*", re.IGNORECASE)


def cikti_temizle(metin: str) -> str:
    """Ham yanıttan düşünce/ön-ek/sarma temizler, ilk anlamlı satırı döndürür.
    Açıklamalar tek cümlelik kısa notlar olduğundan tek satır dönmek güvenli."""
    metin = _THINK_REGEX.sub("", metin).strip()
    satirlar = [s.strip() for s in metin.splitlines() if s.strip()]
    temiz = [s for s in satirlar if not _DUSUNCE_ONSOZ.match(s)]
    aday_liste = temiz or satirlar
    for s in aday_liste:
        s2 = _ON_EK_TEMIZLE.sub("", s).strip().strip('"\'“”‘’*`>-').strip()
        if len(s2) >= 3 and re.search(r"[a-zçğıöşüA-ZÇĞİÖŞÜ]{2,}", s2):
            return s2
    if aday_liste:
        return _ON_EK_TEMIZLE.sub("", aday_liste[-1]).strip().strip('"\'“”‘’*`>-').strip()
    return ""


def _red_mi(metin: str) -> bool:
    """Model üretmek yerine reddetmiş mi (moderasyon/güvenlik refleksi)?"""
    d = metin.lower()
    return any(k in d for k in RED_KALIPLARI)


def ollama_cagir(
    system_prompt: str,
    user_prompt: str,
    model: str,
    host: str,
    num_predict: int = 90,
    keep_alive: str | int | None = None,
    temperature: float = 0.9,
    seed: int | None = None,
    min_p: float = 0.1,
    stop: list[str] | None = None,
    ham: bool = False,
    num_ctx: int = 2048,
) -> str:
    istek_govdesi: dict = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "think": False,  # Modeli ne olursa olsun düşünme sürecini kapattık
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
            "num_ctx": num_ctx,
            # Yerel model için önerilen: temperature + min_p (top_p'den daha sağlam;
            # yüksek sıcaklıkta aday kümesini şişirmez -> çeşitlilik ↑ tutarlılık korunur,
            # bkz. arXiv:2407.01082). min_p kategoriye göre geçilir (yeterli 0.05, diğerleri 0.1).
            # repeat_penalty tekrarı kırar. seed None ise Ollama rastgele seçer; retry'da
            # farklı seed vererek gerçekten farklı üretim.
            "top_p": 0.95,
            "min_p": min_p,
            "repeat_penalty": 1.15,
        },
    }
    if seed is not None:
        istek_govdesi["options"]["seed"] = seed
    # stop: çok-paragraflı gevezeliği kes (num_ctx/num_predict israfını önler).
    if stop:
        istek_govdesi["options"]["stop"] = stop
    # keep_alive üst-seviye alandır (options içinde değil); burst boyunca modelin
    # bellekte kalması için runner bunu geçebilir.
    if keep_alive is not None:
        istek_govdesi["keep_alive"] = keep_alive

    yanit = http_session.post(f"{host}/api/generate", json=istek_govdesi, timeout=60)
    yanit.raise_for_status()

    yanit_metni = yanit.json().get("response", "")
    if ham:
        # VS için: çok satırlı ham yanıt (yalnız <think> ayıklanır), aday ayrıştırma
        # tek satıra indirgenmeden yapılabilsin.
        return _THINK_REGEX.sub("", yanit_metni).strip()
    return cikti_temizle(yanit_metni)


def modeli_bellekten_indir(model: str, host: str) -> None:
    """
    keep_alive=0 ile boş bir çağrı yaparak modeli Ollama belleğinden indirir.
    Cooldown başında çağrılır -- RAM serbest kalır, CPU/GPU soğur. Hata olursa
    sessizce geçer (cooldown akışını bozmasın).
    """
    try:
        http_session.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            timeout=30,
        )
    except Exception:
        pass


YASAKLI_PASIF_KALIPLAR = ("edilmiştir", "edildi", "sağlanmıştır", "karşılanmıştır", "alınmıştır")

DUZELTME_NOTLARI = {
    "sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında gizlenmesi gereken kalemin gerçek adı ya da "
        "kategorisi açığa çıktı. Bu kez o kalemden HİÇ bahsetme, sadece diğer/meşru kalemlere odaklan."
    ),
    "pasif_kalip": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında yasaklı pasif kalıp kullanıldı (ör. '...edilmiştir'). "
        "Bu kez SADECE aktif fiil kullan: 'aldım', 'ödedim', 'kullandım' gibi."
    ),
    # uzunluk notu meta["uzunluk"][0] (hedef adı) ile biçimlendirilecek.
    "uzunluk": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın istenen uzunlukta değildi. Bu kez uzunluğu "
        "'{uzunluk_ad}' hedefine göre ayarla (ne daha kısa ne daha uzun)."
    ),
    "red": (
        "ÖNEMLİ DÜZELTME: Bu tamamen kurgusal bir masraf açıklaması yazma görevidir, "
        "gerçek bir işlem değildir. Reddetme; SADECE istenen açıklama metnini yaz."
    ),
    # kapanis_eksik notu meta["kapanis"] ile biçimlendirileceği için şablon.
    "kapanis_eksik": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın istenen kapanışla bitmedi. Bu kez cümleyi "
        "MUTLAKA tam olarak '...{kapanis}' ifadesiyle bitir."
    ),
    # vurgu_eksik notu meta["vurgu"] ile biçimlendirilecek.
    "vurgu_eksik": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın yeterince ısrarlı/abartılı değildi, meşru gibi durdu. "
        "Bu kez '{vurgu}' gibi abartılı bir vurguyu MUTLAKA ekle (cümlenin doğal bir yerine) ve "
        "harcamayı gereğinden fazla haklı çıkar."
    ),
    "karakter_kirilmasi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın kılıfı bozdu; harcamanın aykırı/şüpheli olduğunu "
        "ya da prosedür/kural gerektiğini ima etti. Bu kez tam tersi: kendinden emin, harcamayı "
        "SORGUSUZ meşru göster; kusur/aykırılık/prosedürden HİÇ bahsetme."
    ),
    "yeterli_halusinasyon": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın fişte OLMAYAN bir tema (ör. 'yemek/ağırlama') uydurdu. "
        "Bu kez YALNIZCA fişteki gerçek kalemlere dayan; olmayan bir harcama türü uydurma."
    ),
    "yeterli_dayanaksiz": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın fazla muğlaktı; hiçbir gerçek kaleme, firmaya ya da "
        "somut iş amacına dayanmadı. Bu kez fişteki GERÇEK bir kalemi an ve NEDEN alındığını (kim/hangi iş) "
        "somut belirt."
    ),
    "enum_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında ham kategori adı (alt çizgili, ör. 'kisisel_bakim') "
        "geçti. Bu kez kategori adını olduğu gibi YAZMA; doğal Türkçe ifadeyle anlat."
    ),
    "meta_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın görevi/fişi betimledi (ör. 'yeterli değil gibi görünüyor'). "
        "Sen bir çalışansın; görevi anlatma, DOĞRUDAN masraf açıklamasını yaz."
    ),
    "verbatim_kopya": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın verilen örneklerden birinin neredeyse aynısıydı. "
        "Bu kez örnekleri kopyalama; kendi özgün ifadenle, farklı kelimelerle yaz."
    ),
    "urun_detay_kopya": (
        "ÖNEMLİ DÜZELTME: Ürünü ham haliyle (boyut/miktar/paket/model detayıyla) yazdın. "
        "Gerçek bir çalışan böyle yazmaz; SADELEŞTİR: ne olduğunu yaz (ör. 'el sabunu', "
        "'kupa seti', 'sabun'), '500Ml', '4'lü paket', renk/model gibi detayları ATLA."
    ),
    "firma_icin_hatasi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında firma adının ardından '... için' geldi, bu satın "
        "alınan yeri değil amacı gösterir. Firmayı kaynak olarak yaz: 'ABC Yazılım'dan lisans aldık', "
        "'XYZ Market'ten malzeme aldım' gibi doğru '-dan/-den/-tan/-ten' ekiyle."
    ),
}


_TR_HARF_MAP = str.maketrans({
    "ğ": "g", "ü": "u", "ş": "s", "ı": "i", "ö": "o", "ç": "c",
    "â": "a", "î": "i", "û": "u", "İ": "i",
})


def _tr_normalize(s: str) -> str:
    """Türkçe karakterleri ASCII'ye indirger + küçük harfe çevirir. Kategori
    adları ASCII saklanırken (eglence) modelin doğru Türkçe (eğlence) yazması
    arasındaki eşleşme farkını kapatır."""
    return s.lower().translate(_TR_HARF_MAP)


def _kok(token: str) -> str:
    """Kaba kök: 4 harften uzun bir kelimenin sondaki tek sesli ekini atar
    ('hizmeti' -> 'hizmet'), böylece çekim ekli biçimler ('hizmetleri') de
    substring olarak yakalanır."""
    return token[:-1] if len(token) > 4 and token[-1] in "iıuüea" else token


# ---------------------------------------------------------------------------
# DEDUP + ÇEŞİTLİLİK: normalize edip token-jaccard ile yakın-kopya tespit,
# distinct-1/2 çeşitlilik metriği. Harici bağımlılık yok; mevcut _tr_normalize'ı
# yeniden kullanır. Tüketiciler (runner/pilot) mode collapse'ı ölçmek/işaretlemek
# için kullanır -- üretim akışına maliyet bindirmez.
# ---------------------------------------------------------------------------

def _dedup_normalize(s: str) -> str:
    s = _tr_normalize(s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_set(s: str) -> set[str]:
    return set(_dedup_normalize(s).split())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    birlesim = len(a | b)
    return len(a & b) / birlesim if birlesim else 0.0


def yakin_kopya_mi(yeni: str, kabul_edilenler: list[set[str]], esik: float = 0.8) -> bool:
    yt = _token_set(yeni)
    return any(jaccard(yt, var_olan) >= esik for var_olan in kabul_edilenler)


def distinct_n(metinler: list[str], n: int) -> float:
    """distinct-n: benzersiz n-gram / toplam n-gram. 1'e yakın = çeşitli,
    0'a yakın = tekrarlı/mode collapse."""
    toplam, benzersiz = 0, set()
    for m in metinler:
        tok = _dedup_normalize(m).split()
        grams = [tuple(tok[i:i + n]) for i in range(len(tok) - n + 1)]
        toplam += len(grams)
        benzersiz.update(grams)
    return len(benzersiz) / toplam if toplam else 0.0


def _sizinti_var_mi(metin: str, gizlenecek: dict) -> bool:
    """Gizlenmesi gereken kalemin kategorisi ya da ürün adı metne sızmış mı?
    Türkçe morfoloji/diakritik farklarına dayanıklı kök-token eşleşmesi kullanır."""
    n = _tr_normalize(metin)
    # 1) Kategori: tüm kök token'ları metinde geçiyorsa sızıntı (ör. yemek_hizmeti
    #    -> 'yemek' VE 'hizmet' ikisi de varsa). Tümünü şart koşmak yanlış pozitifi azaltır.
    kategori_kokler = [_kok(t) for t in _tr_normalize(gizlenecek["harcama_kategorisi"]).split("_") if t]
    if kategori_kokler and all(kok in n for kok in kategori_kokler):
        return True
    # 2) Ürün adı: anlamlı (>3 harf) kelimelerinden herhangi biri geçiyorsa sızıntı.
    for kelime in _tr_normalize(gizlenecek["aciklama"]).split():
        if len(kelime) > 3 and kelime in n:
            return True
    return False


def _kapanis_var_mi_herhangi(metin: str) -> bool:
    """Metin havuzdaki AI-kapanışlarından HERHANGİ birini İÇERİYOR mu? Eskiden
    'ile bitiyor mu' idi; ai artık tek cümle olduğundan kapanış genelde sonda gelir
    ama 'içerir' esnekliği (ör. kapanıştan sonra minik bir ek) boşuna retry'ı önler.
    Kapanış işareti (formal AI kalıbı) metinde geçtiği sürece ai_uretimi sinyali korunur."""
    normalize = metin.lower()
    return any(k.lower() in normalize for k in AI_URETIMI_KAPANIS_IPUCLARI)


def _vurgu_var_mi(metin: str, vurgu: str | None = None) -> bool:
    """manipulatif 'aşırı haklı çıkarma' metninde abartılı vurgu işareti var mı?
    Önce ATANAN vurgunun ayırt edici kelimeleri aranır (havuz genişledikçe kural
    otomatik uyum sağlar), sonra genel emphatic-marker fallback denetlenir."""
    n = _tr_normalize(metin)
    if vurgu:
        vurgu_kok = [_tr_normalize(w) for w in vurgu.split() if len(w) > 3]
        if vurgu_kok and any(k in n for k in vurgu_kok):
            return True
    return any(anahtar in n for anahtar in _VURGU_ANAHTARLARI)


# --- Faz 2: ek kural-tabanlı ihlal denetimleri (judge yerine) --------------

def _enum_sizinti_var_mi(metin: str, kalemler: list[dict]) -> bool:
    """Ham kategori enum adı ('kisisel_bakim' gibi alt çizgili) açıklamaya sızmış mı?
    Model bazen etiketi olduğu gibi yazıyor -> sınıflandırıcıya bedava sinyal (#48)."""
    d = metin.lower()
    return any("_" in k["harcama_kategorisi"] and k["harcama_kategorisi"] in d for k in kalemler)


# Görev/etiketi betimleyen (rol yapmayı bırakan) kalıplar. ai_uretimi'nin meşru
# resmi dilini ('fiş kapsamında', 'fiş içeriğinde') YAKALAMAYACAK kadar dar tutuldu.
_META_SIZINTI_KALIPLARI = (
    "yeterli degil", "yetersiz degil", "gibi gorunuyor", "gibi duruyor",
    "aciklama yaz", "aciklama gir", "bu bir aciklama", "bu bir yeterli",
    "bu bir yetersiz", "bu bir manipulat", "aciklama metni",
)


def _meta_sizinti_var_mi(metin: str) -> bool:
    """Model rol yapmayı bırakıp görevi/etiketi betimlemiş mi? (#12: 'masraf fişi
    içeriği yeterli değilmiş gibi görünüyor')."""
    n = _tr_normalize(metin)
    return any(k in n for k in _META_SIZINTI_KALIPLARI)


# manipulatif KARAKTER KIRILMASI: kılıfı bozup ihlali itiraf/prosedür anlatma
# (#92 'politikalarına aykırıdır', #56 'yazılı olması gerekmektedir', #93 'fazla mı').
_MANIP_KIRILMA_KALIPLARI = (
    "aykiri", "yazili olmasi", "fazla mi", "uygun degil", "supheli",
    "odenmemeli", "sorgulan", "prosedur", "politikalara ayk",
)


def _karakter_kirilmasi_mi(metin: str) -> bool:
    n = _tr_normalize(metin)
    return any(k in n for k in _MANIP_KIRILMA_KALIPLARI)


# yeterli HALÜSİNASYON: fişte gerçekten olmayan bir tema uydurma. Gözlenen baskın
# hata "müşteri yemeği" dolgusu (#75 teknoloji, #94 kişisel bakım fişinde). Tema
# kelimesi geçiyor ama fişte ilgili kategori YOKSA halüsinasyon.
# NOT: "ağırlama/ikram" YEMEK teması SAYILMAZ -- 'müşteri ağırlamak' gıda gerektirmeyen
# meşru bir iş amacıdır (bkz. iyi örnek #66: ağırlama için araç kiralama). Yalnız açık
# yemek kelimeleri tetikler.
_YEMEK_TEMA = ("yemek", "yemegi", "yedik", "kahvalti", "restoran")
_YEMEK_KATEGORILERI = {"yemek_hizmeti", "konaklama", "temel_gida"}


def _yeterli_halusinasyon_mi(metin: str, kalemler: list[dict]) -> bool:
    n = _tr_normalize(metin)
    if not any(t in n for t in _YEMEK_TEMA):
        return False
    return not any(k["harcama_kategorisi"] in _YEMEK_KATEGORILERI for k in kalemler)


def _yeterli_dayanaksiz_mi(metin: str, fatura: dict) -> bool:
    """yeterli açıklama en azından BİR çıpaya dayanmalı: gerçek bir kalem kelimesi,
    firma adı ya da bir iş-amacı ismi (proje/ekip/müşteri...). Hiçbiri yoksa muğlak/
    dayanaksız -> pratikte yetersiz'e kayıyor demektir. Cömert: yalnız hepten boş
    ('gerekli olduğu için aldım' gibi) durumları yakalar, kısa yeterliyi cezalandırmaz."""
    n = _tr_normalize(metin)
    n_tok = set(n.split())
    # 1) iş-amacı ismi
    if any(a in n for a in _YETERLI_AMAC_ISIMLERI):
        return False
    # 2) firma adı token'ı
    firma = _tr_normalize(firma_adi_kisalt(fatura["satici_unvan"]))
    if any(len(t) > 3 and t in n for t in firma.split()):
        return False
    # 3) gerçek kalem kelimesi
    for k in fatura["kalemler"]:
        for kel in _tr_normalize(k["aciklama"]).split():
            if len(kel) > 3 and kel in n_tok:
                return False
    return True


# İNSAN-vs-AI AYRACI: gerçek bir çalışan ürünü SADELEŞTİRİR ('F Saff Sıvı El Sabunu
# Meyve Aromalı 500Ml' -> 'el sabunu'); boyut/miktar/paket/model gibi detayları elle
# yazmaz. AI ise ham ürün adını olduğu gibi taşır. Bu "ürün-detay gürültüsü" 3 insan
# kategorisinde ihlaldir; ai_uretimi'nde serbesttir (kasıtlı -> ayraç sinyali).
# Servis adlarında (iş sağlığı ve güvenliği hizmeti vb.) YANLIŞ POZİTİF olmasın diye
# yalnız sayı+birim/paket gürültüsüne bakılır, kelime uzunluğuna değil.
_URUN_DETAY_GURULTU = re.compile(
    r"\b\d+[.,]?\d*\s*(?:ml|cl|gr|kg|lt|cm|mm|mah|watt|kwh|kw|gb|tb|mb|inç|inch|dpi|hz|w)\b"
    r"|\b\d+\s*['’´`]?\s*l[iıuü]\b"      # 4'lü, 3 li, 5lu (paket-adet)
    r"|\bpkt\b",
    re.IGNORECASE,
)


def _urun_detay_kopya_mi(metin: str) -> bool:
    """Açıklamada ham ürün detayı (boyut/miktar/paket: '500Ml', '4 lu', '750 gr',
    '120w', 'pkt') geçiyor mu? İnsan kategorilerinde bu, çalışanın değil AI'ın
    ham kalem adını kopyaladığının işaretidir."""
    return bool(_URUN_DETAY_GURULTU.search(metin))


def _firma_icin_hatasi_mi(metin: str, fatura: dict) -> bool:
    """Firma adının hemen ardından 'için' gelmesi ('ABC için X aldık' gibi),
    doğru kaynak ekinin (-dan/-den/-tan/-ten) yerine yanlışlıkla amaç eki
    kullanıldığını gösterir (bkz. sistem prompt kuralı). Firma adı kısaltılmış
    hâliyle (firma_adi_kisalt) aranır, Türkçe-dayanıklı normalize ile (_tr_normalize)."""
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    if not firma_kisa:
        return False
    metin_norm = _tr_normalize(metin)
    firma_norm = _tr_normalize(firma_kisa)
    desen = re.escape(firma_norm) + r"\W{0,3}icin\b"
    return re.search(desen, metin_norm) is not None


def _verbatim_kopya_mi(metin: str, kategori: str) -> bool:
    """Üretilen metin, prompt'ta gösterilen FEW-SHOT örneklerinden birine neredeyse
    birebir mi? Stil demirleme yerine kopya -> çeşitlilik ölür. yetersiz'e UYGULANMAZ:
    kısa muğlak öbekler ('iş gideri') doğal olarak örnek havuzuyla örtüşür, bu geçerli."""
    if kategori == "yetersiz":
        return False
    havuz = FEWSHOT.get(kategori, [])
    if not havuz:
        return False
    mt = _token_set(metin)
    return any(jaccard(mt, _token_set(o)) >= 0.9 for o in havuz)


def ihlalleri_bul(metin: str, kategori: str, fatura: dict, meta: dict | None = None) -> list[str]:
    meta = meta or {}
    ihlaller = []

    kalemler = fatura["kalemler"]

    if kategori == "manipulatif":
        # meta yoksa (geriye dönük çağrı) gizlenecek'i yeniden hesapla; ama meta
        # varsa onu kullan -- çünkü prompt_olustur gizleme dalını yalnızca gerçek
        # gizlenecek durumda seçer (temizde gizlenecek=None olur, sızıntı aranmaz).
        gizlenecek = meta["gizlenecek"] if "gizlenecek" in meta else gizlenecek_kalem_bul(kalemler)
        if gizlenecek and _sizinti_var_mi(metin, gizlenecek):
            ihlaller.append("sizinti")
        # "aşırı haklı çıkarma" dalında (meta["vurgu"] varsa) zorunlu vurgu var mı?
        if meta.get("vurgu") and not _vurgu_var_mi(metin, meta.get("vurgu")):
            ihlaller.append("vurgu_eksik")
        # Faz 2: kılıfı bozup ihlali itiraf/prosedür anlatma -> manipülatif niyeti bozar.
        if _karakter_kirilmasi_mi(metin):
            ihlaller.append("karakter_kirilmasi")

    # Pasif/resmi kalıp yasağı: Faz 2 ile manipulatif'e de genişletildi (pasif kalıp
    # manipülatifi ai_uretimi'ne benzetip sınıf sınırını bulanıklaştırıyordu).
    if kategori in ("yeterli", "yetersiz", "manipulatif") and any(k in metin.lower() for k in YASAKLI_PASIF_KALIPLAR):
        ihlaller.append("pasif_kalip")

    # ai_uretimi: havuzdaki HERHANGİ bir AI-kapanışıyla bitmesi yeter (tek belirli
    # olanı dayatmıyoruz -> gereksiz retry azalır, çeşitlilik prompt'tan gelir).
    if kategori == "ai_uretimi" and not _kapanis_var_mi_herhangi(metin):
        ihlaller.append("kapanis_eksik")

    # Faz 2: yeterli halüsinasyonu (fişte olmayan 'yemek/ağırlama' teması uydurma).
    if kategori == "yeterli" and _yeterli_halusinasyon_mi(metin, kalemler):
        ihlaller.append("yeterli_halusinasyon")

    # Faz 3: yeterli dayanak kontrolü (gerçek kalem/firma/iş-amacı çıpası yoksa muğlak).
    if kategori == "yeterli" and _yeterli_dayanaksiz_mi(metin, fatura):
        ihlaller.append("yeterli_dayanaksiz")

    # Faz 6: ürün-detay kopyası -- SADECE insan kategorileri (yeterli/yetersiz/
    # manipulatif). ai_uretimi serbest (ham ürün adını taşıması AI ayracıdır).
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _urun_detay_kopya_mi(metin):
        ihlaller.append("urun_detay_kopya")

    # Faz 2: kategoriden BAĞIMSIZ denetimler (her kategori için).
    if _enum_sizinti_var_mi(metin, kalemler):
        ihlaller.append("enum_sizinti")
    if _meta_sizinti_var_mi(metin):
        ihlaller.append("meta_sizinti")
    if _verbatim_kopya_mi(metin, kategori):
        ihlaller.append("verbatim_kopya")
    if _firma_icin_hatasi_mi(metin, fatura):
        ihlaller.append("firma_icin_hatasi")

    # Uzunluk: kategoriye özel SABİT sınır yerine, prompt_olustur'un bu faturaya
    # atadığı kategoriden-bağımsız uzunluk hedefine göre denetle (kategori-uzunluk
    # sahte korelasyonu kırılır). meta yoksa (geriye dönük çağrı) esnek bir aralık.
    uz = meta.get("uzunluk")
    if uz:
        _ad, uz_alt, uz_ust = uz
    else:
        uz_alt, uz_ust = (8 if kategori == "yetersiz" else 15), 230
    # yetersiz doğal olarak çok kısa olabilir (muğlaklık amaç) -> alt sınırı zorlama,
    # yalnız aşırı uzunu ele. Bu, iyi kısa yetersiz çıktılarındaki boşuna flag'i keser.
    if kategori == "yetersiz":
        uz_alt = 6
    # Üst sınıra küçük tolerans: rastgele hedef ile modelin doğal uzunluğu arasındaki
    # ufak sapmalarda gereksiz retry olmasın (flag gürültüsünü azaltır).
    uz_ust_tol = int(uz_ust * 1.15)
    if not (uz_alt <= len(metin) <= uz_ust_tol):
        ihlaller.append("uzunluk")

    return ihlaller


def duzeltme_notu_uret(ihlaller: list[str], meta: dict | None = None) -> str:
    meta = meta or {}
    notlar = []
    for i in ihlaller:
        if i not in DUZELTME_NOTLARI:
            continue
        if i == "kapanis_eksik":
            notlar.append(DUZELTME_NOTLARI[i].format(kapanis=meta.get("kapanis", "")))
        elif i == "vurgu_eksik":
            notlar.append(DUZELTME_NOTLARI[i].format(vurgu=meta.get("vurgu", "Kesinlikle")))
        elif i == "uzunluk":
            uz = meta.get("uzunluk")
            notlar.append(DUZELTME_NOTLARI[i].format(uzunluk_ad=uz[0] if uz else "kısa"))
        else:
            notlar.append(DUZELTME_NOTLARI[i])
    return "\n".join(notlar)


# ---------------------------------------------------------------------------
# VERBALIZED SAMPLING (Faz 4): collapse-eğilimli kategorilerde (yetersiz, manipulatif)
# tek çağrıda N farklı aday + olasılık üretip birini ÖRNEKLEYEREK mode collapse'ın
# kök nedeni "typicality bias"ı kır (bkz. arXiv:2510.01171). Argmax yerine olasılığa
# göre örnekleme, modeli en tipik tek moddan uzaklaştırır -> çeşitlilik ↑.
# ---------------------------------------------------------------------------

VS_KATEGORILER = {"yetersiz", "manipulatif"}
VS_ADAY_SAYISI = 3

VS_SUFFIX = (
    "\n\nÖNEMLİ: Tek bir açıklama değil; yukarıdaki talimata TAM uyan, birbirinden "
    "belirgin biçimde FARKLI {n} açıklama üret. Her birine, o açıklamayı ne kadar olası/"
    "tipik bulduğunu gösteren 0 ile 1 arası bir olasılık ata. SADECE şu formatta yaz, "
    "her satırda bir aday, başka HİÇBİR şey ekleme:\n"
    "0.5 | birinci açıklama\n0.3 | ikinci açıklama\n0.2 | üçüncü açıklama"
)

# '0.5 | metin' / '.3: metin' / '1. 0.2 - metin' / '- 50% | metin' gibi biçimleri yakalar.
_VS_SATIR = re.compile(
    r"^\s*[-*•]?\s*(?:\d+[.\)]\s+)?[\(\[]?\s*(\d?\.?\d+)\s*%?\s*[\)\]]?\s*[|:\-–—]\s+(.+?)\s*$"
)


def _vs_ayristir(ham: str) -> list[tuple[float, str]]:
    """VS ham yanıtını (olasılık, metin) adaylarına ayrıştırır. Her adayın metni
    tek tek cikti_temizle'den geçer. Olasılık >1 ise yüzde kabul edilip /100 yapılır."""
    adaylar: list[tuple[float, str]] = []
    for satir in ham.splitlines():
        m = _VS_SATIR.match(satir)
        if not m:
            continue
        try:
            p = float(m.group(1))
        except ValueError:
            continue
        if p > 1:
            p = p / 100.0
        metin = cikti_temizle(m.group(2))
        if metin and len(metin) >= 3:
            adaylar.append((max(p, 0.0), metin))
    return adaylar


def _tek_fatura_vs(fatura, etiket, model, host, keep_alive, kategori,
                   system_prompt, user_prompt, meta, taban_sicaklik, min_p):
    """VS akışı: N aday üret -> ihlalsizleri süz -> olasılığa göre örnekle. Hiçbiri
    geçerli değilse düzeltme notuyla bir kez daha dener; yine olmazsa en iyi adayı
    (ihlalleriyle) döndürür. tek_fatura_isleme ile aynı 6'lı demeti döndürür."""
    vs_prompt = user_prompt + VS_SUFFIX.format(n=VS_ADAY_SAYISI)
    vs_token = 200 if kategori == "yetersiz" else 380
    en_iyi_metin = None
    en_iyi_ihl: list[str] | None = None

    for deneme in range(1, 3):
        sicaklik = taban_sicaklik if deneme == 1 else min(taban_sicaklik + 0.2, 1.3)
        seed = random.randint(1, 2**31 - 1)
        try:
            ham = ollama_cagir(
                system_prompt, vs_prompt, model, host,
                num_predict=vs_token, keep_alive=keep_alive, temperature=sicaklik,
                seed=seed, min_p=min_p, ham=True, num_ctx=1536,
            )
        except Exception as e:
            return fatura, etiket, None, str(e), [], deneme

        adaylar = _vs_ayristir(ham)
        if not adaylar:
            # VS formatı tutmadı -> tek metne indirge (graceful degrade, normal akış gibi)
            tek = cikti_temizle(ham)
            if tek:
                adaylar = [(1.0, tek)]

        gecerliler: list[tuple[float, str]] = []
        for p, t in adaylar:
            ihl = ihlalleri_bul(t, kategori, fatura, meta)
            if _red_mi(t):
                ihl = ["red"] + ihl
            if not ihl:
                gecerliler.append((p, t))
            elif en_iyi_metin is None:
                en_iyi_metin, en_iyi_ihl = t, ihl

        if gecerliler:
            # Argmax DEĞİL: olasılığa göre örnekle (typicality bias'ı kır -> çeşitlilik).
            agirliklar = [max(p, 1e-3) for p, _ in gecerliler]
            secilen = random.choices([t for _, t in gecerliler], weights=agirliklar, k=1)[0]
            return fatura, etiket, secilen, None, [], deneme

        if deneme == 1 and en_iyi_ihl:
            vs_prompt = vs_prompt + "\n\n" + duzeltme_notu_uret(en_iyi_ihl, meta)

    if en_iyi_metin is None:
        return fatura, etiket, None, "VS: aday ayrıştırılamadı", [], 2
    return fatura, etiket, en_iyi_metin, None, en_iyi_ihl or [], 2


def tek_fatura_isleme(fatura, etiket, model, host, keep_alive: str | int | None = None):
    """
    Bir faturayı işler: prompt kur, Ollama'yı çağır, ihlal varsa bir kez
    düzeltici retry uygula. Dönüş:
        (fatura, etiket, metin, hata, kalan_ihlaller, deneme_sayisi)
    Retry aynı worker thread'inde çalışır; ThreadPoolExecutor sayesinde
    başka faturalar işlenirken paralel gerçekleşir.

    yetersiz/manipulatif için Verbalized Sampling akışına (çok-aday) dallanır.
    """
    kategori = etiket["aciklama_kategorisi"]
    system_prompt, user_prompt, meta = prompt_olustur(fatura, kategori, etiket.get("anomali_turleri"))
    # 2-cümleye açık kategorilerde (manipulatif/ai) truncation'ı önlemek için token
    # limiti yükseltildi; yetersiz kısa olduğu için düşük tutulur.
    # ai_uretimi 1-2 cümleye açık (paragraf değil) -> orta num_predict (140).
    token_limiti = {"manipulatif": 160, "ai_uretimi": 140, "yetersiz": 70}.get(kategori, 100)
    # yeterli, fişteki gerçek kalemlere DAYANMALI -> düşük temp + düşük min_p uydurmayı
    # azaltır. Diğerleri çeşitlilik için yüksek temp'te (1.1) kalır, min_p 0.1 güvenlik ağı.
    taban_sicaklik = KATEGORI_SICAKLIK.get(kategori, 0.9)
    min_p = 0.05 if kategori == "yeterli" else 0.1

    # Faz 4: collapse-eğilimli kategoriler VS akışına gider.
    if kategori in VS_KATEGORILER:
        return _tek_fatura_vs(fatura, etiket, model, host, keep_alive, kategori,
                              system_prompt, user_prompt, meta, taban_sicaklik, min_p)

    metin = None
    ihlaller: list[str] = []
    for deneme in range(1, 3):  # ilk deneme + 1 düzeltici retry
        # Sıcaklık merdiveni: retry'da temp'i biraz yükselt (takılan üretimden
        # çıkış) + her denemede farklı seed -> yeniden deneme gerçekten farklı.
        # min_p güvenlik ağı olduğu için tavan 1.3'e kadar açıldı.
        sicaklik = taban_sicaklik if deneme == 1 else min(taban_sicaklik + 0.2, 1.3)
        seed = random.randint(1, 2**31 - 1)
        try:
            metin = ollama_cagir(
                system_prompt, user_prompt, model, host,
                num_predict=token_limiti, keep_alive=keep_alive,
                temperature=sicaklik, seed=seed, min_p=min_p, stop=["\n\n"],
            )
        except Exception as e:
            return fatura, etiket, None, str(e), [], deneme

        ihlaller = ihlalleri_bul(metin, kategori, fatura, meta)
        # Red (moderasyon refleksi) kategori-bağımsız; kural ihlallerine ekle ki
        # düzeltme notuyla yeniden denensin.
        if _red_mi(metin):
            ihlaller = ["red"] + ihlaller
        if not ihlaller:
            return fatura, etiket, metin, None, [], deneme

        if deneme == 1:
            user_prompt = user_prompt + "\n\n" + duzeltme_notu_uret(ihlaller, meta)

    return fatura, etiket, metin, None, ihlaller, 2
