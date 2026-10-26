import sys
import requests
from database import get_config
from ml_service import MercadoLivreAPI

BASE = "https://api.mercadolibre.com"

item_id = (
    (sys.argv[1] if len(sys.argv) > 1 else input("Item ID (ex.: MLB5136589984): "))
    .strip()
    .upper()
)

ml = MercadoLivreAPI()
token = ml._get_access_token()

print(
    "Refresh token salvo no banco:", "SIM" if get_config("ml_refresh_token") else "NÃO"
)
print("Token obtido:", "SIM" if token else "NÃO")

auth = {"Authorization": f"Bearer {token}"} if token else {}
ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def test(label, url, headers=None, params=None):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        print(f"\n[{label}] -> HTTP {r.status_code}")
        print(f"  {r.text[:300]}")
    except Exception as e:
        print(f"\n[{label}] erro: {e}")


test("/users/me (quem é o dono do token)", f"{BASE}/users/me", auth)
test("/items/{id} COM token", f"{BASE}/items/{item_id}", auth)
test("/items/{id} SEM token", f"{BASE}/items/{item_id}", ua)
test("/items?ids= (multiget) COM token", f"{BASE}/items", auth, {"ids": item_id})
test(
    "/items?ids= com atributos",
    f"{BASE}/items",
    auth,
    {
        "ids": item_id,
        "attributes": "id,title,price,original_price,thumbnail,permalink,catalog_product_id",
    },
)
