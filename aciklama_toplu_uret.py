"""
Toplu açıklama üretim runner'ı: batch_hazirla.py'nin ürettiği batch
dosyalarını sırayla işler. Burst + cooldown temposuyla çalışır (N fatura üret
-> X dk dur -> devam), kaldığı yerden devam eder (resumable) ve her batch için
ayrı bir çıktı JSON'una anlık yazar.

Kullanım:
    python aciklama_toplu_uret.py --burst-size 200 --cooldown-min 15 --workers 3
Kesilirse (Ctrl-C / çökme / ertesi gün) aynı komut kaldığı yerden devam eder.
"""

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from aciklama_uretim_core import (
    OLLAMA_HOST_VARSAYILAN,
    MODEL_VARSAYILAN,
    KATEGORILER,
    tek_fatura_isleme,
    modeli_bellekten_indir,
    yakin_kopya_mi,
    _token_set,
    distinct_n,
)

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"


def durum_yukle(dizin: Path) -> dict:
    with open(dizin / "durum.json", "r", encoding="utf-8") as f:
        return json.load(f)


def durum_kaydet(dizin: Path, durum: dict) -> None:
    with open(dizin / "durum.json", "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=2)


def cikti_yukle(yol: Path) -> dict:
    """Varsa daha önce üretilmiş çıktıları (fatura_no -> kayıt) yükler."""
    if yol.exists():
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def cikti_kaydet(yol: Path, cikti: dict) -> None:
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(cikti, f, ensure_ascii=False, indent=2)


def dilimle(liste: list, boyut: int):
    for i in range(0, len(liste), boyut):
        yield liste[i : i + boyut]


def main():
    parser = argparse.ArgumentParser(description="Toplu açıklama üretimi (burst + cooldown, resumable)")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="batch + durum.json dizini")
    parser.add_argument("--burst-size", type=int, default=200, help="Cooldown öncesi işlenecek fatura sayısı")
    parser.add_argument("--cooldown-min", type=float, default=15.0, help="Burst arası mola (dakika)")
    parser.add_argument("--workers", type=int, default=2, help="Paralel istek sayısı (16GB RAM için 2 önerilir; OLLAMA_NUM_PARALLEL'dan büyük olması FAYDASIZ)")
    parser.add_argument("--model", default=MODEL_VARSAYILAN)
    parser.add_argument("--host", default=OLLAMA_HOST_VARSAYILAN)
    parser.add_argument("--max-batch", type=int, default=0, help="Bu koşuda en fazla kaç batch işlensin (0 = sınırsız)")
    parser.add_argument("--insan-md", action="store_true", help="İnsan incelemesi için ayrıca MD raporu yaz")
    args = parser.parse_args()

    dizin = Path(args.cikti_dizini)
    durum = durum_yukle(dizin)
    kalan_batchler = [b for b in durum["batchler"] if not b["tamam"]]

    if not kalan_batchler:
        print("[✓] Tüm batch'ler zaten tamamlanmış. Yapılacak iş yok.")
        return

    print(f"[+] {len(kalan_batchler)} bekleyen batch var (toplam {durum['batch_sayisi']}).")
    print(f"[+] Tempo: {args.burst_size} fatura/burst, {args.cooldown_min} dk cooldown, {args.workers} worker.\n")

    baslangic = time.time()
    genel_uretilen = 0
    genel_retry = 0
    genel_hala_ihlalli = 0
    genel_kategori = Counter()
    islenen_batch = 0
    # Çeşitlilik/dedup ölçümü (run boyunca kategori-içi birikir). Yakın-kopya
    # ÇIKTIYI DÜŞÜRMEZ (resumability + her faturaya açıklama garantisi bozulmasın)
    # -- sadece kayda 'yakin_kopya' bayrağı basar ve raporda görünür kılar.
    kabul_token_setleri: dict[str, list[set[str]]] = {k: [] for k in KATEGORILER}
    kategori_metinleri: dict[str, list[str]] = {k: [] for k in KATEGORILER}
    yakin_kopya_sayaci = 0
    # Model burst boyunca bellekte kalsın; cooldown başında explicit indireceğiz.
    keep_alive = f"{int(args.cooldown_min * 60) + 300}s"

    for batch in kalan_batchler:
        if args.max_batch and islenen_batch >= args.max_batch:
            print(f"[+] --max-batch={args.max_batch} sınırına ulaşıldı, duruluyor.")
            break

        batch_yolu = dizin / batch["dosya"]
        cikti_yolu = dizin / batch["cikti_dosyasi"]
        md_yolu = dizin / batch["cikti_dosyasi"].replace(".json", ".md")

        with open(batch_yolu, "r", encoding="utf-8") as f:
            faturalar = json.load(f)

        cikti = cikti_yukle(cikti_yolu)  # resumability: bitmişleri atla
        # Resume: bu batch'te daha önce üretilenleri dedup birikimine kat ki
        # yakın-kopya bayrağı tutarlı kalsın.
        for _k in cikti.values():
            _kat = _k.get("aciklama_kategorisi")
            _m = _k.get("aciklama_metni")
            if _kat in kabul_token_setleri and _m:
                kabul_token_setleri[_kat].append(_token_set(_m))
                kategori_metinleri[_kat].append(_m)
        kalanlar = [f for f in faturalar if f["fatura_no"] not in cikti]

        print(f"=== {batch['dosya']}: {len(faturalar)} fatura, {len(cikti)} zaten üretilmiş, {len(kalanlar)} kaldı ===")

        if not kalanlar:
            batch["tamam"] = True
            durum_kaydet(dizin, durum)
            print(f"    (bu batch zaten tamam, işaretlendi)\n")
            continue

        dilimler = list(dilimle(kalanlar, args.burst_size))
        for dilim_no, dilim in enumerate(dilimler, 1):
            dilim_basi = time.time()
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        tek_fatura_isleme, fatura, fatura, args.model, args.host, keep_alive
                    ): fatura
                    for fatura in dilim
                }
                for future in as_completed(futures):
                    fatura, _etiket, metin, hata, ihlaller, deneme_sayisi = future.result()
                    fno = fatura["fatura_no"]
                    kategori = fatura["aciklama_kategorisi"]

                    if hata or metin is None:
                        print(f"    [X] {fno} - HATA: {hata or 'Metin boş'}")
                        continue

                    # Dedup: aynı kategoride kabul edilenlere çok benziyorsa
                    # işaretle (düşürme). yetersiz'de tekrar doğal ('iş gideri'
                    # vb.) -> eşik daha gevşek.
                    dedup_esik = 0.95 if kategori == "yetersiz" else 0.8
                    yakin = yakin_kopya_mi(metin, kabul_token_setleri.get(kategori, []), dedup_esik)
                    if yakin:
                        yakin_kopya_sayaci += 1
                    if kategori in kabul_token_setleri:
                        kabul_token_setleri[kategori].append(_token_set(metin))
                        kategori_metinleri[kategori].append(metin)

                    cikti[fno] = {
                        "aciklama_metni": metin,
                        "aciklama_kategorisi": kategori,
                        "deneme_sayisi": deneme_sayisi,
                        "kalan_ihlaller": ihlaller,
                        "yakin_kopya": yakin,
                    }
                    genel_uretilen += 1
                    genel_kategori[kategori] += 1
                    if deneme_sayisi == 2:
                        genel_retry += 1
                        if ihlaller:
                            genel_hala_ihlalli += 1

                    # Her fatura bitince hemen diske yaz + yazdır -- burst
                    # sonunu beklemeden anlık ilerleme görülsün ve kesintide
                    # (Ctrl-C/çökme) o ana kadar üretilenler kaybolmasın.
                    cikti_kaydet(cikti_yolu, cikti)
                    ihlal_notu = f", kalan_ihlal={ihlaller}" if ihlaller else ""
                    yakin_notu = ", yakin_kopya" if yakin else ""
                    print(f"    [{genel_uretilen}] {fno} ({kategori}, deneme={deneme_sayisi}{ihlal_notu}{yakin_notu})")

            gecen = time.time() - dilim_basi
            print(f"    [dilim {dilim_no}/{len(dilimler)}] {len(dilim)} fatura işlendi ({gecen:.0f} sn), toplam üretilen: {genel_uretilen}")

            # Son dilim değilse cooldown (cooldown_min<=0 ise tamamen atlanır --
            # pilot script'teki gibi model hiç indirilmeden sıcak kalmaya devam eder)
            if dilim_no < len(dilimler) and args.cooldown_min > 0:
                print(f"    [cooldown] model bellekten indiriliyor, {args.cooldown_min} dk mola...")
                modeli_bellekten_indir(args.model, args.host)
                time.sleep(args.cooldown_min * 60)

        # Batch tamam
        batch["tamam"] = True
        batch["tamamlanma_zamani"] = time.strftime("%Y-%m-%d %H:%M:%S")
        durum_kaydet(dizin, durum)
        islenen_batch += 1

        if args.insan_md:
            _md_yaz(md_yolu, faturalar, cikti, args.model)

        print(f"=== {batch['dosya']} TAMAM ===\n")

        # Batch'ler arası da cooldown (son işlenen batch değilse, iş kaldıysa ve cooldown_min>0 ise)
        kalan_var = any(not b["tamam"] for b in durum["batchler"])
        sinir_var = args.max_batch and islenen_batch >= args.max_batch
        if kalan_var and not sinir_var and args.cooldown_min > 0:
            print(f"[cooldown] batch arası, model indiriliyor, {args.cooldown_min} dk mola...\n")
            modeli_bellekten_indir(args.model, args.host)
            time.sleep(args.cooldown_min * 60)

    # Özet
    gecen = time.time() - baslangic
    dk, sn = divmod(gecen, 60)
    kalan_toplam = sum(1 for b in durum["batchler"] if not b["tamam"])
    print("\n" + "=" * 50)
    print(f"Bu koşuda üretilen açıklama: {genel_uretilen}")
    print(f"Retry tetiklenen: {genel_retry} (bunlardan {genel_hala_ihlalli} tanesi 2. denemede de ihlalli)")
    print(f"Kategori dağılımı: {dict(genel_kategori)}")
    print(f"Yakın-kopya işaretlenen (düşürülmedi): {yakin_kopya_sayaci}")
    print("Çeşitlilik (distinct-1 / distinct-2, 1'e yakın = çeşitli):")
    for kat in KATEGORILER:
        metinler = kategori_metinleri.get(kat, [])
        if not metinler:
            continue
        print(f"  {kat:12s} n={len(metinler):5d} | distinct-1={distinct_n(metinler, 1):.3f} "
              f"distinct-2={distinct_n(metinler, 2):.3f}")
    print(f"Kalan batch sayısı: {kalan_toplam}")
    print(f"Geçen süre (cooldown dahil): {int(dk)} dk {int(sn)} sn")
    if kalan_toplam == 0:
        print("Tüm batch'ler tamamlandı! Sonraki adım: python aciklama_birlestir.py")
    else:
        print("Devam etmek için aynı komutu tekrar çalıştır (kaldığı yerden devam eder).")
    print("=" * 50)


def _md_yaz(md_yolu: Path, faturalar: list[dict], cikti: dict, model: str) -> None:
    from aciklama_uretim_core import kalemler_ozetle
    with open(md_yolu, "w", encoding="utf-8") as f:
        f.write(f"# Ollama ({model}) — {md_yolu.stem}\n\n---\n\n")
        for idx, fatura in enumerate(faturalar, 1):
            kayit = cikti.get(fatura["fatura_no"])
            if not kayit:
                continue
            uyari = ""
            if kayit["kalan_ihlaller"]:
                uyari = f"*⚠️ kalan ihlaller: {kayit['kalan_ihlaller']}*\n\n"
            f.write(f"## {idx}. {fatura['fatura_no']}\n\n")
            f.write(f"- **Kategori:** `{kayit['aciklama_kategorisi']}`\n")
            f.write(f"- **Anomali Türleri:** `{fatura['anomali_turleri']}`\n\n")
            f.write(f"**Kalemler:**\n{kalemler_ozetle(fatura['kalemler'])}\n\n")
            f.write(f"**Üretilen Açıklama:**\n> {kayit['aciklama_metni']}\n\n")
            f.write(uyari)
            f.write("---\n\n")


if __name__ == "__main__":
    main()
