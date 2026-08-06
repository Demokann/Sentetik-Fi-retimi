"""
OpenStreetMap (Overpass API) üzerinden Türkiye'deki gerçek işletme adlarını
is_kolu kategorilerine göre çeker, tek bir CSV'ye yazar. OSM verisi ODbL ile
lisanslı -- toplu çekim/yeniden kullanım (atıfla) açıkça izin verilen bir
kullanım, Google/Yandex Maps'in ToS'unun aksine (bkz. https://www.openstreetmap.org/copyright).

ŞEHİR KUTULARI + ŞEHİR BAŞI KOTA:
Önceki sürüm tüm Türkiye bbox'ını kaba grid'e bölüp tarıyordu. İki sorunu vardı:
(1) dikdörtgen Halep/Musul/Erbil/Rodos/Yerevan'ı da kapsadığı için çekilen adların
%22'si (market'te %45'i) Türkiye dışıydı -- Arapça/Yunanca isimler bu yüzden geliyordu;
(2) grid'in büyük kısmı deniz ve boş step olduğundan istekler boşa gidiyordu.
Bu sürüm YALNIZCA sınırlardan uzak, büyükten küçüğe sıralı ~55 şehir kutusunu tarar.

COĞRAFİ DAĞILIM ÖNEMLİ DEĞİL (bilinçli karar): hedef, adların illere yayılması
değil, iş koluna uygun Türkçe firma adı HACMİNİ hızlı toplamak. Hepsi İstanbul'dan
gelse de olur. Bu yüzden şehir başı kota varsayılan olarak KAPALI (--kutu-kota 0);
hedef hangi kutudan dolarsa oradan dolar, dolunca tarama biter. Yayılım istenirse
--kutu-kota 100-200 ile açılabilir (yavaşlatır).

YABANCI İSİM KORUMASI iki katmanlı: kutuların sınırlardan uzak seçilmesi +
latin_disi_mi() süzgeci. Overpass'ın area(TR) filtresi DENENDİ ve BIRAKILDI --
İstanbul kutusunda 93 sn'de timeout verirken filtresiz sorgu 57 sn'de dönüyor
(Türkiye sınır poligonu karmaşık, node başına kesişim testi pahalı).

Kullanım:
    python firma_adlari_osm_cek.py                       # varsayılan hedef 3000/is_kolu
    python firma_adlari_osm_cek.py --hedef 3000 --bekleme 10
    # Daha hızlı/kaba tarama için yalnızca en büyük N şehir:
    python firma_adlari_osm_cek.py --sehir-limit 25
"""

import argparse
import csv
import re
import time
from pathlib import Path

import requests

# Overpass sunucuları. Ana sunucu (overpass-api.de) IP başına 2 slot + biriken
# sorgu süresi kotası uyguluyor; uzun çekimlerde kalıcı 429'a düşüyor. Aynalar
# ayrı kotaya sahip -> ana sunucu tıkandığında --sunucu ile geçilir.
OVERPASS_SUNUCULAR = {
    "ana": "https://overpass-api.de/api/interpreter",
    "kumi": "https://overpass.kumi.systems/api/interpreter",
    "coffee": "https://overpass.private.coffee/api/interpreter",
}
OVERPASS_URL = OVERPASS_SUNUCULAR["ana"]
# overpass-api.de varsayılan requests User-Agent'ını reddediyor (406) -- açıkça
# kimliklenen bir User-Agent göndermek gerekiyor.
ISTEK_BASLIKLARI = {"User-Agent": "masrafAI-veri-cikarma/1.0 (egitim amacli sentetik veri projesi)"}
CIKTI_CSV = Path("data/firma_adlari_osm.csv")

