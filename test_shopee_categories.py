import os
import json
import requests
from dotenv import load_dotenv
from shopee_service import ShopeeAffiliateAPI

load_dotenv()

app_id = os.environ.get("SHOPEE_APP_ID")
app_secret = os.environ.get("SHOPEE_APP_SECRET")

print("=== 1. TESTANDO GRAPHQL DE AFILIADOS (Introspecção) ===")
api = ShopeeAffiliateAPI(app_id, app_secret)
query_introspection = """
{
  __type(name: "Query") {
    fields {
      name
      args {
        name
        type {
          name
          kind
          ofType {
            name
          }
        }
      }
    }
  }
}
"""
res_gql = api._graphql_request(query_introspection)
print(json.dumps(res_gql, indent=2))

print("\n=== 2. TESTANDO ÁRVORE PÚBLICA DE CATEGORIAS DA SHOPEE ===")
try:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    r = requests.get(
        "https://shopee.com.br/api/v4/pages/get_category_tree",
        headers=headers,
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        cats = data.get("data", {}).get("category_list", [])
        print(f"Sucesso! Encontradas {len(cats)} categorias principais:")
        for c in cats[:12]:
            name = c.get("display_name")
            catid = c.get("catid")
            children = c.get("children") or []
            sub_names = [ch.get("display_name") for ch in children[:5]]
            print(f" - {name} (catid={catid}) | Subcategorias: {sub_names}")
    else:
        print(f"Erro HTTP {r.status_code}: {r.text[:200]}")
except Exception as e:
    print(f"Exceção na requisição da árvore: {e}")
