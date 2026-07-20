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
import urllib.request
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


def prompt_olustur(fatura: dict, kategori: str) -> str:
    kalem_ozeti = kalemler_ozetle(fatura["kalemler"])
    ortak = (
        f"Sen bir şirket çalışanısın. Aşağıdaki masraf fişini şirketin masraf "
        f"yönetim uygulamasına yüklerken kısa bir açıklama yazıyorsun.\n"
        f"Fiş kalemleri: {kalem_ozeti}\n"
        f"Toplam tutar: {fatura['genel_toplam']} TL\n\n"
    )

    if kategori == "yeterli":
        talimat = (
            "Açıklama, harcamanın İŞ AMACINI net ve spesifik şekilde belirtsin "
            "(kiminle, neden, hangi iş bağlamında). Tek cümle, 40-90 karakter arası. "
            "Sadece açıklama metnini yaz, başka bir şey ekleme."
        )
    elif kategori == "yetersiz":
        talimat = (
            "Açıklama MUĞLAK ve genel olsun, spesifik detay VERME (kiminle, "
            "neden gibi bilgi olmasın). Yine de en az 30 karakter olsun, tek "
            "kelimelik bir cevap OLMASIN. Sadece açıklama metnini yaz."
        )
    elif kategori == "manipulatif":
        yasakli = yasakli_kalem_bul(fatura["kalemler"])
        if yasakli:
            baglam = (
                f"Fişte aslında '{yasakli['aciklama']}' ({yasakli['harcama_kategorisi']}) "
                f"kalemi var ve bu şirket politikasına göre masraf olarak KABUL EDİLMİYOR."
            )
        else:
            baglam = (
                "Bu masrafta şirket politikasına aykırı bir durum var "
                "(iş koluna uygun olmayan bir kalem ya da tekrar eden bir fiş no)."
            )
        talimat = (
            f"{baglam} Açıklamayı, gerçek durumu GİZLEYİP meşru bir iş gideriymiş "
            f"gibi göster (ör: toplantı, ekip etkinliği, müşteri ağırlama, iş yemeği). "
            f"Gerçek ürün/kategori adını veya yasaklı kelimeleri KULLANMA. "
            f"Sadece açıklama metnini yaz."
        )
    else:  # ai_uretimi
        talimat = (
            "Açıklama, yapay zeka tarafından üretilmiş gibi hissettiren, kalıpsal/"
            "şablon bir cümle olsun -- resmi, jenerik, 'Bu harcama ... kapsamında "
            "gerçekleştirilmiştir' tarzı ifadeler kullan. Sadece açıklama metnini yaz."
        )

    return ortak + talimat


def ollama_cagir(prompt: str, model: str, host: str) -> str:
    istek_govdesi = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,   # qwen3 hibrit düşünme modunu kapatir -- hiz icin şart
        "options": {"num_predict": 60, "temperature": 0.9},
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
    parser.add_argument("--limit", type=int, default=30, help="İşlenecek fatura sayisi")
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

    islenen = 0
    for fatura in faturalar:
        if islenen >= args.limit:
            break
        etiket = etiket_map.get(fatura["fatura_no"])
        if etiket is None:
            continue

        kategori = etiket["aciklama_kategorisi"]
        prompt = prompt_olustur(fatura, kategori)

        try:
            metin = ollama_cagir(prompt, args.model, args.host)
        except Exception as e:
            print(f"[{fatura['fatura_no']}] HATA: {e}")
            continue

        # Manipülatif için hızlı sızıntı kontrolü: yasaklı kategori adı/ürün
        # ismi açıklamaya sızmış mı?
        uyari = ""
        if kategori == "manipulatif":
            yasakli = yasakli_kalem_bul(fatura["kalemler"])
            if yasakli and (
                yasakli["harcama_kategorisi"].lower() in metin.lower()
                or yasakli["aciklama"].lower() in metin.lower()
            ):
                uyari = "  ⚠️  SIZINTI: gerçek kategori/ürün adı açıklamada geçiyor"

        print(f"\n[{fatura['fatura_no']}] kategori={kategori}  is_anomali={etiket['is_anomali']}  turler={etiket['anomali_turleri']}")
        print(f"  Kalemler: {kalemler_ozetle(fatura['kalemler'])}")
        print(f"  Üretilen: {metin}{uyari}")

        islenen += 1

    print(f"\n{islenen} fatura işlendi.")


if __name__ == "__main__":
    main()