import asyncio
import json
import sys

import ml_offer


async def run(url: str):
    offer = await ml_offer.get_ml_offer(url.strip())

    if not offer:
        print(f"\n❌ Não foi possível montar a oferta. Motivo: {ml_offer.last_error}")
        return

    print("\n" + "=" * 60)
    print(json.dumps(offer, ensure_ascii=False, indent=2))
    print("=" * 60)
    print("\nPRÉVIA DA MENSAGEM:\n")
    print(ml_offer.preview_message(offer))
    print(f"\nImagem: {offer.get('image_url')}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(run(sys.argv[1]))
    else:
        print("Cole um link do Mercado Livre (ENTER vazio para sair).")
        while True:
            link = input("\n🔗 Link: ").strip()
            if not link:
                break
            asyncio.run(run(link))
