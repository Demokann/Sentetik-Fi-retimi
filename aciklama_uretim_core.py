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

# ---------------------------------------------------------------------------
# MODEL PROFİLLERİ -- modelin Ollama'ya nasıl konuşulacağını belirler.
# ---------------------------------------------------------------------------
# Neden gerekli: qwen3:8b Ollama'nın KENDİ kütüphanesinden geliyor; şablonu,
# düşünme anahtarı ve stop token'ları küratörlenmiş bir Modelfile'da duruyor,
# `think: false` çalışıyor. HF'den (`hf.co/...`) çekilen GGUF'ta ise yalnızca
# dosya var: şablon, kuantize edenin gömdüğü düz ChatML olabilir ve Qwen3'ün
# `enable_thinking` mantığını İÇERMEZ. O durumda `think: false` sessizce etkisiz
# kalır (hata da vermez), `/no_think` direktifi de sadece bir metin parçası olur;
# model muhakemeyi ETİKETSİZ biçimde doğrudan cevaba döker.
#
# Trendyol-LLM-8B-T1'de ölçüldü (2026-07-28):
#   think:false            -> muhakeme düz metin olarak `response`'a sızdı
#   /no_think eki          -> etkisiz; muhakeme 200 token'ı yedi, response BOŞ
#   raw ChatML + prefill   -> ÇALIŞTI (7,9 sn, düşünme yok, gerçek cevap)
#
# Bu yüzden profil üç ayarı birlikte taşır; üçü tek tek işe yaramıyor:
#   raw_chatml     : Ollama şablonunu baypas et, ChatML'i biz kur.
#   think_prefill  : asistan sırasını boş <think></think> çiftiyle başlat ->
#                    model düşünme bölümünü KAPANMIŞ bulur, doğrudan yazar.
#   think_alani    : istek gövdesindeki "think" alanı (None -> alanı hiç koyma).
#   stop           : profil zorunlu stop dizisi. RAW modda `\n\n` KULLANILAMAZ --
#                    prefill'in kendi `\n\n`'i üretimi daha ilk anda keserdi.
VARSAYILAN_MODEL_PROFILI = {
    "raw_chatml": False,
    "think_prefill": False,
    "think_alani": False,   # mevcut davranış: "think": false gönder
    "stop": None,           # çağıranın verdiği stop kullanılır
}

# Anahtar: model adında ARANAN parça (küçük harf, ilk eşleşen kazanır).
MODEL_PROFILLERI: dict[str, dict] = {
    "trendyol": {
        "raw_chatml": True,
        "think_prefill": True,
        "think_alani": None,
        "stop": ["<|im_end|>"],
    },
}


def model_profili(model: str) -> dict:
    ad = (model or "").lower()
    for anahtar, profil in MODEL_PROFILLERI.items():
        if anahtar in ad:
            return {**VARSAYILAN_MODEL_PROFILI, **profil}
    return dict(VARSAYILAN_MODEL_PROFILI)


