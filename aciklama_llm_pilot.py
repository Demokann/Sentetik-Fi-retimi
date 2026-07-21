"""
Faz 1 pilot script: faturalar.json + faturalar_etiketler.json içinden ilk
N faturayı alır, her birinin aciklama_kategorisi'ne göre prompt kurup 
Ollama üzerinden paralel olarak qwen3:8b'ye gönderir. Çıktıları .md dosyasına yazar.
"""

import argparse
import json
import random
import re
import time # Süre ölçümü için eklendi
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

def prompt_olustur(fatura: dict, kategori: str) -> tuple[str, str]:
    kalem_ozeti = kalemler_ozetle(fatura["kalemler"])
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    
    # SISTEM PROMPTU (Sabit, Ollama bunu önbelleğe alacak)
    system_prompt = (
        "Sen bir şirket çalışanısın. Masraf fişini masraf uygulamasına yüklerken kısa bir açıklama yazıyorsun.\n"
        "KURALLAR:\n"
        "- Açıklama HER ZAMAN İŞ/ŞİRKET bağlamında olsun, kişisel hayatla ilgili ASLA yazma.\n"
        "- Ürünü pazarlama diliyle anlatma, NEDEN aldığını yaz.\n"
        "- Tutar yazma.\n"
        "- Fişte OLMAYAN bir kalemden bahsetme.\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        talimat = (
            f"Açıklama, harcamanın İŞ AMACINI net belirtsin (kiminle, neden). Satıcıyı ({firma_kisa}) "
            f"doğal şekilde dahil et. Üslup: {uslup}. "
            f"'temin edilmiştir' gibi resmi/pasif kalıpları KULLANMA, 'aldım', 'ödedim' gibi aktif fiiller kullan. "
            f"Tek cümle, 40-90 karakter arası. Sadece açıklama metnini yaz."
        )
    elif kategori == "yetersiz":
        talimat = (
            "Açıklama MUĞLAK ve genel olsun, spesifik detay VERME. Firma adını istersen kullanabilirsin "
            "ama iş amacını açıklama. Resmi kalıpları kullanma. Sadece açıklama metnini yaz."
        )
    elif kategori == "manipulatif":
        gizlenecek = gizlenecek_kalem_bul(fatura["kalemler"])
        if gizlenecek:
            baglam = f"Fişte '{gizlenecek['aciklama']}' ({gizlenecek['harcama_kategorisi']}) kalemi var ve bu aykırı olabilir."
            talimat = (
                f"{baglam} SADECE bu kalemi gizleyip meşru bir iş gideriymiş gibi göster. Diğer kalemlerle "
                f"tutarlı kal, satıcı adını ({firma_kisa}) kullan. Gerçek ürün/kategori adını KULLANMA. "
                f"Sadece açıklama metnini yaz."
            )
        else:
            talimat = (
                "Bu masraf sorunsuz. Yine de açıklamayı gereğinden FAZLA ısrarla haklı çıkarmaya çalışan "
                "bir üslupla yaz: 'kesinlikle', 'yüzde yüz iş amaçlıdır' gibi. Sadece açıklama metnini yaz."
            )
    else:  # ai_uretimi
        talimat = (
            "Açıklama, yapay zeka tarafından üretilmiş gibi kalıpsal/şablon bir cümle olsun ('... kapsamında gerçekleştirilmiştir' gibi). "
            "Satıcı/firma adını ASLA kullanma. Tek cümle. Sadece açıklama metnini yaz."
        )

    # USER PROMPTU (Sadece değişen kısım)
    user_prompt = f"Satıcı/Firma: {firma_kisa}\nFiş kalemleri: {kalem_ozeti}\nTalimat: {talimat}"

    return system_prompt, user_prompt


# Session oluştur (Global seviyede)
http_session = requests.Session()

def ollama_cagir(system_prompt: str, user_prompt: str, model: str, host: str, num_predict: int = 90) -> str:
    istek_govdesi = {
        "model": model,
        "system": system_prompt, 
        "prompt": user_prompt,   
        "stream": False,
        "think": False, # Modeli ne olursa olsun düşünme sürecini kapattık
        "options": {
            "num_predict": num_predict, 
            "temperature": 0.9,
            "num_ctx": 1024, 
        }
    }

    yanit = http_session.post(f"{host}/api/generate", json=istek_govdesi, timeout=60)
    yanit.raise_for_status()
    
    # Her ihtimale karşı metin içindeki <think> bloklarını Python regex ile temizliyoruz
    metin = yanit.json().get("response", "").strip()
    metin = re.sub(r'<think>.*?</think>', '', metin, flags=re.DOTALL).strip()
    
    return metin

    # Eğer model qwen3 ise think parametresini kapat
    if "qwen3" in model.lower():
        istek_govdesi["think"] = False

    yanit = http_session.post(f"{host}/api/generate", json=istek_govdesi, timeout=60)
    yanit.raise_for_status()
    return yanit.json().get("response", "").strip()

def tek_fatura_isleme(fatura, etiket, model, host):
    kategori = etiket["aciklama_kategorisi"]
    system_prompt, user_prompt = prompt_olustur(fatura, kategori)
    token_limiti = 130 if kategori == "manipulatif" else 90

    try:
        metin = ollama_cagir(system_prompt, user_prompt, model, host, num_predict=token_limiti)
        return fatura, etiket, metin, None
    except Exception as e:
        return fatura, etiket, None, str(e)


