import os
import re
import json
import html
import requests
from urllib.parse import unquote
from dotenv import load_dotenv
from database import get_config, update_config

load_dotenv()


class MercadoLivreAPI:
    def __init__(self):
        self.client_id = os.environ.get("ML_CLIENT_ID")
        self.client_secret = os.environ.get("ML_CLIENT_SECRET")
        self.redirect_uri = "https://www.google.com/"
        self._access_token = None

    def _get_access_token(self) -> str | None:
        """Obtém e renova automaticamente o Token de Usuário via Refresh Token."""
        if self._access_token:
            return self._access_token

        saved_refresh = get_config("ml_refresh_token")
        if saved_refresh:
            url = "https://api.mercadolibre.com/oauth/token"
            payload = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": saved_refresh,
            }
            try:
                resp = requests.post(url, data=payload, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data.get("access_token")
                    new_refresh = data.get("refresh_token")
                    if self._access_token:
                        update_config("ml_access_token", self._access_token)
                    if new_refresh:
                        update_config("ml_refresh_token", new_refresh)
                    return self._access_token
            except Exception:
                pass

        url = "https://api.mercadolibre.com/oauth/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            resp = requests.post(url, data=payload, timeout=10)
            if resp.status_code == 200:
                self._access_token = resp.json().get("access_token")
                return self._access_token
        except Exception:
            pass
        return None

    def _format_item_response(self, data: dict) -> dict:
        pictures = data.get("pictures", [])
        high_res_img = (
            pictures[0].get("secure_url") or pictures[0].get("url")
            if pictures
            else None
        )
        thumb = high_res_img or data.get("thumbnail", "").replace("-I.jpg", "-O.jpg")

        return {
            "itemId": data.get("id"),
            "title": data.get("title") or data.get("name"),
            "price": data.get("price"),
            "original_price": data.get("original_price"),
            "thumbnail": thumb,
            "permalink": data.get("permalink"),
            "sold_quantity": data.get("sold_quantity", 0),
        }

    def _pick_best_item(
        self, results: list, buy_box_item_id: str | None = None
    ) -> dict | None:
        """Escolhe o anúncio: buy box winner se estiver na lista, senão o menor preço novo."""
        with_price = [r for r in results if r.get("price") is not None]
        novos = [r for r in with_price if r.get("condition") == "new"]
        candidates = novos or with_price
        if not candidates:
            return None

        if buy_box_item_id:
            for r in candidates:
                if r.get("item_id") == buy_box_item_id:
                    return r

        return min(candidates, key=lambda r: r["price"])

    def get_item_from_url_or_id(
        self, item_id: str, original_url: str = ""
    ) -> dict | None:
        token = self._get_access_token()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        clean_id = item_id.replace("-", "").upper()

        # ---------- 1. Catálogo: /products/{id} ----------
        try:
            resp_prod = requests.get(
                f"https://api.mercadolibre.com/products/{clean_id}",
                headers=headers,
                timeout=10,
            )
        except Exception:
            resp_prod = None

        if resp_prod is not None and resp_prod.status_code == 200:
            prod_data = resp_prod.json()
            buy_box = prod_data.get("buy_box_winner") or {}
            buy_box_item_id = buy_box.get("item_id")

            print(f"🔎 buy_box_winner.item_id = {buy_box_item_id!r}")

            price = None
            original_price = None
            chosen_item_id = buy_box_item_id

            # ---------- 2. Fonte principal: /products/{id}/items ----------
            try:
                pi = requests.get(
                    f"https://api.mercadolibre.com/products/{clean_id}/items",
                    headers=headers,
                    params={"limit": 50},
                    timeout=10,
                )
                print(f"   /products/{clean_id}/items status={pi.status_code}")
                if pi.status_code == 200:
                    best = self._pick_best_item(
                        pi.json().get("results", []), buy_box_item_id
                    )
                    if best:
                        chosen_item_id = best.get("item_id")
                        price = best.get("price")
                        original_price = best.get("original_price")
                        print(
                            f"   ✔ Anúncio escolhido: {chosen_item_id} | price={price} | original={original_price}"
                        )
            except Exception as e:
                print(f"   /products/{clean_id}/items erro: {e}")

            # ---------- 3. Refinamento: /sale_price (preço promocional) ----------
            if chosen_item_id:
                try:
                    sp = requests.get(
                        f"https://api.mercadolibre.com/items/{chosen_item_id}/sale_price",
                        headers=headers,
                        params={"context": "channel_marketplace"},
                        timeout=10,
                    )
                    print(
                        f"   /sale_price status={sp.status_code} body={sp.text[:200]}"
                    )
                    if sp.status_code == 200:
                        sp_data = sp.json()
                        if sp_data.get("amount") is not None:
                            price = sp_data["amount"]
                            original_price = (
                                sp_data.get("regular_amount") or original_price
                            )
                except Exception as e:
                    print(f"   /sale_price erro: {e}")

            # ---------- 4. Último fallback: preço dentro do próprio buy_box_winner ----------
            if price is None:
                price = buy_box.get("price")
                original_price = buy_box.get("original_price")

            pictures = prod_data.get("pictures", [])
            thumb = (pictures[0].get("url") if pictures else None) or prod_data.get(
                "thumbnail"
            )

            print(f"✅ Preço final: {price}")
            return {
                "itemId": chosen_item_id or prod_data.get("id"),
                "title": prod_data.get("name") or buy_box.get("title"),
                "price": price,
                "original_price": original_price,
                "thumbnail": thumb,
                "permalink": prod_data.get("permalink") or original_url,
                "sold_quantity": 0,
            }

        # ---------- 5. Fallback: item direto /items/{clean_id} ----------
        try:
            resp_item = requests.get(
                f"https://api.mercadolibre.com/items/{clean_id}",
                headers=headers,
                timeout=10,
            )
        except Exception:
            resp_item = None

        if resp_item is not None and resp_item.status_code == 200:
            print("✅ Dados obtidos via /items (fallback direto).")
            return self._format_item_response(resp_item.json())

        print(f"⚠️ Nenhum dado encontrado para {clean_id}")
        return None
