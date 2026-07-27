"""
SON adım: tüm batch çıktı JSON'larını okuyup fatura_no -> aciklama_metni
haritası kurar, ardından asıl faturalar.json'a bu alanı ekleyerek eğitim
verisini (faturalar_aciklamali.json) üretir. Ollama kapalıyken çalıştırılır,
RAM sorunu yoktur.

Not: main.py:fatura_to_dict şu an aciklama_metni'ni export etmiyor; üretilen
metni eğitim girdisine katan yer burasıdır.

Kullanım:
    python aciklama_birlestir.py
"""

import argparse
import glob
import json
from pathlib import Path

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"


def aciklama_haritasi_kur(dizin: Path) -> dict[str, str]:
    """batch_*_ciktilar.json dosyalarından kayit_id -> aciklama_metni haritası.

    Anahtar fatura_no DEĞİL: mukerrer_fis_yukleme / fatura_no_cakismasi anomalilerinde
    ayni fatura_no iki kayitta bulunur ve her birinin KENDİ açiklamasi vardir."""
    harita: dict[str, str] = {}
    for yol in sorted(glob.glob(str(dizin / "batch_*_ciktilar.json"))):
        with open(yol, "r", encoding="utf-8") as f:
            cikti = json.load(f)
        for kid, kayit in cikti.items():
            harita[kid] = kayit["aciklama_metni"]
    return harita


def main():
    parser = argparse.ArgumentParser(description="Üretilen açıklamaları faturalar.json'a merge et")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="batch çıktı dizini")
    parser.add_argument("--input-json", default="data/faturalar.json", help="asıl faturalar.json")
    parser.add_argument("--output-json", default="data/faturalar_aciklamali.json", help="çıktı dosyası")
    parser.add_argument("--sadece-uretilenler", action="store_true",
                        help="Sadece açıklaması üretilmiş faturaları yaz (alt küme çalıştıysa küçük dosya)")
    args = parser.parse_args()

    dizin = Path(args.cikti_dizini)
    harita = aciklama_haritasi_kur(dizin)
    print(f"[+] {len(harita)} adet üretilmiş açıklama bulundu.")

    if not harita:
        print("HATA: hiç üretilmiş açıklama yok. Önce aciklama_toplu_uret.py çalıştır.")
        return

    print(f"[+] {args.input_json} okunuyor...")
    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)

    eslesen = 0
    sonuc = []
    for fatura in faturalar:
        metin = harita.get(fatura["kayit_id"])
        if metin is not None:
            fatura["aciklama_metni"] = metin
            eslesen += 1
            sonuc.append(fatura)
        elif not args.sadece_uretilenler:
            fatura["aciklama_metni"] = ""
            sonuc.append(fatura)

    with open(args.output_json, "w", encoding="utf-8") as f:
        # indent=2: faturalar.json ile aynı biçim (main.py) -- nihai çıktı elle
        # incelenen dosya, tek satır olunca editörde açmak zorlaşıyor.
        json.dump(sonuc, f, ensure_ascii=False, indent=2)

    print(f"[+] {eslesen} faturaya aciklama_metni eklendi.")
    print(f"[+] Toplam {len(sonuc)} fatura yazıldı -> {args.output_json}")
    if eslesen < len(harita):
        print(f"[!] UYARI: {len(harita) - eslesen} açıklamanın kayit_id'si faturalar.json'da eşleşmedi.")


if __name__ == "__main__":
    main()