def chatml_sar(system_prompt: str, user_prompt: str, think_prefill: bool) -> str:
    """system+user'ı ChatML'e sarar (raw mod). Sistem bölümü SABİT kaldığı için
    Ollama'nın prefix önbelleği raw modda da korunur."""
    p = (
        "<|im_start|>system\n" + system_prompt + "<|im_end|>\n"
        "<|im_start|>user\n" + user_prompt + "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    return p + "<think>\n\n</think>\n\n" if think_prefill else p

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

# Kısaltma YALNIZCA SENTETİK adlara uygulanır. Regex sektör kelimelerini de
# ("Turizm", "Kozmetik", "Ticaret", "ve") söktüğü için registry'deki GERÇEK OSM
# adlarını bozuyordu: 'Kozmetik Shop' -> 'Shop', 'Umut Market ve Şarküteri' ->
# 'Umut Market Şarküteri', 'Cansur İç Giyim' -> 'Cansur Giyim' (18.623 OSM adının
# 414'ü, %2,2). Ayrım için registry lookup'a gerek yok -- SONDAKİ hukuki ek
# kusursuz bir proxy (ölçüldü): sentetik 5896/5896 eşleşiyor, osm 23/18623 (%0,12),
# şahıs 0/4363. Eşleşmeyen ad (OSM gerçeği / şahıs şirketi) OLDUĞU GİBİ döner.
_SENTETIK_SON_EK = re.compile(r"(A\.Ş\.|Ltd\.\s*Şti\.|San\.|Tic\.|Nak\.|Paz\.|Taş\.)\s*$")


def firma_adi_kisalt(unvan: str) -> str:
    if not _SENTETIK_SON_EK.search(unvan):
        return unvan
    kisa = re.sub(_UNVAN_EKLERI_REGEX, "", unvan)
    kisa = re.sub(r"\s+", " ", kisa).strip(" .,-")
    return kisa if kisa else unvan


# Ürün adının sonunda ölçü/ambalaj ya da renk bilgisi kalabilir ('... 500 Yaprak',
# '... Siyah'); baş ismi bulmadan önce bunlar atılır.
_OLCU_AMBALAJ_KELIMELERI = {
    "yaprak", "adet", "adetli", "paket", "pkt", "kutu", "koli", "rulo", "sise",
    "şişe", "gram", "gr", "kilo", "kg", "litre", "lt", "ml", "cl", "metre", "mt",
}
_RENK_KELIMELERI = {
    "siyah", "beyaz", "kirmizi", "kırmızı", "mavi", "yesil", "yeşil", "gri", "sari",
    "sarı", "lacivert", "pembe", "mor", "kahverengi", "turuncu", "bej", "krem",
}


def kalem_adi_sadelestir(ad: str) -> str:
    """Ham kalem adını çalışanın yazacağı hâle indirger.

    Türkçede isim tamlamasının BAŞ İSMİ SONDADIR ('F Saff Sıvı El Sabunu 500Ml' ->
    'el sabunu', 'Renkli A4 Fotokopi Kağıdı 500 Yaprak' -> 'fotokopi kağıdı'), bu
    yüzden ölçü/ambalaj/renk gürültüsü atıldıktan sonra SON iki anlamlı kelime alınır
    (baştan almak 'saff sıvı el' gibi bozuk sonuç veriyordu).

    Ölçü/model gürültüsü için mevcut `_URUN_DETAY_GURULTU` regex'i yeniden kullanılır
    (modülün alt kısmında tanımlı -- çalışma anında çözülür).

    `yeterli` talimatındaki SADELEŞTİRME örneğini faturanın KENDİ kaleminden kurmak
    için: sabit örnekler ('F Saff Sıvı El Sabunu 500Ml' -> 'el sabunu') 8B'de
    içeriğe sızıyordu (model örnekteki ürünü alakasız fişte anlatıyordu)."""
    sade = _URUN_DETAY_GURULTU.sub(" ", ad)
    sade = re.sub(r"[^\w\s]", " ", sade)
    kelimeler = [k for k in sade.split() if len(k) > 1 and not k.isdigit()]
    while kelimeler and (_tr_normalize(kelimeler[-1]) in _OLCU_AMBALAJ_KELIMELERI
                         or _tr_normalize(kelimeler[-1]) in _RENK_KELIMELERI):
        kelimeler.pop()
    return " ".join(kelimeler[-2:]).lower()


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

# Üst sınırlar sözel tarifle uyumlu olacak şekilde gevşetildi: iki doğal Türkçe
# cümle rahatça 150 karakteri, üç cümle 250'yi buluyor; eski bantlar (135/230)
# gereksiz `uzunluk` ihlali -> boşuna retry üretiyordu. Nihai değerler pilot
# ölçümüyle (aciklama_analiz.py, kalan-ihlal frekansı) kalibre edilir.
UZUNLUK_HEDEFLERI = [
    ("çok kısa", "en fazla 4-5 kelimelik tek bir öbek", 8, 45),
    ("kısa", "tek cümle, 6-10 kelime", 20, 85),
    ("orta", "1-2 cümle", 45, 150),
    ("uzun", "2-3 cümle, biraz daha detaylı", 90, 250),
]

# Kategori başına yumuşak ağırlık: dört uzunluğa da şans verir, sadece eğim farkı.
_UZUNLUK_AGIRLIK = {
    "yetersiz": [0.40, 0.35, 0.20, 0.05],
    # ai_uretimi: HEM kısa (kalıba uyan tek öbek) HEM ölçülü-uzun (1-2 cümle resmi
    # paragraf) alabilsin -> 'uzunsa ai' diye TEK YÖNLÜ sahte sinyal vermeyelim
    # (ai kısa da olabilir, uzun da). Aşırı uzun (3+ cümle/paragraf) hariç, dört
    # uzunluğa da makul şans. Karakteri artık system prompt taşıyor.
    # "çok kısa" (8-45 krk) ai_uretimi için İMKANSIZ: zorunlu açılış (~25 krk) +
    # kapanış (~30 krk) zaten ~70 karakter eder -> ihlal GARANTİ, retry düzeltemez.
    # Pilotta 4 uzunluk ihlalinin 3'ü buydu ve ai retry oranını %50'ye çıkarmıştı.
    "ai_uretimi": [0.00, 0.30, 0.40, 0.30],
    # manipulatif de "çok kısa" alamaz -- kurumsal kılıf 45 karaktere sığmıyor (bkz.
    # ihlalleri_bul'daki 55 karakterlik alt sınır).
    "manipulatif": [0.00, 0.30, 0.45, 0.25],
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
    # yeterli örnekleri YETERLI_ISKELE yapılarını örnekler: amaç-önce / kalem-önce /
    # olay-çıpalı / iki-parçalı; hepsi Türkçenin neden-sonuç, amaç-sonuç bağlacını taşır.
    "yeterli": [
        "Bölge bayi ziyaretinde ekiple öğle yemeği için ödedim.",
        "Yeni ekip üyeleri için kırtasiye ve toner aldım, stok bitmişti.",
        "Müşteri sunumu öncesi toplantı odasına ikram ısmarladım.",
        "Saha kurulumunda kullanmak üzere kablo ve bağlantı parçası satın aldım.",
        "Toner bittiği için yenisini aldım, ay sonu raporlarını basmamız gerekiyordu.",
        "Fuar standında görevliydik, o yüzden ekibe öğle yemeği ısmarladım.",
        "Yeni gelen stajyerlere kulaklık aldım; açık ofiste toplantılara giremiyorlardı.",
    ],
    "yetersiz": [
        "genel ofis ihtiyacı",
        "iş gideri",
        "muhtelif harcama, departman için",
        "gerekliydi aldım",
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

# manipulatif few-shot'ı DALA GÖRE ayrılır. Tek ortak havuz kullanılırken kurnaz dal
# "abartılı vurgu KULLANMA" derken prompt'una vurgulu örnekler giriyordu -> dal ayrımı
# bulanıklaşıyordu. Her dal kendi hedef üslubunu görür.
FEWSHOT_MANIPULATIF = {
    # Gerçek/aykırı kalemi hiç anmadan kurumsal kılıf: sızıntı YOK, kılıf iddialı.
    "gizleme": [
        "Yönetim kurulu değerlendirme toplantısı kapsamında ağırlama gideri.",
        "Önemli müşteri ağırlama organizasyonu, tümüyle iş geliştirme amaçlı.",
        "Bölge bayileriyle yıl sonu değerlendirme programının temsil gideri.",
        "Stratejik iş ortağımızla yürütülen görüşmelerin ağırlama masrafı.",
        "Ekip motivasyon programı kapsamındaki kurumsal etkinlik gideri.",
    ],
    # Abartılı, savunmacı, ısrarcı: zorunlu vurgu bu dalda gelir.
    "bariz": [
        "Kesinlikle tamamen proje bütçesi kapsamındadır, iş dışı hiçbir kalem yoktur.",
        "Yüzde yüz iş amaçlı bir harcama, kişisel hiçbir yanı yok, emin olabilirsiniz.",
        "Bunu tümüyle iş için aldım, gerekirse toplantı kaydını da paylaşabilirim.",
        "Hiç şüphesiz şirket gideri; zaten başka türlü işi yürütmemiz mümkün değildi.",
        "Baştan sona iş kapsamında, gerçekten gerekliydi, lütfen onaylayın.",
    ],
    # Sakin, rutin görünen, sorgusuz onaylanmayı hedefleyen şişirilmiş kurumsal kılıf.
    "kurnaz": [
        "Departman değerlendirme toplantısı kapsamında rutin temsil gideri.",
        "Aylık operasyon planlaması görüşmesinin standart ağırlama kalemi.",
        "İş geliştirme sürecinin olağan gideri, bütçe kaleminde tanımlı.",
        "Saha ekibiyle yapılan periyodik koordinasyon toplantısının gideri.",
        "Yıllık tedarik planı çerçevesinde alınan mutat departman ihtiyacı.",
    ],
    # Tutarı hiç anmadan kaçınılmazlık kurar (limit_asimi dalı).
    "zorunluluk": [
        "Toplantı uzayınca açık olan tek yer orasıydı, mecburen oradan aldık.",
        "Acil ihtiyaçtı, başka tedarikçi bulamadım, işi durdurmamak için oradan aldım.",
        "Kurulum aynı gün bitmeliydi, elimizdeki tek seçenek buydu.",
        "Müşteri programı değişti, son anda oradan almak zorunda kaldım.",
        "Stok tükenmişti ve iş bekleyemezdi, bulabildiğim yerden temin ettim.",
    ],
}


def _fewshot_havuz(kategori: str, dal: str | None = None) -> list[str]:
    """Kategori (ve manipulatif ise DAL) için few-shot örnek havuzu."""
    if kategori == "manipulatif":
        if dal in FEWSHOT_MANIPULATIF:
            return FEWSHOT_MANIPULATIF[dal]
        return [o for havuz in FEWSHOT_MANIPULATIF.values() for o in havuz]
    return FEWSHOT.get(kategori, [])


def fewshot_blok(kategori: str, adet: int = 2, dal: str | None = None) -> str:
    havuz = _fewshot_havuz(kategori, dal)
    if not havuz:
        return ""
    secim = random.sample(havuz, min(adet, len(havuz)))
    satirlar = "\n".join(f"- {s}" for s in secim)
    return f"Örnek TARZLAR (birebir kopyalama, yalnız tarzı yakala):\n{satirlar}\n"


# NOT: ilk iki satırın AYNI olması KASITLIDIR (hata değil). 8B'ye "kesinlikle böyle
# yap / asla şöyle yapma" gibi uç talimat vermek yerine istenen davranış ÖRNEK olarak
# sunuluyor; aynı örneği iki kez koymak o davranışın seçilme sıklığını (2/3) artırır.
YETERLI_USLUP_IPUCLARI = [
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "gündelik konuşma diliyle, fazla resmi olmayan bir ifade",
]

# yeterli İSKELE: iyi örnekler (#66/#87) hem iş amacı verir, hem firma adını doğal
# kullanır, hem de gerçek kalemi kısaca dahil eder. Tek kalıba çakılmamak için
# fatura başına rastgele bir YAPI seçilir. Asıl amaç: Türkçenin AMAÇ-SONUÇ /
# NEDEN-SONUÇ cümle kalıplarını ("...olduğu için", "bu yüzden", "...amacıyla",
# "...gerekiyordu") modele hatırlatıp mantıklı senaryolar kurdurmak -- 8B'de
# `yeterli`nin en güçlü çeşitlilik kaldıracı budur. {firma} çalışma anında doldurulur.
YETERLI_ISKELE = [
    "Önce iş amacını (kim için / neden) söyle, sonra {firma}'dan hangi gerçek kalemi aldığını belirt.",
    "Önce fişteki gerçek kalem(ler)i aldığını yaz, ardından bunu neden/hangi iş için yaptığını ekle.",
    "Bir iş olayına bağla (toplantı, bayi ziyareti, saha işi, sunum öncesi, ekip ihtiyacı gibi) ve o "
    "kapsamda {firma}'dan gerçek kalemi aldığını anlat.",
    "Kısa ana cümlede amaç+gerçek kalemi ver; istersen ikinci kısa cümlede küçük bir bağlam/gerekçe ekle.",
    "NEDEN-SONUÇ kur: önce ortaya çıkan ihtiyacı/durumu söyle ('... bitmişti', '... gerekiyordu'), "
    "sonra bu yüzden {firma}'dan neyi aldığını yaz.",
    "AMAÇ-SONUÇ kur: yapılacak işi/etkinliği söyle, ardından o iş için gerçek kalemi aldığını "
    "'...amacıyla / ...için' bağlacıyla bağla.",
]

# yeterli DAYANAK kontrolü için iş-amacı isim havuzu (metin bunlardan hiçbirini,
# gerçek kalemi ya da firma adını içermiyorsa 'dayanaksız/muğlak' sayılır).
# DİKKAT: eşleşme token-PREFIX'tir, substring DEĞİL -- eskiden 2 harflik "is" girdisi
# substring aranıyordu ve 'kişisel', 'sistem', 'bisiklet', 'danışman' gibi alakasız
# kelimelerde eşleşip kuralı fiilen DEVRE DIŞI bırakıyordu. Kısa/tehlikeli girdiler
# havuzdan çıkarıldı, yerlerine çok-kelimeli güvenli ifadeler kondu (aşağıda).
_YETERLI_AMAC_ISIMLERI = (
    # EKİP/KURUM ekseni
    "proje", "toplanti", "ziyaret", "musteri", "ekip", "etkinlik", "sunum", "bayi",
    "saha", "organizasyon", "lansman", "fuar", "egitim", "departman", "ofis",
    "misafir", "agirlama", "ikram", "servis", "sube", "calisan", "personel",
    "sirket", "birim", "sevkiyat", "kurulum", "denetim", "seyahat", "stajyer",
    "rapor", "sozlesme", "teslimat", "sevk",
    # BİREYSEL eksen -- olay havuzuna BIREYSEL_OLAY eklendiğinde bu sözcükler
    # olmadan kural, kendi ürettiğimiz doğru örnekleri ihlal sayardı
    # ("Otoparka bıraktım, sabahki randevuya yetişmem gerekiyordu." gibi).
    "randevu", "gorusme", "mesai", "vardiya", "gorev", "mulakat", "oryantasyon",
    "sprint", "kampanya", "stok", "bakim", "ariza", "sayim", "teslim", "konaklama",
    "prototip", "sertifika", "danismanlik",
)

# Substring aranabilecek kadar güvenli çok-kelimeli iş-amacı ifadeleri.
_YETERLI_AMAC_IFADELERI = (
    "is icin", "isle ilgili", "is gereg", "is seyahat", "is toplanti",
    # bireysel bağlam kalıpları (fiil/durum -- tek kelimelik kök vermiyor)
    "ise gel", "ise gid", "gec kal", "yetis", "yol ust", "sabah erken",
    "aksam gec", "tek basima", "kendi masam", "mesaiye kal", "gorev sirasinda",
)


def _amac_koku_var_mi(n_tok: set[str]) -> bool:
    """Token'ların herhangi biri bir iş-amacı köküne indirgeniyor mu?

    Neden düz prefix YETMİYOR: Türkçede ünsüz yumuşaması var, 'ekip' -> 'ekibine'
    olur ve `startswith('ekip')` tutmaz. Ölçüldü: 'Üretim ekibine antrepo hizmetleri
    aldım.' geçerli bir yeterli olduğu hâlde kural onu DAYANAKSIZ sayıyordu.
    Çözüm: her token için sondan kısaltarak aday kökler üretilir ve son harfin
    yumuşaması geri alınır (b->p, c->ç, d->t, g/ğ->k)."""
    yumusama = {"b": "p", "c": "c", "d": "t", "g": "k"}
    for tok in n_tok:
        for boy in range(len(tok), 3, -1):     # en az 4 harflik kök
            aday = tok[:boy]
            if aday in _YETERLI_AMAC_ISIMLERI:
                return True
            sert = aday[:-1] + yumusama.get(aday[-1], aday[-1])
            if sert in _YETERLI_AMAC_ISIMLERI:
                return True
    return False

# ai_uretimi kategorisinde model tek bir kapanış kalıbına ("...kapsamında
# gerçekleştirilmiştir") aşırı yakınsıyordu (200 örnekte %70). Kapanışı
# fatura başına rastgele seçip zorunlu kılarak kalıp çeşitliliğini garantiye
# alıyoruz.
# Havuz 6 -> 22: 20k'lık üretimde ai_uretimi payı ~%10 (~2000 metin) olduğundan
# 6 kapanış, kapanış başına ~330 tekrar demekti (collapse garantisi). 22'de ~90'a iner.
# NOT: belgeyi/süreci anlatan kapanışlar ("belgelendirilmiştir", "raporlanmıştır")
# bilerek DIŞARIDA -- açıklama harcamayı anlatır, fişin kendisini değil.
AI_URETIMI_KAPANIS_IPUCLARI = [
    "kapsamında gerçekleştirilmiştir",
    "için işlem yapılmıştır",
    "amacıyla gerçekleştirilmiştir",
    "doğrultusunda sağlanmıştır",
    "kapsamında temin edilmiştir",
    "çerçevesinde gerçekleştirilmiştir",
    "kapsamında karşılanmıştır",
    "doğrultusunda temin edilmiştir",
    "amacıyla tedarik edilmiştir",
    "çerçevesinde karşılanmıştır",
    "kapsamında tanzim edilmiştir",
    "doğrultusunda değerlendirilmiştir",
    "amacıyla kayıt altına alınmıştır",
    "çerçevesinde uygun görülmüştür",
    "kapsamında talep edilmiştir",
    "doğrultusunda onaya sunulmuştur",
    "çerçevesinde işleme alınmıştır",
    "kapsamında gider olarak kaydedilmiştir",
    "doğrultusunda gerekli görülmüştür",
    "amacıyla sağlanmıştır",
    "çerçevesinde temin edilmiştir",
    "doğrultusunda tamamlanmıştır",
]

# ai_uretimi AÇILIŞ havuzu: tüm çıktılar 'Belirtilen fiş kapsamında gerçekleştirilen
# harcamalar...' ile başlıyordu (deneme_80'de 20/20). Kaynak, ai talimatındaki sabit
# örnek ifadeydi -> model onu birebir açılış yapıyordu. Açılışı fatura başına rastgele
# döndürerek collapse'ı kırıyoruz (kapanış havuzuyla aynı mantık).
AI_URETIMI_ACILIS_IPUCLARI = [
    "İlgili masraf kalemi",
    "Söz konusu harcama",
    "İşbu masraf",
    "Fişe konu kalemler",
    "Bahse konu giderler",
    "Kayda alınan masraf",
    "Değerlendirilen harcama kalemleri",
    "Belirtilen fiş kapsamında gerçekleştirilen harcamalar",
    "Mevcut gider kalemi",
    "Sunulan masraf belgesindeki harcama",
    "Yukarıda belirtilen gider",
    "Tarafımca gerçekleştirilen harcama",
    "İlgili döneme ait masraf",
    "Söz konusu tedarik işlemi",
    "Beyan edilen gider kalemleri",
    "İncelenen harcama",
    "Anılan masraf kalemi",
    "Gerçekleştirilen alım",
    "İlgili birim tarafından yapılan harcama",
    "Bahsi geçen gider kalemi",
]

# yetersiz kategorisinde de benzer şekilde "...temin edilmiştir" kalıbına
# yakınsama vardı (%22). Üslup çeşitliliği için rastgele seçim.
YETERSIZ_USLUP_IPUCLARI = [
    "sadece firma adını an, iş amacından hiç bahsetme",
    "'ihtiyaç için', 'iş ile ilgili' gibi çok genel geçer ifadelerle geç",
    "hangi ürün/hizmet olduğunu belirtme, sadece genel bir harcama olduğunu söyle",
    "kısa ve detaysız, tek bir öbek halinde",
    "'çeşitli', 'genel', 'muhtelif' gibi belirsizlik bildiren kelimelerle",
    # Yetersiz her zaman kalemden kaçmaz; bazen kalemi/firmayı ANAR ama BAĞLAM vermez
    # ("ABC ile toplantı gideri" der, "DEF projesi için ekiple yemek" demez).
    "fişteki bir kalemi ya da firmayı an ama neden alındığını söyleme "
    "('X ile toplantı gideri', 'Y karşılandı' gibi bağlamsız)",
]

# yetersiz talimatındaki literal örnekler SABİT verildiğinde model bunları birebir
# kopyalıyordu ('genel ofis ihtiyacı' 5'te 4). Havuzdan her seferinde 2 rastgele
# örnek gösterip döndürerek collapse'ı kırıyoruz (few-shot ise yetersiz'de artık
# hiç kullanılmıyor -- talimat örnekleriyle çakışıp verbatim kopyayı besliyordu).
YETERSIZ_ORNEK_HAVUZ = [
    # isim-öbeği (muğlak, kategorik)
    "genel ofis ihtiyacı", "iş gideri", "muhtelif harcama", "departman ihtiyacı",
    "çeşitli alışveriş", "rutin harcama", "genel gider", "ihtiyaç malzemesi",
    "ofis için", "gerekli malzeme", "aylık ihtiyaç", "olağan gider",
    "işle ilgili alım", "standart harcama", "birim gideri",
    # üşengeç/baştan-savma cümle (whaatif'in en iyi verdiği ton)
    "masraf", "gider", "bir şeyler aldım işte", "gerekliydi aldım",
    "iş için lazımdı", "çeşitli şeyler işte", "toplantı için bir şeyler", "gerekliydi işte",
    "lazımdı aldım", "iş icabı", "ihtiyacım oldu", "böyle bir masraf çıktı",
    "gerekiyordu", "aldım işte", "iş kaynaklı",
    # kalemi/firmayı anan ama BAĞLAMSIZ (yeni üslup seçeneğiyle uyumlu)
    "market alışverişi", "yemek gideri", "ulaşım masrafı", "kırtasiye alımı",
]

# manipulatif "aşırı haklı çıkarma" dalı: model bazen abartılı vurguyu atlayıp
# meşru/yeterli gibi okunan cümle üretiyordu. Zorunlu bir vurgu açılışı seçip
# dayatarak (ve validator'la denetleyerek) manipülatif "işareti"ni garantiye
# alıyoruz. _VURGU_ANAHTARLARI, üretilen metinde vurgunun varlığını (Türkçe
# normalize edilmiş) yakalamak için kullanılır.
# Havuz 16 -> 40: `vurgu_eksik` bir İHLAL (retry tetikler), yani bariz dalda vurgu
# GARANTİ metne giriyor. Bu kasıtlı (kalın çizgi: model manipülatif işaretini önce
# net öğrensin) ama dar havuz "kesinlikle => manipulatif" gibi ezberlenebilir bir
# kısayol bırakır. ~1200 bariz metinde 16 vurgu = ~75 tekrar; 40'ta ~30'a iner.
MANIPULATIF_VURGU_IPUCLARI = [
    "Kesinlikle",
    "Yüzde yüz iş amaçlı",
    "Tamamen iş gereği",
    "Hiç şüphesiz iş için",
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
    "Katiyen özel bir harcama değil",
    "Sonuna kadar iş kaynaklı",
    "Apaçık şirket masrafı",
    "İstisnasız iş kapsamında",
    "Baştan aşağı kurumsal",
    "Bire bir işle bağlantılı",
    "Tereddütsüz proje gideri",
    "Elbette ki iş amaçlı",
    "Açık açık iş için",
    "Kılı kırk yararak söylüyorum, iş için",
    "Dosdoğru iş amaçlı",
    "Adı üstünde şirket gideri",
    "Elimi vicdanıma koyarak iş için",
    "Kesin olarak departman ihtiyacı",
    "Yüzde yüz ekip gideri",
    "Bütünüyle iş kapsamında",
    "Tamamen kurumsal gereklilik",
    "Hiç abartısız iş için",
    "Salt iş amaçlı",
    "Yalnızca ve yalnızca iş için",
]
# Genel emphatic-marker fallback (atanan vurgu paraphrase edilse de yakalansın).
# HAVUZLA SENKRON TUTULMALI: her MANIPULATIF_VURGU_IPUCLARI öğesi buradaki bir kökle
# ya da kendi ayırt edici kelimesiyle yakalanabilmeli (_vurgu_var_mi önce atanan
# vurgunun kelimelerine, sonra bu listeye bakar).
_VURGU_ANAHTARLARI = (
    "kesinlikle", "yuzde yuz", "tamamen", "tumuyle", "suphesiz", "kuskusuz",
    "tartismasiz", "eksiksiz", "gonul rahat", "bastan sona", "her acidan",
    "net bicimde", "bal gibi", "katiyen", "sonuna kadar",
    "apacik", "istisnasiz", "bastan asagi", "bire bir",
    "tereddut", "elbette", "acik acik", "kili kirk",
    "dosdogru", "adi ustunde", "kesin olarak", "butunuyle",
    "kurumsal gereklilik", "abartisiz", "salt is", "yalnizca ve yalnizca",
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


# ---------------------------------------------------------------------------
# DEPARTMAN + OLAY + ÖLÇEK -- açıklamanın AMAÇ çıpası
# ---------------------------------------------------------------------------
# Neden: `yeterli`nin tanımı gereği zorunlu unsuru "hangi iş bağlamı" ama prompt bunu
# yalnız soyut olarak istiyordu; T1 pilotunda bir üretim salt kalem listesi çıktı
# ("Barbekü Soslu Tavuk, Tavuklu Pizza, Hot Dog ve Ispanaklı Gözleme aldım.") ve kural
# bunu ihlal saymadı. Somut olay havuzu hem amacı garantiler hem senaryo çeşitliliğini
# BİZİM kontrolümüze alır (model uydurmasına değil).
#
# Departman personadan TÜRETİLİR, bağımsız rastgele slot DEĞİL: 'satış temsilcisi' +
# 'Ar-Ge departmanı' çelişkisi 8B'de karakteri bozar (bkz. çelişki yasağı, CLAUDE.md §6).
ROL_DEPARTMAN = {
    "satış temsilcisi":        "Satış",
    "yazılım mühendisi":       "Ar-Ge",
    "saha teknisyeni":         "Saha Operasyon",
    "yönetici asistanı":       "İdari İşler",
    "muhasebe uzmanı":         "Finans",
    "proje yöneticisi":        "Proje Yönetimi",
    "pazarlama uzmanı":        "Pazarlama",
    "insan kaynakları uzmanı": "İnsan Kaynakları",
    "lojistik sorumlusu":      "Lojistik",
    "operasyon uzmanı":        "Operasyon",
}

# 18 harcama kategorisi -> 10 olay grubu (tam çapraz çarpım 180 hücre olurdu).
# Yasaklı kategoriler (alkol/eglence/tutun/kumar) ve 'diger' burada YOK -> 'genel'e düşer.
KATEGORI_GRUBU = {
    "yemek_hizmeti": "yemek", "temel_gida": "yemek",
    "ulasim_hizmeti": "ulasim", "ulasim_bireysel": "ulasim",
    "konaklama": "konaklama",
    "ofis_sarf_malzeme": "ofis", "ofis_mobilya": "ofis",
    "teknoloji_ekipman": "teknoloji", "yazilim_lisans": "teknoloji",
    "danismanlik": "hizmet",
    "giyim": "giyim", "kisisel_bakim": "bakim", "temizlik": "temizlik",
}

# ÖLÇEK: harcamayı ekip/kurum için mi, çalışan kendisi için mi yaptı? Departmandan
# BAĞIMSIZ ikinci boyut. Bireysel senaryolar tek kalemli küçük fişleri doğal kılar ve
# "hep ekip" tekdüzeliğini kırar (T1 pilotunda 8 yeterli'nin 6'sı ekip bağlamındaydı).
OLCEK_AGIRLIK = {                # (bireysel, ekip)
    "ulasim":    (0.75, 0.25),   # taksi/yakıt/otopark doğası gereği bireysel
    "bakim":     (0.70, 0.30),
    "konaklama": (0.60, 0.40),
    "giyim":     (0.50, 0.50),
    "teknoloji": (0.40, 0.60),
    "genel":     (0.40, 0.60),
    "yemek":     (0.35, 0.65),
    "hizmet":    (0.30, 0.70),
    "ofis":      (0.25, 0.75),
    "temizlik":  (0.15, 0.85),
}

# Her departmanda geçerli, kalem grubuna bağlı EKİP olayları.
GRUP_OLAY = {
    "yemek":      ["ekip öğle yemeği", "müşteri ağırlama", "toplantı ikramı",
                   "saha ekibine mola", "mesai sonrası çalışma yemeği", "eğitim günü ikramı",
                   "vardiya değişimi ikramı", "yoğun sezon desteği", "ziyaretçi kahvaltısı",
                   "ay sonu kapanış çalışması"],
    "ulasim":     ["şehir içi müşteri ziyareti", "havalimanı transferi",
                   "saha noktasına gidiş", "fuar alanına ulaşım", "şube turu",
                   "acil müdahale çağrısı", "eğitim merkezine gidiş"],
    "konaklama":  ["şehir dışı görev", "fuar katılımı", "bayi ziyareti", "saha kurulumu",
                   "bölge toplantısı", "denetim ziyareti"],
    "ofis":       ["ofis stoğunun bitmesi", "yeni çalışan kurulumu",
                   "toplantı odası hazırlığı", "ay sonu raporlama", "arşiv düzenleme",
                   "eğitim materyali hazırlığı", "yeni şube açılışı"],
    "teknoloji":  ["ekipman arızası", "yeni çalışan donanımı", "sunum hazırlığı",
                   "lisans yenileme", "sistem güncellemesi", "yedekleme ihtiyacı",
                   "uzaktan çalışma kurulumu"],
    "hizmet":     ["dönemsel danışmanlık", "denetim hazırlığı", "süreç iyileştirme çalışması",
                   "mevzuat değişikliği", "sertifikasyon süreci"],
    "giyim":      ["saha ekibi kıyafeti", "fuar standı kıyafeti", "tanıtım etkinliği",
                   "yeni sezon hazırlığı", "kurumsal etkinlik"],
    "bakim":      ["ofis kiti tamamlama", "misafir hazırlığı", "saha ekibi ihtiyacı",
                   "sosyal alan düzeni", "etkinlik hazırlığı"],
    "temizlik":   ["ofis temizlik ihtiyacı", "etkinlik sonrası toparlanma", "ortak alan bakımı",
                   "depo düzenleme", "sezon başı temizliği"],
    "genel":      ["departman ihtiyacı", "ekip talebi", "operasyonel gereklilik",
                   "acil ihtiyaç", "rutin tedarik"],
}

# Olayın prompt'a girerken aldığı ÇERÇEVE. Model olay ifadesini büyük ölçüde aynen
# taşıyor (qwen3_3: bağlam alan çıktıların hepsi 'Denetim hazırlığı için...',
# 'Sahada öğle molası için...' diye kopyaladı). Çekirdek isim tekrarı sorun değil --
# amaç sözcüğünün tekrarlaması gerçek veride de olur -- ama cümlenin TAMAMI aynı
# kalıba oturmasın diye çerçeve rastgele değişir.
# DİKKAT: çerçeve yalnız EKİP/zorunluluk dalında kullanılır. Bireysel olayların bir
# kısmı kendi içinde 'için' taşıyor ('evden çalışma için kırtasiye') ve çerçeveyle
# birleşince bozuk Türkçe çıkıyordu ('... için kırtasiye sırasında yaptın').
OLAY_CERCEVELERI = [
    "{olay} sırasında", "{olay} nedeniyle", "{olay} öncesinde", "{olay} kapsamında",
    "{olay} için", "{olay} sonrasında", "{olay} planlandığı için",
]

# Aynı grup anahtarlarıyla BİREYSEL karşılıklar -> seçim mantığı değişmez.
BIREYSEL_OLAY = {
    "yemek":      ["tek başıma öğle arası", "sahada öğle molası", "mesaiye kalınca akşam yemeği",
                   "yol üstünde hızlı bir şeyler", "eğitim günü öğle arası",
                   "erken vardiya öncesi kahvaltı", "müşteri beklerken ara öğün"],
    "ulasim":     ["görüşmeye yetişme", "işe geliş-gidiş", "otoparka bırakma", "araç yakıtı",
                   "randevu dönüşü", "gece geç çıkışta dönüş", "servis kaçırma",
                   "yağmurda saha noktasına gidiş"],
    "konaklama":  ["tek kişilik görev seyahati", "sabah erken toplantı için gece kalma",
                   "eğitim programı konaklaması", "uçuş iptali nedeniyle mecburi kalış"],
    "ofis":       ["kendi masam için sarf ihtiyacı", "not defteri/kalem bitmesi",
                   "evden çalışma için kırtasiye", "kendi dosyalarımı düzenleme"],
    "teknoloji":  ["kendi dizüstümün adaptörü", "kulaklık arızası", "kablo/aksesuar ihtiyacı",
                   "sunum için taşınabilir bellek", "şarj aleti kaybolması"],
    "hizmet":     ["kendi süreçlerim için danışmanlık", "sertifika/eğitim katılımı",
                   "mesleki yetkinlik yenileme"],
    "giyim":      ["saha için iş kıyafeti", "müşteri ziyareti öncesi hazırlık",
                   "hava koşulları nedeniyle ek kıyafet"],
    "bakim":      ["seyahat sırasında kişisel ihtiyaç", "sahada hijyen ihtiyacı",
                   "uzun görev öncesi hazırlık"],
    "temizlik":   ["kendi çalışma alanımın temizliği", "araç içi temizlik",
                   "saha dönüşü ekipman temizliği"],
    "genel":      ["kişisel iş ihtiyacı", "görev sırasında çıkan ihtiyaç",
                   "beklenmedik bir gereklilik"],
}

# Departmana ÖZGÜ ekip olayları; yalnız uygun kalem gruplarında devreye girer.
DEPARTMAN_OLAY: dict[str, list[tuple[str, set[str]]]] = {
    "İnsan Kaynakları": [("mülakat ikramı", {"yemek"}),
                         ("işe alım görüşmeleri", {"yemek", "ulasim"}),
                         ("oryantasyon programı", {"ofis", "teknoloji", "yemek"}),
                         ("çalışan bağlılığı etkinliği", {"yemek", "genel"})],
    "Ar-Ge":            [("sprint değerlendirme toplantısı", {"yemek", "ofis"}),
                         ("prototip çalışması", {"teknoloji", "ofis"}),
                         ("geliştirme ortamı kurulumu", {"teknoloji"})],
    "Satış":            [("bayi ziyareti", {"ulasim", "konaklama", "yemek"}),
                         ("müşteri sunumu", {"yemek", "ofis", "teknoloji"}),
                         ("teklif görüşmesi", {"yemek", "ulasim"})],
    "Pazarlama":        [("fuar standı hazırlığı", {"giyim", "ofis", "konaklama"}),
                         ("lansman etkinliği", {"yemek", "genel"}),
                         ("kampanya çekimi", {"teknoloji", "giyim"})],
    "Finans":           [("ay sonu kapanışı", {"ofis", "yemek"}),
                         ("denetim hazırlığı", {"ofis", "hizmet"}),
                         ("mali müşavirlik görüşmesi", {"hizmet", "yemek"})],
    "Lojistik":         [("sevkiyat planlaması", {"ulasim", "hizmet"}),
                         ("depo sayımı", {"ofis", "yemek"}),
                         ("araç filosu ihtiyacı", {"ulasim"})],
    "Operasyon":        [("vardiya devri", {"yemek", "ofis"}),
                         ("süreç denetimi", {"hizmet", "ofis"}),
                         ("saha ekibi desteği", {"yemek", "ulasim", "bakim"})],
    "Proje Yönetimi":   [("proje başlangıç toplantısı", {"yemek", "ofis"}),
                         ("müşteri statü toplantısı", {"yemek", "ulasim"}),
                         ("teslim öncesi hazırlık", {"ofis", "teknoloji"})],
    "İdari İşler":      [("misafir ağırlama", {"yemek", "bakim"}),
                         ("ofis düzeni", {"ofis", "temizlik"}),
                         ("toplantı organizasyonu", {"yemek", "ofis", "genel"})],
    "Saha Operasyon":   [("kurulum ziyareti", {"ulasim", "konaklama", "teknoloji"}),
                         ("arıza müdahalesi", {"teknoloji", "ulasim"}),
                         ("periyodik bakım", {"teknoloji", "temizlik"})],
}


def olay_sec(departman: str | None, baskin_kategori_ham: str) -> tuple[str, str]:
    """(olcek, olay) döner. Departmana özgü olaylara ağırlık verir ama genel havuzu
    kapatmaz -- her departman her olayı yaşayabilir, kapatmak çeşitliliği kısardı."""
    grup = KATEGORI_GRUBU.get(baskin_kategori_ham, "genel")
    olcek = random.choices(("bireysel", "ekip"),
                           weights=OLCEK_AGIRLIK.get(grup, (0.4, 0.6)))[0]
    if olcek == "bireysel":
        return olcek, random.choice(BIREYSEL_OLAY.get(grup, BIREYSEL_OLAY["genel"]))
    ozel = [o for o, gruplar in DEPARTMAN_OLAY.get(departman or "", []) if grup in gruplar]
    genel = GRUP_OLAY.get(grup, GRUP_OLAY["genel"])
    return olcek, random.choice(ozel * 2 + genel if ozel else genel)


def baglam_notu_uret(persona: dict, baskin_ham: str, manipulatif_dal: str | None = None) -> tuple[str, dict]:
    """Prompt'a eklenecek BAĞLAM cümlesi + meta. Boş string dönebilir (bilinçli).

    ~%50 olasılık KASITLI: her `yeterli` "X birimindesin" diye başlarsa şablon imzası
    doğar ve aşağı akıştaki model kategoriyi içerikten değil kalıptan öğrenir (leakage).

    manipulatif_dal verilirse yalnız 'gizleme'/'zorunluluk' dallarına bağlam verilir --
    o faturalar ANOMALİLİ olduğu için `onay_durumu` metne bakmadan zaten 'onaylanmadi';
    metnin meşru görünmesi etiketi bozmaz, aksine örneği gerçekçi kılar. 'bariz'/'kurnaz'
    ağırlıkla TEMİZ faturalarda çalışır ve orada etiket TAMAMEN metinden gelir; sağlam bir
    iş bağlamı vermek onları `yeterli`den ayırt edilemez kılıp etiketi gürültüye çevirirdi.
    """
    if manipulatif_dal is not None and manipulatif_dal not in ("gizleme", "zorunluluk"):
        return "", {}
    if random.random() >= 0.5:
        return "", {}
    dep = ROL_DEPARTMAN.get(persona.get("rol", ""))
    if not dep:
        return "", {}
    olcek, olay = olay_sec(dep, baskin_ham)
    cerceveli = random.choice(OLAY_CERCEVELERI).format(olay=olay)
    meta = {"departman": dep, "olay": olay, "olcek": olcek}

    if manipulatif_dal == "gizleme":
        # Olay ÖRTÜNÜN kendisi: somut bir bahane, kurumsal bulamaç değil.
        notu = (f" BAĞLAM: {dep} birimindesin; anlatacağın kılıf '{olay}'. Bunu kendi "
                f"cümlenle, doğal biçimde kur.")
    elif manipulatif_dal == "zorunluluk":
        notu = (f" BAĞLAM: {dep} birimindesin ve harcama {cerceveli} oldu; "
                f"kaçınılmazlığı BU duruma yasla, havada bırakma.")
    elif olcek == "bireysel":
        notu = (f" BAĞLAM: {dep} birimindesin; bu harcamayı KENDİN için, tek başına yaptın. "
                f"Durum: '{olay}'. 'ekip için/departman için' deme -- bu senin kendi "
                f"masrafın. Bağlamı kendi cümlenle anlat.")
    else:
        notu = (f" BAĞLAM: {dep} birimindesin ve bu harcama {cerceveli} yapıldı. "
                f"Bunu kendi cümlenle anlat.")
    return notu, meta


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
    # Persona kategori dallarından ÖNCE üretilir: departman ondan türetiliyor
    # (bkz. baglam_notu_uret) ve `yeterli`/`manipulatif` talimatına giriyor.
    persona = persona_uret()
    baskin_ham = baskin_kategori(fatura["kalemler"])   # ham enum (KATEGORI_GRUBU anahtarı)

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
        # "Yalnızca fişte bulunan kalemlerden söz et" kuralı, gizlenecek kalemi hiç
        # anmaması gereken MANİPÜLATİF ve kalemden hiç bahsetmeyen YETERSİZ talimatıyla
        # çelişiyordu -> 8B çelişkili talimatta ortalama alıp karakteri bozuyordu.
        # Kural artık "ne yapma"ya değil UYDURMAYA odaklı: kalem anmamak serbest.
        "- Fişte OLMAYAN bir ürün/hizmet aldığını söyleme (uydurma).\n"
        "- (AI_URETIMI hariç) tutar/rakam yazma; fiyat, toplam, adet gibi sayılar açıklamada geçmesin.\n"
        "- Açıklama HARİCİNDE hiçbir şey yazma ('İşte açıklama:', 'Tabii', 'Açıklama:' YASAK).\n"
        "- Tamamen Türkçe konuş; İngilizce kelime kullanma.\n"
        "- (AI_URETIMI hariç) edilgen/resmi kalıplar (edildi, edilmiştir, sağlanmıştır, karşılanmıştır) KULLANMA; "
        "birinci tekil şahıs, doğal ve konuşma diline yakın yaz.\n"
        "- İki karaktere birden benzeyen belirsiz cümle kurma; net bir karaktere gir.\n"
        # Eskiden koşulsuz "firma adını kullan" deniyordu; ai_uretimi ("ASLA kullanma")
        # ve yetersiz ("istersen kullanma") talimatlarıyla çelişiyordu. Kural artık
        # KULLANIP KULLANMAMAYI değil, kullanılırsa BİÇİMİNİ düzenliyor.
        "- Firma/satıcı adını KULLANACAKSAN kaynak olarak kullan, doğru '-dan/-den/-tan/-ten' ekiyle: "
        "'ABC Yazılım'dan lisans aldık', 'XYZ Market'ten malzeme aldım' gibi.\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        baskin = baskin_ham.replace("_", " ")
        # YAPI iskelesi: Türkçenin amaç-sonuç / neden-sonuç kalıbını hatırlatıp
        # mantıklı senaryo kurdurur -- `yeterli`nin ana çeşitlilik kaldıracı.
        iskele = random.choice(YETERLI_ISKELE).format(firma=firma_kisa)
        # Sadeleştirme örneği faturanın KENDİ kaleminden kurulur (sabit örnek
        # veriliyorken 8B onu alakasız fişlerin metnine taşıyordu).
        ham_ad = max((k["aciklama"] for k in fatura["kalemler"]), key=len, default="")
        sade_ad = kalem_adi_sadelestir(ham_ad)
        sade_ornek = (f"('{ham_ad}' -> '{sade_ad}' gibi)"
                      if sade_ad and sade_ad != ham_ad.lower()
                      else "('F Saff Sıvı El Sabunu 500Ml' -> 'el sabunu' gibi)")
        talimat = (
            f"KARAKTER: YETERLİ çalışan. Harcamanın İŞ AMACINI (kiminle, ne için) net ve kendinden emin "
            f"söyle - saklayacak bir şeyin yok. YAPI: {iskele} "
            f"Fişteki gerçek kalemi ÇALIŞAN gibi SADELEŞTİREREK an {sade_ornek}; "
            f"boyut/miktar/model detayı yazma, kategori adını ('{baskin}') amaç yerine kullanma. "
            f"Birinci tekil şahıs ve GEÇMİŞ zaman (harcama olmuş bitmiş -- 'alacağım' değil 'aldım'); "
            f"edilgen ya da 3. şahıs ('alındı', 'aldı') KULLANMA. Üslup: {uslup}. "
            f"Bu bir YETERSİZ not DEĞİL: amacı belirsiz bırakma. Bu bir MANİPÜLATİF not da DEĞİL: gerçek "
            f"amacı savunmaya geçmeden söyle. Firma: {firma_kisa}."
        )
        _baglam, _bmeta = baglam_notu_uret(persona, baskin_ham)
        talimat += _baglam
        meta.update(_bmeta)

    elif kategori == "yetersiz":
        uslup = random.choice(YETERSIZ_USLUP_IPUCLARI)
        baskin_yetersiz = baskin_kategori(fatura["kalemler"]).replace("_", " ")
        ornekler = ", ".join(f"'{o}'" for o in random.sample(YETERSIZ_ORNEK_HAVUZ, 2))
        talimat = (
            f"KARAKTER: YETERSİZ çalışan. Baştan savma, muğlak, geçiştirmelik bir not yaz. İşi kiminle/neden "
            f"yaptığını ASLA söyleme - ama gizlemeye de uğraşma, sadece yazmaya üşen. Üslup: {uslup}. "
            f"Kuru bir öbek ('genel gider') ya da umursamaz bir söz ('gerekliydi aldım işte') olabilir, ör. {ornekler} "
            f"(birebir kopyalama, tarzını yakala). FİİL kullanacaksan BİRİNCİ TEKİL ŞAHIS kullan "
            f"('aldım', 'ödedim'); 'alındı', 'aldılar', 'karşılandı' gibi edilgen ya da 3. şahıs YAZMA "
            f"(üşengeçsin ama notu sen yazıyorsun). İstersen hiç fiil kullanma, kuru bir öbek bırak. "
            f"Kategori adını ('{baskin_yetersiz}') olduğu gibi yazma. "
            f"Bu bir MANİPÜLATİF not DEĞİL: kurnaz gerekçe uydurma, sahte "
            f"kılıf yok. Firma: {firma_kisa} (istersen kullanma)."
        )
    elif kategori == "manipulatif":
        # DAL SEÇİMİ ANOMALİ-FARKINDALI: çalışan neyi örttüğünü BİLİR, dolayısıyla
        # ürettiği metnin üslubu anomali türüyle nedensel olarak bağlıdır. Eskiden
        # yalnız gizlenecek KALEM aranıyor, bulunmazsa bariz/kurnaz yazı-turası
        # atılıyordu -- oysa limit_asimi/fatura_no_tekrari/gelecek_tarihli de A-grubu
        # (bilinçli) anomaliler; onlarda da örtülen somut bir şey var, sadece "kalem"
        # değil. Eşleme:
        #   yasakli_kategori | is_kolu_kategori_uyumsuzlugu -> GİZLEME  (somut kalem)
        #   limit_asimi                                     -> ZORUNLULUK (tutarı anmadan kaçınılmazlık)
        #   mukerrer_fis_yukleme                            -> KURNAZ  (aynı fişi ikinci kez yüklemek
        #                                                      bilinçli bir kurnazlıktır -> metin dikkat
        #                                                      çekmemeli, rutin görünmeli)
        #   temiz / gelecek_tarihli / fatura_no_cakismasi /  -> BARİZ | KURNAZ 50/50
#   salt teknik anomali
        # gelecek_tarihli BİLEREK dışarıda: çalışan gelecek tarihli bir fişi bilerek
        # yüklemez (böyle bir fiş zaten olamaz) -> örtülecek bir kurnazlık yok, dal
        # seçimi genel yazı-turasına kalır.
        #
        # NOT: `yasakli_kategori` buraya ETİKET ADIYLA değil, KALEMİN KENDİSİYLE giriyor --
        # yasakli_kalem_bul() alkol/eğlence/tütün/kumar kategorili kalemi bulur ve gizleme
        # dalını açar (o kalemden hiç bahsetmeyip "araya kaynatma" davranışı). Etiket yerine
        # kalemi aramak daha sağlam: union etiketi eksik kalsa bile gizleme dalı çalışır.
        yasakli = yasakli_kalem_bul(fatura["kalemler"])
        if yasakli:
            dal, gizlenecek = "gizleme", yasakli
        elif "is_kolu_kategori_uyumsuzlugu" in anomali_turleri:
            dal, gizlenecek = "gizleme", aykiri_kalem_bul(fatura["kalemler"])
        elif "limit_asimi" in anomali_turleri:
            dal, gizlenecek = "zorunluluk", None
        elif "mukerrer_fis_yukleme" in anomali_turleri:
            dal, gizlenecek = "kurnaz", None
        else:
            # 70/30 bariz lehine (eskiden 50/50): kurnaz dal bilerek "ince çizgi"
            # üretiyor ve pilotta insan gözüyle manipülatif olduğu ayırt edilemiyordu.
            # v1'de önce KALIN çizgi öğrenilsin; ince örnekler gizleme/zorunluluk/
            # kurnaz dallarından yine geliyor.
            dal, gizlenecek = ("bariz" if random.random() < 0.70 else "kurnaz"), None
        # aykiri_kalem_bul None dönebilir (tek kalemli / tek kategorili fiş) -> gizlenecek
        # bir şey yoksa gizleme talimatı kurulamaz, kurnaz dala düş.
        if dal == "gizleme" and not gizlenecek:
            dal = "kurnaz"
        meta["gizlenecek"] = gizlenecek
        meta["dal"] = dal

        # Koşul `dal == "gizleme"` yerine doğrudan `gizlenecek` üzerinden kuruluyor:
        # yukarıdaki fallback'ten sonra ikisi EŞDEĞER (gizleme dalı ⟺ gizlenecek dolu),
        # ama bu biçim tip denetleyicisine de None olmadığını gösterir.
        if gizlenecek:
            gizli_kat = gizlenecek["harcama_kategorisi"].replace("_", " ")
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu fişte '{gizlenecek['aciklama']}' ({gizli_kat}) var ve "
                f"şirket bunu ödemez/uygun görmez. Görevin bunu kurnazca GİZLEMEK! 'Müşteri ağırlama', "
                f"'temsil gideri', 'toplantı gideri' gibi aşırı kurumsal kılıflar uydur. Gerçek kalemin adını "
                f"ya da türünü ('{gizli_kat}') hiçbir formda geçirme. Bu bir YETERLİ not DEĞİL: gerçek amacı "
                f"gizliyorsun. Bu bir YETERSİZ not da DEĞİL: kısa geçiştirmiyorsun, iddialı bir kılıf kuruyorsun. "
                f"Kendinden aşırı emin, sahte kurumsal dille yaz ama BİRİNCİ TEKİL ŞAHIS kal: 'alındı', "
                f"'karşılandı', 'gerçekleştirildi' gibi edilgen/3. şahıs kalıp KULLANMA. "
                f"Satıcı: {firma_kisa}."
            )
        elif dal == "zorunluluk":
            # ZORUNLULUK dalı (limit_asimi): çalışan masrafın fazla olduğunu BİLİR.
            # Tutarı hiç anmadan kaçınılmazlık kurar. Abartılı vurgu YOK -- olursa
            # bariz dala benzer ve ayrım kaybolur.
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu masraf şirketin uygun gördüğünden PAHALIYA geldi ve bunu "
                f"biliyorsun. Tutardan, limitten, fiyattan HİÇ söz etme; bunun yerine harcamayı KAÇINILMAZ "
                f"göster: tek uygun seçenek oydu, acil ihtiyaçtı, başka yer/zaman yoktu, iş bekleyemezdi gibi "
                f"bir zorunluluk kur. Sakin ve kendinden emin yaz; abartılı vurgu ya da savunmacı ısrar "
                f"KULLANMA. Bu bir YETERLİ not DEĞİL: gerçek gerekçeyi değil, kendini aklayan bir zorunluluk "
                f"hikâyesi anlatıyorsun. Birinci tekil şahıs; 'zorunda kalındı', 'alındı' gibi edilgen/3. şahıs "
                f"kalıp YAZMA ('kalmadım', 'aldım' de). "
                f"Kusur/aykırılık/prosedürden bahsetme. Satıcı: {firma_kisa}."
            )
        elif dal == "bariz":
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
                f"fazla ısrarcısın. Edilgen ya da 3. şahıs ('alındı', 'kullanıldı') YAZMA; birinci tekil şahıs kal. "
                f"Kusur/aykırılık/prosedürden bahsetme. Sadece açıklama metnini yaz."
            )
        else:
            # KURNAZ dal: abartılı vurgu YOK. İki durumu birden karşılar: (a) fişte
            # dikkat çekmemesi gereken bir şey var (mukerrer_fis_yukleme -- aynı fişi ikinci
            # kez yükleme), (b) masraf sorunsuz ama sıradan alışveriş şişirilmiş kurumsal
            # kılıfla önemli gösteriliyor. Ortak hedef: metin SORGUSUZ onaylansın.
            # Sınırda (yeterliye yakın) -> ayırt etmeyi öğretir.
            talimat = (
                "KARAKTER: MANİPÜLATİF çalışan. Amacın bu masrafın sorgusuzca onaylanması: dikkat çekmeyen, "
                "rutin ve inandırıcı görünen bir gerekçe yaz. Sıradan bir alışverişi ÖNEMLİ bir iş kararıymış "
                # Buradaki üç literal örnek KALDIRILDI: 'stratejik değerlendirme toplantısı'
                # pilotlarda birebir kopyalanıyordu (§15'te ölçüldü, qwen3_3'te hâlâ çıktı).
                # Kılıfın nasıl kurulacağını dala özel few-shot havuzu zaten gösteriyor.
                "gibi gösteren, gereksiz derecede kurumsal bir kılıf kur. Abartılı ünlem/vurgu ya da "
                "savunmacı ısrar KULLANMA - sakin ol, fazla açıklama yapma. Birinci tekil şahıs ya da kısa "
                "kurumsal öbek; 'edilmiştir' gibi pasif kalıp YAZMA. Bu bir YETERLİ not DEĞİL: sıradan gerekçe "
                "değil, şişirilmiş kurumsal kılıf. Kusur/prosedürden bahsetme. Sadece açıklama metnini yaz."
            )
        # Bağlam YALNIZ gizleme/zorunluluk dallarına (baglam_notu_uret filtreler):
        # o iki dal anomalili faturalarda çalışır, etiket metinden bağımsızdır.
        _baglam, _bmeta = baglam_notu_uret(persona, baskin_ham, manipulatif_dal=dal)
        talimat += _baglam
        meta.update(_bmeta)
    else:  # ai_uretimi
        kapanis = random.choice(AI_URETIMI_KAPANIS_IPUCLARI)
        acilis = random.choice(AI_URETIMI_ACILIS_IPUCLARI)
        meta["kapanis"] = kapanis
        # Gerçek bir AI, fiş görselinden firma adını da fatura no/tarihi de OKUR ve
        # zaman zaman gereksiz belge ayrıntısı vererek kendini ele verir. Bu yüzden
        # "firma adını ASLA kullanma" yasağı kaldırıldı; ayrıntılar UYDURULMASIN diye
        # gerçek değerler prompt'a veriliyor (fişle çelişen sahte tarih/no üretmesin).
        # TEK meta detay: ikisi birden istendiğinde model uzun bir liste cümlesi kurup
        # token tavanına takılıyor ve cümle ORTASINDAN kesiliyordu (pilot #29, 412 krk).
        meta_detay = random.random() < 0.30
        if meta_detay:
            if random.random() < 0.5:
                detay = f"fiş tarihi {fatura.get('fatura_tarihi', '')}"
            else:
                detay = f"fatura no {fatura.get('fatura_no', '')}"
            meta_notu = (f" Ayrıca bir AI'ın yapacağı gibi TEK bir gereksiz belge ayrıntısına gönderme "
                         f"yap: {detay} (verilen değeri AYNEN kullan, uydurma; başka belge ayrıntısı ekleme).")
        else:
            meta_notu = ""
        talimat = (
            "KARAKTER: AI_URETIMI (tek istisna). İnsan değil, ChatGPT gibi bir yapay zeka gibi yaz: duygusuz, "
            "aşırı resmi, kalıpsal/şablon. Diğer üçünün aksine doğallık ARANMIYOR; bariz yapay/robotik dursun. "
            # PEMBE FİL DÜZELTMESİ: burada eskiden "hep aynı kalıbı ('Belirtilen fiş...')
            # tekrarlama" yazıyordu. Yasaklanan ifadeyi ADIYLA anmak tam tersini yaptı:
            # qwen3 pilotunda 8 ai_uretimi çıktısının 4'ü tam o ifadeyle BAŞLADI, 2'si daha
            # içinde geçirdi (havuzda 20 açılış var, beklenen ~0,4). Kural artık yalnız
            # HEDEFLENEN davranışı söylüyor (CLAUDE.md §6 pozitif çerçeveleme).
            f"Cümleye '{acilis}' gibi bir açılışla başla ve '...{kapanis}' ifadesiyle bitir; verilen açılış/"
            f"kapanışı AYNEN KULLAN, kendi kalıbını uydurma. Kısa tek cümle de olur, resmi "
            f"1-2 cümle de -- 3+ cümlelik paragraf yazma. Satıcı/firma adını kullanabilirsin.{meta_notu}"
        )

    # Few-shot SADECE user prompt'a girer (system prompt sabit -> Ollama önbelleği
    # korunur). yetersiz'de few-shot KULLANILMAZ: talimattaki dönüşümlü örneklerle
    # çakışıp verbatim kopyayı besliyordu ('genel ofis ihtiyacı' collapse'ı).
    # manipulatif'te few-shot DALA göre seçilir: ortak havuz kullanılırken kurnaz dal
    # "abartılı vurgu KULLANMA" derken prompt'una vurgulu örnekler giriyordu.
    fs_blok = "" if kategori == "yetersiz" else fewshot_blok(kategori, adet=2, dal=meta.get("dal"))

    # Persona da user prompt'ta; ai_uretimi hariç (robotik/kişiliksiz kalmalı).
    persona_notu = "" if kategori == "ai_uretimi" else f"Yazan kişi: {persona_metni(persona)}.\n"

    # USER PROMPTU (Sadece değişen kısım) + persona + kategoriden bağımsız uzunluk hedefi.
    user_prompt = (
        f"Satıcı/Firma: {firma_kisa}\nFiş kalemleri: {kalem_ozeti}\n{fs_blok}{persona_notu}"
        # Uzunluk hedefine SAYISAL tavan eşlik eder: niteliksel tarif ('1-2 cümle')
        # tek başına zayıf bir sınır, model rahatça iki katına çıkıyor (T1'de ölçüldü).
        f"Talimat: {talimat}\nUzunluk hedefi: {uz_tarif} — en fazla ~{uz_ust} karakter."
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
# DİKKAT -- `\b` ve ZORUNLU İKİ NOKTA ŞART. Eski desen (`[\s:]*`, sınır yok) ön-eki
# normal Türkçe kelimelerin başında da yakalayıp metni kesiyordu:
#   'İşten dolayı acil ihtiyaç vardı.' -> 'n dolayı acil ihtiyaç vardı.'
#   'Notebook aldım ekip için.'        -> 'ebook aldım ekip için.'
# Bu bozulma qwen3 koşularında da sessizce oluyordu (T1 pilotunda fark edildi).
# Artık ön-ek ancak GERÇEKTEN ön-ekse ('Açıklama:', 'İşte açıklama:') siliniyor.
_ON_EK_TEMIZLE = re.compile(
    r"^(Açıklama|Not|Masraf Açıklaması|İşte|Cevap)\b[^:\n]{0,40}:\s*", re.IGNORECASE
)

# Model rol yapmayı bırakıp NE YAPTIĞINI anlatan bir not eklerse (reasoning
# modellerinde sık): '*(Not: Yetersiz çalışan olarak yazıldığı için...)*',
# '*(Tek cümle, 6-10 kelime: "...")*'. Bunlar ayrı satır olarak gelir ve
# GROUND-TRUTH kategori adını metne taşıdığı için veri setine sızmaları
# leakage'tır. Satır düzeyinde ayıklanır; inline gelirse `meta_sizinti` kuralı
# (ihlalleri_bul) yakalayıp retry tetikler.
# İki yol: (a) parantez/köşeli parantezle açılıp meta kelimeyle devam eden satır,
# (b) meta kelime + iki nokta ile başlayan satır. Sadece kelimeyi aramak YANLIŞ
# POZİTİF verirdi -- 'Not defteri aldım' geçerli bir açıklamadır.
_META_ANAHTARLARI = (
    r"not|uyarı|uyari|tek cümle|tek cumle|karakter|talimat|kategori|açıklama notu|aciklama notu"
)
_META_YORUM_SATIRI = re.compile(
    rf"^[\*\s]*(?:[\(\[][\*\s]*(?:{_META_ANAHTARLARI})\b|(?:{_META_ANAHTARLARI})\s*:)[^\n]*$",
    re.IGNORECASE,
)
# Meta yorum ayrı satır yerine metnin SONUNA eklenmişse (aynı satırda) kuyruğu kes.
# Satır-İÇİ markdown vurgusu. Kenar temizliği (`.strip('*')`) yalnız uçları alıyordu;
# T1 çıktılarının 4/31'inde metnin ORTASINDA '**Tarım Kredi Kooperatifi**' gibi
# kalıntı vardı (qwen3'te 0). Çalışanın masraf notunda markdown olmaz -> ayıkla.
_MARKDOWN_VURGU = re.compile(r"\*\*|__|`")

_META_KUYRUK = re.compile(
    rf"[\*\s]*[\(\[][\*\s]*(?:{_META_ANAHTARLARI})\b[^\)\]]*[\)\]][\*\s]*$", re.IGNORECASE
)


def cikti_temizle(metin: str) -> str:
    """Ham yanıttan düşünce/ön-ek/sarma temizler, kalan anlamlı satırları TEK metne
    birleştirir.

    Eskiden yalnız İLK anlamlı satır dönüyordu; "2-3 cümle" uzunluk hedefi alan
    üretimlerde model tek '\\n' ile satır kırınca metnin gerisi sessizce atılıyor,
    ardından `uzunluk` ihlali doğup boşuna retry tetikliyordu. Çift satır sonu zaten
    stop dizisiyle (`stop=["\\n\\n"]`) kesildiği için kalan satırlar aynı açıklamanın
    parçasıdır. VS akışı bu fonksiyondan geçmez (ham=True)."""
    metin = _THINK_REGEX.sub("", metin)
    metin = _MARKDOWN_VURGU.sub("", metin).strip()
    satirlar = [s.strip() for s in metin.splitlines() if s.strip()]
    temiz = [s for s in satirlar if not _DUSUNCE_ONSOZ.match(s)]
    # Meta yorum satırlarını (model kendi çıktısını açıklıyor) at -- ama HEPSİ
    # meta ise elde bir şey bırak: o zaman `meta_sizinti` kuralı ihlali görsün ve
    # retry tetiklensin (sessizce boş metin döndürmek teşhisi zorlaştırırdı).
    meta_suzulmus = [s for s in temiz if not _META_YORUM_SATIRI.match(s)]
    if meta_suzulmus:
        temiz = meta_suzulmus
    aday_liste = temiz or satirlar
    parcalar: list[str] = []
    for s in aday_liste:
        # Ön-ek ('Açıklama:', 'İşte...') yalnız metnin BAŞINDA anlamlı.
        s2 = _ON_EK_TEMIZLE.sub("", s) if not parcalar else s
        s2 = s2.strip().strip('"\'“”‘’*`>-').strip()
        if len(s2) >= 3 and re.search(r"[a-zçğıöşüA-ZÇĞİÖŞÜ]{2,}", s2):
            parcalar.append(s2)
    if parcalar:
        # Aynı satıra iliştirilmiş meta kuyruğunu da kes ('... aldım *(Not: ...)*').
        return _META_KUYRUK.sub("", " ".join(parcalar)).strip()
    if aday_liste:
        return _ON_EK_TEMIZLE.sub("", aday_liste[-1]).strip().strip('"\'“”‘’*`>-').strip()
    return ""


_CUMLE_SONU = re.compile(r"(?<=[.!?…])\s+")


def uzunluk_buda(metin: str, uz_ust: int, tolerans: float = 1.15) -> str:
    """Hedef tavanı aşan metni TAM CÜMLE sınırından budar.

    Neden gerekli: uzunluk ihlali retry ile güvenilir biçimde düzelmiyor (T1'de
    7/31, retry sonrası bile). num_predict'i kısmak ise cümleyi ORTASINDAN keser --
    bozuk gramer, veri setini zehirler. Budama deterministiktir ve yalnız cümle
    sonlarından keser.

    İlk cümle bile tavana sığmıyorsa metne DOKUNULMAZ: yarım cümle bırakmaktansa
    uzun bırakıp `ihlalleri_bul`'un uzunluk ihlalini görmesi (ve retry/raporun
    tetiklenmesi) yeğdir. Budama sonrası metin yine ihlal denetiminden geçer;
    budama bir dayanağı (kalem/amaç) düşürdüyse ilgili kural zaten yakalar."""
    if not metin or uz_ust <= 0:
        return metin
    tavan = int(uz_ust * tolerans)
    if len(metin) <= tavan:
        return metin
    parcalar = _CUMLE_SONU.split(metin.strip())
    tutulan: list[str] = []
    for p in parcalar:
        aday = " ".join(tutulan + [p])
        if tutulan and len(aday) > tavan:
            break
        tutulan.append(p)
        if len(aday) > tavan:   # ilk cümle zaten taşıyor -> dokunma
            return metin
    budanmis = " ".join(tutulan).strip()
    return budanmis if budanmis else metin


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
    profil = model_profili(model)
    istek_govdesi: dict = {
        "model": model,
        "stream": False,
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

    # Model profiline göre istek biçimi (bkz. MODEL_PROFILLERI).
    if profil["raw_chatml"]:
        istek_govdesi["raw"] = True   # Ollama şablonunu uygulama, prompt'u aynen gönder
        istek_govdesi["prompt"] = chatml_sar(system_prompt, user_prompt, profil["think_prefill"])
    else:
        istek_govdesi["system"] = system_prompt
        istek_govdesi["prompt"] = user_prompt
    # think alanı: None ise gövdeye HİÇ konmaz (şablonu desteklemeyen modelde
    # alanın varlığı ya hata verir ya da sessizce yanıltır).
    if profil["think_alani"] is not None:
        istek_govdesi["think"] = profil["think_alani"]

    if seed is not None:
        istek_govdesi["options"]["seed"] = seed
    # stop: çok-paragraflı gevezeliği kes (num_ctx/num_predict israfını önler).
    # Profil stop tanımlıyorsa çağıranınkini EZER -- raw modda `\n\n` prefill'i
    # anında kesip boş metin üretirdi.
    stop = profil["stop"] or stop
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

# ---------------------------------------------------------------------------
# EDİLGEN / 3. ŞAHIS DENETİMİ (pasif_kalip'in morfolojik hali)
# ---------------------------------------------------------------------------
# Sabit 5 kalıplık liste yetersizdi: pilot koşusunda 24 insan çıktısının 8'i (%33)
# edilgen ya da 3. şahıs geldi ve HİÇBİRİ yakalanmadı -- 'aldılar', 'alınmış',
# 'karşılandı', 'alındı', 'kullanıldı', 'gerçekleştirildi', 'aldı'. Oysa hem sistem
# promptu hem karakter tanımı birinci tekil şahıs istiyor (çalışan kendi notunu yazar).
#
# KELİME SINIRI ŞART: substring denetimi yapılırsa 'aldı' -> 'aldım'ı da yakalar ve
# doğru çıktıyı ihlal sayar. \b ile 'aldım'/'aldık' (1. şahıs) SERBEST kalır,
# 'aldı'/'aldılar' (3. şahıs) yakalanır.
_PASIF_KOKLER = (
    "alın", "kalın", "karşılan", "kullanıl", "yapıl", "öden", "veril", "edil",
    "gerçekleştiril", "sağlan", "temin edil", "tedarik edil", "tamamlan", "gönderil",
)
_PASIF_REGEX = re.compile(
    r"\b(?:" + "|".join(_PASIF_KOKLER) + r")(?:dı|di|du|dü|mış|miş|muş|müş)(?:tır|tir|tur|tür)?\b",
    re.IGNORECASE,
)

# 3. şahıs (tekil/çoğul) geçmiş zaman -- YALNIZCA bilinen fiil kökleriyle; serbest
# ek taramasi 'toplantılar' gibi isimleri yanlişlikla yakalıyordu.
_UCUNCU_SAHIS_KOKLER = (
    "al", "öde", "kullan", "karşıla", "yap", "ver", "getir", "gerçekleştir",
    "temin et", "tedarik et", "satın al",
)
_UCUNCU_SAHIS_REGEX = re.compile(
    r"\b(?:" + "|".join(_UCUNCU_SAHIS_KOKLER) + r")(?:dı|di|du|dü|tı|ti|tu|tü)(?:lar|ler)?\b",
    re.IGNORECASE,
)


def _pasif_ya_da_ucuncu_sahis_mi(metin: str) -> bool:
    """İnsan kategorilerinde edilgen ya da 3. şahıs anlatım var mı?
    (ai_uretimi MUAF -- resmi/edilgen dil onun karakteri.)"""
    d = metin.lower()
    return (any(k in d for k in YASAKLI_PASIF_KALIPLAR)
            or bool(_PASIF_REGEX.search(metin))
            or bool(_UCUNCU_SAHIS_REGEX.search(metin)))


# ---------------------------------------------------------------------------
# KATEGORİ ADI SIZINTISI (boşluklu form)
# ---------------------------------------------------------------------------
# `kalemler_ozetle_prompt` kategoriyi insan-okur gösteriyor ('kisisel bakim') ki model
# ham enum'u ('kisisel_bakim') kopyalayamasin. Ama pilotta model bu kez BOŞLUKLU formu
# kopyaladi: 24 insan çiktisinin 7'si (%29) kategori adini ürün adi yerine kullandi
# ('yemek hizmeti aldım', 'teknoloji ekipmanı aldı'). Gerçek bir çalişan kategori adi
# değil ÜRÜN adi yazar ('öğle yemeği', 'tv askısı').
#
# YALNIZCA ÇOK KELİMELİ kategoriler denetlenir: 'konaklama', 'giyim', 'temizlik' gibi
# tek kelimeliler doğal Türkçedir ve yasaklanirsa yanliş pozitif üretir. Çok kelimeli
# form ('ofis sarf malzeme', 'teknoloji ekipman') ise ancak sistem etiketinden kopyalanmiş
# olabilir. Çekim ekleri substring eşleşmesiyle kapsanir ('teknoloji ekipman' ⊂
# 'teknoloji ekipmanlarının'). ai_uretimi MUAF -- urun_detay_kopya ile ayni mantik.
def _kategori_adi_sizinti_mi(metin: str, kalemler: list[dict]) -> bool:
    n = _tr_normalize(metin)
    for k in kalemler:
        enum_ad = k["harcama_kategorisi"]
        if "_" not in enum_ad:
            continue   # tek kelimeli -> doğal Türkçe olabilir, denetleme
        if _tr_normalize(enum_ad.replace("_", " ")) in n:
            return True
    return False

DUZELTME_NOTLARI = {
    "sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında gizlenmesi gereken kalemin gerçek adı ya da "
        "kategorisi açığa çıktı. Bu kez o kalemden HİÇ bahsetme, sadece diğer/meşru kalemlere odaklan."
    ),
    "pasif_kalip": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında EDİLGEN ya da 3. ŞAHIS anlatım vardı "
        "('alındı', 'aldı', 'aldılar', 'karşılandı', 'kullanıldı', '...edilmiştir' gibi). "
        "Notu YAZAN SENSİN: bu kez SADECE birinci tekil şahıs aktif fiil kullan -- "
        "'aldım', 'ödedim', 'kullandım'. İstersen fiili tamamen at, öbek olarak bırak."
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
        "ÖNEMLİ DÜZELTME: Az önceki cevabın harcamanın SEBEBİNİ söylemedi (sadece ne aldığını "
        "yazmak yetmez). Bu kez hangi iş bağlamında olduğunu somut belirt: toplantı, müşteri "
        "ziyareti, saha işi, mesai, randevuya yetişme gibi. Kalem ve firma adı isteğe bağlı."
    ),
    "enum_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında SİSTEM KATEGORİ ADI geçti (ör. 'kisisel_bakim', "
        "'yemek hizmeti', 'teknoloji ekipman'). Gerçek bir çalışan kategori adı değil ÜRÜNÜN "
        "kendi adını yazar ('öğle yemeği', 'tv askısı', 'şampuan'). Bu kez kategori adını hiç kullanma."
    ),
    # T1 pilotunda (2026-07-28) bu not YETMEDİ: model açıklamanın sonuna kendi
    # yaptığını anlatan bir not ekledi ve GROUND-TRUTH kategori adını metne taşıdı
    # ('**Yeterli çalışan** olarak: Harcamayı net amaçlandı...'). Etiket doğrudan
    # feature'a sızdığı için not artık YASAĞI ADIYLA söylüyor.
    "meta_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın görevi/fişi betimledi ya da nasıl yazdığını "
        "açıkladı. Sadece masraf açıklamasının KENDİSİNİ yaz: sonuna not/parantez içi "
        "yorum EKLEME, 'yeterli/yetersiz/manipülatif/AI' gibi karakter adlarını ve "
        "talimattan aldığın kelimeleri (uzunluk, karakter, kategori) HİÇ kullanma."
    ),
    "latin_disi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında Türkçe/Latin alfabesi dışında karakter vardı. "
        "Bu kez metnin TAMAMINI Türkçe yaz; başka alfabeden tek karakter bile kullanma."
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
    "tutar_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında tutar/rakam geçti. Gerçek bir çalışan "
        "açıklamaya fiyat yazmaz; bu kez hiçbir sayı, tutar ya da 'TL' ifadesi kullanma."
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
    arasındaki eşleşme farkını kapatır.

    DİKKAT -- `\\u0307` (combining dot above) SİLİNMELİ: Python'da 'İ'.lower()
    ASCII 'i' değil 'i̇' (i + birleşen nokta) üretir. Bu yüzden modelin BÜYÜK
    harfle yazdığı her kelime normalize sonrası eşleşmiyordu ve substring'e dayanan
    TÜM kurallar (meta_sizinti, sizinti, enum_sizinti, karakter_kirilmasi) o metinlerde
    sessizce devre dışı kalıyordu. T1 pilotunda yakalandı: metin 'YETERSİZ çalışanın'
    diyordu, `_META_SIZINTI_KALIPLARI`'ndaki 'yetersiz calisan' eşleşmiyordu."""
    return s.lower().replace("̇", "").translate(_TR_HARF_MAP)


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


