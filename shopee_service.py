import hashlib
import time
import requests
import re
import json
import asyncio
import random
import logging
import html as html_lib
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from database import add_to_queue, is_offer_recent, get_config, update_config
from offer_comparator import (
    is_product_recently_used_or_blocked,
    load_recent_queue_profiles,
)

logger = logging.getLogger(__name__)
cuiaba_tz = timezone(timedelta(hours=-4))

# Dicionário estático de fallback caso a Shopee demore a responder na busca dinâmica
SHOPEE_CATEGORIES = {
    "Roupas Femininas": {
        "catid": 11059998,
        "label": "Roupas Femininas",
        "subterms": ["Vestidos", "Blusas", "Saias", "Shorts", "Jeans", "Cropped"],
    },
    "Casa e Construção": {
        "catid": 11059983,
        "label": "Casa e Construção",
        "subterms": [
            "Decoração",
            "Artigos de Cozinha",
            "Roupas de Cama",
            "Banheiros",
            "Iluminação",
        ],
    },
    "Eletroportáteis e Cozinha": {
        "catid": 0,
        "label": "Eletroportáteis e Cozinha",
        "subterms": [
            "Fritadeira Air Fryer",
            "Liquidificador",
            "Panela de Pressão",
            "Chaleira Elétrica",
            "Batedeira",
            "Cafeteira",
        ],
    },
    "Beleza": {
        "catid": 11059974,
        "label": "Beleza",
        "subterms": [
            "Maquiagem",
            "Cuidados com o Cabelo",
            "Perfumes e Fragrâncias",
            "Cuidados com a Pele",
        ],
    },
    "Moda Infantil": {
        "catid": 11059973,
        "label": "Moda Infantil",
        "subterms": [
            "Roupas de Meninas",
            "Roupas de Meninos",
            "Calçados Infantis",
            "Acessórios Infantis",
        ],
    },
    "Sapatos Femininos": {
        "catid": 11059999,
        "label": "Sapatos Femininos",
        "subterms": ["Tênis", "Botas", "Sandálias", "Sapatilhas", "Mule", "Saltos"],
    },
}


