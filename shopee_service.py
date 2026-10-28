import hashlib
import time
import requests
import re
import json
import asyncio
import random
import logging
from datetime import datetime, timezone, timedelta
from database import add_to_queue, is_offer_recent, get_config
from offer_comparator import (
    is_product_recently_used_or_blocked,
    load_recent_queue_profiles,
)

logger = logging.getLogger(__name__)
cuiaba_tz = timezone(timedelta(hours=-4))

# Expansão de subtermos por categoria para multiplicar a variedade de páginas na API
CATEGORY_SEARCH_EXPANSION = {
    "roupas femininas": [
        "Roupas Femininas",
        "Vestido Feminino",
        "Blusa Feminina",
        "Calça Feminina",
        "Conjunto Feminino",
        "Moda Feminina",
        "Cropped Feminino",
        "Saia Feminina",
    ],
    "moda infantil": [
        "Moda Infantil",
        "Roupa Infantil Menina",
        "Roupa Infantil Menino",
        "Conjunto Infantil",
        "Vestido Infantil",
        "Calçado Infantil",
        "Enxoval Bebê Roupa",
    ],
    "sapatos femininos": [
        "Sapatos Femininos",
        "Sandália Feminina",
        "Tênis Feminino",
        "Rasteirinha Feminina",
        "Bota Scarpin Feminino",
        " Sapatilha Mule Feminino",
    ],
}


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
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.warning(f"Aviso GraphQL Shopee: {data['errors']}")
                return {}
            return data.get("data", {})
        except Exception as e:
            logger.error(f"Erro na requisição GraphQL Shopee: {e}")
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
        has_next = bool(conn_data.get("pageInfo", {}).get("hasNextPage", False))
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
        has_next = bool(conn_data.get("pageInfo", {}).get("hasNextPage", False))
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
        clean_name = " ".join(raw_name.split())

        if len(clean_name) > 100:
            clean_name = clean_name[:100].rsplit(" ", 1)[0] + "..."

        msg = f"🔥 <b>{clean_name}</b>\n\n"

        price_min = offer.get("priceMin")
        price_max = offer.get("priceMax")

        if price_min:
            p_min = float(price_min)
            p_max = float(price_max) if price_max else p_min

            if p_min != p_max and p_max > 0:
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

    # ==========================================================================
    # COLETA PROFUNDA PAGINADA (SEM LIMITE BAIXO DE PÁGINAS)
    # ==========================================================================
    def get_category_offers_pool(
        self, categories_csv: str, max_pages_per_term: int = 3
    ) -> list[dict]:
        """
        Método auxiliar de compatibilidade (usado também pelo script de diagnóstico).
        """
        raw_categories = [
            c.strip() for c in (categories_csv or "").split(",") if c.strip()
        ]
        if not raw_categories:
            raw_categories = [
                "Roupas Femininas",
                "Moda Infantil",
                "Sapatos Femininos",
            ]

        pool: list[dict] = []
        for cat in raw_categories:
            cat_lower = cat.lower()
            subterms = CATEGORY_SEARCH_EXPANSION.get(cat_lower, [cat])
            chosen_terms = random.sample(subterms, k=min(2, len(subterms)))

            for term in chosen_terms:
                page = 1
                pages_fetched = 0
                while pages_fetched < max_pages_per_term:
                    nodes, has_next = self.get_best_selling_offers(
                        keyword=term, page=page, limit=50
                    )
                    if not nodes:
                        break
                    for n in nodes:
                        n["_source_bucket"] = cat
                    pool.extend(nodes)
                    pages_fetched += 1
                    if not has_next:
                        break
                    page += 1

        random.shuffle(pool)
        return pool

    def _harvest_valid_offers_deep(
        self,
        target_date: str,
        dedup_days: int,
        dedup_threshold: float,
        general_target: int,
        categories_target: int,
        categories_csv: str,
        keywords_csv: str,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """
        Varre páginas profundas da API da Shopee (usando hasNextPage real, sem trava em 5 ou 10 páginas)
        filtrando contra o banco de dados até acumular um amplo pool de ofertas INÉDITAS para sortear.
        Retorna: (valid_general_pool, valid_categories_pool, all_raw_backup_nodes)
        """
        preloaded_profiles = load_recent_queue_profiles(days=dedup_days)
        seen_ids: set[int] = set()
        all_raw_backup: list[dict] = []

        # Queremos acumular pelo menos 4x a quantidade necessária de ofertas inéditas
        # para que o sorteio final seja realmente aleatório entre dezenas de opções válidas!
        desired_general_pool_size = max(general_target * 4, 15)
        desired_cat_pool_size = max(categories_target * 4, 30)

        def _filter_unseen_nodes(
            nodes: list[dict], bucket_label: str, existing_pool_titles: list[str]
        ) -> list[dict]:
            approved = []
            for offer in nodes:
                item_id = offer.get("itemId")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                product_name = (offer.get("productName") or "").strip()
                if not product_name:
                    continue

                offer["_source_bucket"] = bucket_label
                all_raw_backup.append(offer)

                product_url = (
                    offer.get("productLink")
                    or offer.get("offerLink")
                    or f"https://shopee.com.br/product/0/{item_id}"
                )

                if is_offer_recent(
                    product_url,
                    item_id=item_id,
                    days=dedup_days,
                    target_date=target_date,
                ):
                    continue

                is_dup, _ = is_product_recently_used_or_blocked(
                    candidate_title=product_name,
                    candidate_url=product_url,
                    days=dedup_days,
                    threshold=dedup_threshold,
                    extra_accepted_titles=existing_pool_titles,
                    preloaded_db_profiles=preloaded_profiles,
                )
                if is_dup:
                    continue

                existing_pool_titles.append(product_name)
                approved.append(offer)
            return approved

        # ------------------------------------------------------------------
        # 1. VARREDURA PROFUNDA DE CATEGORIAS (70%: Roupas Femininas, Moda Infantil, Sapatos Femininos)
        # ------------------------------------------------------------------
        raw_categories = [
            c.strip() for c in (categories_csv or "").split(",") if c.strip()
        ]
        if not raw_categories:
            raw_categories = [
                "Roupas Femininas",
                "Moda Infantil",
                "Sapatos Femininos",
            ]

        per_cat_desired = max(
            10, int(desired_cat_pool_size / max(1, len(raw_categories))) + 4
        )
        valid_by_cat: dict[str, list[dict]] = {cat: [] for cat in raw_categories}
        cat_accepted_titles: list[str] = []

        for cat in raw_categories:
            cat_lower = cat.lower()
            subterms = list(CATEGORY_SEARCH_EXPANSION.get(cat_lower, [cat]))
            random.shuffle(subterms)

            # Percorre os subtermos e avança nas páginas (1, 2, 3... até 35) enquanto precisar de inéditos
            for term in subterms:
                if len(valid_by_cat[cat]) >= per_cat_desired:
                    break

                page = 1
                while page <= 35 and len(valid_by_cat[cat]) < per_cat_desired:
                    nodes, has_next = self.get_best_selling_offers(
                        keyword=term, page=page, limit=50
                    )
                    if not nodes:
                        break

                    random.shuffle(nodes)
                    new_valid = _filter_unseen_nodes(nodes, cat, cat_accepted_titles)
                    valid_by_cat[cat].extend(new_valid)

                    if not has_next:
                        break
                    page += 1

            random.shuffle(valid_by_cat[cat])

        # Combina os candidatos inéditos das 3 categorias em formato Round-Robin (para balancear as 3 categorias)
        valid_categories_pool: list[dict] = []
        cat_keys = list(valid_by_cat.keys())
        while any(valid_by_cat[k] for k in cat_keys):
            random.shuffle(cat_keys)
            for k in cat_keys:
                if valid_by_cat[k]:
                    valid_categories_pool.append(valid_by_cat[k].pop(0))

        # ------------------------------------------------------------------
        # 2. VARREDURA PROFUNDA DO MODO GERAL (30%: TOP_PERFORMING + MAIS VENDIDOS)
        # ------------------------------------------------------------------
        valid_general_pool: list[dict] = []
        gen_accepted_titles: list[str] = list(cat_accepted_titles)
        keywords = [k.strip() for k in (keywords_csv or "").split(",") if k.strip()]

        # 2.A: Avança nas páginas de TOP_PERFORMING enquanto precisar de itens inéditos
        page_top = 1
        while page_top <= 35 and len(valid_general_pool) < (
            desired_general_pool_size // 2 + 5
        ):
            nodes, has_next = self.get_top_performing_offers(page=page_top, limit=50)
            if not nodes:
                break
            random.shuffle(nodes)
            valid_general_pool.extend(
                _filter_unseen_nodes(nodes, "Mais Vendidos / Top", gen_accepted_titles)
            )
            if not has_next:
                break
            page_top += 1

        # 2.B: Avança nas páginas de MAIS VENDIDOS (por palavra-chave ou geral)
        if keywords:
            random.shuffle(keywords)
            for kw in keywords:
                if len(valid_general_pool) >= desired_general_pool_size:
                    break
                page_kw = 1
                while (
                    page_kw <= 35
                    and len(valid_general_pool) < desired_general_pool_size
                ):
                    nodes, has_next = self.get_best_selling_offers(
                        keyword=kw, page=page_kw, limit=50
                    )
                    if not nodes:
                        break
                    random.shuffle(nodes)
                    valid_general_pool.extend(
                        _filter_unseen_nodes(
                            nodes, f"Busca: {kw.title()}", gen_accepted_titles
                        )
                    )
                    if not has_next:
                        break
                    page_kw += 1
        else:
            page_sold = 1
            while (
                page_sold <= 35 and len(valid_general_pool) < desired_general_pool_size
            ):
                nodes, has_next = self.get_best_selling_offers(
                    keyword=None, page=page_sold, limit=50
                )
                if not nodes:
                    break
                random.shuffle(nodes)
                valid_general_pool.extend(
                    _filter_unseen_nodes(
                        nodes, "Mais Vendidos / Top", gen_accepted_titles
                    )
                )
                if not has_next:
                    break
                page_sold += 1

        random.shuffle(valid_general_pool)
        return valid_general_pool, valid_categories_pool, all_raw_backup

    def generate_auto_offers_to_queue(
        self,
        count: int = 15,
        target_date: str | None = None,
        keywords_csv: str | None = None,
    ) -> int:
        """
        Gera ofertas automáticas da Shopee dividindo:
        - 30% (configurável em `shopee_general_pct`): Busca Geral (TOP_PERFORMING + Mais Vendidos)
        - 70% (restante): Categorias específicas (Roupas Femininas, Moda Infantil, Sapatos Femininos)
        Varre páginas profundas sem limite baixo e sorteia dentro dos inéditos.
        """
        if not target_date:
            target_date = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        if keywords_csv is None:
            keywords_csv = get_config("auto_keywords") or ""

        general_pct = int(get_config("shopee_general_pct") or "30")
        general_pct = max(0, min(100, general_pct))
        target_categories_csv = (
            get_config("shopee_target_categories")
            or "Roupas Femininas, Moda Infantil, Sapatos Femininos"
        )

        dedup_days = int(get_config("dedup_history_days") or "7")
        dedup_threshold = float(get_config("dedup_similarity_threshold") or "60")

        general_target = round(count * (general_pct / 100.0))
        if general_pct > 0 and count >= 2 and general_target == 0:
            general_target = 1
        categories_target = max(0, count - general_target)

        logger.info(
            f"🔎 Iniciando varredura paginada profunda na Shopee: meta={count} ({general_target} Geral / {categories_target} Categorias)..."
        )

        valid_general_pool, valid_categories_pool, all_raw_backup = (
            self._harvest_valid_offers_deep(
                target_date=target_date,
                dedup_days=dedup_days,
                dedup_threshold=dedup_threshold,
                general_target=general_target,
                categories_target=categories_target,
                categories_csv=target_categories_csv,
                keywords_csv=keywords_csv,
            )
        )

        logger.info(
            f"🎯 Pools de inéditos prontos para sorteio: {len(valid_categories_pool)} de Categorias | {len(valid_general_pool)} Gerais."
        )

        # Sorteia dentro dos pools de inéditos
        chosen_categories = valid_categories_pool[:categories_target]
        remaining_cat = valid_categories_pool[categories_target:]

        chosen_general = valid_general_pool[:general_target]
        remaining_gen = valid_general_pool[general_target:]

        # Se uma das duas listas (30% ou 70%) teve menos inéditos que a cota, completa com a sobra da outra
        total_so_far = len(chosen_general) + len(chosen_categories)
        if total_so_far < count:
            missing = count - total_so_far
            leftovers = remaining_cat + remaining_gen
            chosen_categories.extend(leftovers[:missing])

        # VÁLVULA DE SEGURANÇA ANTI-TRAVAMENTO:
        # Se mesmo varrendo dezenas de páginas ainda faltarem itens para bater `count`,
        # relaxa a janela de repetição apenas para os itens faltantes (ex: não repetir no mesmo dia/últimos 2 dias)
        total_so_far = len(chosen_general) + len(chosen_categories)
        if total_so_far < count and all_raw_backup:
            missing = count - total_so_far
            logger.warning(
                f"⚠️ Pool estrito de {dedup_days}d rendeu {total_so_far}/{count} itens. Acionando fallback seguro para completar +{missing} ofertas!"
            )
            already_chosen_ids = {
                o.get("itemId") for o in (chosen_general + chosen_categories)
            }
            random.shuffle(all_raw_backup)
            for backup_offer in all_raw_backup:
                if len(chosen_general) + len(chosen_categories) >= count:
                    break
                b_id = backup_offer.get("itemId")
                if not b_id or b_id in already_chosen_ids:
                    continue
                b_url = (
                    backup_offer.get("productLink")
                    or backup_offer.get("offerLink")
                    or f"https://shopee.com.br/product/0/{b_id}"
                )
                # Garante que pelo menos não esteja na fila de hoje/ontem
                if is_offer_recent(
                    b_url, item_id=b_id, days=1, target_date=target_date
                ):
                    continue
                already_chosen_ids.add(b_id)
                chosen_categories.append(backup_offer)

        # Intercala as ofertas (2 de Categorias para 1 Geral) para distribuir ao longo do dia
        final_offers: list[dict] = []
        while chosen_categories or chosen_general:
            for _ in range(2):
                if chosen_categories:
                    final_offers.append(chosen_categories.pop(0))
            if chosen_general:
                final_offers.append(chosen_general.pop(0))

        added = 0
        for offer in final_offers:
            item_id = offer.get("itemId")
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

            msg = self.format_product_message(offer, aff_link)
            media_url = offer.get("imageUrl")
            category_name = offer.get("_source_bucket", "Mais Vendidos / Top")

            add_to_queue(
                store="Shopee",
                product_url=product_url,
                message=msg,
                media_url=media_url,
                media_type="image",
                priority=0,
                target_date=target_date,
                category=category_name,
            )
            added += 1

        logger.info(
            f"✅ Shopee Auto concluído: {added}/{count} ofertas inéditas sorteadas e inseridas na fila ({target_date})."
        )
        return added

    # ==========================================================================
    # RELATÓRIO DE VENDAS E COMISSÕES (CONVERSION REPORT)
    # ==========================================================================
    def get_sales_report(self, days: int = 30) -> dict:
        """
        Busca os pedidos e comissões gerados na Shopee via GraphQL `conversionReport`.
        """
        now_dt = datetime.now(cuiaba_tz)
        start_dt = now_dt - timedelta(days=days)
        purchase_time_start = int(start_dt.timestamp())
        purchase_time_end = int(now_dt.timestamp())

        query_full = f"""query {{
            conversionReport(purchaseTimeStart: {purchase_time_start}, purchaseTimeEnd: {purchase_time_end}, limit: 100) {{
                nodes {{
                    conversionId
                    purchaseTime
                    totalCommission
                    netCommission
                    buyerType
                    orders {{
                        orderId
                        orderStatus
                        items {{
                            itemId
                            itemName
                            itemPrice
                            actualAmount
                            qty
                            itemTotalCommission
                            imageUrl
                        }}
                    }}
                }}
                pageInfo {{
                    hasNextPage
                }}
            }}
        }}"""

        query_minimal = f"""query {{
            conversionReport(purchaseTimeStart: {purchase_time_start}, purchaseTimeEnd: {purchase_time_end}, limit: 100) {{
                nodes {{
                    conversionId
                    purchaseTime
                    totalCommission
                    orders {{
                        orderId
                        orderStatus
                        items {{
                            itemName
                            actualAmount
                            qty
                        }}
                    }}
                }}
            }}
        }}"""

        data = self._graphql_request(query_full)
        if not data or "conversionReport" not in data:
            data = self._graphql_request(query_minimal)

        nodes = (data.get("conversionReport") or {}).get("nodes") or []

        orders_list = []
        total_orders = 0
        pending_orders = 0
        completed_orders = 0
        cancelled_orders = 0
        total_gmv = 0.0
        total_commission_valid = 0.0
        completed_commission = 0.0
        pending_commission = 0.0

        def _to_float(val) -> float:
            if val is None or val == "":
                return 0.0
            try:
                return float(str(val).replace(",", "."))
            except Exception:
                return 0.0

        for node in nodes:
            p_time = node.get("purchaseTime")
            if p_time:
                try:
                    dt_str = datetime.fromtimestamp(int(p_time), tz=cuiaba_tz).strftime(
                        "%d/%m/%Y %H:%M"
                    )
                except Exception:
                    dt_str = str(p_time)
            else:
                dt_str = "-"

            conv_comm = _to_float(
                node.get("netCommission") or node.get("totalCommission")
            )
            node_orders = node.get("orders") or []

            if not node_orders:
                total_orders += 1
                pending_orders += 1
                total_commission_valid += conv_comm
                pending_commission += conv_comm
                orders_list.append(
                    {
                        "platform": "Shopee",
                        "order_id": str(node.get("conversionId") or "N/A"),
                        "purchase_time": dt_str,
                        "raw_ts": int(p_time or 0),
                        "status": "PENDING",
                        "status_label": "Pendente",
                        "items_summary": "Pedido registrado na conversão",
                        "items_count": 1,
                        "order_amount": 0.0,
                        "commission": conv_comm,
                        "buyer_type": node.get("buyerType") or "-",
                    }
                )
                continue

            for ord_obj in node_orders:
                order_id = ord_obj.get("orderId") or node.get("conversionId") or "N/A"
                raw_status = (ord_obj.get("orderStatus") or "PENDING").upper()
                items = ord_obj.get("items") or []

                order_gmv = 0.0
                items_comm = 0.0
                item_names = []
                total_qty = 0

                for it in items:
                    qty = int(it.get("qty") or 1)
                    total_qty += qty
                    actual_amt = _to_float(
                        it.get("actualAmount") or it.get("itemPrice")
                    )
                    order_gmv += actual_amt
                    items_comm += _to_float(it.get("itemTotalCommission"))
                    nm = (it.get("itemName") or "Produto Shopee").strip()
                    item_names.append(f"{qty}x {nm}")

                order_comm = (
                    items_comm
                    if items_comm > 0
                    else (conv_comm / max(1, len(node_orders)))
                )

                is_cancelled = any(
                    k in raw_status
                    for k in ("CANCEL", "INVALID", "UNPAID", "RETURN", "REJECT")
                )
                is_completed = any(
                    k in raw_status
                    for k in ("COMPLETE", "COMPLETED", "APPROVED", "PAID")
                )

                total_orders += 1
                if is_cancelled:
                    cancelled_orders += 1
                    status_label = "Cancelado"
                elif is_completed:
                    completed_orders += 1
                    status_label = "Concluído"
                    total_gmv += order_gmv
                    total_commission_valid += order_comm
                    completed_commission += order_comm
                else:
                    pending_orders += 1
                    status_label = "Pendente"
                    total_gmv += order_gmv
                    total_commission_valid += order_comm
                    pending_commission += order_comm

                orders_list.append(
                    {
                        "platform": "Shopee",
                        "order_id": str(order_id),
                        "purchase_time": dt_str,
                        "raw_ts": int(p_time or 0),
                        "status": raw_status,
                        "status_label": status_label,
                        "items_summary": (
                            " | ".join(item_names) if item_names else "Item Shopee"
                        ),
                        "items_count": total_qty or 1,
                        "order_amount": round(order_gmv, 2),
                        "commission": round(order_comm, 2),
                        "buyer_type": node.get("buyerType") or "-",
                    }
                )

        orders_list.sort(key=lambda x: x["raw_ts"], reverse=True)

        return {
            "days": days,
            "total_orders": total_orders,
            "pending_orders": pending_orders,
            "completed_orders": completed_orders,
            "cancelled_orders": cancelled_orders,
            "total_gmv": round(total_gmv, 2),
            "total_commission": round(total_commission_valid, 2),
            "completed_commission": round(completed_commission, 2),
            "pending_commission": round(pending_commission, 2),
            "orders": orders_list,
        }
