"""
Firma Registry Üretici — TEK kalıcı firma-kimliği kaynağı.

Firma kişiliği artık FATURA bazlı değil, FIRMA bazlıdır: her firma bir kez
üretilip (ad + kimlik no + is_kolu + firma_türü) bu registry'de DONAR. Fatura
üretimi (rastgele_fatura) bu registry'den bir firma SEÇER, icat etmez. Böylece:
  - Aynı ad → hep aynı VKN (ayni_ad_farkli_vkn yapısal olarak imkânsız).
  - Aynı VKN → hep aynı ad (ayni_vkn_farkli_ad yapısal olarak imkânsız).
  - Batch'ler arası tutarlılık kontrolle değil, İNŞAYLA gelir.

Registry TEK dosyada harmanlanmış kaynaklardan oluşur (hepsi aynı şemada):
  1. OSM gerçek işletme adları (tüzel kişi)          -> kaynak=osm
  2. Sentetik is_kolu-bazlı adlar (tüzel kişi dolgu)  -> kaynak=sentetik
  3. Şahıs şirketi (fake.name + TCKN)                 -> kaynak=sahis

İnce OSM iş kolları (ör. lojistik) hedefe kadar SENTETİK adla doldurulur; OSM
re-pull (firma_adlari_osm_cek.py) ileride OSM payını artırıp dolguyu azaltır —
registry şeması değişmez.

Kullanım:
    python firma_registry_olustur.py --hedef-per-iskolu 1500 --seed 42

Çıktı: data/firma_registry.csv
"""

import argparse
import csv
import random
from collections import Counter
from pathlib import Path

from schema import IsKolu, FirmaTuru
from generators.field_generator import (
    rastgele_firma_adi, rastgele_vkn, rastgele_tckn, FIRMA_TURU_AGIRLIK,
)

OSM_CSV = Path("data/firma_adlari_osm.csv")
REGISTRY_CSV = Path("data/firma_registry.csv")

# Tüzel türler (şahıs hariç) ve orijinal ağırlıkları — şahıs oranını registry
# kompozisyonunda ayrıca uyguladığımız için burada yalnız tüzel türleri harmanlıyoruz.
TUZEL_TURLER = [FirmaTuru.KISA_UNVAN, FirmaTuru.UZUN_UNVAN, FirmaTuru.YABANCI_ORTAKLI]
TUZEL_AGIRLIK = [FIRMA_TURU_AGIRLIK[t] for t in TUZEL_TURLER]
SAHIS_ORANI = FIRMA_TURU_AGIRLIK[FirmaTuru.SAHIS_SIRKETI] / sum(FIRMA_TURU_AGIRLIK.values())

# İsim üretiminde çakışma olursa kaç kez yeniden deneyelim (havuzlar geniş,
# çakışma nadir; tükenirse sayaç ekiyle benzersizleştirilir).
MAX_ISIM_DENEME = 60


def osm_adlarini_yukle() -> dict[str, list[str]]:
    """OSM CSV'yi is_kolu -> [isim, ...] olarak yükler. Yoksa boş döner."""
    if not OSM_CSV.exists():
        print(f"[!] {OSM_CSV} yok — registry tamamen sentetik adlarla üretilecek.")
        return {}
    gruplar: dict[str, list[str]] = {}
    with open(OSM_CSV, encoding="utf-8") as f:
        for satir in csv.DictReader(f):
            isim = (satir.get("isim") or "").strip()
            is_kolu = (satir.get("is_kolu") or "").strip()
            if isim and is_kolu:
                gruplar.setdefault(is_kolu, []).append(isim)
    return gruplar


def benzersiz_kimlik(uretici, kullanilmis: set[str]) -> str:
    """Verilen kimlik üreticisinden (VKN/TCKN) global BENZERSIZ bir no döndürür."""
    while True:
        no = uretici()
        if no not in kullanilmis:
            kullanilmis.add(no)
            return no


def benzersiz_isim(uretici, kullanilmis: set[str]) -> str | None:
    """Global benzersiz bir ad döndürür; havuz tükenirse None (çağıran ek yapar)."""
    for _ in range(MAX_ISIM_DENEME):
        ad = uretici()
        if ad not in kullanilmis:
            kullanilmis.add(ad)
            return ad
    return None


