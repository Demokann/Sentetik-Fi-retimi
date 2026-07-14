import json
from collections import Counter

etiketler = json.load(open("data/faturalar_etiketler.json", encoding="utf-8"))
rapor = json.load(open("data/faturalar_rapor.json", encoding="utf-8"))

yakalanan_no = {detay["fatura_no"] for detay in rapor["hata_detaylari"].values()}
yakalanan_no |= {kayit.split(":")[1] for kayit in rapor["fatura_no_tekrarlari"]}

kacan = [e for e in etiketler if e["is_anomali"] and e["fatura_no"] not in yakalanan_no]

print(f"Toplam kaçan: {len(kacan)}")
print(Counter(tur for e in kacan for tur in e["anomali_turleri"]))