from generators.field_generator import ACIKLAMA_HAVUZU, YEMEK_URUNLERI_CSV, yemek_urunleri_yukle
from schema import HarcamaKategorisi
for k in (HarcamaKategorisi.ULASIM_BIREYSEL, HarcamaKategorisi.ULASIM_HIZMETI):
    print(k.value, len(ACIKLAMA_HAVUZU[k]))
from pathlib import Path
from generators.field_generator import ANOMALI_URUNLERI_CSV, anomali_urunleri_yukle, ULASIM_URUNLERI_CSV, ulasim_urunleri_yukle

print("Yol:", ANOMALI_URUNLERI_CSV)
print("Dosya var mı?:", ANOMALI_URUNLERI_CSV.exists())

sonuc = anomali_urunleri_yukle()
print("Yüklenen kategoriler:", {k.value: len(v) for k, v in sonuc.items()})



print("Yol:", ULASIM_URUNLERI_CSV)
print("Dosya var mı?:", ULASIM_URUNLERI_CSV.exists())

sonuc = ulasim_urunleri_yukle()
print("Yüklenen kategoriler:", {k.value: len(v) for k, v in sonuc.items()})

