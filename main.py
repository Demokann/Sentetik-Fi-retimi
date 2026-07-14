import argparse
import json
import csv
from pathlib import Path
from validators import dogrulama_raporu_olustur, raporu_yazdir, vkn_firma_tutarlilik_hatalarini_bul
from generators.field_generator import rastgele_fatura
from generators.anomaly_injector import karisik_veri_seti_uret



def fatura_to_dict(fatura) -> dict:
    """Pydantic modelini JSON-uyumlu dict'e çevirir, hesaplanan alanlari da ekler."""
    return {
        "fatura_no": fatura.fatura_no,
        "fatura_tarihi": fatura.fatura_tarihi,
        "satici_vkn": fatura.satici_vkn,
        "satici_unvan": fatura.satici_unvan,
        "alici_vkn": fatura.alici_vkn,
        "alici_unvan": fatura.alici_unvan,
        "toplam_vergisiz_tutar": float(fatura.toplam_vergisiz_tutar),   # yeni
        "toplam_kdv_tutari": float(fatura.toplam_kdv_tutari),            # yeni
        "toplam_iskonto": float(fatura.toplam_iskonto),                  # yeni
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

def etiket_to_dict(fatura) -> dict:
    """
    Modelin GÖRMEMESİ gereken ground-truth etiket bilgisini ayri bir
    dict'e çevirir. fatura_to_dict()'ten kasitli olarak ayri tutulur —
    veri sizintisini (data leakage) önlemek için etiketler modelin
    girdisine hiç dahil edilmemeli, sadece değerlendirme/eğitim etiketi
    olarak kullanilmali.
    """
    return {
        "fatura_no": fatura.fatura_no,
        "is_anomali": fatura.is_anomali,
        "anomali_turleri": fatura.anomali_turleri,
    }


def etiketleri_kaydet(faturalar, dosya_yolu: Path) -> None:
    etiketler = [etiket_to_dict(f) for f in faturalar]
    with open(dosya_yolu, "w", encoding="utf-8") as f:
        json.dump(etiketler, f, ensure_ascii=False, indent=2)

#faturalarin üretiminde artik fatura_to_dict fonksiyonu kullaniliyor, 
#çünkü pydantic modelini JSON uyumlu dict'e çeviriyor ve hesaplanan alanlari da ekliyor.
#bu fonksiyon çağrilmiyor doğrulanmadan fatura üretimi gerekirse diye kalsin.
def faturalari_uret(adet: int) -> list[dict]:
    """`adet` kadar rastgele fatura üretir ve dict listesine çevirir."""
    return [fatura_to_dict(rastgele_fatura()) for _ in range(adet)]


def json_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    with open(dosya_yolu, "w", encoding="utf-8") as f:
        json.dump(faturalar, f, ensure_ascii=False, indent=2)


def csv_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    """Her kalemi bir satir olarak yazar; fatura bilgileri her satirda tekrarlanir."""
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
    parser.add_argument("--count", type=int, default=100, help="Üretilecek fatura sayisi")
    parser.add_argument("--output-dir", type=str, default="data", help="Çikti klasörü")
    parser.add_argument("--filename", type=str, default="faturalar", help="Dosya adi (uzantisiz)")
    parser.add_argument("--anomali-orani", type=float, default=0.0, help="0.0-1.0 arasi, anomalili fatura orani")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{args.count} adet sentetik fatura üretiliyor...")

    # Tek seferde üret, hem doğrulama hem export bu tek listeyi kullansin
    fatura_nesneleri = karisik_veri_seti_uret(args.count, args.anomali_orani)

    # VKN-firma tutarsizliklari (ayni ad farkli VKN VEYA ayni VKN farkli ad)
    # JSON/CSV export'a hiç dahil edilmesin -- sadece raporda görünmesi
    # yetmiyor, veri setine kirli örnek olarak sizmasin istiyoruz.
    # fatura_no_tekrari anomalisi KASITLI olarak ayni VKN'ye farkli unvan
    # bindiriyor (validators.py'nin bunu gerçek anomali sayabilmesi için
    # VKN'lerin eşleşmesi şart, bkz. anomaly_injector.py). Bu VKN'ler
    # genel tutarlilik filtresinden MUAF tutulmali, yoksa kasitli anomali
    # yanlislikla silinir.
    korunan_vknler = {
        f.satici_vkn for f in fatura_nesneleri
        if "fatura_no_tekrari" in f.anomali_turleri
    }
    korunan_adlar = {
        f.satici_unvan for f in fatura_nesneleri
        if "fatura_no_tekrari" in f.anomali_turleri
    }
    vkn_firma_hatalari = vkn_firma_tutarlilik_hatalarini_bul(fatura_nesneleri)
    celiskili_adlar = set(vkn_firma_hatalari["ayni_ad_farkli_vkn"].keys()) - korunan_adlar
    celiskili_vknler = set(vkn_firma_hatalari["ayni_vkn_farkli_ad"].keys()) - korunan_vknler
    if celiskili_adlar or celiskili_vknler:
        once_sayisi = len(fatura_nesneleri)
        fatura_nesneleri = [
            f for f in fatura_nesneleri
            if f.satici_unvan not in celiskili_adlar and f.satici_vkn not in celiskili_vknler
        ]
        print(f"VKN-firma tutarsizligi nedeniyle elenen fatura sayisi: {once_sayisi - len(fatura_nesneleri)}")
    rapor = dogrulama_raporu_olustur(fatura_nesneleri)
    raporu_yazdir(rapor)

    faturalar = [fatura_to_dict(f) for f in fatura_nesneleri]

    json_yolu = output_dir / f"{args.filename}.json"
    csv_yolu = output_dir / f"{args.filename}.csv"

    json_olarak_kaydet(faturalar, json_yolu)
    csv_olarak_kaydet(faturalar, csv_yolu)

    etiket_yolu = output_dir / f"{args.filename}_etiketler.json"
    etiketleri_kaydet(fatura_nesneleri, etiket_yolu)

    toplam_kalem = sum(len(f["kalemler"]) for f in faturalar)

    print(f"Tamamlandi:")
    print(f"  {len(faturalar)} fatura, {toplam_kalem} kalem üretildi")
    print(f"  JSON -> {json_yolu}")
    print(f"  CSV  -> {csv_yolu}")
    print(f"  Etiketler -> {etiket_yolu}")
    rapor_yolu = output_dir / f"{args.filename}_rapor.json"
    with open(rapor_yolu, "w", encoding="utf-8") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=2, default=list)


if __name__ == "__main__":
    main()