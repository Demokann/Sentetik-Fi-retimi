from generators.field_generator import ACIKLAMA_HAVUZU
from schema import HarcamaKategorisi
for k in (HarcamaKategorisi.ALKOL, HarcamaKategorisi.EGLENCE, HarcamaKategorisi.TUTUN_URUNLERI, HarcamaKategorisi.KUMAR):
    print(k.value, len(ACIKLAMA_HAVUZU[k]))