def main():
    parser = argparse.ArgumentParser(description="Faz 1 açıklama üretim pilotu - Optimize Ollama")
    parser.add_argument("--input-json", required=True, help="faturalar.json yolu")
    parser.add_argument("--etiket-json", required=True, help="faturalar_etiketler.json yolu")
    parser.add_argument("--output-md", default="ollama_sonuclar.md", help="Çıktının yazılacağı Markdown dosyası") # YENİ PARAMETRE
    parser.add_argument("--limit", type=int, default=200, help="Toplam işlenecek fatura sayisi (üst sinir)")
    parser.add_argument("--per-kategori", type=int, default=50, help="Her kategoriden örnek")
    parser.add_argument("--workers", type=int, default=4, help="Paralel istek sayısı")
    parser.add_argument("--model", default=MODEL_VARSAYILAN)
    parser.add_argument("--host", default=OLLAMA_HOST_VARSAYILAN)
    args = parser.parse_args()

    # Süre ölçümünü başlat
    baslangic_zamani = time.time()

    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    etiket_map = {e["fatura_no"]: e for e in etiketler}

    if "aciklama_kategorisi" not in etiketler[0]:
        print("HATA: etiketler.json'da aciklama_kategorisi yok.")
        return

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

    islenecek_liste = secilenler[: args.limit]
    print(f"\n[+] Toplam {len(islenecek_liste)} fatura {args.workers} paralel worker ile işleniyor...")
    print(f"[+] Çıktılar anlık olarak '{args.output_md}' dosyasına yazılacak.\n")

    islenen = 0
    
    # Çıktı dosyasını yazma modunda aç (Baştan başlar, öncekini ezer)
    with open(args.output_md, "w", encoding="utf-8") as out_file:
        out_file.write(f"# Ollama ({args.model}) Masraf Açıklamaları Sonuç Raporu\n\n")
        out_file.write("---\n\n")

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(tek_fatura_isleme, fatura, etiket, args.model, args.host): (fatura, etiket)
                for fatura, etiket in islenecek_liste
            }

            for idx, future in enumerate(as_completed(futures), 1):
                fatura, etiket, metin, hata = future.result()
                
                # Pylance tip denetimini çözen satır
                if hata or metin is None:
                    print(f"[X] {fatura['fatura_no']} - HATA: {hata or 'Metin boş geldi'}")
                    continue

                kategori = etiket["aciklama_kategorisi"]
                uyari = ""
                
                if kategori == "manipulatif":
                    gizlenecek = gizlenecek_kalem_bul(fatura["kalemler"])
                    if gizlenecek and (
                        gizlenecek["harcama_kategorisi"].lower() in metin.lower()
                        or gizlenecek["harcama_kategorisi"].lower().replace("_", " ") in metin.lower()
                        or gizlenecek["aciklama"].lower() in metin.lower()
                    ):
                        uyari = "⚠️ SIZINTI: gerçek kategori/ürün adı açıklamada geçiyor."
                    
                YASAKLI_PASIF_KALIPLAR = ("edilmiştir", "edildi", "sağlanmıştır", "karşılanmıştır", "alınmıştır")
                if kategori == "yeterli" and any(k in metin.lower() for k in YASAKLI_PASIF_KALIPLAR):
                    uyari += " ⚠️ PASIF KALIP: yeterli kategoride yasaklı ifade var."
                if len(metin) < 15 or (kategori == "yeterli" and not (40 <= len(metin) <= 100)):
                    uyari += f" ⚠️ UZUNLUK DİKKAT: {len(metin)} karakter."

                # MD Dosyasına Yazılacak Şablon
                md_icerik = f"## {idx}. {fatura['fatura_no']}\n\n"
                md_icerik += f"- **Kategori:** `{kategori}`\n"
                md_icerik += f"- **Anomali Var mı?:** `{etiket['is_anomali']}`\n"
                md_icerik += f"- **Anomali Türleri:** `{etiket['anomali_turleri']}`\n\n"
                md_icerik += f"**Kalemler:**\n{kalemler_ozetle(fatura['kalemler'])}\n\n"
                md_icerik += f"**Üretilen Açıklama:**\n> {metin}\n\n"
                if uyari:
                    md_icerik += f"*{uyari.strip()}*\n\n"
                md_icerik += "---\n\n"

                out_file.write(md_icerik)
                out_file.flush() # Veriyi anlık olarak dosyaya aktarır (crash olursa veri kaybını önler)

                # Terminale sadece kısa bir ilerleme bilgisi bas
                print(f"[✔] İşlenen: {idx}/{len(islenecek_liste)} | Fatura: {fatura['fatura_no']} ({kategori})")
                islenen += 1

    # Süre Hesaplama
    bitis_zamani = time.time()
    gecen_sure = bitis_zamani - baslangic_zamani
    dakika, saniye = divmod(gecen_sure, 60)

    print(f"\nİşlem başarıyla bitti.")
    print(f"Toplam İşlenen Fatura: {islenen}")
    print(f"Harcanan Toplam Süre: {int(dakika)} dakika {int(saniye)} saniye")
    print(f"Sonuçları görüntülemek için '{args.output_md}' dosyasına bakabilirsin.\n")


if __name__ == "__main__":
    main()