# Resmî/edilgen AI kapanışının MORFOLOJİK imzası: '-mIştIr' (gerçekleştirilmiştir,
# sağlanmıştır, temin edilmiştir...). Havuz ÜYELİĞİ tek başına şart koşulursa
# ai_uretimi veri sette kapalı bir string kümesine indirgenir (sınıflandırıcı üslubu
# değil listeyi ezberler) ve havuz her büyüdüğünde validator'ı güncellemek gerekir.
_AI_RESMI_KAPANIS = re.compile(r"(mış|miş|muş|müş)t[ıi]r\b", re.IGNORECASE)


def _kapanis_var_mi_herhangi(metin: str) -> bool:
    """Metin resmî/edilgen bir AI kapanışı taşıyor mu? İki yoldan biri yeter:
    (a) '-mIştIr' morfolojik imzası, (b) havuzdaki kapanışlardan birinin geçmesi.
    'Bitiyor mu' değil 'içeriyor mu' esnekliği korunur (kapanıştan sonra minik bir
    ek gelmesi boşuna retry üretmesin)."""
    if _AI_RESMI_KAPANIS.search(metin):
        return True
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
    # GROUND-TRUTH kategori adının metne sızması. Reasoning modelleri (T1) rol
    # yapmayı bırakıp "hangi karakteri oynadığını" yazabiliyor:
    #   'gerekliydi aldım işte (Not: YETERSİZ ÇALIŞAN olarak yazıldığı için...)'
    # Bu, etiketin doğrudan feature'a sızması demektir -> ihlal say, retry tetikle.
    # cikti_temizle ayrı satırdaki meta yorumu zaten atıyor; bu kural inline
    # kalanlar için backstop.
    "yeterli calisan", "yetersiz calisan", "manipulatif calisan", "ai uretimi",
    "karakter olarak", "karaktere gir", "talimatlara uy", "talimata uy",
    "istenen karakter", "kelime siniri", "uzunluk hedefi",
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


