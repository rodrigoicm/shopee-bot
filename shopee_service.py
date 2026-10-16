import hashlib
import time
import requests
import re
import json
import random
from datetime import datetime, timezone, timedelta
from database import add_to_queue, is_offer_recent

cuiaba_tz = timezone(timedelta(hours=-4))


class ShopeeAffiliateAPI:
    GRAPHQL_URL = "https://open-api.affiliate.shopee.com.br/graphql"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    def _sign(self, payload: str, timestamp: str) -> str:
        factor = f"{self.app_id}{timestamp}{payload}{self.app_secret}"
        return hashlib.sha256(factor.encode("utf-8")).hexdigest()

    def _graphql_request(self, query: str) -> dict:
        payload_str = json.dumps({"query": query})
        timestamp = str(int(time.time()))
        signature = self._sign(payload_str, timestamp)

        headers = {
            "Authorization": f"SHA256 Credential={self.app_id}, Timestamp={timestamp}, Signature={signature}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                self.GRAPHQL_URL,
                data=payload_str.encode("utf-8"),
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return {}
            return data.get("data", {})
        except Exception:
            return {}

    def get_product_offer(self, item_id: int) -> dict | None:
        query = f"""query {{
            productOfferV2(itemId: {item_id}) {{
                nodes {{
                    itemId
                    productName
                    priceMin
                    priceMax
                    priceDiscountRate
                    ratingStar
                    sales
                    shopName
                    productLink
                    offerLink
                    imageUrl
                }}
            }}
        }}"""
        data = self._graphql_request(query)
        nodes = data.get("productOfferV2", {}).get("nodes", [])
        return nodes[0] if nodes else None

    def get_top_performing_offers(
        self, page: int = 1, limit: int = 50
    ) -> tuple[list[dict], bool]:
        """Busca ofertas de alta performance (listType = 2 = TOP_PERFORMING)."""
        query = f"""query {{
            productOfferV2(listType: 2, page: {page}, limit: {limit}) {{
                nodes {{
                    itemId
                    productName
                    priceMin
                    priceMax
                    priceDiscountRate
                    ratingStar
                    sales
                    shopName
                    productLink
                    offerLink
                    imageUrl
                }}
                pageInfo {{
                    page
                    limit
                    hasNextPage
                }}
            }}
        }}"""
        data = self._graphql_request(query)
        conn_data = data.get("productOfferV2", {})
        nodes = conn_data.get("nodes", [])
        has_next = conn_data.get("pageInfo", {}).get("hasNextPage", False)
        return nodes, has_next

    def generate_short_link(self, origin_url: str) -> str:
        safe_origin_url = json.dumps(origin_url)
        query = f"mutation {{ generateShortLink(input: {{originUrl: {safe_origin_url}}}) {{ shortLink }} }}"
        result = self._graphql_request(query)
        return result.get("generateShortLink", {}).get("shortLink", "")

    def resolve_short_link(self, short_url: str) -> str:
        try:
            session = requests.Session()
            resp = session.get(
                short_url,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                timeout=10,
            )
            return resp.url
        except Exception:
            return short_url

    def extract_item_id(self, url: str) -> int | None:
        match_param = re.search(r"[?&](?:itemId|item_id)=(\d+)", url, re.IGNORECASE)
        if match_param:
            return int(match_param.group(1))
        match_path = re.search(r"i\.\d+\.(\d+)", url) or re.search(
            r"-i\.\d+\.(\d+)", url
        )
        if match_path:
            return int(match_path.group(1))
        match_product = re.search(r"/product/\d+/(\d+)", url) or re.search(
            r"/sp/product/(\d+)", url
        )
        if match_product:
            return int(match_product.group(1))
        match_end_id = re.search(r"/(\d+)(?:\?|$)", url)
        if match_end_id:
            return int(match_end_id.group(1))
        return None

    def generate_persuasive_copy(self, offer: dict) -> str:
        discount = offer.get("priceDiscountRate")
        rating = offer.get("ratingStar")
        sales = offer.get("sales")

        lines = ["💡 <b>POR QUE VALE A PENA?</b>"]
        if discount and int(discount) > 0:
            lines.append(f"🔥 <b>{discount}% OFF</b> aplicado no item!\n")
        if sales and int(sales) > 10:
            lines.append(f"📦 Mais de <b>{int(sales):,} unidades vendidas</b>!")
        if rating and float(rating) >= 4.0:
            lines.append(f"⭐ Avaliação: <b>{rating}/5.0</b>")

        lines.append("\n🏪 Loja: <b>Shopee</b>")
        lines.append(
            "\n\n🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>"
        )
        lines.append(
            "\n⚠️ <b>Atenção:</b> O estoque e o preço promocional podem mudar a qualquer momento."
        )
        lines.append("\n👇 <b>Clique no link abaixo para garantir o seu:</b>")
        return "\n".join(lines)

    def format_product_message(self, offer: dict, link: str) -> str:
        name = offer.get("productName", "Produto Incrível")
        price_min = offer.get("priceMin")
        price_max = offer.get("priceMax")

        msg = f"🔥 <b>{name}</b>\n\n"

        if price_min:
            p_min = float(price_min)
            p_max = float(price_max) if price_max else p_min
            if p_min != p_max and p_max > 0:
                msg += (
                    f"💰 <b>Preço:</b> R$ {p_min:,.2f} - R$ {p_max:,.2f}\n\n".replace(
                        ",", "X"
                    )
                    .replace(".", ",")
                    .replace("X", ".")
                )
            else:
                msg += (
                    f"💰 <b>Preço:</b> R$ {p_min:,.2f}\n\n".replace(",", "X")
                    .replace(".", ",")
                    .replace("X", ".")
                )

        msg += self.generate_persuasive_copy(offer)
        msg += f"\n👉 {link}"
        return msg

    def generate_auto_offers_to_queue(
        self, count: int = 15, target_date: str | None = None
    ) -> int:
        """
        Busca ofertas de listType = 2 (TOP_PERFORMING) em páginas sorteadas,
        embaralha a ordem, ignora produtos repetidos nos últimos 2 dias e salva na fila.
        """
        if not target_date:
            target_date = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        pages_to_try = [1, 2, 3, 4, 5]
        random.shuffle(pages_to_try)

        collected_offers = []
        for page in pages_to_try:
            nodes, _ = self.get_top_performing_offers(page=page, limit=50)
            if nodes:
                collected_offers.extend(nodes)
            if len(collected_offers) >= count * 4:
                break

        if not collected_offers:
            return 0

        # Embaralha todas as ofertas coletadas para não seguir a sequência da API
        random.shuffle(collected_offers)

        added = 0
        seen_ids = set()

        for offer in collected_offers:
            if added >= count:
                break

            item_id = offer.get("itemId")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            product_url = (
                offer.get("productLink")
                or offer.get("offerLink")
                or f"https://shopee.com.br/product/0/{item_id}"
            )
            aff_link = (
                offer.get("offerLink")
                or self.generate_short_link(product_url)
                or product_url
            )

            # Verifica se já não foi inserido nos últimos 2 dias ou na data alvo
            if is_offer_recent(
                product_url, item_id=item_id, days=2, target_date=target_date
            ):
                continue

            msg = self.format_product_message(offer, aff_link)
            media_url = offer.get("imageUrl")

            add_to_queue(
                store="Shopee",
                product_url=product_url,
                message=msg,
                media_url=media_url,
                priority=0,  # 0 = Auto
                target_date=target_date,
            )
            added += 1

        return added
