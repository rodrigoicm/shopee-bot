import hashlib
import time
import requests
import re
import json
import asyncio
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

    def get_best_selling_offers(
        self, keyword: str | None = None, page: int = 1, limit: int = 50
    ) -> tuple[list[dict], bool]:
        """Busca ofertas ordenadas por Mais Vendidos (sortType = 2 = ITEM_SOLD_DESC)."""
        if keyword and keyword.strip():
            safe_kw = json.dumps(keyword.strip())
            params = f"keyword: {safe_kw}, sortType: 2, page: {page}, limit: {limit}"
        else:
            params = f"listType: 0, sortType: 2, page: {page}, limit: {limit}"

        query = f"""query {{
            productOfferV2({params}) {{
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

    async def extract_shopee_video_fast(self, browser, product_url: str) -> str | None:
        """Extrai o link .mp4 da Shopee com timeout rígido para nunca travar."""
        if not browser:
            return None

        captured_video_url = None
        context = None

        async def _run_extraction():
            nonlocal captured_video_url, context
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
            )
            page = await context.new_page()

            await page.add_init_script("""
                window.__captured_video = null;
                const checkVideo = () => {
                    const v = document.querySelector('video, video source');
                    if (v && (v.src || v.currentSrc)) {
                        window.__captured_video = v.src || v.currentSrc;
                    }
                };
                const observer = new MutationObserver(checkVideo);
                observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
            """)

            def on_request(request):
                nonlocal captured_video_url
                req_url = request.url
                if ".mp4" in req_url and (
                    "susercontent.com" in req_url or "shopee" in req_url
                ):
                    captured_video_url = req_url.split("?")[0]

            async def on_response(response):
                nonlocal captured_video_url
                if captured_video_url:
                    return
                res_url = response.url
                if ".mp4" in res_url and (
                    "susercontent.com" in res_url or "shopee" in res_url
                ):
                    captured_video_url = res_url.split("?")[0]
                    return
                if (
                    "pdp/get" in res_url or "item/get" in res_url
                ) and response.status == 200:
                    try:
                        body = await asyncio.wait_for(response.text(), timeout=2.0)
                        match = re.search(r'https?://[^"\'\s\\]+\.mp4', body)
                        if match:
                            captured_video_url = (
                                match.group(0)
                                .replace("\\u002F", "/")
                                .replace("\\/", "/")
                            )
                    except Exception:
                        pass

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                await page.goto(product_url, wait_until="commit", timeout=8000)
            except Exception:
                pass

            for _ in range(12):
                if captured_video_url:
                    break

                if "/buyer/login" in page.url or "/verify/" in page.url:
                    break

                try:
                    found = await page.evaluate(r"""() => {
                        if (window.__captured_video) return window.__captured_video;
                        const v = document.querySelector('video, video source');
                        if (v && (v.src || v.currentSrc)) return v.src || v.currentSrc;
                        const html = document.documentElement.innerHTML;
                        const m = html.match(/https?:\/\/[^"'\s\\]+\.mp4/);
                        return m ? m[0] : null;
                    }""")
                    if found and "http" in found:
                        captured_video_url = found.replace("\\u002F", "/").replace(
                            "\\/", "/"
                        )
                        break
                except Exception:
                    pass

                await asyncio.sleep(0.5)

            return captured_video_url

        try:
            return await asyncio.wait_for(_run_extraction(), timeout=10.0)
        except Exception:
            return captured_video_url
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

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
        raw_name = offer.get("productName", "Produto Incrível")

        # Limpeza do título: remove espaços duplos
        clean_name = " ".join(raw_name.split())

        # Corte inteligente: se passar de 100 caracteres, corta no último espaço antes do limite
        if len(clean_name) > 100:
            clean_name = clean_name[:100].rsplit(" ", 1)[0] + "..."

        msg = f"🔥 <b>{clean_name}</b>\n\n"

        price_min = offer.get("priceMin")
        price_max = offer.get("priceMax")

        if price_min:
            p_min = float(price_min)
            p_max = float(price_max) if price_max else p_min

            if p_min != p_max and p_max > 0:
                # Substitui a faixa de preço por "A partir de"
                msg += (
                    f"💰 <b>Preço:</b> A partir de R$ {p_min:,.2f}\n\n".replace(
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
        self,
        count: int = 15,
        target_date: str | None = None,
        keywords_csv: str | None = None,
    ) -> int:
        """
        Mescla ofertas de TOP_PERFORMING (listType = 2) e Mais Vendidos (sortType = 2),
        embaralha a ordem, ignora produtos repetidos nos últimos 2 dias e salva na fila.
        """
        if not target_date:
            target_date = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        if keywords_csv is None:
            from database import get_config

            keywords_csv = get_config("auto_keywords") or ""

        keywords = [k.strip() for k in keywords_csv.split(",") if k.strip()]

        pages_top = [1, 2, 3, 4, 5]
        pages_sold = [1, 2, 3, 4, 5]
        random.shuffle(pages_top)
        random.shuffle(pages_sold)

        collected_offers = []

        # 1. Coleta lote de TOP_PERFORMING (listType = 2)
        for page in pages_top[:2]:
            nodes, _ = self.get_top_performing_offers(page=page, limit=50)
            if nodes:
                collected_offers.extend(nodes)

        # 2. Coleta lote de MAIS VENDIDOS (sortType = 2)
        if keywords:
            random.shuffle(keywords)
            for kw in keywords[:3]:
                nodes, _ = self.get_best_selling_offers(
                    keyword=kw, page=random.choice([1, 2]), limit=50
                )
                if nodes:
                    collected_offers.extend(nodes)
        else:
            for page in pages_sold[:2]:
                nodes, _ = self.get_best_selling_offers(
                    keyword=None, page=page, limit=50
                )
                if nodes:
                    collected_offers.extend(nodes)

        if not collected_offers:
            return 0

        # Embaralha todas as ofertas mescladas para intercalar TOP_PERFORMING e Mais Vendidos
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

            # Ignora se já foi inserido nos últimos 2 dias ou na data alvo
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
                media_type="image",
                priority=0,  # 0 = Auto
                target_date=target_date,
            )
            added += 1

        return added
