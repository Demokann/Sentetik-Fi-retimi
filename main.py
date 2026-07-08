import argparse
import json
import csv
from pathlib import Path
from validators import dogrulama_raporu_olustur, raporu_yazdir
from generators.field_generator import rastgele_fatura


def fatura_to_dict(fatura) -> dict:
    """Pydantic modelini JSON-uyumlu dict'e çevirir, hesaplanan alanları da ekler."""
    return {
        "fatura_no": fatura.fatura_no,
        "fatura_tarihi": fatura.fatura_tarihi,
        "satici_vkn": fatura.satici_vkn,
        "satici_unvan": fatura.satici_unvan,
        "alici_vkn": fatura.alici_vkn,
        "alici_unvan": fatura.alici_unvan,
        "genel_toplam": float(fatura.genel_toplam),
        "kalemler": [
            {
                "kalem_no": k.kalem_no,
                "aciklama": k.aciklama,
                "harcama_kategorisi": k.harcama_kategorisi.value,
                "miktar": k.miktar,
                "birim": k.birim,
                "birim_fiyat": float(k.birim_fiyat),
                "iskonto_orani": k.iskonto_orani,
                "kdv_orani": k.kdv_orani,
                "ara_toplam": float(k.ara_toplam),
                "kdv_tutari": float(k.kdv_tutari),
                "satir_toplam": float(k.satir_toplam),
            }
            for k in fatura.kalemler
        ],
    }

#faturaların üretiminde artık fatura_to_dict fonksiyonu kullanılıyor, 
#çünkü pydantic modelini JSON uyumlu dict'e çeviriyor ve hesaplanan alanları da ekliyor.
#bu fonksiyon çağrılmıyor doğrulanmadan fatura üretimi gerekirse diye kalsın.
def faturalari_uret(adet: int) -> list[dict]:
    """`adet` kadar rastgele fatura üretir ve dict listesine çevirir."""
    return [fatura_to_dict(rastgele_fatura()) for _ in range(adet)]


def json_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    with open(dosya_yolu, "w", encoding="utf-8") as f:
        json.dump(faturalar, f, ensure_ascii=False, indent=2)


def csv_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    """Her kalemi bir satır olarak yazar; fatura bilgileri her satırda tekrarlanır."""
    satirlar = []
    for fatura in faturalar:
        for kalem in fatura["kalemler"]:
            satirlar.append({
                "fatura_no": fatura["fatura_no"],
                "fatura_tarihi": fatura["fatura_tarihi"],
                "satici_vkn": fatura["satici_vkn"],
                "satici_unvan": fatura["satici_unvan"],
                "alici_vkn": fatura["alici_vkn"],
                "alici_unvan": fatura["alici_unvan"],
                **kalem,
                "fatura_genel_toplam": fatura["genel_toplam"],
            })

    if not satirlar:
        return

    with open(dosya_yolu, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=satirlar[0].keys())
        writer.writeheader()
        writer.writerows(satirlar)


def main():
    parser = argparse.ArgumentParser(description="Sentetik Türkçe fatura üretici")
    parser.add_argument("--count", type=int, default=100, help="Üretilecek fatura sayısı")
    parser.add_argument("--output-dir", type=str, default="data", help="Çıktı klasörü")
    parser.add_argument("--filename", type=str, default="faturalar", help="Dosya adı (uzantısız)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{args.count} adet sentetik fatura üretiliyor...")

    # Tek seferde üret, hem doğrulama hem export bu tek listeyi kullansın
    fatura_nesneleri = [rastgele_fatura() for _ in range(args.count)]

    rapor = dogrulama_raporu_olustur(fatura_nesneleri)
    raporu_yazdir(rapor)

    faturalar = [fatura_to_dict(f) for f in fatura_nesneleri]

    json_yolu = output_dir / f"{args.filename}.json"
    csv_yolu = output_dir / f"{args.filename}.csv"

    json_olarak_kaydet(faturalar, json_yolu)
    csv_olarak_kaydet(faturalar, csv_yolu)

    toplam_kalem = sum(len(f["kalemler"]) for f in faturalar)

    print(f"Tamamlandı:")
    print(f"  {len(faturalar)} fatura, {toplam_kalem} kalem üretildi")
    print(f"  JSON -> {json_yolu}")
    print(f"  CSV  -> {csv_yolu}")


if __name__ == "__main__":
    main()