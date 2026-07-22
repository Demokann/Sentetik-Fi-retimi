"""
Faz 1 pilot script: faturalar.json + faturalar_etiketler.json içinden ilk
N faturayı alır, her birinin aciklama_kategorisi'ne göre prompt kurup
Ollama üzerinden paralel olarak qwen3:8b'ye gönderir. Çıktıları .md dosyasına yazar.

Not: prompt/ollama/retry mantığı artık aciklama_uretim_core.py'de tek kaynak
olarak duruyor; bu script yalnızca küçük örneklem koşusu + MD raporu üretir.
"""

import argparse
import json
import random
import time  # Süre ölçümü için
from concurrent.futures import ThreadPoolExecutor, as_completed

from aciklama_uretim_core import (
    OLLAMA_HOST_VARSAYILAN,
    MODEL_VARSAYILAN,
    kalemler_ozetle,
    tek_fatura_isleme,
)


def main():
    parser = argparse.ArgumentParser(description="Faz 1 açıklama üretim pilotu - Optimize Ollama")
    parser.add_argument("--input-json", required=True, help="faturalar.json yolu")
    parser.add_argument("--etiket-json", required=True, help="faturalar_etiketler.json yolu")
    parser.add_argument("--output-md", default="ollama_sonuclar.md", help="Çıktının yazılacağı Markdown dosyası")
    parser.add_argument("--limit", type=int, default=200, help="Toplam işlenecek fatura sayisi (üst sinir)")
    parser.add_argument("--per-kategori", type=int, default=50, help="Her kategoriden örnek")
    parser.add_argument("--workers", type=int, default=2, help="Paralel istek sayısı (OLLAMA_NUM_PARALLEL'dan büyük olması faydasız)")
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
    retry_tetiklendi = 0
    retry_sonrasi_hala_ihlalli = 0

    UYARI_METINLERI = {
        "sizinti": "⚠️ SIZINTI: gerçek kategori/ürün adı açıklamada geçiyor.",
        "pasif_kalip": "⚠️ PASIF KALIP: yasaklı resmi/pasif ifade var.",
        "kapanis_eksik": "⚠️ KAPANIS: ai_uretimi bir AI-kapanışıyla bitmiyor.",
        "vurgu_eksik": "⚠️ VURGU: manipulatif abartılı vurgu içermiyor (meşru gibi).",
    }

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
                fatura, etiket, metin, hata, ihlaller, deneme_sayisi = future.result()

                if hata or metin is None:
                    print(f"[X] {fatura['fatura_no']} - HATA: {hata or 'Metin boş geldi'}")
                    continue

                kategori = etiket["aciklama_kategorisi"]

                uyari_parcalari = [UYARI_METINLERI[i] for i in ihlaller if i in UYARI_METINLERI]
                if "uzunluk" in ihlaller:
                    uyari_parcalari.append(f"⚠️ UZUNLUK DİKKAT: {len(metin)} karakter.")

                if deneme_sayisi == 2:
                    retry_tetiklendi += 1
                    if ihlaller:
                        retry_sonrasi_hala_ihlalli += 1
                        uyari_parcalari.append("🔁 2. denemede de düzeltilemedi.")
                    else:
                        uyari_parcalari.append("🔁 1. denemede ihlal vardı, 2. denemede düzeltildi.")

                uyari = " ".join(uyari_parcalari)

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
                out_file.flush()  # Veriyi anlık olarak dosyaya aktarır (crash olursa veri kaybını önler)

                # Terminale sadece kısa bir ilerleme bilgisi bas
                print(f"[✔] İşlenen: {idx}/{len(islenecek_liste)} | Fatura: {fatura['fatura_no']} ({kategori})")
                islenen += 1

    # Süre Hesaplama
    bitis_zamani = time.time()
    gecen_sure = bitis_zamani - baslangic_zamani
    dakika, saniye = divmod(gecen_sure, 60)

    print(f"\nİşlem başarıyla bitti.")
    print(f"Toplam İşlenen Fatura: {islenen}")
    print(f"Retry Tetiklenen: {retry_tetiklendi} (bunlardan {retry_sonrasi_hala_ihlalli} tanesi 2. denemede de ihlalli kaldı)")
    print(f"Harcanan Toplam Süre: {int(dakika)} dakika {int(saniye)} saniye")
    print(f"Sonuçları görüntülemek için '{args.output_md}' dosyasına bakabilirsin.\n")


if __name__ == "__main__":
    main()
