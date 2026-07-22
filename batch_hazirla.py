"""
BİR KEZ çalışır (Ollama kapalıyken, RAM sorunu yok): faturalar.json +
faturalar_etiketler.json'dan aciklama_kategorisi oranlarını koruyan dengeli
bir alt küme örnekler, 1000'lik batch dosyalarına böler ve bir durum.json
manifesti yazar. Toplu üretim (aciklama_toplu_uret.py) 155 MB'lık asıl dosyaya
bir daha dokunmaz -- yalnızca bu batch dosyalarını okur.

Kullanım:
    python batch_hazirla.py --toplam 22000 --batch-size 1000 --min-per-kategori 2500
"""

import argparse
import json
import random
from pathlib import Path

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"


def batch_kaydi_olustur(fatura: dict, etiket: dict) -> dict:
    """Runner'ın prompt kurmak ve ihlal denetlemek için ihtiyaç duyduğu
    MİNİMUM alanları taşıyan kompakt kayıt. Asıl dosyaya bir daha dönülmez."""
    return {
        "fatura_no": fatura["fatura_no"],
        "satici_unvan": fatura["satici_unvan"],
        "kalemler": fatura["kalemler"],
        "aciklama_kategorisi": etiket["aciklama_kategorisi"],
        "is_anomali": etiket["is_anomali"],
        "anomali_turleri": etiket["anomali_turleri"],
    }


def dengeli_ornekle(
    kategori_havuzlari: dict[str, list[dict]],
    toplam: int,
    min_per_kategori: int,
) -> list[dict]:
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


def main():
    parser = argparse.ArgumentParser(description="Dengeli alt küme örnekle ve batch dosyalarına böl")
    parser.add_argument("--input-json", default="data/faturalar.json", help="faturalar.json yolu")
    parser.add_argument("--etiket-json", default="data/faturalar_etiketler.json", help="etiketler.json yolu")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="Batch dosyalarının yazılacağı dizin")
    parser.add_argument("--toplam", type=int, default=22000, help="Hedef toplam fatura sayısı (yaklaşık)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Her batch dosyasındaki fatura sayısı")
    parser.add_argument("--min-per-kategori", type=int, default=2500, help="Nadir sınıflar için taban örnek sayısı")
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
    secilenler: list[dict] = []
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
            json.dump(dilim, f, ensure_ascii=False)
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
            "min_per_kategori": args.min_per_kategori,
            "seed": args.seed,
        },
        "toplam_secilen": len(secilenler),
        "batch_sayisi": len(batch_manifest),
        "batchler": batch_manifest,
    }
    with open(cikti_dizini / "durum.json", "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=2)

    print(f"[+] {len(batch_manifest)} batch dosyası + durum.json yazıldı -> {cikti_dizini}/")
    print(f"[+] Sonraki adım: python aciklama_toplu_uret.py")


if __name__ == "__main__":
    main()
