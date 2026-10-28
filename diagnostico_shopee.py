"""
Script de Diagnóstico e Simulação (Dry-Run) - NÃO altera o banco de dados.
Valida:
1. O que está salvo hoje na coluna 'category' do SQLite;
2. Tempo de resposta e retorno real das Categorias (70%) vs Geral (30%) na Shopee;
3. Tempo gasto pelo Comparador de Ofertas e pelo Relatório de Vendas.
"""

import os
import time
import sqlite3
from dotenv import load_dotenv
from database import DB_NAME, get_config
from shopee_service import ShopeeAffiliateAPI
import offer_comparator

load_dotenv()


def run_diagnostics():
    print("=" * 75)
    print("🔍 1. VERIFICANDO CATEGORIAS SALVAS ATUALMENTE NO BANCO (SQLITE)")
    print("=" * 75)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COALESCE(category, 'NULL'), COUNT(*) FROM queue GROUP BY category"
        )
        rows = cursor.fetchall()
        for cat, qtd in rows:
            print(f"   • Categoria: {cat:<28} | Qtd na fila/histórico: {qtd}")
    except Exception as e:
        print(f"   ❌ Erro ao ler coluna category: {e}")
    finally:
        conn.close()

    print("\n" + "=" * 75)
    print("⏱️ 2. MEDINDO TEMPO DO COMPARADOR DE OFERTAS (LOCAL)")
    print("=" * 75)
    t0 = time.perf_counter()
    report = offer_comparator.analyze_repeated_offers(days=14, threshold=60.0)
    dt_comp = (time.perf_counter() - t0) * 1000
    print(
        f"   • Itens analisados: {report['total_scanned']} | Grupos repetidos: {report['total_clusters']}"
    )
    print(f"   • Tempo gasto no Comparador: {dt_comp:.1f} ms")

    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    if not app_id or not app_secret:
        print("\n❌ SHOPEE_APP_ID / SHOPEE_APP_SECRET não encontrados no .env")
        return

    api = ShopeeAffiliateAPI(app_id, app_secret)

    print("\n" + "=" * 75)
    print("⏱️ 3. MEDINDO TEMPO DO RELATÓRIO DE VENDAS (API SHOPEE)")
    print("=" * 75)
    t0 = time.perf_counter()
    sales = api.get_sales_report(days=15)
    dt_sales = (time.perf_counter() - t0) * 1000
    print(
        f"   • Pedidos retornados (15d): {sales['total_orders']} | Comissão: R$ {sales['total_commission']:.2f}"
    )
    print(
        f"   • Tempo gasto na API de Vendas: {dt_sales:.1f} ms  <-- (Roda em todo F5 no portal atual!)"
    )

    print("\n" + "=" * 75)
    print("🧪 4. SIMULAÇÃO (DRY-RUN) DA BUSCA 30% GERAL / 70% CATEGORIAS (SEM SALVAR)")
    print("=" * 75)
    cats_csv = (
        get_config("shopee_target_categories")
        or "Roupas Femininas, Moda Infantil, Sapatos Femininos"
    )
    print(f"   • Categorias configuradas: {cats_csv}")

    t0 = time.perf_counter()
    cat_pool = api.get_category_offers_pool(cats_csv)
    dt_cat = (time.perf_counter() - t0) * 1000

    # Verifica se os itens vieram com a chave da categoria
    sample_cats = {}
    for item in cat_pool:
        lbl = (
            item.get("_source_bucket")
            or item.get("_category_label")
            or "SEM_TAG_NO_CODIGO"
        )
        sample_cats[lbl] = sample_cats.get(lbl, 0) + 1

    print(
        f"   • Produtos coletados nas Categorias: {len(cat_pool)} (em {dt_cat:.0f} ms)"
    )
    print("   • Distribuição por Categoria marcada nos itens coletados:")
    for k, v in sample_cats.items():
        print(f"     -> {k}: {v} anúncios encontrados")

    print("\n   📋 Exemplo dos 5 primeiros produtos das Categorias (Simulação):")
    for idx, item in enumerate(cat_pool[:5], start=1):
        lbl = item.get("_source_bucket") or item.get("_category_label") or "SEM_TAG"
        nome = (item.get("productName") or "")[:55]
        preco = item.get("priceMin")
        print(f"     {idx}. [{lbl}] {nome}... (R$ {preco})")


if __name__ == "__main__":
    run_diagnostics()
