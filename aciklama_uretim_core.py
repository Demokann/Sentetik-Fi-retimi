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


def kalemler_ozetle(kalemler: list[dict]) -> str:
    return ", ".join(f"{k['aciklama']} ({k['harcama_kategorisi']})" for k in kalemler)


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


YETERLI_USLUP_IPUCLARI = [
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "gündelik konuşma diliyle, fazla resmi olmayan bir ifade",
]

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

# yetersiz kategorisinde de benzer şekilde "...temin edilmiştir" kalıbına
# yakınsama vardı (%22). Üslup çeşitliliği için rastgele seçim.
YETERSIZ_USLUP_IPUCLARI = [
    "sadece firma adını an, iş amacından hiç bahsetme",
    "'ihtiyaç için', 'iş ile ilgili' gibi çok genel geçer ifadelerle geç",
    "hangi ürün/hizmet olduğunu belirtme, sadece genel bir harcama olduğunu söyle",
    "kısa ve detaysız, tek bir öbek halinde",
    "'çeşitli', 'genel', 'muhtelif' gibi belirsizlik bildiren kelimelerle",
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
    "Hiç şüphesiz iş için",
]
_VURGU_ANAHTARLARI = ("kesinlikle", "yuzde yuz", "tamamen is", "suphesiz")

