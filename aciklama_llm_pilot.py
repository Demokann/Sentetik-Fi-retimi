"""
Faz 1 pilot script: faturalar.json + faturalar_etiketler.json içinden ilk
N faturayı alır, her birinin aciklama_kategorisi'ne (yeterli/yetersiz/
manipulatif/ai_uretimi) göre farklı bir prompt kurup Ollama üzerinden
qwen3:8b'ye gönderir, sonucu terminale basar. Amaç KAYDETMEK değil,
manuel olarak okuyup prompt'u kalibre etmek -- bu yüzden dosyaya yazmiyor.

Önkoşul: `ollama serve` çalişiyor olmali, `ollama pull qwen3:8b` yapilmiş olmali.

Kullanım:
    python aciklama_llm_pilot.py --input-json data/faturalar.json \
        --etiket-json data/faturalar_etiketler.json --limit 30
"""

import argparse
import json
import random
import re
import urllib.request
from collections import Counter
from pathlib import Path

OLLAMA_HOST_VARSAYILAN = "http://localhost:11434"
MODEL_VARSAYILAN = "qwen3:8b"

# schema.py:POLICY_YASAKLI_KATEGORILER ile SENKRON tutulmali -- burada
# bilerek kopyalanmiş, pilot script'i generators/ paketine (pydantic'e)
# bağimli tutmamak icin. main entegrasyonunda schema'dan import edilecek.
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


def aykiri_kalem_bul(kalemler: list[dict]) -> dict | None: #sezgisel olarak nasıl bulur? zaten o durum iş kolu kategori uyumsuzluğu olarak anomali türlerinde etiketli?
    """
    is_kolu_kategori_uyumsuzlugu gibi POLİTİKA-yasaklı OLMAYAN ama iş koluna
    yabanci kalemleri sezgisel olarak bulur: faturadaki BASKIN kategoriden
    farkli, azinlikta kalan kalemi döner. yasakli_kalem_bul'un yakalayamadiği
    durumlar icin (ör. #13'teki kişisel bakim kalemi gibi).
    """
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
    """Manipülatif açıklama icin 'gizlenecek' kalemi öncelikle belirler:
    önce politika-yasakli (en net sinyal), yoksa azinlik/yabanci kalem."""
    return yasakli_kalem_bul(kalemler) or aykiri_kalem_bul(kalemler)


YETERLI_USLUP_IPUCLARI = [
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "gündelik konuşma diliyle, fazla resmi olmayan bir ifade",
]


