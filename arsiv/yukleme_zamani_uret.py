"""`yukleme_zamani` uretip MODEL GIRDISI dosyasina yazan GECICI retrofit araci.

Deterministik (`kayit_id` hash'i), idempotent, ISO 8601 + `+03:00`. Gecikme tek
dagilimdan cekilir, temiz/anomalili ayrimi YOK. Referans "simdi" sabittir
(`REFERANS_SON`), `date.today()` kullanilmaz.

Kalici cozum: alan Faz A'da uretilip `fatura_to_dict`'e girmeli. Tasarim
gerekcesi (referans tarih secimi, simetrik cift bolmesi, gelecek_tarihli
istisnasi) `docs/01-faz-a-fatura-uretimi.md`.

    python -m arsiv.yukleme_zamani_uret            # RAPOR
    python -m arsiv.yukleme_zamani_uret --uygula   # yedek alip yazar
"""

import argparse
import datetime as dt
import hashlib
import json
import random
import shutil
from collections import Counter
from pathlib import Path

from ortak.cift_grup import ciftleri_bul

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
VARSAYILAN_ETIKET_JSON = "data/faturalar_aciklamali_etiketler.json"

TR = dt.timezone(dt.timedelta(hours=3))

# Yukleme bunu ASAMAZ; normal faturalar (05-03..08-01) ile gelecek_tarihli
# olanlarin (08-31..) arasinda secildi.
REFERANS_SON = dt.datetime(2026, 8, 15, 23, 59, tzinfo=TR)
NORMAL_FATURA_ILK = dt.date(2026, 5, 3)
NORMAL_FATURA_SON = dt.date(2026, 8, 1)

# Gecikme: saga carpik, tavan 45 gun. (alt, ust, agirlik)
GECIKME_BANTLARI = [(0, 1, 40), (2, 7, 30), (8, 21, 20), (22, 45, 10)]
# Mukerrer ciftte iki yukleme arasi (gun). 0 = ayni gun, saatler farkli.
ARALIK_BANTLARI = [(0, 0, 40), (1, 1, 35), (2, 3, 15), (4, 7, 10)]
ASGARI_ARALIK = dt.timedelta(hours=1)

# Is saati agirlikli; 12-13 ogle cukuru, gece cok seyrek.
SAAT_AGIRLIKLARI = {
    0: 0.2, 1: 0.1, 2: 0.1, 3: 0.1, 4: 0.1, 5: 0.2, 6: 0.5, 7: 1.5,
    8: 3.0, 9: 8.0, 10: 9.5, 11: 9.0, 12: 4.5, 13: 7.0, 14: 9.0, 15: 9.0,
    16: 8.5, 17: 7.5, 18: 5.0, 19: 3.0, 20: 2.5, 21: 2.0, 22: 1.2, 23: 0.6,
}

# Tarihlerin 2/7'si hafta sonuna duser; hedef pay %12 -> %58'i kaydirilir.
HAFTA_SONU_KALMA = 0.42


def _rnd(*parcalar: str) -> random.Random:
    """kayit_id/cift anahtarindan deterministik uretec."""
    tohum = hashlib.md5("|".join(parcalar).encode("utf-8")).hexdigest()
    return random.Random(int(tohum[:16], 16))


def _bant_sec(rnd: random.Random, bantlar, tavan: int) -> int:
    """Agirlikli bant secip icinden tam sayi ceker. Tavani asan bantlar elenir."""
    uygun = [(a, min(b, tavan), w) for a, b, w in bantlar if a <= tavan]
    if not uygun:
        return max(0, tavan)
    a, b, _ = rnd.choices(uygun, weights=[w for *_, w in uygun], k=1)[0]
    return rnd.randint(a, b)