def _fatura_kaynak_kelimeleri(fatura: dict) -> list[str]:
    """Faturadan gelen (firma adı + kalem adları) normalize kelimeler."""
    kaynaklar = [firma_adi_kisalt(fatura["satici_unvan"])]
    kaynaklar += [k["aciklama"] for k in fatura["kalemler"]]
    kelimeler = []
    for kaynak in kaynaklar:
        kelimeler += [w for w in _tr_normalize(kaynak).split() if len(w) > 2]
    return kelimeler


def _yeterli_halusinasyon_mi(metin: str, fatura: dict) -> bool:
    n = _tr_normalize(metin)
    # Firma adı ve kalem adları taramadan DÜŞÜLÜR: registry'de adında 'restoran/yemek'
    # geçen 161 OSM firması var ve sistem promptu firma adını kullanmaya izin veriyor.
    # Aksi halde is_kolu uyumsuzluğu anomalisinde ("Keyif Restoran'dan teknoloji ürünü
    # aldım") halüsinasyon flag'i düşüyor ve firma adı değişmediği için retry ile
    # DÜZELTİLEMİYOR -> kalan-ihlalli çıktı.
    for kel in _fatura_kaynak_kelimeleri(fatura):
        n = n.replace(kel, " ")
    if not any(t in n for t in _YEMEK_TEMA):
        return False
    return not any(k["harcama_kategorisi"] in _YEMEK_KATEGORILERI for k in fatura["kalemler"])