def prompt_olustur(fatura: dict, kategori: str) -> str:
    kalem_ozeti = kalemler_ozetle(fatura["kalemler"])
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    ortak = (
        f"Sen bir şirket çalışanısın. Aşağıdaki masraf fişini şirketin masraf "
        f"yönetim uygulamasına yüklerken kısa bir açıklama yazıyorsun.\n"
        f"Satıcı/Firma: {firma_kisa}\n"
        f"Fiş kalemleri: {kalem_ozeti}\n\n"
        f"KURALLAR (her kategori için geçerli):\n"
        f"- Açıklama HER ZAMAN İŞ/ŞİRKET bağlamında olsun -- kişisel, ailevi ya da "
        f"özel yaşamla ilgili bir gerekçe ASLA yazma.\n"
        f"- Ürünün ne olduğunu pazarlama diliyle anlatma -- sen ürünü ANLATMIYORSUN, "
        f"NEDEN aldığını yazıyorsun.\n"
        f"- Tutar veya sayısal bir değer YAZMA.\n"
        f"- Fişte OLMAYAN bir kalem türünden bahsetme (ör. yemek kalemi yokken 'iş yemeği' deme); "
        f"sadece yukarıdaki kalemlerle tutarlı bir gerekçe yaz.\n\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        talimat = (
            f"Açıklama, harcamanın İŞ AMACINI net ve spesifik şekilde belirtsin "
            f"(kiminle, neden, hangi iş bağlamında). Satıcı/firma adını ({firma_kisa}) "
            f"doğal şekilde açıklamaya dahil et. Üslup: {uslup}.\n"
            f"ÖRNEK DOĞRU CÜMLELER (bu tarzda yaz): "
            f"'{firma_kisa}'den ekip toplantısı için X aldım.' / "
            f"'Müşteri ziyareti öncesi {firma_kisa}'de X'i ödedim.' / "
            f"'{firma_kisa} üzerinden proje ekibi için X ayarladım.'\n"
            f"'temin edilmiştir', 'temin edildi', 'gerçekleştirilmiştir', 'karşılanmıştır', "
            f"'sağlanmıştır', 'alınmıştır' gibi resmi/pasif kalıpları KULLANMA -- "
            f"bunun yerine 'aldım', 'ödedim', 'ayarladım', 'katıldım', 'düzenledim' gibi "
            f"AKTİF/BİRİNCİ ŞAHIS fiiller kullan. "
            f"Tek cümle, 40-90 karakter arası. Sadece açıklama metnini yaz."
        )
        
    elif kategori == "yetersiz":
        talimat = (
            "Açıklama MUĞLAK ve genel olsun, spesifik detay VERME (kiminle, neden gibi "
            "bilgi olmasın). Firma adını istersen kullanabilirsin ama iş amacını "
            "açıklama. Yine de en az 30 karakter olsun. 'temin edilmiştir' gibi resmi "
            "kalıpları kullanma. Sadece açıklama metnini yaz."
        )
    elif kategori == "manipulatif":
        gizlenecek = gizlenecek_kalem_bul(fatura["kalemler"])
        if gizlenecek:
            baglam = (
                f"Fişte aslında '{gizlenecek['aciklama']}' ({gizlenecek['harcama_kategorisi']}) "
                f"kalemi var ve bu, faturanın geri kalanıyla uyumsuz ya da şirket politikasına "
                f"aykırı olabilir."
            )
            talimat = (
                f"{baglam} Açıklamayı, SADECE bu kalemi gizleyip meşru bir iş gideriymiş gibi "
                f"göster (ör: toplantı, ekip etkinliği, müşteri ağırlama). Diğer kalemlerle "
                f"tutarlı kal, satıcı adını ({firma_kisa}) kullanarak hikayeyi inandırıcı yap. "
                f"Gerçek ürün/kategori adını veya yasaklı kelimeleri KULLANMA. "
                f"Sadece açıklama metnini yaz."
            )
        talimat = (
                "Bu masrafın içeriği aslında sorunsuz ve meşru. Yine de açıklamayı gereğinden "
                "FAZLA ısrarla haklı çıkarmaya çalışan bir üslupla yaz: 'kesinlikle', 'tamamen', "
                "'yüzde yüz iş amaçlıdır', 'hiçbir şüpheye yer bırakmayacak şekilde' gibi "
                "aşırı-savunmacı ifadeler ekle -- normalde kimsenin açıklamaya gerek "
                "duymayacağı kadar sıradan bir harcamayı fazla ısrarla savunuyormuş gibi göster. "
                "ÖRNEK TON: 'Bu harcama tamamen ve kesinlikle şirket işi içindir, kişisel "
                "hiçbir yönü yoktur.' En fazla 2 kısa cümle, toplam 160 karakteri geçme. "
                "Sadece açıklama metnini yaz."
            )
    else:  # ai_uretimi
        talimat = (
            "Açıklama, yapay zeka tarafından üretilmiş gibi hissettiren, kalıpsal/şablon "
            "bir cümle olsun -- resmi, jenerik, 'Bu harcama ... kapsamında gerçekleştirilmiştir' "
            "tarzı ifadeler kullan. Satıcı/firma adını ASLA kullanma -- jenerik kal. "
            "Tek cümle, 50-120 karakter arası. Sadece açıklama metnini yaz."
        )

    return ortak + talimat