def _is_gunune_kaydir(rnd: random.Random, tarih: dt.date,
                      en_erken: dt.date, en_gec: dt.date) -> dt.date:
    """Hafta sonuna dusen yuklemenin %58'ini is gunune tasir."""
    if tarih.weekday() < 5 or rnd.random() < HAFTA_SONU_KALMA:
        return tarih
    ileri = tarih + dt.timedelta(days=7 - tarih.weekday())      # Pazartesi
    if ileri <= en_gec:
        return ileri
    geri = tarih - dt.timedelta(days=tarih.weekday() - 4)       # Cuma
    return geri if geri >= en_erken else tarih


def _an_kur(rnd: random.Random, fatura_tarihi: dt.date, gecikme: int) -> dt.datetime:
    """Gecikme gunu + is saati sekillendirmesi -> tam zaman damgasi."""
    tarih = fatura_tarihi + dt.timedelta(days=gecikme)
    tarih = _is_gunune_kaydir(rnd, tarih, fatura_tarihi, REFERANS_SON.date())
    saat = rnd.choices(list(SAAT_AGIRLIKLARI), weights=list(SAAT_AGIRLIKLARI.values()), k=1)[0]
    an = dt.datetime(tarih.year, tarih.month, tarih.day, saat, rnd.randrange(60), tzinfo=TR)
    return min(an, REFERANS_SON)


def _tavan_gun(fatura_tarihi: dt.date) -> int:
    return max(0, (REFERANS_SON.date() - fatura_tarihi).days)


def _sahte_fatura_tarihi(kayit_id: str) -> dt.date:
    """`gelecek_tarihli` kayitlar icin normal pencereden cekilen capa tarihi."""
    rnd = _rnd("capa", kayit_id)
    yayilim = (NORMAL_FATURA_SON - NORMAL_FATURA_ILK).days
    return NORMAL_FATURA_ILK + dt.timedelta(days=rnd.randrange(yayilim + 1))


def _capa_tarihi(kayit: dict, gelecek_tarihli: bool) -> dt.date:
    if gelecek_tarihli:
        return _sahte_fatura_tarihi(kayit["kayit_id"])
    return dt.date.fromisoformat(kayit["fatura_tarihi"])