def registry_uret(hedef_per_iskolu: int) -> list[dict]:
    osm_adlari = osm_adlarini_yukle()

    kullanilmis_kimlik: set[str] = set()
    kullanilmis_ad: set[str] = set()
    kayitlar: list[dict] = []
    firma_id = 0

    for is_kolu in IsKolu:
        anahtar = is_kolu.value
        sahis_sayisi = round(hedef_per_iskolu * SAHIS_ORANI)
        tuzel_sayisi = hedef_per_iskolu - sahis_sayisi

        # --- Tüzel kişi: önce OSM gerçek adları, sonra sentetik dolgu ---
        havuz = list(osm_adlari.get(anahtar, []))
        random.shuffle(havuz)
        osm_kullanildi = 0
        sentetik_kullanildi = 0

        for i in range(tuzel_sayisi):
            tur = random.choices(TUZEL_TURLER, weights=TUZEL_AGIRLIK, k=1)[0]
            if osm_kullanildi < len(havuz):
                ad = havuz[osm_kullanildi]
                osm_kullanildi += 1
                if ad in kullanilmis_ad:
                    continue  # nadir: aynı ad başka is_kolu'nda çıktı, atla
                kullanilmis_ad.add(ad)
                kaynak = "osm"
            else:
                ad = benzersiz_isim(
                    lambda: rastgele_firma_adi(is_kolu, tur), kullanilmis_ad
                )
                if ad is None:
                    ad = f"{rastgele_firma_adi(is_kolu, tur)} {firma_id}"
                    kullanilmis_ad.add(ad)
                sentetik_kullanildi += 1
                kaynak = "sentetik"

            kayitlar.append({
                "firma_id": firma_id,
                "is_kolu": anahtar,
                "firma_turu": tur.value,
                "satici_unvan": ad,
                "satici_kimlik": benzersiz_kimlik(rastgele_vkn, kullanilmis_kimlik),
                "kaynak": kaynak,
            })
            firma_id += 1

        # --- Şahıs şirketi: fake.name() + TCKN (ada is_kolu yansımaz, kolonda tutulur) ---
        for i in range(sahis_sayisi):
            ad = benzersiz_isim(
                lambda: rastgele_firma_adi(is_kolu, FirmaTuru.SAHIS_SIRKETI),
                kullanilmis_ad,
            )
            if ad is None:
                ad = f"{rastgele_firma_adi(is_kolu, FirmaTuru.SAHIS_SIRKETI)} {firma_id}"
                kullanilmis_ad.add(ad)
            kayitlar.append({
                "firma_id": firma_id,
                "is_kolu": anahtar,
                "firma_turu": FirmaTuru.SAHIS_SIRKETI.value,
                "satici_unvan": ad,
                "satici_kimlik": benzersiz_kimlik(rastgele_tckn, kullanilmis_kimlik),
                "kaynak": "sahis",
            })
            firma_id += 1

        print(f"  {anahtar:<22} hedef={hedef_per_iskolu:<5} "
              f"osm={osm_kullanildi:<5} sentetik={sentetik_kullanildi:<5} sahis={sahis_sayisi}")

    return kayitlar


def registry_yaz(kayitlar: list[dict]) -> None:
    REGISTRY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_CSV, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(
            f, fieldnames=["firma_id", "is_kolu", "firma_turu", "satici_unvan", "satici_kimlik", "kaynak"]
        )
        yazici.writeheader()
        yazici.writerows(kayitlar)


def main():
    parser = argparse.ArgumentParser(description="Firma registry üretici (tek kalıcı kaynak)")
    parser.add_argument("--hedef-per-iskolu", type=int, default=1500,
                        help="Her iş kolu için hedef firma sayısı (OSM + sentetik dolgu + şahıs)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Determinizm için seed — aynı seed = aynı registry (VKN'ler sabit)")
    args = parser.parse_args()

    random.seed(args.seed)
    print(f"Registry üretiliyor (hedef/is_kolu={args.hedef_per_iskolu}, seed={args.seed})...")
    kayitlar = registry_uret(args.hedef_per_iskolu)
    registry_yaz(kayitlar)

    kaynak_sayim = Counter(k["kaynak"] for k in kayitlar)
    print(f"\n[+] Toplam {len(kayitlar)} firma -> {REGISTRY_CSV}")
    print(f"    Kaynak dağılımı: {dict(kaynak_sayim)}")
    print(f"    Benzersiz VKN/TCKN: {len({k['satici_kimlik'] for k in kayitlar})}, "
          f"benzersiz unvan: {len({k['satici_unvan'] for k in kayitlar})}")


if __name__ == "__main__":
    main()
