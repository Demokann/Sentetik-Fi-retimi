"""Fatura JSON'larindan GÖRSEL fiş (receipt) render'lari üretir.

Veri boru hattinin parçasi DEĞİLDİR; fişin kendisini üreten ayri araçtir
(bkz. docs/arsiv/faz_a_fatura_uretimi.md).

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
import hashlib
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template
from playwright.sync_api import sync_playwright


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
    """Tam sayiysa '3', değilse '3,45' (Türkçe ondalik, gereksiz .0 basmaz).

    KARAR BIRIME DEGIL DEGERE BAKAR (2026-08-01). Eskiden `birim in
    TAM_SAYI_BIRIMLERI` ise `int(miktar)` basiliyordu; birim listesine 'Ay' ve
    'Paket' eklenince bu, ESKI veriyi (2,72 Ay) sessizce '2 Ay' diye basar,
    satir toplami ise hâlâ 2,72 uzerinden hesaplanirdi -> 7.427 fisde sahte
    "hesap tutmuyor" sinyali. Degere bakarak fis, verinin hangi kosudan
    geldiginden BAGIMSIZ olarak kendi icinde tutarli kalir; birim listesi de
    yalniz URETIMI yonetir (bkz. field_generator.rastgele_miktar)."""
    if float(miktar).is_integer():
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


def _kayit_hash(kayit_id: str) -> int:
    """kayit_id -> kararli tamsayi. random DEĞİL: ayni fatura her koşuda ayni
    şabloni/saati almali, yoksa resume ile yeniden üretilen fiş öncekinden
    farkli görünür."""
    return int(hashlib.md5(kayit_id.encode("utf-8")).hexdigest(), 16)


def tarih_tr(iso_tarih: str) -> str:
    """'2026-07-26' -> '26.07.2026'."""
    y, a, g = iso_tarih.split("-")
    return f"{g}.{a}.{y}"


def saat_uret(tohum: str) -> str:
    """Fiş saati (08:00-20:59). Veride saat alani YOK, gerçek fişte var.

    Tohumdan türetilir: hem kararlidir hem de etiketlerle KORELASYONSUZDUR.
    Saati anomaliye/kategoriye bağli üretmek görsele sahte bir sinyal ekler ve
    aşaği akiştaki model onu kisayol olarak öğrenir.

    Tohum kural olarak kayit_id'dir; TEK istisna `saat_tohumlari_kur`un
    eşleştirdiği mükerrer yükleme çiftleridir (bkz. o fonksiyon)."""
    h = _kayit_hash(tohum)
    return f"{8 + h % 13:02d}:{(h // 13) % 60:02d}"


# `saat_tohumlari_kur`un özdeşlik imzasindan DIŞLANAN alanlar: fişin üstünde
# BASILMAYAN, dolayisiyla iki görselin ayni olup olmadigini belirlemeyen alanlar.
#   kayit_id      -- çiftin tanimi geregi tek farki; imzaya girerse hiçbir şey eşleşmez.
#   aciklama_metni-- Faz B çiktisi. Mükerrer çiftte KASITLI olarak farklidir
#                    (bkz. anomaly_injector._mukerrer_fis_yukleme_uygula: ayni fişi
#                    ikinci kez yükleyen çalişan sifirdan not yazar). Fişe basilmaz.
# Girdi olarak `faturalar_aciklamali.json` verildiğinde bu alan olmadan eşleşme
# 1496'dan 0'a düşüyordu -- yani düzeltme sessizce devre disi kaliyordu.
IMZA_DISI_ALANLAR = {"kayit_id", "aciklama_metni"}


def saat_tohumlari_kur(faturalar: list[dict]) -> dict[str, str]:
    """`kayit_id -> saat tohumu` eşlemesi: AYNI fişin iki kez yüklendiği
    (`mukerrer_fis_yukleme`) çiftlerde iki görselin ayni saati basmasini sağlar.

    Sorun: saat `kayit_id`'nin hash'inden türüyordu, f2 ise f1'in `kayit_id`
    HARİÇ birebir kopyasi. Dolayisiyla ayni kâğit iki farkli saatle basiliyordu
    -- oysa saat kâğidin üstünde basilidir, ikinci yükleme onu değiştirmez.
    Model çelişkili kanit görüyordu: yapisal alanlarin tamami ayni ("ayni fiş")
    ama saat farkli ("iki ayri alişveriş"). Ölçüldü: fisler_25k'da eşi de sette
    olan 125 çiftin 30'u bu durumdaydi; şablon_1'e SAAT satiri eklendikten sonra
    (saatsiz olduğu için sorunsuz görünen 33 çift dahil) 125'inin tamami
    bozulacakti -- bu yüzden iki değişiklik birlikte gitmek ZORUNDA.

    GÜVENLİK -- eşleme etiket dosyasindan DEĞİL verinin kendisinden çikar:
    yalnizca `kayit_id` HARİÇ tüm alanlari birebir eşit olan kayitlar ayni
    tohuma bağlanir. Bu test
      * `fatura_no_cakismasi`'ni (ayni vkn+fatura_no ama tarih/kalemler farkli),
      * doğal fatura_no çakişmalarini (CLAUDE.md §7, ~5 adet; her şey farkli)
    yapisal olarak DIŞARIDA birakir. Etiket render katmanina sizmaz ve "yanliş
    fiş eşitlendi" vakasi tanim geregi imkânsizdir: eşitlenen iki kayit zaten
    birebir ayniysa ayni saati basmalari DOĞRU davraniştir.

    ŞABLON KASITLI OLARAK EŞİTLENMEZ (`sablon_sec` kayit_id'de kalir): çift
    piksel piksel özdeşleşirse mükerrerlik perceptual hash ile çözülebilir hale
    gelir. Farkli düzen + ayni saat, modeli içerik eşleştirmeye zorlayan tutarli
    bir zor vaka birakir (ölçüldü: 62 çift).
    """
    gruplar: dict[tuple[str, str], list[dict]] = {}
    for f in faturalar:
        gruplar.setdefault((f["satici_vkn"], f["fatura_no"]), []).append(f)

    tohumlar: dict[str, str] = {}
    aday_grup = 0
    for kayitlar in gruplar.values():
        if len(kayitlar) < 2:
            continue
        aday_grup += 1
        # IMZA_DISI alanlarin disindakilerin imzasina göre kümele.
        kumeler: dict[str, list[str]] = {}
        for f in kayitlar:
            imza = json.dumps({k: v for k, v in f.items() if k not in IMZA_DISI_ALANLAR},
                              sort_keys=True, ensure_ascii=False)
            kumeler.setdefault(imza, []).append(f["kayit_id"])
        for kimlikler in kumeler.values():
            if len(kimlikler) > 1:
                # min(): hangi kaydin f1 oldugu veriden okunamaz (etikete
                # bakmadan). Deterministik bir temsilci yeterli -- amac ortak
                # bir saat, belirli bir saat degil.
                tohum = min(kimlikler)
                for kid in kimlikler:
                    tohumlar[kid] = tohum

    # GÜRÜLTÜLÜ UYARI: ayni (vkn, fatura_no) tasiyan gruplar VAR ama hiçbiri
    # özdeş çikmadiysa imza büyük olasilikla fişte BASILMAYAN bir alan yüzünden
    # ayrişiyordur (IMZA_DISI_ALANLAR'a bak). Sessizce geçerse düzeltme
    # uygulanmadan 24k fiş render edilir ve fark edilmez.
    if aday_grup and not tohumlar:
        print(f"  [!] UYARI: ayni (vkn, fatura_no) tasiyan {aday_grup} grup var ama "
              f"hiçbiri özdeş çikmadi -> saat tohumu UYGULANMADI. Girdi dosyasinda "
              f"IMZA_DISI_ALANLAR'a eklenmemiş fazladan bir alan olabilir.")
    return tohumlar


def kdv_gruplari_kur(kalemler: list[dict]) -> list[dict]:
    """KDV oranina göre kirilim (e-arşiv fişindeki KDV tablosu için).

    Kalem düzeyindeki `kdv_tutari`ndan toplanir -- `kdv_tutari` anomalisi
    böylece bu tabloda da iz birakir."""
    gruplar: dict[float, dict] = {}
    for k in kalemler:
        g = gruplar.setdefault(k["kdv_orani"], {"oran": k["kdv_orani"], "kdv": 0.0, "toplam": 0.0})
        g["kdv"] += k["kdv_tutari"]
        g["toplam"] += k["satir_toplam"]
    for g in gruplar.values():
        g["kdv"] = round(g["kdv"], 2)
        g["toplam"] = round(g["toplam"], 2)
    return [gruplar[o] for o in sorted(gruplar)]


def baglam_kur(fatura: dict, saat_tohumlari: dict[str, str] | None = None) -> dict:
    """fatura_to_dict çiktisi + şablonlarin ihtiyaç duyduğu türetilmiş alanlar."""
    kayit_id: str = fatura["kayit_id"]
    # Tohum kural olarak kayit_id; eşlemede varsa mükerrer çiftin ortak tohumu.
    tohum = saat_tohumlari.get(kayit_id, kayit_id) if saat_tohumlari else kayit_id
    baglam = dict(fatura)
    baglam["satici_kimlik_etiketi"] = kimlik_etiketi(fatura["satici_vkn"])
    baglam["kalemler"] = [kalem_gosterimi(k) for k in fatura["kalemler"]]
    baglam["fatura_tarihi_tr"] = tarih_tr(fatura["fatura_tarihi"])
    baglam["saat"] = saat_uret(tohum)
    baglam["kdv_gruplari"] = kdv_gruplari_kur(fatura["kalemler"])
    return baglam


def sablon_sec(fatura: dict, sablonlar: list[Template]) -> Template:
    """Faturaya şablon atar. Tek şablonla üretilen 25k fişin hepsi ayni
    göründüğü için model fiş tipini değil tek bir düzeni öğreniyordu.

    Seçim kayit_id'den DETERMİNİSTİK (bkz. _kayit_hash) ve etiketten
    BAĞIMSIZ: fiş tipini anomaliye bağlamak görsele sahte sinyal ekler."""
    return sablonlar[_kayit_hash(fatura["kayit_id"]) % len(sablonlar)]


def main():
    parser = argparse.ArgumentParser(description="JSON faturalardan fiş (receipt) görseli üretir")
    parser.add_argument("--input-json", required=True, help="faturalar.json dosya yolu")
    parser.add_argument("--output-dir", default="data/fisler", help="Görsellerin kaydedileceği klasör")
    parser.add_argument("--template", default="fis_sablon_1.html,fis_sablon_2.html",
                        help="Jinja2 şablon yolu/yolları (virgülle ayır). Birden fazlaysa her "
                             "faturaya kayit_id'den deterministik olarak biri atanır.")
    parser.add_argument("--limit", type=int, default=None, help="Test için ilk N faturayı işle (varsayılan: hepsi)")
    parser.add_argument("--yeniden", action="store_true",
                        help="Var olan PNG'leri de yeniden üret (varsayılan: atlanır, resume)")
    parser.add_argument("--tohum-json", default=None,
                        help="Saat tohumlarının hesaplanacağı TAM fatura dosyası "
                             "(varsayılan: --input-json). Alt küme render ederken "
                             "tam dosyayı ver, yoksa mükerrer çiftin eşi görünmez ve "
                             "fiş tam koşudakinden FARKLI saat alır.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)

    # Tohumlar --limit'ten ÖNCE, tam havuzdan hesaplanir: dilim mükerrer çiftin
    # bir üyesini disarda birakirsa saat tam koşudakinden farkli çikardi.
    if args.tohum_json:
        with open(args.tohum_json, "r", encoding="utf-8") as f:
            saat_tohumlari = saat_tohumlari_kur(json.load(f))
    else:
        saat_tohumlari = saat_tohumlari_kur(faturalar)
    print(f"Saat tohumu eşlenen kayıt: {len(saat_tohumlari)} "
          f"({len(set(saat_tohumlari.values()))} mükerrer yükleme kümesi)")

    if args.limit:
        faturalar = faturalar[: args.limit]

    yollar = [Path(y.strip()) for y in args.template.split(",") if y.strip()]
    dizinler = sorted({str(y.parent or Path(".")) for y in yollar})
    env = Environment(loader=FileSystemLoader(dizinler))
    env.filters["tutar"] = tutar_formatla
    sablonlar = [env.get_template(y.name) for y in yollar]

    print(f"{len(faturalar)} fatura için fiş üretiliyor "
          f"({len(sablonlar)} şablon: {', '.join(y.name for y in yollar)})...")

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
                sablon = sablon_sec(fatura, sablonlar)
                sayfa.set_content(sablon.render(**baglam_kur(fatura, saat_tohumlari)))
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
