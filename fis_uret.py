import argparse
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright


def kimlik_etiketi(kimlik_no: str) -> str:
    """10 haneliyse VKN, 11 haneliyse TCKN etiketi döndürür."""
    return "TCKN" if len(kimlik_no) == 11 else "VKN"


def main():
    parser = argparse.ArgumentParser(description="JSON faturalardan fiş (receipt) görseli üretir")
    parser.add_argument("--input-json", required=True, help="faturalar.json dosya yolu")
    parser.add_argument("--output-dir", default="data/fisler", help="Görsellerin kaydedileceği klasör")
    parser.add_argument("--template", default="make_receipt.html", help="Jinja2 HTML şablon dosyasının yolu")
    parser.add_argument("--limit", type=int, default=None, help="Test için ilk N faturayı işle (varsayılan: hepsi)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)

    if args.limit:
        faturalar = faturalar[: args.limit]

    sablon_yolu = Path(args.template)
    env = Environment(loader=FileSystemLoader(str(sablon_yolu.parent or Path("."))))
    template = env.get_template(sablon_yolu.name)

    print(f"{len(faturalar)} fatura için fiş üretiliyor...")

    with sync_playwright() as p:
        tarayici = p.chromium.launch()
        sayfa = tarayici.new_page()

        for i, fatura in enumerate(faturalar, start=1):
            # fatura_to_dict() ciktisi (JSON'daki hali) sablonun ihtiyac
            # duydugu TUM alanlari zaten iceriyor -- hicbir alani elemeden,
            # oldugu gibi kopyaliyoruz. Tek eklenen alan: VKN/TCKN etiketi.
            baglam = dict(fatura)
            baglam["satici_kimlik_etiketi"] = kimlik_etiketi(fatura["satici_vkn"])

            html_icerik = template.render(**baglam)
            sayfa.set_content(html_icerik)

            eleman = sayfa.locator(".receipt-container")
            cikti_yolu = output_dir / f"{i:06d}_{fatura['fatura_no']}.png"
            eleman.screenshot(path=str(cikti_yolu))

            if i % 500 == 0:
                print(f"  {i}/{len(faturalar)} tamamlandı")

        tarayici.close()

    print(f"Tamamlandi: {len(faturalar)} fis -> {output_dir}/")


if __name__ == "__main__":
    main()
