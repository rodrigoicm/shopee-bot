"""
Teste isolado do encurtador SiteStripe.
Uso:
    python test_encurtador.py "https://www.amazon.com.br/dp/B09MLBPL1K"
    python test_encurtador.py "https://www.amazon.com.br/dp/B09MLBPL1K?tag=promocuiaba-20"
    python test_encurtador.py "https://amzn.to/xxxxx"
    python test_encurtador.py "B09MLBPL1K"

Sem argumento → entra em modo interativo (pergunta o link).
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from amazon_service import (
    get_partner_tag,
    build_tagged_long_url,
    generate_sitestripe_short_link,
    extract_asin,
    resolve_amazon_url,
    AMAZON_URL_PATTERN,
)


def _get_input_link() -> str:
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()
    print("🔗 Cole o link da Amazon (ou o ASIN puro, ex: B09MLBPL1K):")
    print("   > ", end="", flush=True)
    return input().strip()


def main():
    raw = _get_input_link()

    if not raw:
        print("❌ Nenhum link informado. Abortando.")
        sys.exit(1)

    # Se for só ASIN (10 caracteres alfanuméricos, começa com B)
    import re

    if re.fullmatch(r"[A-Z0-9]{10}", raw.upper()):
        raw = f"https://www.amazon.com.br/dp/{raw.upper()}"
        print(f"💡 ASIN puro detectado → montando URL: {raw}\n")

    # Valida se parece link da Amazon
    if not re.search(AMAZON_URL_PATTERN, raw):
        print(f"⚠️  O link não parece da Amazon, mas vamos tentar mesmo assim.")
        print(f"   {raw}\n")

    # ─── Header com env ───
    cookie = os.environ.get("AMAZON_COOKIE", "")
    store_id = os.environ.get("AMAZON_STORE_ID", "")
    mkt_id = os.environ.get("AMAZON_MARKETPLACE_ID", "")

    print("═" * 72)
    print(
        f"AMAZON_COOKIE        : {'✓ ' + str(len(cookie)) + ' chars' if cookie else '✗ FALTANDO'}"
    )
    print(f"AMAZON_STORE_ID      : {store_id if store_id else '✗ FALTANDO'}")
    print(f"AMAZON_PARTNER_TAG   : {get_partner_tag()}")
    print(f"AMAZON_MARKETPLACE_ID: {mkt_id if mkt_id else '✗ FALTANDO'}")
    print("═" * 72)

    # ─── 1. Link original ───
    print(f"\n1) Link informado:")
    print(f"   {raw}\n")

    # ─── 2. Resolve (se for link curto) ───
    resolved = resolve_amazon_url(raw)
    if resolved != raw:
        print(f"2) Link expandido:")
        print(f"   {resolved}\n")
    else:
        print(f"2) Sem expansão necessária\n")

    # ─── 3. Extrai ASIN ───
    asin = extract_asin(resolved) or extract_asin(raw)
    if asin:
        print(f"3) ASIN extraído:")
        print(f"   {asin}\n")
    else:
        print(f"3) ⚠️  ASIN não encontrado — não será possível montar URL canônica\n")

    # ─── 4. Monta URL longa com tag ───
    long_url, asin_from_build = build_tagged_long_url(resolved)
    print(f"4) URL longa com tag:")
    print(f"   {long_url}\n")

    # ─── 5. Chama o encurtador ───
    print(f"5) Chamando SiteStripe...")
    short = generate_sitestripe_short_link(long_url)
    print(f"   Resposta: {short}\n")

    # ─── 6. Veredito ───
    print("═" * 72)
    if short.startswith("https://link.amazon") or short.startswith("https://amzn.to"):
        print("✅ SUCESSO — link curto gerado pelo SiteStripe!")
        print(f"   → {short}")
        print("\n💡 Abra no navegador para confirmar que redireciona certo.")
    elif "?tag=" in short:
        print("❌ FALLBACK — SiteStripe não respondeu, código caiu para link longo.")
        print(f"   → {short}")
        print("\n🔍 Possíveis causas:")
        if not cookie:
            print("   • AMAZON_COOKIE está vazio → rode curl_amazon_to_env.py")
        if not store_id:
            print("   • AMAZON_STORE_ID está vazio → adicione ao .env")
        if cookie and store_id:
            print(
                "   • Cookie expirado → refaça login no navegador e capture novo cURL"
            )
            print("   • CloudFront bloqueou (fingerprint TLS) → veja log acima")
            print(
                "   • Checagem 'amzn.to' ainda presente no amazon_service.py (deveria ser .startswith('http'))"
            )
    else:
        print(f"⚠️  Resultado inesperado: {short}")
    print("═" * 72)


if __name__ == "__main__":
    main()