def ollama_cagir(prompt: str, model: str, host: str, num_predict: int = 90) -> str:
    istek_govdesi = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,   # qwen3 hibrit düşünme modunu kapatir -- hiz icin şart
        "options": {"num_predict": num_predict, "temperature": 0.9},
    }).encode("utf-8")

    istek = urllib.request.Request(
        f"{host}/api/generate",
        data=istek_govdesi,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(istek, timeout=60) as yanit:
        sonuc = json.loads(yanit.read().decode("utf-8"))
    return sonuc.get("response", "").strip()


def main():
    parser = argparse.ArgumentParser(description="Faz 1 açıklama üretim pilotu")
    parser.add_argument("--input-json", required=True, help="faturalar.json yolu")
    parser.add_argument("--etiket-json", required=True, help="faturalar_etiketler.json yolu")
    parser.add_argument("--limit", type=int, default=200, help="Toplam işlenecek fatura sayisi (üst sinir)")
    parser.add_argument("--per-kategori", type=int, default=50, help="Her aciklama_kategorisi'nden en fazla kaç örnek alinacak")
    parser.add_argument("--model", default=MODEL_VARSAYILAN)
    parser.add_argument("--host", default=OLLAMA_HOST_VARSAYILAN)
    args = parser.parse_args()

    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    etiket_map = {e["fatura_no"]: e for e in etiketler}

    if "aciklama_kategorisi" not in etiketler[0]:
        print("HATA: etiketler.json'da aciklama_kategorisi yok. Önce "
              "aciklama_uretici.py entegrasyonuyla main.py'i tekrar çalıştır.")
        return

    # Kategori bazinda STRATIFY edilmiş örneklem: her kategoriden en fazla
    # --per-kategori kadar fatura alinir. Aksi halde sirali okuma, hacimce
    # kucuk kategorileri (manipulatif, ai_uretimi) yeterince test etmeden
    # "run bitti, sorun yok" yanilgisina yol acar.
    kategori_havuzlari: dict[str, list[dict]] = {}
    for fatura in faturalar:
        etiket = etiket_map.get(fatura["fatura_no"])
        if etiket is None:
            continue
        kategori_havuzlari.setdefault(etiket["aciklama_kategorisi"], []).append(fatura)

    secilenler: list[tuple[dict, dict]] = []
    for kategori, havuz in kategori_havuzlari.items():
        random.shuffle(havuz)
        for fatura in havuz[: args.per_kategori]:
            secilenler.append((fatura, etiket_map[fatura["fatura_no"]]))
    random.shuffle(secilenler)

    islenen = 0
    for fatura, etiket in secilenler:
        if islenen >= args.limit:
            break

        kategori = etiket["aciklama_kategorisi"]
        prompt = prompt_olustur(fatura, kategori)

        try:
            token_limiti = 130 if kategori == "manipulatif" else 90
            metin = ollama_cagir(prompt, args.model, args.host, num_predict=token_limiti)
        except Exception as e:
            print(f"[{fatura['fatura_no']}] HATA: {e}")
            continue

        # Manipülatif için hızlı sızıntı kontrolü: yasaklı kategori adı/ürün
        # ismi açıklamaya sızmış mı?
        uyari = ""
        if kategori == "manipulatif":
            gizlenecek = gizlenecek_kalem_bul(fatura["kalemler"])
            if gizlenecek and (
                gizlenecek["harcama_kategorisi"].lower() in metin.lower()
                or gizlenecek["harcama_kategorisi"].lower().replace("_", " ") in metin.lower()
                or gizlenecek["aciklama"].lower() in metin.lower()
            ):
                uyari = "  ⚠️  SIZINTI: gerçek kategori/ürün adı açıklamada geçiyor"
                
        YASAKLI_PASIF_KALIPLAR = ("edilmiştir", "edildi", "sağlanmıştır", "karşılanmıştır", "alınmıştır")
        if kategori == "yeterli" and any(k in metin.lower() for k in YASAKLI_PASIF_KALIPLAR):
            uyari += "  ⚠️  PASIF KALIP: yeterli kategoride yasakli ifade var"
        if len(metin) < 15 or (kategori == "yeterli" and not (40 <= len(metin) <= 100)):
            uyari += f"  ⚠️  UZUNLUK: {len(metin)} karakter (beklenenin dişinda/çok kisa, olasi kesilme)"

        print(f"\n[{fatura['fatura_no']}] kategori={kategori}  is_anomali={etiket['is_anomali']}  turler={etiket['anomali_turleri']}")
        print(f"  Kalemler: {kalemler_ozetle(fatura['kalemler'])}")
        print(f"  Üretilen: {metin}{uyari}")

        islenen += 1

    print(f"\n{islenen} fatura işlendi.")


if __name__ == "__main__":
    main()