# is_kolu -> Overpass OSM node etiket sorgu satırları (birden fazla olabilir).
# schema.py:IS_KOLU_KATEGORILERI ile aynı 11 iş kolu.
IS_KOLU_OSM_ETIKETLERI = {
    "restoran": ['node["amenity"="restaurant"]', 'node["amenity"="fast_food"]'],
    "market": ['node["shop"="supermarket"]', 'node["shop"="convenience"]'],
    "otel": ['node["tourism"="hotel"]'],
    "ofis_tedarik": ['node["shop"="stationery"]', 'node["shop"="furniture"]'],
    "teknoloji": ['node["shop"="electronics"]', 'node["shop"="computer"]'],
    "danismanlik_firmasi": ['node["office"="consulting"]', 'node["office"="it"]'],
    "lojistik_firmasi": ['node["office"="logistics"]', 'node["shop"="storage_rental"]'],
    "ulasim_saglayici": ['node["amenity"="fuel"]', 'node["amenity"="taxi"]', 'node["amenity"="car_rental"]'],
    "organizasyon": ['node["office"="event_management"]', 'node["amenity"="events_venue"]'],
    "giyim_magazasi": ['node["shop"="clothes"]'],
    "kisisel_bakim": ['node["shop"="hairdresser"]', 'node["shop"="beauty"]', 'node["shop"="cosmetics"]'],
}

# Türkiye'nin OSM alan id'si (relation 174737 + 3600000000). ŞU AN KULLANILMIYOR:
# sorguya area kısıtı eklemek doğru sonucu veriyor ama İstanbul gibi yoğun
# kutularda timeout'a yol açtı (bkz. sorgu_olustur docstring). Referans için
# duruyor -- tekrar denenecekse önce maliyeti ölç.
TURKIYE_AREA_ID = 3600174737

# Taranacak şehir kutuları: (ad, lat, lon, dlat, dlon) -> bbox = lat±dlat, lon±dlon.
# Tüm Türkiye'yi grid'lemek yerine YALNIZCA şehirler taranır; üç kazanç:
#   1) Akdeniz/Karadeniz/boş step üzerinde istek harcanmaz (grid'in çoğu boştu).
#   2) Kutular küçük -> sorgular hafif, 504/bölme neredeyse hiç olmaz.
#   3) Kutular sınırlardan uzak seçildi; latin_disi_mi() de ikinci güvence.
# Sıra ÖNEMLİ: büyük şehirler başta, şehir başı kota dolunca sıradakine geçilir.
# Sınıra çok yakın iller (Hatay, Edirne, Mardin, Kilis, Iğdır) bilerek DIŞARIDA.
SEHIR_KUTULARI = [
    ("istanbul", 41.03, 28.95, 0.28, 0.75),
    ("ankara", 39.93, 32.86, 0.25, 0.35),
    ("izmir", 38.42, 27.15, 0.25, 0.30),
    ("bursa", 40.19, 29.06, 0.20, 0.30),
    ("antalya", 36.89, 30.71, 0.20, 0.35),
    ("adana", 37.00, 35.32, 0.18, 0.30),
    ("konya", 37.87, 32.48, 0.20, 0.30),
    ("gaziantep", 37.07, 37.38, 0.15, 0.25),
    ("mersin", 36.80, 34.63, 0.15, 0.30),
    ("kayseri", 38.73, 35.49, 0.18, 0.28),
    ("eskisehir", 39.78, 30.52, 0.18, 0.28),
    ("kocaeli", 40.77, 29.92, 0.18, 0.35),
    ("samsun", 41.29, 36.33, 0.15, 0.30),
    ("denizli", 37.78, 29.09, 0.18, 0.28),
    ("sanliurfa", 37.16, 38.80, 0.15, 0.25),
    ("diyarbakir", 37.91, 40.24, 0.18, 0.28),
    ("malatya", 38.35, 38.32, 0.18, 0.28),
    ("kahramanmaras", 37.58, 36.93, 0.18, 0.28),
    ("erzurum", 39.90, 41.27, 0.18, 0.28),
    ("manisa", 38.61, 27.43, 0.18, 0.28),
    ("balikesir", 39.65, 27.89, 0.20, 0.30),
    ("tekirdag", 40.98, 27.51, 0.18, 0.30),
    ("trabzon", 41.00, 39.72, 0.12, 0.28),
    ("aydin", 37.85, 27.84, 0.15, 0.28),
    ("sakarya", 40.78, 30.40, 0.18, 0.28),
    ("mugla", 37.21, 28.36, 0.15, 0.25),
    ("sivas", 39.75, 37.02, 0.18, 0.28),
    ("elazig", 38.68, 39.22, 0.15, 0.25),
    ("van", 38.49, 43.38, 0.18, 0.28),
    ("afyonkarahisar", 38.76, 30.54, 0.18, 0.28),
    ("batman", 37.88, 41.13, 0.15, 0.25),
    ("ordu", 40.98, 37.88, 0.12, 0.28),
    ("tokat", 40.31, 36.55, 0.18, 0.28),
    ("corum", 40.55, 34.95, 0.18, 0.28),
    ("zonguldak", 41.45, 31.79, 0.12, 0.28),
    ("kutahya", 39.42, 29.98, 0.18, 0.28),
    ("isparta", 37.76, 30.55, 0.18, 0.28),
    ("osmaniye", 37.07, 36.25, 0.15, 0.25),
    ("canakkale", 40.15, 26.41, 0.15, 0.25),
    ("giresun", 40.91, 38.39, 0.12, 0.25),
    ("bolu", 40.74, 31.61, 0.18, 0.28),
    ("usak", 38.68, 29.41, 0.18, 0.28),
    ("nevsehir", 38.62, 34.71, 0.18, 0.28),
    ("aksaray", 38.37, 34.03, 0.18, 0.28),
    ("karaman", 37.18, 33.22, 0.18, 0.28),
    ("nigde", 37.97, 34.68, 0.18, 0.28),
    ("kirikkale", 39.85, 33.51, 0.15, 0.25),
    ("yozgat", 39.82, 34.81, 0.18, 0.28),
    ("amasya", 40.65, 35.83, 0.15, 0.25),
    ("kastamonu", 41.38, 33.78, 0.15, 0.28),
    ("duzce", 40.84, 31.16, 0.15, 0.25),
    ("burdur", 37.72, 30.29, 0.15, 0.25),
    ("erzincan", 39.75, 39.49, 0.15, 0.25),
    ("adiyaman", 37.76, 38.28, 0.15, 0.25),
]