def _yeterli_dayanaksiz_mi(metin: str, fatura: dict) -> bool:
    """yeterli açıklama İŞ AMACINI/BAĞLAMINI söylemek ZORUNDA.

    KURAL DEĞİŞTİ (2026-07-28): eskiden "iş amacı VEYA firma adı VEYA gerçek kalem"
    üçlüsünden biri yeterliydi -> salt kalem listesi geçerli sayılıyordu. T1 pilotunda
    ölçüldü: 'Barbekü Soslu Tavuk, Tavuklu Pizza, Hot Dog ve Ispanaklı Gözleme aldım.'
    hiçbir ihlal almadı, oysa `yeterli`nin TANIMI amacı söylemesidir. Yeni sözleşme:

        ZORUNLU : masraf sebebi/bağlamı (toplantı, ziyaret, mesai, randevu, işe geliş...)
        SERBEST : kalem (sadeleştirilmiş), firma adı, neden-sonuç bağlacı
        YASAK   : salt kalem listesi ('X, Y ve Z aldım.')

    Kalem/firma artık tek başına ÇIPA SAYILMAZ; amaç yoksa açıklama pratikte
    `yetersiz`e kaymıştır. Eşleşme token+kök bazlıdır (bkz. _amac_koku_var_mi):
    substring denemesi eskiden 2 harflik "is" girdisiyle 'kişisel'/'sistem'/'bisiklet'
    gibi kelimelerde eşleşip kuralı fiilen devre dışı bırakıyordu."""
    n = _tr_normalize(metin)
    n_tok = set(re.findall(r"\w+", n))
    # FİRMA ADI TOKEN'LARI DÜŞÜLÜR: registry'deki 28.882 firmanın 2.238'i (%7,7) adında
    # bir amaç kelimesi taşıyor (danismanlik 766, organizasyon 419, denetim 381,
    # etkinlik 367, seyahat 199...). Firma adı anıldığı anda kural kendiliğinden tatmin
    # oluyordu: "Üzerimdeki stresle başa çıkmak için ... Kaçira Organizasyon'dan aldım."
    # amaç TAŞIMADIĞI hâlde geçiyordu. Amaç, firma adı DIŞINDA bir yerden gelmeli.
    # (Aynı ilke _yeterli_halusinasyon_mi'de zaten var: firma/kalem adları taranmaz.)
    for kaynak in (fatura["satici_unvan"], firma_adi_kisalt(fatura["satici_unvan"])):
        n_tok -= set(re.findall(r"\w+", _tr_normalize(kaynak)))
    # 1) çok-kelimeli iş-amacı/bağlam ifadesi (substring aranması güvenli)
    if any(ifade in n for ifade in _YETERLI_AMAC_IFADELERI):
        return False
    # 2) iş-amacı kökü (ünsüz yumuşaması dahil: 'ekibine' -> 'ekip')
    return not _amac_koku_var_mi(n_tok)


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


