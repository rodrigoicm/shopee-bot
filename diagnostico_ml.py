"""
Teste rápido de linha de comando para validar a coleta real do Mercado Livre via ml_auto.py
"""

import ml_auto


def main():
    print("=" * 80)
    print("🧪 EXECUTANDO SIMULAÇÃO COMPLETA DO MERCADO LIVRE (DRY-RUN COM SESSÃO)")
    print("=" * 80)

    res = ml_auto.simulate_ml_offers_selection(count=10)

    print(f"⏱️ Tempo total de coleta + deduplicação: {res['elapsed_ms']} ms")
    print(f"🚫 Categorias Bloqueadas (Mais Vendidos): {res['blocked_categories']}")
    print(f"✅ Categorias Aceitas ({len(res['accepted_categories'])}):")
    for cat, qtd in res["accepted_categories"].items():
        print(f"   • {cat:<35}: {qtd} produtos")

    print(f"\n📦 Total bruto em Mais Vendidos: {res['raw_best_count']} produtos")
    print(f"📦 Total bruto em Ofertas (< R$100): {res['raw_offers_count']} produtos")
    print(f"🛡️ Duplicatas barradas pelo offer_comparator: {res['skipped_duplicates']}")
    print(
        f"🎯 Sorteados para validação: {len(res['items'])} "
        f"({res['chosen_best_count']} Mais Vendidos / {res['chosen_offers_count']} Ofertas)\n"
    )

    print("=" * 80)
    print("📋 LISTA DOS 10 PRODUTOS SORTEADOS (30% MAIS VENDIDOS / 70% OFERTAS)")
    print("=" * 80)
    for idx, item in enumerate(res["items"], start=1):
        pix_str = f" [Pix: R$ {item['pix_price']:.2f}]" if item.get("pix_price") else ""
        disc_str = f" ({item['discount_label']})" if item.get("discount_label") else ""
        print(
            f"{idx:02d}. [{item['category_name']}] {item['title'][:45]}... -> R$ {item['current_price']:.2f}{pix_str}{disc_str}"
        )

    if res["items"]:
        print("\n" + "=" * 80)
        print("📝 PRÉVIA DA MENSAGEM FORMATADA DO 1º PRODUTO")
        print("=" * 80)
        print(res["items"][0]["preview_message"])


if __name__ == "__main__":
    main()