def zamanlari_uret(girdiler: list[dict], etiketler: dict[str, dict]) -> dict[str, dt.datetime]:
    """kayit_id -> yukleme ani. Ciftler once islenir, kalan kayitlar tekil."""
    girdi_map = {r["kayit_id"]: r for r in girdiler}
    zaman: dict[str, dt.datetime] = {}

    def turleri(kid):
        return set(etiketler[kid]["anomali_turleri"])

    ciftler = ciftleri_bul(girdiler)
    for (vkn, no), uyeler in ciftler.items():
        mukerrer = [k for k in uyeler if "mukerrer_fis_yukleme" in turleri(k)]
        if len(mukerrer) != 1:
            # Cakisma cifti (ya da simetrik etiket): bagimsiz cekim.
            for kid in uyeler:
                r = girdi_map[kid]
                capa = _capa_tarihi(r, "gelecek_tarihli" in turleri(kid))
                rnd = _rnd("yukleme", kid)
                zaman[kid] = _an_kur(rnd, capa, _bant_sec(rnd, GECIKME_BANTLARI, _tavan_gun(capa)))
            continue

        kopya = mukerrer[0]
        esi = [k for k in uyeler if k != kopya][0]
        capa = _capa_tarihi(girdi_map[kopya], "gelecek_tarihli" in turleri(kopya))
        tavan = _tavan_gun(capa)

        c_rnd = _rnd("cift", vkn, no)
        taban = _bant_sec(c_rnd, GECIKME_BANTLARI, tavan)
        aralik = _bant_sec(c_rnd, ARALIK_BANTLARI, 7)
        erken_g = max(0, taban - aralik // 2)
        gec_g = min(tavan, taban + (aralik - aralik // 2))

        an_a = _an_kur(_rnd("yukleme", esi), capa, erken_g)
        an_b = _an_kur(_rnd("yukleme", kopya), capa, gec_g)
        zaman[kopya], zaman[esi] = max(an_a, an_b), min(an_a, an_b)
        if zaman[kopya] - zaman[esi] < ASGARI_ARALIK:
            if zaman[esi] + ASGARI_ARALIK <= REFERANS_SON:
                zaman[kopya] = zaman[esi] + ASGARI_ARALIK
            else:
                zaman[esi] = zaman[kopya] - ASGARI_ARALIK

    for r in girdiler:
        kid = r["kayit_id"]
        if kid in zaman:
            continue
        capa = _capa_tarihi(r, "gelecek_tarihli" in turleri(kid))
        rnd = _rnd("yukleme", kid)
        zaman[kid] = _an_kur(rnd, capa, _bant_sec(rnd, GECIKME_BANTLARI, _tavan_gun(capa)))

    # Esit damga `onceki_ayni_fatura` turevinde iki kaydi da "ilk" sayardi.
    for uyeler in ciftler.values():
        a, b = sorted(uyeler, key=lambda k: (zaman[k], k))
        if zaman[a] == zaman[b]:
            zaman[b] += dt.timedelta(minutes=17)
    return zaman


def rapor_uret(girdiler, etiketler, zaman) -> dict:
    girdi_map = {r["kayit_id"]: r for r in girdiler}
    ciftler = ciftleri_bul(girdiler)

    def turleri(kid):
        return set(etiketler[kid]["anomali_turleri"])

    gelecek = {k for k in zaman if "gelecek_tarihli" in turleri(k)}
    cift_uye = {k for uyeler in ciftler.values() for k in uyeler}

    def gecikme(kid):
        return (zaman[kid].date() - dt.date.fromisoformat(girdi_map[kid]["fatura_tarihi"])).days

    kumeler = {
        "temiz (cift disi)": [k for k in zaman if k not in gelecek and k not in cift_uye
                              and not etiketler[k]["is_anomali"]],
        "anomali (cift disi)": [k for k in zaman if k not in gelecek and k not in cift_uye
                                and etiketler[k]["is_anomali"]],
        "mukerrer cift uyesi": [k for uyeler in ciftler.values() for k in uyeler
                                if any("mukerrer_fis_yukleme" in turleri(x) for x in uyeler)],
        "cakisma cift uyesi": [k for uyeler in ciftler.values() for k in uyeler
                               if not any("mukerrer_fis_yukleme" in turleri(x) for x in uyeler)],
    }
    ozet = {}
    for ad, idler in kumeler.items():
        gec = sorted(gecikme(k) for k in idler if k not in gelecek)
        ozet[ad] = {
            "adet": len(idler),
            "gecikme_ortalama": round(sum(gec) / len(gec), 2) if gec else None,
            "gecikme_medyan": gec[len(gec) // 2] if gec else None,
        }

    saatler = Counter(zaman[k].hour for k in zaman)
    hafta_sonu = sum(1 for k in zaman if zaman[k].weekday() >= 5)

    # Denetimler
    normal_ihlal = [k for k in zaman if k not in gelecek
                    and zaman[k].date() < dt.date.fromisoformat(girdi_map[k]["fatura_tarihi"])]
    gelecek_ihlal = [k for k in gelecek
                     if zaman[k].date() >= dt.date.fromisoformat(girdi_map[k]["fatura_tarihi"])]
    tavan_ihlal = [k for k in zaman if zaman[k] > REFERANS_SON]
    esit_damga, kopya_sirasi_hatali = [], []
    for uyeler in ciftler.values():
        a, b = uyeler
        if zaman[a] == zaman[b]:
            esit_damga.append(uyeler)
        muk = [k for k in uyeler if "mukerrer_fis_yukleme" in turleri(k)]
        if len(muk) == 1 and zaman[muk[0]] != max(zaman[a], zaman[b]):
            kopya_sirasi_hatali.append(uyeler)

    return {
        "kayit": len(zaman),
        "referans_son": REFERANS_SON.isoformat(),
        "en_erken": min(zaman.values()).isoformat(),
        "en_gec": max(zaman.values()).isoformat(),
        "kume_gecikmeleri": ozet,
        "hafta_sonu_orani": round(hafta_sonu / len(zaman), 4),
        "is_saati_orani": round(sum(n for s, n in saatler.items() if 9 <= s < 18) / len(zaman), 4),
        "gece_orani": round(sum(n for s, n in saatler.items() if s < 7 or s >= 22) / len(zaman), 4),
        "denetim": {
            "normal_kayit_fatura_oncesi_yuklenmis": len(normal_ihlal),
            "gelecek_tarihli_fatura_sonrasi_yuklenmis": len(gelecek_ihlal),
            "referansi_asan": len(tavan_ihlal),
            "ayni_damgali_cift": len(esit_damga),
            "kopya_once_yuklenmis_cift": len(kopya_sirasi_hatali),
        },
    }


def raporu_yazdir(rapor: dict) -> None:
    print(f"\n[+] {rapor['kayit']} kayit  |  {rapor['en_erken']}  ..  {rapor['en_gec']}"
          f"  (referans {rapor['referans_son']})")
    print(f"    is saati (09-18) %{100 * rapor['is_saati_orani']:.1f}   "
          f"hafta sonu %{100 * rapor['hafta_sonu_orani']:.1f}   "
          f"gece (22-07) %{100 * rapor['gece_orani']:.1f}")

    print("\n--- gecikme dagilimi (kumeler AYRISMAMALI) ---")
    print(f"    {'kume':24s} {'adet':>7s} {'ortalama':>10s} {'medyan':>8s}")
    for ad, o in rapor["kume_gecikmeleri"].items():
        print(f"    {ad:24s} {o['adet']:7d} {str(o['gecikme_ortalama']):>10s} "
              f"{str(o['gecikme_medyan']):>8s}")

    print("\n--- denetim ---")
    tamam = True
    for ad, n in rapor["denetim"].items():
        if ad == "gelecek_tarihli_fatura_sonrasi_yuklenmis":
            continue
        tamam &= n == 0
        print(f"    {'OK ' if n == 0 else 'HATA'} {ad}: {n}")
    n = rapor["denetim"]["gelecek_tarihli_fatura_sonrasi_yuklenmis"]
    tamam &= n == 0
    print(f"    {'OK ' if n == 0 else 'HATA'} gelecek_tarihli_fatura_ONCESI_yuklenmemis: {n}")
    print(f"\n[{'+' if tamam else '!'}] Denetim: {'TAMAM' if tamam else 'HATA'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="yukleme_zamani uret ve model girdisine yaz")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON)
    ap.add_argument("--etiket-json", default=VARSAYILAN_ETIKET_JSON)
    ap.add_argument("--uygula", action="store_true", help="Yedek alip yazar (varsayilan: rapor)")
    args = ap.parse_args()

    with open(args.girdi_json, "r", encoding="utf-8") as f:
        girdiler = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = {e["kayit_id"]: e for e in json.load(f)}

    eksik = {r["kayit_id"] for r in girdiler} - set(etiketler)
    if eksik:
        print(f"HATA: {len(eksik)} kaydin etiketi yok, sira/kopya kararlari verilemez.")
        return

    print(f"[+] {len(girdiler)} kayit okundu.")
    zaman = zamanlari_uret(girdiler, etiketler)
    rapor = rapor_uret(girdiler, etiketler, zaman)
    raporu_yazdir(rapor)

    if any(rapor["denetim"].values()):
        print("[!] Denetim basarisiz, dosya YAZILMADI.")
        return
    if not args.uygula:
        print("\n[i] RAPOR modu, dosya yazilmadi. Yazmak icin: --uygula")
        return

    yedek = Path(args.girdi_json).with_suffix(".json.yedek")
    shutil.copy2(args.girdi_json, yedek)
    print(f"\n[+] Yedek: {yedek}")
    for r in girdiler:
        r["yukleme_zamani"] = zaman[r["kayit_id"]].isoformat()
    with open(args.girdi_json, "w", encoding="utf-8") as f:
        json.dump(girdiler, f, ensure_ascii=False, indent=2)
    print(f"[+] {args.girdi_json} guncellendi ({len(girdiler)} kayit).")


if __name__ == "__main__":
    main()