# TUTAR/RAKAM SIZINTISI: sistem promptu insan kategorilerinde rakam yasaklıyordu ama
# denetlenmiyordu. Gerçek bir çalışan açıklamaya fiyat yazmaz; ai_uretimi ise MUAF
# (fazla detay vermek AI ayracıdır -- urun_detay_kopya ile aynı mantık).
# Yıllar (19xx/20xx) hariç tutulur: 'fuar 2026' meşru bir bağlam ifadesidir.
_TUTAR_SIZINTI = re.compile(
    r"\b(?!19\d{2}\b|20\d{2}\b)\d{3,}\b"   # 3+ haneli sayı (yıl değilse)
    r"|\b\d+[.,]\d{2}\b"                     # 1250,00 / 45.90
    r"|₺|\bTL\b|\blira\b",
    re.IGNORECASE,
)


# LATİN DIŞI ALFABE: sistem promptu "tamamen Türkçe" diyor ama denetlenmiyordu.
# qwen3_3 pilotunda bir çıktı Çince sızdırdı: "Vizyon'dan giyim类产品 aldım".
# Çok dilli modellerde nadir ama gerçek; tek karakter bile metni kullanılamaz kılar.
# CJK + Kiril + Arap/Fars + Yunan + İbrani + Hangul + Kana blokları.
_LATIN_DISI = re.compile(
    r"[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ"
    r"぀-ヿ㐀-䶿一-鿿가-힯]"
)