# --- Adaptif bölme parametreleri (CLI'dan ezilebilir) ---
VARSAYILAN_HEDEF = 3000        # is_kolu başına toplanmaya çalışılacak benzersiz isim
VARSAYILAN_BEKLEME = 10        # istekler arası saniye (ücretsiz sunucuya nezaket)
MAX_DERINLIK = 4               # bir tile en fazla kaç kez 4'e bölünsün (504 halinde)
PER_TILE_TIMEOUT = 100         # Overpass sorgu timeout'u (sn)
HTTP_TIMEOUT = 130             # requests istek timeout'u (sn), Overpass timeout'undan büyük
RATE_LIMIT_BEKLEME = 30        # 429 (Too Many Requests) sonrası ek bekleme
MAX_THROTTLE_DENEME = 4        # 429'da aynı tile kaç kez (üstel geri çekilmeyle) yeniden denensin

# _istek dönüş sinyalleri: liste -> başarı; aşağıdakiler -> başarısız ama SEBEBİ farklı.
BOL = "bol"      # gerçek timeout (sorgu ağır) -> tile'ı 4'e bölmek mantıklı
BEKLE = "bekle"  # rate limit -> bölmek yükü ARTIRIR; aynı tile'ı bekleyip tekrar dene


def sorgu_olustur(etiketler: list[str], bbox: tuple, limit: int) -> str:
    """Düz bbox sorgusu -- area(TR) filtresi BİLEREK kullanılmıyor.
    Ölçüldü (istanbul kutusu, market tagleri): area(TR) ile 93 sn -> timeout,
    filtresiz 57 sn -> 6307 eleman. Türkiye sınır poligonu karmaşık olduğundan
    her node için kesişim testi sorguyu timeout'a itiyor. Yabancı isim koruması
    artık iki katmanda: (1) SEHIR_KUTULARI sınırlardan uzak seçildi,
    (2) latin_disi_mi() ile alfabe bazlı son süzgeç."""
    g, b, k, d = bbox
    bbox_str = f"{g},{b},{k},{d}"
    govde = "\n  ".join(f"{e}({bbox_str});" for e in etiketler)
    return f"""
[out:json][timeout:{PER_TILE_TIMEOUT}];
(
  {govde}
);
out {limit};
""".strip()


