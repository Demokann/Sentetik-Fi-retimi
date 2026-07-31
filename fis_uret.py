"""Fatura JSON'larindan GÖRSEL fiş (receipt) render'lari üretir.

Veri boru hattinin parçasi DEĞİLDİR; fişin kendisini üreten ayri araçtir
(bkz. docs/faz_a_fatura_uretimi.md).

GÖSTERİM ARİTMETİĞİ (2026-07-31) -- fişin kendi içinde tutarli olmasi ŞART:

    brut_birim = birim_fiyat x (1 + kdv_orani/100)      KDV DAHİL birim fiyat
    brut_tutar = birim_fiyat x miktar x (1 + kdv/100)   iskonto ÖNCESİ satir
    indirim    = brut_tutar x iskonto_orani/100
    brut_tutar - indirim = satir_toplam                 (ve toplami genel_toplam)

Neden böyle: eski şablon fiyat sütununa `satir_toplam`'i (iskonto ZATEN
düşülmüş) basip altina bir de İNDİRİM satiri ekliyordu -- ayni iskonto iki
kez görünüyor, fiş toplami tutmuyordu. Üstelik indirim tutari `ara_toplam`
(iskonto SONRASI, KDV hariç) üzerinden hesaplandigi için sayi da yanlişti;
ölçüldü: 1.685 iskontolu kalemde ortalama 510,86 TL eksik yaziliyordu.
120k faturanin %56,8'i iskontolu kalem içeriyor, yani TEMİZ fişlerin
yarisindan fazlasi görselde aritmetik olarak bozuktu.

Yuvarlama: brüt tutar TEK SEFERDE yuvarlanir (birim fiyati yuvarlayip
miktarla çarpmak 5 kuruşa kadar sapma üretiyordu). Ölçüldü (4.000 fatura /
8.358 temiz kalem): sapma %87,2'de tam sifir, %100'ünde <= 1 kuruş.
Anomalili kalemlerde sapma BÜYÜK kalir -- `ara_toplam` anomalisi (fişte
kalem ara_toplam'i hiç basilmadigi için eskiden GÖRÜNMEZDİ) böylece
görselde tespit edilebilir hale gelir.
"""

import argparse
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from generators.field_generator import TAM_SAYI_BIRIMLERI


def kimlik_etiketi(kimlik_no: str) -> str:
    """10 haneliyse VKN, 11 haneliyse TCKN etiketi döndürür."""
    return "TCKN" if len(kimlik_no) == 11 else "VKN"


def tutar_formatla(deger) -> str:
    """1234.5 -> '1.234,50'. Türkçe biçim, DAİMA iki ondalik.

    Ham float'i şablona vermek `*1101.0`, `*648.5` gibi tek ondalikli
    tutarlar üretiyordu: gerçek fişte tutar her zaman iki hanelidir ve
    ayraç virgüldür. Tutarsiz ondalik gösterimi ayrica `ondalik_kaymasi`
    anomalisinin görsel imzasini bulandiriyordu."""
    return (
        f"{float(deger):,.2f}"
        .replace(",", "\x00")   # binlik ayraci geçici işaret
        .replace(".", ",")      # ondalik -> virgül
        .replace("\x00", ".")   # binlik -> nokta
    )


def miktar_formatla(birim: str, miktar: float) -> str:
    """Tam sayiysa '3', değilse '3,45' (Türkçe ondalik, gereksiz .0 basmaz)."""
    if birim in TAM_SAYI_BIRIMLERI:
        return str(int(miktar))
    return f"{miktar:.2f}".replace(".", ",")


