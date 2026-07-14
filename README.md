1. Depoyu Klonlayın (İndirin)
Öncelikle projeyi bilgisayarınıza indirmek için terminal veya komut satırından şu komutu çalıştırın:
Bash
git clone https://github.com/KULLANICI_ADIN/PROJE_ADI.git
cd PROJE_ADI
(Not: KULLANICI_ADIN ve PROJE_ADI kısımlarını kendi GitHub bilgilerinize göre güncellemeyi unutmayın.)
2. Gerekli Kütüphaneleri Yükleyin
Kodun bağımlılıklarını yüklemek için şu komutu çalıştırabilirsiniz:
Bash
pip install -r requirements.txt
3. Kodu Çalıştırın
Projenin ana script'ini dinamik parametrelerle çalıştırmak için aşağıdaki örnek komutu kullanabilirsiniz:
Bash
python main.py --count 10000 --anomali-orani 0.15 --output-dir data --filename faturalar
Parametre Açıklamaları
Çalıştırırken kullanabileceğiniz argümanlar ve görevleri:
--count: Üretilecek veya işlenecek toplam veri/fatura sayısı (Örn: 10000).
--anomali-orani: Veri setindeki anomali yüzdesi (Örn: 0.15 yani %15).
--output-dir: Sonuçların kaydedileceği klasör yolu (Örn: data).
--filename: Kaydedilecek dosyanın adı (Örn: faturalar).