def _latin_disi_mi(metin: str) -> bool:
    return bool(_LATIN_DISI.search(metin))


def _tutar_sizinti_mi(metin: str) -> bool:
    return bool(_TUTAR_SIZINTI.search(metin))


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


def _verbatim_kopya_mi(metin: str, kategori: str, dal: str | None = None) -> bool:
    """Üretilen metin, prompt'ta gösterilen FEW-SHOT örneklerinden birine neredeyse
    birebir mi? Stil demirleme yerine kopya -> çeşitlilik ölür. yetersiz'e UYGULANMAZ:
    kısa muğlak öbekler ('iş gideri') doğal olarak örnek havuzuyla örtüşür, bu geçerli.
    manipulatif'te prompt'a giren DAL havuzuna bakılır (gösterilmeyen örnekle
    karşılaştırıp yanlış pozitif üretmemek için)."""
    if kategori == "yetersiz":
        return False
    havuz = _fewshot_havuz(kategori, dal)
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
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _pasif_ya_da_ucuncu_sahis_mi(metin):
        ihlaller.append("pasif_kalip")

    # ai_uretimi: havuzdaki HERHANGİ bir AI-kapanışıyla bitmesi yeter (tek belirli
    # olanı dayatmıyoruz -> gereksiz retry azalır, çeşitlilik prompt'tan gelir).
    if kategori == "ai_uretimi" and not _kapanis_var_mi_herhangi(metin):
        ihlaller.append("kapanis_eksik")

    # Faz 2: yeterli halüsinasyonu (fişte olmayan 'yemek/ağırlama' teması uydurma).
    if kategori == "yeterli" and _yeterli_halusinasyon_mi(metin, fatura):
        ihlaller.append("yeterli_halusinasyon")

    # Faz 3: yeterli dayanak kontrolü (gerçek kalem/firma/iş-amacı çıpası yoksa muğlak).
    if kategori == "yeterli" and _yeterli_dayanaksiz_mi(metin, fatura):
        ihlaller.append("yeterli_dayanaksiz")

    # Faz 6: ürün-detay kopyası -- SADECE insan kategorileri (yeterli/yetersiz/
    # manipulatif). ai_uretimi serbest (ham ürün adını taşıması AI ayracıdır).
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _urun_detay_kopya_mi(metin):
        ihlaller.append("urun_detay_kopya")

    # Tutar/rakam sızıntısı -- aynı kapsam (insan kategorileri); ai_uretimi MUAF.
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _tutar_sizinti_mi(metin):
        ihlaller.append("tutar_sizinti")

    # Faz 2: kategoriden BAĞIMSIZ denetimler (her kategori için).
    # Ham enum ('kisisel_bakim') her kategoride ihlal; BOŞLUKLU form ('kisisel bakim')
    # yalnız insan kategorilerinde (ai_uretimi'nde serbest -- AI ayracı).
    if _enum_sizinti_var_mi(metin, kalemler) or (
        kategori in ("yeterli", "yetersiz", "manipulatif")
        and _kategori_adi_sizinti_mi(metin, kalemler)
    ):
        ihlaller.append("enum_sizinti")
    if _meta_sizinti_var_mi(metin):
        ihlaller.append("meta_sizinti")
    if _latin_disi_mi(metin):
        ihlaller.append("latin_disi")
    if _verbatim_kopya_mi(metin, kategori, meta.get("dal")):
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
    # manipulatif'e ALT SINIR: pilotta 45 karakterlik bir "kurnaz" çıktı ('İş geliştirme
    # amaçlı yemek hizmeti kullanıldı') insan gözüyle YETERSİZ okunuyordu. O uzunlukta
    # şişirilmiş kurumsal kılıf kurulamıyor -> kategori sadakati düşüyor.
    elif kategori == "manipulatif":
        uz_alt = max(uz_alt, 55)
    # Üst sınıra tolerans: rastgele hedef ile modelin doğal uzunluğu arasındaki ufak
    # sapmalarda gereksiz retry olmasın (flag gürültüsünü azaltır). %15 -> %25:
    # `uzunluk` en sık tetiklenen ihlaldi ve retry'ların çoğu metni iyileştirmiyordu.
    uz_ust_tol = int(uz_ust * 1.25)
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
            # Normal akışla aynı budama (yoksa VS kategorileri -- yetersiz/manipulatif --
            # uzunluk tavanından muaf kalırdı).
            t = uzunluk_buda(t, meta.get("uzunluk", (None, 0, 0))[2])
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
    # ai_uretimi 140 -> 220: meta detay + açılış/kapanış zorunluluğu birleşince 140 token
    # cümleyi ORTASINDAN kesiyordu (pilot #29). Uzunluk hedefi zaten ayrıca denetleniyor.
    # num_predict artık KATEGORİYE değil SEÇİLEN UZUNLUK HEDEFİNE bağlı. Sabit limit
    # iki yönden de yanlıştı: 'uzun' hedefi alan bir yeterli 100 token'da cümle
    # ORTASINDAN kesiliyordu (§15/4), 'çok kısa' hedefi alan üretim ise hiç fiziksel
    # fren görmüyordu (T1 pilotunda uzunluk 7/31 ihlalle en sık sorun). ~2 karakter/token
    # (Türkçe) + %50 pay: hedefe uyan metin RAHAT sığar, hedefi katlayan sığmaz.
    uz_ust_hedef = meta.get("uzunluk", (None, 0, 150))[2]
    token_limiti = int(uz_ust_hedef / 2.0) + 30
    if kategori == "ai_uretimi":
        # Zorunlu açılış (~25 krk) + kapanış (~30 krk) taban maliyeti var.
        token_limiti = max(token_limiti, 120)
    token_limiti = min(token_limiti, 260)
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

        # Hedefi aşan metni tam cümleden buda (retry'ın güvenilir düzeltemediği
        # tek ihlal uzunluktu). Denetim budanmış metin üzerinde yapılır.
        metin = uzunluk_buda(metin, meta.get("uzunluk", (None, 0, 0))[2])
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