# Kategoriye özel örnekleme sıcaklığı. yeterli fişteki gerçek kalemlere
# dayanmalı -> düşük temp uydurma/halüsinasyonu azaltır. Diğerleri çeşitlilik
# (ai_uretimi kapanış, yetersiz muğlaklık, manipulatif üslup) için 0.9'da kalır.
KATEGORI_SICAKLIK = {
    "yeterli": 0.6,
    "yetersiz": 0.9,
    "manipulatif": 0.9,
    "ai_uretimi": 0.9,
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
    kalem_ozeti = kalemler_ozetle(fatura["kalemler"])
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    meta: dict = {}

    # SISTEM PROMPTU (Sabit, Ollama bunu önbelleğe alacak)
    system_prompt = (
        "Sen bir şirket çalışanısın. Masraf fişini masraf uygulamasına yüklerken kısa bir açıklama yazıyorsun.\n"
        "KURALLAR:\n"
        "- Açıklamayı her zaman iş/şirket bağlamında tut.\n"
        "- Ürünü değil, onu NEDEN aldığını/harcadığını yaz.\n"
        "- Sadece harcamanın amacını yaz (rakam/tutar olmadan).\n"
        "- Yalnızca fişte gerçekten bulunan kalemlerden söz et.\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        baskin = baskin_kategori(fatura["kalemler"]).replace("_", " ")
        talimat = (
            f"Bu fiş ağırlıklı olarak '{baskin}' harcamasıdır; açıklamayı YALNIZCA buna dayandır, "
            f"fişte olmayan ürün/eylem uydurma. Harcamanın İŞ AMACINI net belirt (kiminle, neden). "
            f"Satıcıyı ({firma_kisa}) doğal şekilde dahil et. Üslup: {uslup}. "
            f"Cümleyi şu aktif fiillerden biriyle kur: 'aldım', 'satın aldım', 'ödedim', 'harcadım', 'ısmarladım'. "
            f"Tek cümle, 40-90 karakter arası. Sadece açıklama metnini yaz."
        )

    elif kategori == "yetersiz":
        uslup = random.choice(YETERSIZ_USLUP_IPUCLARI)
        talimat = (
            f"Açıklama MUĞLAK ve genel kalsın, spesifik detaya girme. Üslup: {uslup}. "
            "Firma adını istersen anabilirsin. Kısa bir isim/öbek olarak bırak, ör. "
            "'genel ofis ihtiyacı', 'iş gideri', 'muhtelif harcama', 'departman ihtiyacı'. "
            "Kısa tut (en fazla ~70 karakter). Sadece açıklama metnini yaz."
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
            baglam = f"Fişte '{gizlenecek['aciklama']}' ({gizlenecek['harcama_kategorisi']}) kalemi var ve bu aykırı olabilir."
            talimat = (
                f"{baglam} Bu kalemi meşru bir iş gideriymiş gibi göster; onu genel/örtük bir ifadeyle geçiştir "
                f"(ör. 'ikram', 'toplantı gideri', 'ağırlama', 'departman ihtiyacı'). Diğer kalemlerle tutarlı kal, "
                f"satıcı adını ({firma_kisa}) kullan. Sadece açıklama metnini yaz."
            )
        else:
            vurgu = random.choice(MANIPULATIF_VURGU_IPUCLARI)
            meta["vurgu"] = vurgu
            talimat = (
                f"Bu masraf aslında sorunsuz. Yine de açıklamayı, harcamayı gereğinden FAZLA ısrarla ve "
                f"abartılı bir üslupla haklı çıkarmaya çalış. Açıklamada '{vurgu}' gibi abartılı bir vurgu "
                f"MUTLAKA geçsin; ama onu cümlenin başına, ortasına ya da sonuna doğal düşen bir yere yerleştir. "
                f"Sadece açıklama metnini yaz."
            )
    else:  # ai_uretimi
        kapanis = random.choice(AI_URETIMI_KAPANIS_IPUCLARI)
        meta["kapanis"] = kapanis
        talimat = (
            "Açıklama, yapay zeka tarafından üretilmiş gibi kalıpsal/şablon bir cümle olsun. "
            f"Cümleyi MUTLAKA '...{kapanis}' ifadesiyle bitir. "
            "Satıcı/firma adını ASLA kullanma. Tek cümle. Sadece açıklama metnini yaz."
        )

    # USER PROMPTU (Sadece değişen kısım)
    user_prompt = f"Satıcı/Firma: {firma_kisa}\nFiş kalemleri: {kalem_ozeti}\nTalimat: {talimat}"

    return system_prompt, user_prompt, meta


# Session oluştur (Global seviyede) -- keep-alive bağlantı havuzu için
http_session = requests.Session()


def ollama_cagir(
    system_prompt: str,
    user_prompt: str,
    model: str,
    host: str,
    num_predict: int = 90,
    keep_alive: str | int | None = None,
    temperature: float = 0.9,
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
            "num_ctx": 1024,
        },
    }
    # keep_alive üst-seviye alandır (options içinde değil); burst boyunca modelin
    # bellekte kalması için runner bunu geçebilir.
    if keep_alive is not None:
        istek_govdesi["keep_alive"] = keep_alive

    yanit = http_session.post(f"{host}/api/generate", json=istek_govdesi, timeout=60)
    yanit.raise_for_status()

    # Her ihtimale karşı metin içindeki <think> bloklarını Python regex ile temizliyoruz
    metin = yanit.json().get("response", "").strip()
    metin = re.sub(r"<think>.*?</think>", "", metin, flags=re.DOTALL).strip()

    return metin


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
    "uzunluk": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabının uzunluğu kurala uymadı. Bu kez KESİNLİKLE "
        "tek cümle ve istenen karakter aralığında yaz."
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
    """Metin, havuzdaki AI-kapanışlarından HERHANGİ biriyle bitiyor mu? (Tek
    belirli olanı dayatmak yerine -- çeşitlilik prompt'ta zaten isteniyor,
    ama gereksiz retry'ı bu esneklik önler.)"""
    normalize = metin.lower().rstrip(" .!?\n\t")
    return any(normalize.endswith(k.lower()) for k in AI_URETIMI_KAPANIS_IPUCLARI)


def _vurgu_var_mi(metin: str) -> bool:
    """manipulatif 'aşırı haklı çıkarma' metninde abartılı vurgu işareti var mı?
    (Türkçe normalize edilmiş anahtarlardan herhangi biri geçiyorsa var sayılır.)"""
    n = _tr_normalize(metin)
    return any(anahtar in n for anahtar in _VURGU_ANAHTARLARI)


def ihlalleri_bul(metin: str, kategori: str, fatura: dict, meta: dict | None = None) -> list[str]:
    meta = meta or {}
    ihlaller = []

    if kategori == "manipulatif":
        # meta yoksa (geriye dönük çağrı) gizlenecek'i yeniden hesapla; ama meta
        # varsa onu kullan -- çünkü prompt_olustur gizleme dalını yalnızca gerçek
        # gizlenecek durumda seçer (temizde gizlenecek=None olur, sızıntı aranmaz).
        gizlenecek = meta["gizlenecek"] if "gizlenecek" in meta else gizlenecek_kalem_bul(fatura["kalemler"])
        if gizlenecek and _sizinti_var_mi(metin, gizlenecek):
            ihlaller.append("sizinti")
        # "aşırı haklı çıkarma" dalında (meta["vurgu"] varsa) zorunlu vurgu var mı?
        if meta.get("vurgu") and not _vurgu_var_mi(metin):
            ihlaller.append("vurgu_eksik")

    # Pasif/resmi kalıp yasağı hem yeterli hem yetersiz için geçerli (ikisinde de
    # prompt bunu yasaklıyor; eskiden yalnız yeterli denetleniyordu).
    if kategori in ("yeterli", "yetersiz") and any(k in metin.lower() for k in YASAKLI_PASIF_KALIPLAR):
        ihlaller.append("pasif_kalip")

    # ai_uretimi: havuzdaki HERHANGİ bir AI-kapanışıyla bitmesi yeter (tek belirli
    # olanı dayatmıyoruz -> gereksiz retry azalır, çeşitlilik prompt'tan gelir).
    if kategori == "ai_uretimi" and not _kapanis_var_mi_herhangi(metin):
        ihlaller.append("kapanis_eksik")

    # Uzunluk: kategoriye özel alt/üst sınır. yeterli üst sınırı 120'ye gevşetildi
    # (çok-kalemli faturalarda boşa retry'ı önler); yetersiz alt sınırı 8'e indi
    # ('iş gideri' gibi bizim de örnek verdiğimiz kısa/muğlak öbekler geçsin).
    alt_sinir = 8 if kategori == "yetersiz" else 15
    if (
        len(metin) < alt_sinir
        or (kategori == "yeterli" and not (40 <= len(metin) <= 120))
        or (kategori == "yetersiz" and len(metin) > 80)
    ):
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
        else:
            notlar.append(DUZELTME_NOTLARI[i])
    return "\n".join(notlar)


def tek_fatura_isleme(fatura, etiket, model, host, keep_alive: str | int | None = None):
    """
    Bir faturayı işler: prompt kur, Ollama'yı çağır, ihlal varsa bir kez
    düzeltici retry uygula. Dönüş:
        (fatura, etiket, metin, hata, kalan_ihlaller, deneme_sayisi)
    Retry aynı worker thread'inde çalışır; ThreadPoolExecutor sayesinde
    başka faturalar işlenirken paralel gerçekleşir.
    """
    kategori = etiket["aciklama_kategorisi"]
    system_prompt, user_prompt, meta = prompt_olustur(fatura, kategori, etiket.get("anomali_turleri"))
    token_limiti = 130 if kategori == "manipulatif" else 90
    # yeterli, fişteki gerçek kalemlere DAYANMALI -> düşük temp uydurmayı azaltır.
    # ai_uretimi/yetersiz/manipulatif ise çeşitlilik/muğlaklık için yüksek temp'te kalır.
    sicaklik = KATEGORI_SICAKLIK.get(kategori, 0.9)

    metin = None
    ihlaller: list[str] = []
    for deneme in range(1, 3):  # ilk deneme + 1 düzeltici retry
        try:
            metin = ollama_cagir(
                system_prompt, user_prompt, model, host,
                num_predict=token_limiti, keep_alive=keep_alive, temperature=sicaklik,
            )
        except Exception as e:
            return fatura, etiket, None, str(e), [], deneme

        ihlaller = ihlalleri_bul(metin, kategori, fatura, meta)
        if not ihlaller:
            return fatura, etiket, metin, None, [], deneme

        if deneme == 1:
            user_prompt = user_prompt + "\n\n" + duzeltme_notu_uret(ihlaller, meta)

    return fatura, etiket, metin, None, ihlaller, 2