class ShopeeAffiliateAPI:
    GRAPHQL_URL = "https://open-api.affiliate.shopee.com.br/graphql"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    @staticmethod
    def get_available_shopee_categories() -> dict:
        """Retorna as categorias da Shopee, buscando do cache do banco ou da API pública."""
        try:
            cached_json = get_config("shopee_cached_categories_json")
            if cached_json:
                data = json.loads(cached_json)
                if data and isinstance(data, dict):
                    return data
        except Exception:
            pass
        return SHOPEE_CATEGORIES

    @staticmethod
    def fetch_dynamic_shopee_categories() -> dict:
        """Busca a árvore oficial de categorias da Shopee Brasil e atualiza o cache."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            }
            resp = requests.get(
                "https://shopee.com.br/api/v4/pages/get_category_tree",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                cats = data.get("data", {}).get("category_list", [])
                result = {}
                for c in cats:
                    c_name = c.get("display_name")
                    c_id = c.get("catid")
                    children = c.get("children") or []
                    sub_names = [
                        ch.get("display_name")
                        for ch in children
                        if ch.get("display_name")
                    ]
                    if c_name:
                        result[c_name] = {
                            "catid": c_id,
                            "label": c_name,
                            "subterms": sub_names if sub_names else [c_name],
                        }
                if result:
                    update_config(
                        "shopee_cached_categories_json",
                        json.dumps(result, ensure_ascii=False),
                    )
                    return result
        except Exception as e:
            logger.warning(f"⚠️ Erro ao atualizar árvore dinâmica da Shopee: {e}")
        return SHOPEE_CATEGORIES

    @staticmethod
    def _extract_product_type_key(title: str) -> str:
        """
        Extrai o substantivo principal (ex: 'chaleira', 'liquidificador')
        para barrar múltiplas ofertas do mesmo tipo de produto no mesmo lote.
        """
        if not title:
            return ""
        nfkd = unicodedata.normalize("NFKD", title.lower())
        clean = "".join(c for c in nfkd if not unicodedata.combining(c))

        clean = re.sub(r"\bair\s*fryer\b", "airfryer", clean)
        clean = re.sub(r"\bpao\s+de\s+queijo\b", "paodequeijo", clean)
        clean = re.sub(r"\bpanela\s+de\s+pressao\b", "panelapressao", clean)
        clean = re.sub(r"\bfone\s+de\s+ouvido\b", "foneouvido", clean)
        clean = re.sub(r"\bcaixa\s+de\s+som\b", "caixasom", clean)
        clean = re.sub(r"\bpower\s*bank\b", "powerbank", clean)
        clean = re.sub(r"\bwhey\s*protein\b", "whey", clean)

        prefixes_to_skip = {
            "kit",
            "jogo",
            "par",
            "conjunto",
            "combo",
            "pack",
            "caixa",
            "super",
            "mini",
            "novo",
            "nova",
            "original",
            "oferta",
            "promocao",
            "barato",
            "lindo",
            "linda",
            "top",
            "plus",
            "max",
            "pro",
            "ultra",
            "mega",
            "pecas",
            "pcs",
            "un",
            "unidades",
            "litros",
            "ml",
            "kg",
            "gramas",
        }
        words = re.findall(r"[a-z]{3,}", clean)
        for w in words:
            if w in prefixes_to_skip:
                continue
            if len(w) > 4 and w.endswith("s") and not w.endswith("is"):
                w = w[:-1]
            return w
        return words[0] if words else ""

    @staticmethod
    def _is_title_blocked(title: str, blocked_terms: list[str]) -> bool:
        if not title or not blocked_terms:
            return False
        t_low = title.lower()
        for term in blocked_terms:
            clean_t = term.strip().lower()
            if clean_t and clean_t in t_low:
                return True
        return False

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
        short_url = re.sub(
            r"(https?://s\.shopee\.com\.br/[A-Za-z0-9]{10}).*$",
            r"\1",
            short_url.strip(),
        )
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

    def clean_shopee_url_for_affiliate(self, url: str) -> str:
        resolved = (
            self.resolve_short_link(url)
            if (
                url.startswith("http")
                and ("s.shopee" in url or "shope.ee" in url or "shp.ee" in url)
            )
            else url
        )
        try:
            parsed = urlparse(resolved)
            if "/product/" in parsed.path or re.search(r"-i\.\d+\.\d+", parsed.path):
                return urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
                )

            blocked_params = {
                "uls_trackid",
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_content",
                "utm_term",
                "mmp_pid",
                "af_siteid",
                "af_click_lookback",
                "af_viewthrough_lookback",
                "pid",
                "c",
                "is_from_login",
                "smtt",
                "sp_atk",
                "xptdk",
            }
            clean_query = [
                (k, v)
                for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in blocked_params and not k.lower().startswith("af_")
            ]
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    urlencode(clean_query),
                    "",
                )
            )
        except Exception:
            return resolved

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

    def generate_persuasive_copy(
        self,
        offer: dict,
        store_name: str = "Shopee",
        extra_coupon_links: list[str] | None = None,
    ) -> str:
        lines = []
        if extra_coupon_links:
            for c_link in extra_coupon_links:
                lines.append(f"🎟️ <b>Resgate o cupom:</b> {c_link}")
        lines.append("🚚 <i>Frete Grátis c/ cupom no app</i>")
        return "\n".join(lines)

    def format_product_message(
        self,
        offer: dict,
        link: str,
        store_name: str = "Shopee",
        extra_coupon_links: list[str] | None = None,
        include_ai_hook: bool = True,
    ) -> str:
        from ai_copy_service import clean_product_title, prepend_ai_hook_to_message

        raw_name = offer.get("productName", "Produto em Oferta")
        clean_name = clean_product_title(raw_name, max_len=68)

        discount = offer.get("priceDiscountRate")
        disc_badge = (
            f" <i>({int(discount)}% OFF)</i>"
            if (discount and int(discount) > 0)
            else ""
        )

        lines = [f"🔥 <b>{html_lib.escape(clean_name)}</b>"]

        price_min = offer.get("priceMin")
        price_max = offer.get("priceMax")
        has_variation_range = False

        if price_min:
            p_min = float(price_min)
            p_max = float(price_max) if price_max else p_min
            p_min_str = (
                f"R$ {p_min:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            )

            if p_min != p_max and p_max > p_min:
                has_variation_range = True
                lines.append(f"💰 A partir de <b>{p_min_str}</b>{disc_badge}")
            else:
                lines.append(f"💰 Por <b>{p_min_str}</b>{disc_badge}")

        lines.append(
            self.generate_persuasive_copy(
                offer, store_name=store_name, extra_coupon_links=extra_coupon_links
            )
        )
        lines.append(f"\n🛒 <b>Compre na {html_lib.escape(store_name)}:</b>\n👉 {link}")

        base_msg = "\n".join(lines)
        if not include_ai_hook:
            return base_msg

        return prepend_ai_hook_to_message(
            base_message=base_msg,
            title=clean_name,
            current_price=offer.get("priceMin"),
            original_price=None,
            discount_pct=discount,
            store=store_name,
            sales=offer.get("sales"),
            max_price=offer.get("priceMax") if has_variation_range else None,
            is_starting_price=has_variation_range,
        )

    @staticmethod
    def format_coupon_page_message(
        aff_link: str, store_name: str = "Shopee", custom_title: str | None = None
    ) -> str:
        header = (
            custom_title
            or f"🎟️ <b>CENTRAL DE CUPONS E DESCONTOS {store_name.upper()}!</b>"
        )
        return (
            f"{header}\n\n"
            "🔥 <b>Liberamos a página oficial com todos os cupons ativos do dia!</b>\n\n"
            "✅ Cupons de <b>Frete Grátis</b> extras\n"
            "✅ Descontos em <b>Lojas Oficiais</b> e Moedas\n"
            "✅ Vouchers especiais por categoria\n\n"
            "⚠️ <i>Dica: Resgate agora mesmo antes que esgotem os lotes e aplique no seu carrinho!</i>\n\n"
            "👇 <b>Clique no link abaixo para resgatar todos os cupons:</b>\n"
            f"👉 {aff_link}"
        )

    def _harvest_valid_offers_deep(
        self,
        target_date: str,
        dedup_days: int,
        dedup_threshold: float,
        general_target: int,
        categories_target: int,
        categories_csv: str,
        keywords_csv: str,
        blocked_terms: list[str] | None = None,
        stats_out: dict | None = None,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        preloaded_profiles = load_recent_queue_profiles(days=dedup_days)
        seen_ids: set[int] = set()
        seen_product_types: set[str] = set()
        all_raw_backup: list[dict] = []
        blocked_list = blocked_terms or []
        skipped_dups = 0
        accepted_cats_count: dict[str, int] = {}

        desired_general_pool_size = max(general_target * 2, 8)
        desired_cat_pool_size = max(categories_target * 2, 14)

        def _filter_unseen_nodes(
            nodes: list[dict],
            bucket_label: str,
            existing_pool_titles: list[str],
            max_take: int = 999,
        ) -> list[dict]:
            nonlocal skipped_dups
            approved = []
            for offer in nodes:
                if len(approved) >= max_take:
                    break
                item_id = offer.get("itemId")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                product_name = (offer.get("productName") or "").strip()
                if not product_name:
                    continue

                if self._is_title_blocked(product_name, blocked_list):
                    skipped_dups += 1
                    continue

                prod_type_key = self._extract_product_type_key(product_name)
                if prod_type_key and prod_type_key in seen_product_types:
                    skipped_dups += 1
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
                    skipped_dups += 1
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
                    skipped_dups += 1
                    continue

                if prod_type_key:
                    seen_product_types.add(prod_type_key)
                existing_pool_titles.append(product_name)
                approved.append(offer)
                accepted_cats_count[bucket_label] = (
                    accepted_cats_count.get(bucket_label, 0) + 1
                )
            return approved

        dynamic_categories = self.get_available_shopee_categories()

        raw_categories = [
            c.strip() for c in (categories_csv or "").split(",") if c.strip()
        ]
        if not raw_categories:
            raw_categories = list(dynamic_categories.keys())[:3]

        per_cat_desired = max(
            6, int(desired_cat_pool_size / max(1, len(raw_categories))) + 2
        )
        valid_by_cat: dict[str, list[dict]] = {cat: [] for cat in raw_categories}
        cat_accepted_titles: list[str] = []

        for cat in raw_categories:
            # Busca subtermos da árvore dinâmica da Shopee
            cat_match = next(
                (v for k, v in dynamic_categories.items() if k.lower() == cat.lower()),
                None,
            )
            if cat_match and cat_match.get("subterms"):
                subterms = list(cat_match["subterms"])
            else:
                subterms = [cat]

            random.shuffle(subterms)
            max_per_subterm = max(2, int(per_cat_desired / max(1, len(subterms))) + 1)

            for term in subterms:
                if len(valid_by_cat[cat]) >= per_cat_desired:
                    break

                chosen_page = random.choice([1, 2])
                nodes, _ = self.get_best_selling_offers(
                    keyword=term, page=chosen_page, limit=50
                )
                if not nodes and chosen_page != 1:
                    nodes, _ = self.get_best_selling_offers(
                        keyword=term, page=1, limit=50
                    )
                if not nodes:
                    continue

                random.shuffle(nodes)
                new_valid = _filter_unseen_nodes(
                    nodes, cat, cat_accepted_titles, max_take=max_per_subterm
                )
                valid_by_cat[cat].extend(new_valid)

            random.shuffle(valid_by_cat[cat])

        valid_categories_pool: list[dict] = []
        cat_keys = list(valid_by_cat.keys())
        while any(valid_by_cat[k] for k in cat_keys):
            random.shuffle(cat_keys)
            for k in cat_keys:
                if valid_by_cat[k]:
                    valid_categories_pool.append(valid_by_cat[k].pop(0))

        valid_general_pool: list[dict] = []
        gen_accepted_titles: list[str] = list(cat_accepted_titles)
        keywords = [k.strip() for k in (keywords_csv or "").split(",") if k.strip()]

        for page_top in random.sample([1, 2, 3], k=2):
            if len(valid_general_pool) >= desired_general_pool_size:
                break
            nodes, _ = self.get_top_performing_offers(page=page_top, limit=50)
            if not nodes:
                continue
            random.shuffle(nodes)
            valid_general_pool.extend(
                _filter_unseen_nodes(
                    nodes, "Mais Vendidos / Top", gen_accepted_titles, max_take=6
                )
            )

        if keywords:
            random.shuffle(keywords)
            for kw in keywords:
                if len(valid_general_pool) >= desired_general_pool_size:
                    break
                nodes, _ = self.get_best_selling_offers(keyword=kw, page=1, limit=50)
                if not nodes:
                    continue
                random.shuffle(nodes)
                valid_general_pool.extend(
                    _filter_unseen_nodes(
                        nodes, f"Busca: {kw.title()}", gen_accepted_titles, max_take=2
                    )
                )
        elif len(valid_general_pool) < desired_general_pool_size:
            nodes, _ = self.get_best_selling_offers(
                keyword=None, page=random.choice([1, 2]), limit=50
            )
            if nodes:
                random.shuffle(nodes)
                valid_general_pool.extend(
                    _filter_unseen_nodes(
                        nodes, "Mais Vendidos / Top", gen_accepted_titles, max_take=6
                    )
                )

        random.shuffle(valid_general_pool)
        if stats_out is not None:
            stats_out["skipped_duplicates"] = skipped_dups
            stats_out["accepted_categories"] = accepted_cats_count
        return valid_general_pool, valid_categories_pool, all_raw_backup

    def simulate_shopee_offers_selection(
        self,
        count: int = 10,
        target_date: str | None = None,
        categories_override: list[str] | None = None,
        keywords_override: str | None = None,
    ) -> dict:
        t0 = time.perf_counter()
        if not target_date:
            target_date = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        general_pct = int(get_config("shopee_general_pct") or "30")
        general_pct = max(0, min(100, general_pct))

        if categories_override:
            target_categories_csv = ", ".join(categories_override)
        else:
            target_categories_csv = (
                get_config("shopee_target_categories")
                or "Roupas Femininas, Moda Infantil, Sapatos Femininos"
            )

        keywords_csv = (
            keywords_override
            if keywords_override is not None
            else (get_config("auto_keywords") or "")
        )

        global_blocked = get_config("dedup_blocked_terms") or ""
        shopee_blocked = get_config("shopee_blocked_terms") or ""
        combined_blocked = [
            t.strip()
            for t in re.split(r"[,;\n]+", f"{global_blocked},{shopee_blocked}")
            if t.strip()
        ]

        dedup_days = int(get_config("dedup_history_days") or "7")
        dedup_threshold = float(get_config("dedup_similarity_threshold") or "60")

        general_target = round(count * (general_pct / 100.0))
        if general_pct > 0 and count >= 2 and general_target == 0:
            general_target = 1
        categories_target = max(0, count - general_target)

        stats: dict = {}
        valid_general_pool, valid_categories_pool, _ = self._harvest_valid_offers_deep(
            target_date=target_date,
            dedup_days=dedup_days,
            dedup_threshold=dedup_threshold,
            general_target=general_target,
            categories_target=categories_target,
            categories_csv=target_categories_csv,
            keywords_csv=keywords_csv,
            blocked_terms=combined_blocked,
            stats_out=stats,
        )

        chosen_categories = list(valid_categories_pool[:categories_target])
        remaining_cat = list(valid_categories_pool[categories_target:])

        chosen_general = list(valid_general_pool[:general_target])
        remaining_gen = list(valid_general_pool[general_target:])

        total_so_far = len(chosen_general) + len(chosen_categories)
        if total_so_far < count:
            missing = count - total_so_far
            leftovers = remaining_cat + remaining_gen
            chosen_categories.extend(leftovers[:missing])

        final_offers: list[dict] = []
        while chosen_categories or chosen_general:
            for _ in range(2):
                if chosen_categories:
                    final_offers.append(chosen_categories.pop(0))
            if chosen_general:
                final_offers.append(chosen_general.pop(0))

        items_formatted = []
        for offer in final_offers:
            item_id = offer.get("itemId")
            product_url = (
                offer.get("productLink")
                or offer.get("offerLink")
                or f"https://shopee.com.br/product/0/{item_id}"
            )
            aff_link = offer.get("offerLink") or product_url
            preview_msg = self.format_product_message(
                offer, aff_link, include_ai_hook=False
            )
            items_formatted.append(
                {
                    "item_id": item_id,
                    "title": offer.get("productName") or "Produto Shopee",
                    "product_url": product_url,
                    "affiliate_url": aff_link,
                    "image_url": offer.get("imageUrl") or "",
                    "price_min": float(offer.get("priceMin") or 0),
                    "price_max": float(offer.get("priceMax") or 0),
                    "discount_pct": int(offer.get("priceDiscountRate") or 0),
                    "rating": float(offer.get("ratingStar") or 0),
                    "sales": int(offer.get("sales") or 0),
                    "category_name": offer.get("_source_bucket", "Shopee"),
                    "preview_message": preview_msg,
                    "raw_offer": offer,
                }
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "elapsed_ms": elapsed_ms,
            "raw_cat_count": len(valid_categories_pool),
            "raw_gen_count": len(valid_general_pool),
            "skipped_duplicates": stats.get("skipped_duplicates", 0),
            "accepted_categories": stats.get("accepted_categories", {}),
            "items": items_formatted,
        }

    def enqueue_simulated_shopee_item(
        self, cand: dict, target_date: str, priority: int = 0
    ) -> bool:
        try:
            raw_offer = cand.get("raw_offer") or {}
            product_url = cand["product_url"]
            aff_link = (
                cand.get("affiliate_url")
                or self.generate_short_link(product_url)
                or product_url
            )
            msg = self.format_product_message(raw_offer, aff_link, include_ai_hook=True)
            add_to_queue(
                store="Shopee",
                product_url=product_url,
                message=msg,
                media_url=cand.get("image_url"),
                media_type="image",
                priority=priority,
                target_date=target_date,
                category=cand.get("category_name", "Shopee: Laboratório"),
            )
            return True
        except Exception as e:
            logger.error(f"Erro ao enfileirar item do Laboratório Shopee: {e}")
            return False

    def generate_auto_offers_to_queue(
        self,
        count: int = 15,
        target_date: str | None = None,
        keywords_csv: str | None = None,
    ) -> int:
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

        global_blocked = get_config("dedup_blocked_terms") or ""
        shopee_blocked = get_config("shopee_blocked_terms") or ""
        combined_blocked = [
            t.strip()
            for t in re.split(r"[,;\n]+", f"{global_blocked},{shopee_blocked}")
            if t.strip()
        ]

        dedup_days = int(get_config("dedup_history_days") or "7")
        dedup_threshold = float(get_config("dedup_similarity_threshold") or "60")

        general_target = round(count * (general_pct / 100.0))
        if general_pct > 0 and count >= 2 and general_target == 0:
            general_target = 1
        categories_target = max(0, count - general_target)

        logger.info(
            f"🔎 Iniciando varredura paginada na Shopee: meta={count} ({general_target} Geral / {categories_target} Categorias)..."
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
                blocked_terms=combined_blocked,
            )
        )

        logger.info(
            f"🎯 Pools de inéditos prontos para sorteio: {len(valid_categories_pool)} de Categorias | {len(valid_general_pool)} Gerais."
        )

        chosen_categories = valid_categories_pool[:categories_target]
        remaining_cat = valid_categories_pool[categories_target:]

        chosen_general = valid_general_pool[:general_target]
        remaining_gen = valid_general_pool[general_target:]

        total_so_far = len(chosen_general) + len(chosen_categories)
        if total_so_far < count:
            missing = count - total_so_far
            leftovers = remaining_cat + remaining_gen
            chosen_categories.extend(leftovers[:missing])

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
                b_name = (backup_offer.get("productName") or "").strip()
                if self._is_title_blocked(b_name, combined_blocked):
                    continue
                b_url = (
                    backup_offer.get("productLink")
                    or backup_offer.get("offerLink")
                    or f"https://shopee.com.br/product/0/{b_id}"
                )
                if is_offer_recent(
                    b_url, item_id=b_id, days=1, target_date=target_date
                ):
                    continue
                already_chosen_ids.add(b_id)
                chosen_categories.append(backup_offer)

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

    def get_sales_report(self, days: int = 30) -> dict:
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
