import os
import re
import json
import html
import requests
from urllib.parse import unquote, urlparse, parse_qs
from dotenv import load_dotenv
from database import get_config, update_config

load_dotenv()

# Desconto Pix estimado (no exemplo testado: 998,89 -> 948,95 = 5%). Ajuste ou ignore se não quiser usar.
PIX_DISCOUNT = 0.05


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

    def _parse_url_hints(self, url: str) -> dict:
        """Extrai do link o anúncio (wid) e a promoção (deal) que o usuário estava vendo."""
        hints = {"wid": None, "deal": None}
        if not url:
            return hints
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            frag = parse_qs(parsed.fragment)  # o ML coloca wid=... depois do '#'
        except Exception:
            return hints

        wid = (qs.get("wid") or frag.get("wid") or [None])[0]
        if wid:
            hints["wid"] = wid.upper()

        for raw in qs.get("pdp_filters", []):
            m = re.search(r"deal:([A-Za-z]+\d+)", raw)
            if m:
                hints["deal"] = m.group(1).upper()
            m = re.search(r"item_id:(MLB\d+)", raw, re.I)
            if m and not hints["wid"]:
                hints["wid"] = m.group(1).upper()
        return hints

    def _fetch_catalog_items(self, clean_id: str, headers: dict) -> list:
        """Busca todos os anúncios do catálogo, paginando (o catálogo pode ter 100+)."""
        results = []
        offset = 0
        limit = 100
        while True:
            try:
                pi = requests.get(
                    f"https://api.mercadolibre.com/products/{clean_id}/items",
                    headers=headers,
                    params={"limit": limit, "offset": offset},
                    timeout=10,
                )
            except Exception as e:
                print(f"   /products/{clean_id}/items erro: {e}")
                break

            print(
                f"   /products/{clean_id}/items offset={offset} status={pi.status_code}"
            )
            if pi.status_code != 200:
                break

            data = pi.json()
            batch = data.get("results", [])
            results.extend(batch)

            total = (data.get("paging") or {}).get("total", len(results))
            offset += limit
            if not batch or offset >= total or offset >= 300:
                break
        return results

    def _pick_best_item(
        self,
        results: list,
        buy_box_item_id: str | None = None,
        hints: dict | None = None,
    ) -> dict | None:
        """Escolhe o anúncio: wid do link > promoção (deal) do link > buy box > menor preço novo."""
        hints = hints or {}
        with_price = [r for r in results if r.get("price") is not None]
        novos = [r for r in with_price if r.get("condition") == "new"]
        candidates = novos or with_price
        if not candidates:
            return None

        # 1. Anúncio exato indicado no link (?wid=MLB...)
        wid = hints.get("wid")
        if wid:
            for r in with_price:
                if r.get("item_id") == wid:
                    print(f"   🎯 Anúncio do link (wid) encontrado: {wid}")
                    return r

        # 2. Anúncios que participam da promoção indicada no link (pdp_filters=deal:...)
        deal = hints.get("deal")
        if deal:
            in_deal = [
                r
                for r in candidates
                if any(
                    str(d).upper().startswith(deal) for d in (r.get("deal_ids") or [])
                )
            ]
            print(f"   🏷️ Anúncios na promoção {deal}: {len(in_deal)}")
            if in_deal:
                candidates = in_deal
            else:
                print("   ⚠️ Nenhum anúncio com esse deal_id; usando regra padrão.")

        # 3. Buy box (se estiver entre os candidatos)
        if buy_box_item_id:
            for r in candidates:
                if r.get("item_id") == buy_box_item_id:
                    return r

        # 4. Sem buy box na API: aproxima o destaque do ML
        #    (loja oficial + Full > só Full > qualquer), e pega o menor preço do grupo
        def is_full(r):
            return (r.get("shipping") or {}).get("logistic_type") == "fulfillment"

        oficial_full = [
            r for r in candidates if r.get("official_store_id") and is_full(r)
        ]
        so_full = [r for r in candidates if is_full(r)]
        pool = oficial_full or so_full or candidates

        return min(pool, key=lambda r: r["price"])

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
            hints = self._parse_url_hints(original_url)
            print(f"   🔗 Pistas da URL: {hints}")
            try:
                results = self._fetch_catalog_items(clean_id, headers)
                all_deals = sorted(
                    {str(d) for r in results for d in (r.get("deal_ids") or [])}
                )
                print(
                    f"   📦 {len(results)} anúncios | deal_ids encontrados: {all_deals}"
                )

                best = self._pick_best_item(results, buy_box_item_id, hints)
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
                "pix_price": round(price * (1 - PIX_DISCOUNT), 2) if price else None,
                "original_price": original_price,
                "thumbnail": thumb,
                "permalink": prod_data.get("permalink") or original_url,
                "sold_quantity": 0,
            }

        if resp_prod is not None:
            print(f"   /products/{clean_id} status={resp_prod.status_code}")

        # ---------- 5. Fallback: item direto /items/{clean_id} ----------
        try:
            resp_item = requests.get(
                f"https://api.mercadolibre.com/items/{clean_id}",
                headers=headers,
                timeout=10,
            )
            print(
                f"   /items/{clean_id} status={resp_item.status_code} body={resp_item.text[:200]}"
            )
        except Exception as e:
            print(f"   /items/{clean_id} erro: {e}")
            resp_item = None

        if resp_item is not None and resp_item.status_code == 200:
            print("✅ Dados obtidos via /items (fallback direto).")
            return self._format_item_response(resp_item.json())

        print(f"⚠️ Nenhum dado encontrado para {clean_id}")
        return None