# Arapça / Ermenice / Gürcüce / Yunanca / Kiril -- bir kutu sınırı aşarsa veya
# Türkiye içindeki yabancı-alfabe tabelalar gelirse son süzgeç.
LATIN_DISI = re.compile(r"[؀-ۿݐ-ݿ԰-֏Ⴀ-ჿ"
                        r"Ͱ-ϿЀ-ӿ֐-׿]")


def latin_disi_mi(ad: str) -> bool:
    return bool(LATIN_DISI.search(ad))


def bbox_dorte_bol(bbox: tuple) -> list[tuple]:
    """Bir bbox'ı (güney,batı,kuzey,doğu) 2x2 dört alt-bbox'a böler."""
    g, b, k, d = bbox
    orta_lat = (g + k) / 2
    orta_lon = (b + d) / 2
    return [
        (g, b, orta_lat, orta_lon),
        (g, orta_lon, orta_lat, d),
        (orta_lat, b, k, orta_lon),
        (orta_lat, orta_lon, k, d),
    ]


def sehir_gridi() -> list[tuple]:
    """SEHIR_KUTULARI'nı (güney,batı,kuzey,doğu) bbox listesine çevirir."""
    return [(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
            for _, lat, lon, dlat, dlon in SEHIR_KUTULARI]


class Cekici:
    def __init__(self, hedef: int, bekleme: int, kutu_kota: int,
                 sunucu: str = OVERPASS_URL):
        self.hedef = hedef
        self.bekleme = bekleme
        self.sunucu = sunucu
        # Şehir başına azami isim; 0 = SINIRSIZ (varsayılan). Sınırsızken hedef
        # nereden dolarsa oradan dolar -- çoğu kategoride İstanbul+Ankara yeter.
        # Amaç coğrafi denge DEĞİL, iş koluna uygun Türkçe ad hacmini hızlı
        # toplamak. Coğrafi yayılım istenirse --kutu-kota ile açılır.
        self.kutu_kota = kutu_kota
        self.istek_sayaci = 0
        self.kesildi = False   # Ctrl+C ile yarıda kesildi mi (kısmi kayıt işareti)

    def _istek(self, etiketler: list[str], bbox: tuple) -> list[dict] | str:
        """Tek bir bbox'ı sorgular. Timeout/504 -> BOL, 429 -> BEKLE; başka hata -> raise."""
        sorgu = sorgu_olustur(etiketler, bbox, self.hedef)
        self.istek_sayaci += 1
        try:
            yanit = requests.post(self.sunucu, data={"data": sorgu},
                                  headers=ISTEK_BASLIKLARI, timeout=HTTP_TIMEOUT)
            if yanit.status_code == 429:
                return BEKLE  # rate limit -> bölme, bekle ve aynı tile'ı tekrar dene
            if yanit.status_code in (504, 502, 503):
                # DİKKAT: overpass-api.de bu kodu İKİ ayrı sebeple döndürüyor ve
                # ayrımı yalnızca gövde metni veriyor:
                #   "Query timed out"    -> sorgu gerçekten ağır, BÖLMEK doğru
                #   "too busy"/Dispatcher-> sunucu meşgul, bölmek İŞE YARAMAZ
                # Ayrım yapılmazsa meşgul sunucuda 3 km'lik tile'lar bile timeout
                # verip derinlik tavanına kadar bölünüyor (tile başına ~341 istek).
                return BOL if "Query timed out" in yanit.text else BEKLE
            yanit.raise_for_status()
            veri = yanit.json()
            # Overpass ağır sorguda HTTP 200 + remark ile de dönebiliyor.
            if "timed out" in str(veri.get("remark", "")):
                return BOL
            return veri.get("elements", [])
        except requests.exceptions.Timeout:
            return BOL        # istemci tarafı timeout -> tile'ı böl
        finally:
            time.sleep(self.bekleme)

    def _istek_dayanikli(self, etiketler: list[str], bbox: tuple) -> list[dict] | None:
        """429'da AYNI tile'ı üstel geri çekilmeyle tekrar dener (bölmez -- bölmek
        tam da sunucunun istemediği şeyi, istek sayısını 4'e katlamayı yapar).
        None -> gerçek timeout, çağıran tile'ı bölmeli."""
        bekleme = RATE_LIMIT_BEKLEME
        for deneme in range(1, MAX_THROTTLE_DENEME + 1):
            sonuc = self._istek(etiketler, bbox)
            if isinstance(sonuc, list):
                return sonuc
            if sonuc == BOL:
                return None
            print(f"      [429] rate limit, {bekleme} sn bekleniyor "
                  f"(deneme {deneme}/{MAX_THROTTLE_DENEME})...")
            time.sleep(bekleme)
            bekleme *= 2
        return None   # ısrarlı throttle -> son çare olarak bölmeyi dene

    def _tile_isle(self, etiketler: list[str], bbox: tuple, derinlik: int,
                   toplanan: dict[str, int], tavan: int) -> None:
        """Bir tile'ı işler; timeout olursa MAX_DERINLIK'e kadar 4'e böler.
        `tavan` = bu şehir kutusu bitene kadar çıkılabilecek azami toplam boyut."""
        if len(toplanan) >= tavan:
            return   # kutu kotası doldu / hedefe ulaşıldı

        elemanlar = self._istek_dayanikli(etiketler, bbox)

        if elemanlar is None:
            if derinlik >= MAX_DERINLIK:
                print(f"      [!] {derinlik}. derinlikte hâlâ timeout, bu tile atlandı: {bbox}")
                return
            for alt in bbox_dorte_bol(bbox):
                if len(toplanan) >= tavan:
                    return
                self._tile_isle(etiketler, alt, derinlik + 1, toplanan, tavan)
            return

        for eleman in elemanlar:
            if len(toplanan) >= tavan:
                break
            isim = eleman.get("tags", {}).get("name")
            if isim and isim not in toplanan and not latin_disi_mi(isim):
                toplanan[isim] = int(eleman.get("id") or 0)   # id yoksa 0 (yalnızca CSV kolonu)

    def kategori_cek(self, is_kolu: str, etiketler: list[str], grid: list[tuple],
                     adlar: list[str]) -> list[dict]:
        toplanan: dict[str, int] = {}   # isim -> osm_id (benzersizlik)
        try:
            for tile, ad in zip(grid, adlar):
                if len(toplanan) >= self.hedef:
                    break
                # kutu_kota=0 -> sınırsız: kutudan çıkan her şeyi al, hedefe kadar devam.
                tavan = (min(self.hedef, len(toplanan) + self.kutu_kota)
                         if self.kutu_kota else self.hedef)
                onceki = len(toplanan)
                self._tile_isle(etiketler, tile, 0, toplanan, tavan)
                if len(toplanan) > onceki:
                    print(f"      {ad:16} +{len(toplanan) - onceki:4}  (toplam {len(toplanan)})")
        except KeyboardInterrupt:
            # Ctrl+C = "bu kadarı yeter" -> iptal DEĞİL, o ana kadarki isimler
            # kaydedilir ve script durur. Hedefe ulaşmayı beklemeye gerek yok.
            self.kesildi = True
            print(f"\n    [Ctrl+C] {is_kolu} yarıda kesildi, "
                  f"{len(toplanan)} isim KAYDEDİLİYOR.")
        return [{"is_kolu": is_kolu, "isim": isim, "osm_id": oid}
                for isim, oid in toplanan.items()]


def mevcut_kayitlari_yukle() -> dict[str, list[dict]]:
    """Daha önce başarıyla çekilmiş kategorileri tekrar çekmemek için (resumable)."""
    if not CIKTI_CSV.exists():
        return {}
    with open(CIKTI_CSV, encoding="utf-8") as f:
        kayitlar = list(csv.DictReader(f))
    gruplar: dict[str, list[dict]] = {}
    for k in kayitlar:
        gruplar.setdefault(k["is_kolu"], []).append(k)
    return gruplar


def csv_yaz(gruplar: dict[str, list[dict]]) -> None:
    tum_kayitlar = [k for kayitlar in gruplar.values() for k in kayitlar]
    CIKTI_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(CIKTI_CSV, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=["is_kolu", "isim", "osm_id"])
        yazici.writeheader()
        yazici.writerows(tum_kayitlar)


def main():
    parser = argparse.ArgumentParser(description="OSM tüm-Türkiye firma adı çekici (adaptif bölme)")
    parser.add_argument("--hedef", type=int, default=VARSAYILAN_HEDEF, help="is_kolu başına hedef benzersiz isim")
    parser.add_argument("--bekleme", type=int, default=VARSAYILAN_BEKLEME, help="istekler arası saniye")
    parser.add_argument("--sehir-limit", type=int, default=len(SEHIR_KUTULARI),
                        help="taranacak şehir sayısı (liste büyükten küçüğe sıralı)")
    parser.add_argument("--kutu-kota", type=int, default=0,
                        help="şehir başına azami isim (0 = sınırsız; coğrafi yayılım istersen 100-200 ver)")
    parser.add_argument("--grid-satir", type=int, help=argparse.SUPPRESS)   # eski mod, kullanılmıyor
    parser.add_argument("--grid-sutun", type=int, help=argparse.SUPPRESS)   # eski mod, kullanılmıyor
    parser.add_argument("--sunucu", default="ana", choices=list(OVERPASS_SUNUCULAR),
                        help="Overpass sunucusu; ana tıkanınca 'kumi' veya 'coffee' aynasına geç")
    parser.add_argument("--yeniden", action="store_true",
                        help="mevcut CSV'yi yok say, hepsini baştan çek")
    args = parser.parse_args()

    if args.grid_satir or args.grid_sutun:
        print("[!] --grid-satir/--grid-sutun artık kullanılmıyor (şehir kutusu moduna geçildi), "
              "yok sayılıyor.")

    grid = sehir_gridi()[:args.sehir_limit]
    adlar = [s[0] for s in SEHIR_KUTULARI][:args.sehir_limit]
    kutu_kota = args.kutu_kota   # 0 = sınırsız (varsayılan)
    print(f"[+] {len(grid)} şehir kutusu taranacak, hedef={args.hedef}/is_kolu, "
          f"sehir-basi-kota={kutu_kota or 'sinirsiz'}, bekleme={args.bekleme}sn")

    gruplar = {} if args.yeniden else mevcut_kayitlari_yukle()
    if gruplar:
        print(f"[+] Zaten çekilmiş kategoriler (atlanacak): {list(gruplar.keys())}")

    sunucu = OVERPASS_SUNUCULAR[args.sunucu]
    print(f"[+] Sunucu: {args.sunucu} ({sunucu})")
    cekici = Cekici(args.hedef, args.bekleme, kutu_kota, sunucu)

    for is_kolu, etiketler in IS_KOLU_OSM_ETIKETLERI.items():
        if is_kolu in gruplar:
            continue
        print(f"[+] {is_kolu} çekiliyor...")
        try:
            kayitlar = cekici.kategori_cek(is_kolu, etiketler, grid, adlar)
        except Exception as e:
            print(f"    [X] HATA: {e} -- bu kategori atlandı, mevcutlar korunuyor.")
            continue
        print(f"    {len(kayitlar)} benzersiz isim ({cekici.istek_sayaci} toplam istek).")
        if kayitlar:
            gruplar[is_kolu] = kayitlar
            csv_yaz(gruplar)   # her kategori sonrası kaydet (kesintiye dayanıklı)
        if cekici.kesildi:
            if kayitlar:
                print(f"    [i] '{is_kolu}' KISMİ olarak kaydedildi. Tekrar çekmek istersen "
                      f"CSV'den bu is_kolu satırlarını sil, yoksa 'tamamlandı' sayılır.")
            else:
                print(f"    [i] '{is_kolu}' için hiç isim toplanmamıştı, CSV'ye yazılmadı; "
                      f"tekrar çalıştırınca baştan denenecek.")
            break

    print(f"\n[+] Toplam {sum(len(v) for v in gruplar.values())} kayıt -> {CIKTI_CSV}")


if __name__ == "__main__":
    main()