def kalem_gosterimi(kalem: dict) -> dict:
    """Kalemin şablonda basilacak alanlarini hesaplar (ham alanlar korunur).

    ANOMALİYİ DÜZELTMEZ: brüt tutar `birim_fiyat`tan, satir toplami ise
    kaydin kendi `satir_toplam`'indan gelir. Enjekte edilmiş bir sapma
    varsa ikisi uyuşmaz ve fişte GÖRÜNÜR -- amaç budur."""
    kdv_carpani = 1 + kalem["kdv_orani"] / 100
    brut_birim = round(kalem["birim_fiyat"] * kdv_carpani, 2)
    brut_tutar = round(kalem["birim_fiyat"] * kalem["miktar"] * kdv_carpani, 2)
    indirim = round(brut_tutar * kalem["iskonto_orani"] / 100, 2)
    return {
        **kalem,
        "miktar": miktar_formatla(kalem["birim"], kalem["miktar"]),
        "brut_birim": brut_birim,
        "brut_tutar": brut_tutar,
        # 0 ise şablon İNDİRİM satirini hiç basmaz.
        "indirim": indirim if kalem["iskonto_orani"] > 0 else 0,
    }


def baglam_kur(fatura: dict) -> dict:
    """fatura_to_dict çiktisi + şablonun ihtiyaç duyduğu türetilmiş alanlar."""
    baglam = dict(fatura)
    baglam["satici_kimlik_etiketi"] = kimlik_etiketi(fatura["satici_vkn"])
    baglam["kalemler"] = [kalem_gosterimi(k) for k in fatura["kalemler"]]
    return baglam


def main():
    parser = argparse.ArgumentParser(description="JSON faturalardan fiş (receipt) görseli üretir")
    parser.add_argument("--input-json", required=True, help="faturalar.json dosya yolu")
    parser.add_argument("--output-dir", default="data/fisler", help="Görsellerin kaydedileceği klasör")
    parser.add_argument("--template", default="make_receipt.html", help="Jinja2 HTML şablon dosyasının yolu")
    parser.add_argument("--limit", type=int, default=None, help="Test için ilk N faturayı işle (varsayılan: hepsi)")
    parser.add_argument("--yeniden", action="store_true",
                        help="Var olan PNG'leri de yeniden üret (varsayılan: atlanır, resume)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)

    if args.limit:
        faturalar = faturalar[: args.limit]

    sablon_yolu = Path(args.template)
    env = Environment(loader=FileSystemLoader(str(sablon_yolu.parent or Path("."))))
    env.filters["tutar"] = tutar_formatla
    template = env.get_template(sablon_yolu.name)

    print(f"{len(faturalar)} fatura için fiş üretiliyor...")

    uretilen = atlanan = hatali = 0
    with sync_playwright() as p:
        tarayici = p.chromium.launch()
        sayfa = tarayici.new_page()

        for i, fatura in enumerate(faturalar, start=1):
            # Dosya adi KAYIT_ID: `fatura_no` tasarim gereği MÜKERRER olabilir
            # (mukerrer_fis_yukleme / fatura_no_cakismasi, CLAUDE.md §7) ve
            # siralama numarasi girdi dosyasina/--limit'e göre kayar. Görselin
            # etiket dosyasindaki satira bağlanabilmesi için tek güvenli
            # anahtar kayit_id'dir.
            cikti_yolu = output_dir / f"{fatura['kayit_id']}.png"
            if cikti_yolu.exists() and not args.yeniden:
                atlanan += 1
                continue

            try:
                sayfa.set_content(template.render(**baglam_kur(fatura)))
                sayfa.locator(".receipt-container").screenshot(path=str(cikti_yolu))
                uretilen += 1
            except Exception as e:
                # Tek bozuk kayit 25k'lik koşuyu düşürmesin.
                hatali += 1
                print(f"  [X] {fatura['kayit_id']}: {type(e).__name__}: {e}")

            if i % 500 == 0:
                print(f"  {i}/{len(faturalar)} işlendi (üretilen {uretilen}, atlanan {atlanan})")

        tarayici.close()

    print(f"Tamamlandi: {uretilen} fis -> {output_dir}/")
    if atlanan:
        print(f"  {atlanan} fis zaten vardi, atlandi (--yeniden ile zorla).")
    if hatali:
        print(f"  [!] {hatali} fis üretilemedi.")


if __name__ == "__main__":
    main()
