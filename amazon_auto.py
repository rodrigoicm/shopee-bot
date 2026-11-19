"""
Motor de busca automática da Amazon Brasil (Bestsellers + Deals).
- Deals: 1 request à página configurada → JSON completo filtrado por categoria real
- Bestsellers: 1 request à página principal + enriquecimento paralelo via /dp/{asin}
- Simulação (dry-run) para o laboratório
- Conversão + enfileiramento com detecção real de fallback (link longo vs amzn.to)
"""

import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import amazon_service
import offer_comparator
from database import DB_NAME, add_to_queue, get_config

load_dotenv()

logger = logging.getLogger(__name__)
CUIABA_TZ = timezone(timedelta(hours=-4))

BESTSELLERS_URL = "https://www.amazon.com.br/gp/bestsellers"
DEALS_URL = "https://www.amazon.com.br/deals"

# ─────────────────────────────────────────────────────────────────────────
# Categorias (slug unificado). best=None → só via Deals
#                                deal=None → só via Bestsellers
# ─────────────────────────────────────────────────────────────────────────
AMAZON_CATEGORIES = {
    "eletronicos": {
        "label": "Eletrônicos",
        "best": "electronics",
        "deal": "deals-collection-eletronicos",
    },
    "eletrodomesticos": {
        "label": "Eletrodomésticos",
        "best": "appliances",
        "deal": "deals-collection-eletro",
    },
    "celulares": {
        "label": "Celulares",
        "best": None,
        "deal": "deals-collection-celular",
    },
    "computadores": {
        "label": "Computadores e Informática",
        "best": "computers",
        "deal": "deals-collection-computadores",
    },
    "casa": {"label": "Casa", "best": "home", "deal": "deals-collection-casa"},
    "cozinha": {
        "label": "Cozinha",
        "best": "kitchen",
        "deal": "deals-collection-cozinha",
    },
    "moveis": {
        "label": "Móveis",
        "best": "furniture",
        "deal": "deals-collection-moveis",
    },
    "ferramentas": {
        "label": "Ferramentas e Construção",
        "best": "hi",
        "deal": "deals-collection-tools",
    },
    "beleza": {"label": "Beleza", "best": "beauty", "deal": "deals-collection-beauty"},
    "saude": {
        "label": "Saúde e Bem-Estar",
        "best": "hpc",
        "deal": "deals-collection-personal-care",
    },
    "moda": {"label": "Moda", "best": "fashion", "deal": "deals-collection-fashion"},
    "esporte": {
        "label": "Esporte",
        "best": "sports",
        "deal": "deals-collection-sports",
    },
    "brinquedos": {
        "label": "Brinquedos e Jogos",
        "best": "toys",
        "deal": "deals-collection-toys-and-games",
    },
    "bebes": {
        "label": "Bebês",
        "best": "baby-products",
        "deal": "deals-collection-baby",
    },
    "petshop": {
        "label": "Pet Shop",
        "best": "pet-products",
        "deal": "deals-collection-petshophub",
    },
    "livros": {"label": "Livros", "best": "books", "deal": "deals-collection-books"},
    "alimentos": {
        "label": "Alimentos e Bebidas",
        "best": "grocery",
        "deal": "deals-collection-grocery",
    },
    "automotivo": {
        "label": "Automotivo",
        "best": "automotive",
        "deal": "deals-collection-automotive",
    },
    "games": {
        "label": "Games e Consoles",
        "best": "videogames",
        "deal": "deals-collection-game",
    },
    "instrumentos": {
        "label": "Instrumentos Musicais",
        "best": "musical-instruments",
        "deal": None,
    },
    "jardim": {
        "label": "Jardim e Piscina",
        "best": "lawn-and-garden",
        "deal": "deals-collection-jardim",
    },
    "papelaria": {"label": "Papelaria e Escritório", "best": "office", "deal": None},
    "dispositivos": {
        "label": "Dispositivos Amazon",
        "best": "amazon-devices",
        "deal": None,
    },
    "cd_vinil": {"label": "CD e Vinil", "best": "music", "deal": None},
}

DEFAULT_CATEGORIES = "eletronicos, celulares, cozinha, casa, beleza, ferramentas"

# ─── Mapeamento para inferir categoria a partir do próprio produto (Deals) ───
PRODUCT_TYPE_TO_SLUG = {
    "INCONTINENCE_PROTECTOR": "bebes",
    "SHAMPOO": "beleza",
    "SKIN_MOISTURIZER": "beleza",
    "CLEANING_AGENT": "casa",
    "TOILET_PAPER": "casa",
    "AIR_FRYER": "cozinha",
    "TELEVISION": "eletronicos",
    "CELLULAR_PHONE": "celulares",
    "NOTEBOOK_COMPUTER": "computadores",
    "ELECTRIC_FAN": "casa",
    "WASHER": "eletrodomesticos",
    "AIR_CONDITIONER": "eletrodomesticos",
    "SPIRITS": "alimentos",
    "ANIMAL_LITTER": "petshop",
}

PRODUCT_SYMBOL_TO_SLUG = {
    "gl_kitchen": "cozinha",
    "gl_home": "casa",
    "gl_home_entertainment": "eletronicos",
    "gl_major_appliances": "eletrodomesticos",
    "gl_wireless": "celulares",
    "gl_pc": "computadores",
    "gl_wine": "alimentos",
    "gl_luxury_beauty": "beleza",
    "gl_pet_products": "petshop",
    "gl_books": "livros",
    "gl_toys": "brinquedos",
    "gl_sports": "esporte",
    "gl_automotive": "automotivo",
    "gl_furniture": "moveis",
    "gl_office_products": "papelaria",
    "gl_shoes": "moda",
    "gl_apparel": "moda",
    "gl_watches": "moda",
    "gl_jewelry": "moda",
    "gl_lawn_and_garden": "jardim",
    "gl_video_games": "games",
    "gl_musical_instruments": "instrumentos",
}

DEPT_ID_TO_SLUG = {
    "18991079011": "alimentos",
    "18991080011": "alimentos",
    "19778003011": "alimentos",
    "18914209011": "automotivo",
    "18914210011": "automotivo",
    "17242603011": "bebes",
    "17242604011": "bebes",
    "16194414011": "beleza",
    "16194415011": "beleza",
    "24068399011": "beleza",
    "16194300011": "brinquedos",
    "16194301011": "brinquedos",
    "16191000011": "casa",
    "16191001011": "casa",
    "16339926011": "computadores",
    "16339927011": "computadores",
    "16957125011": "cozinha",
    "16957126011": "cozinha",
    "16333487011": "dispositivos",
    "16522082011": "eletrodomesticos",
    "16522083011": "eletrodomesticos",
    "17124722011": "eletrodomesticos",
    "16209062011": "eletronicos",
    "16209063011": "eletronicos",
    "16243809011": "eletronicos",
    "16243822011": "eletronicos",
    "7791985011": "games",
    "7791986011": "games",
    "17349396011": "esporte",
    "17349397011": "esporte",
    "16957182011": "ferramentas",
    "16957183011": "ferramentas",
    "18991136011": "petshop",
    "18991137011": "petshop",
    "16957240011": "papelaria",
    "17095637011": "papelaria",
    "17095641011": "papelaria",
    "17095642011": "papelaria",
    "17095643011": "papelaria",
    "6740748011": "livros",
    "7841278011": "livros",
    "5475882011": "livros",
    "5308308011": "livros",
    "17365811011": "moda",
    "17365812011": "moda",
    "18991021011": "jardim",
    "18991022011": "jardim",
    "18991253011": "instrumentos",
    "17100530011": "moveis",
    "17100544011": "saude",
    "18460204011": "saude",
    "16215417011": "saude",
    "16215418011": "saude",
    "16769374011": "saude",
    "16769375011": "saude",
}


def _infer_deals_category(product: dict) -> str | None:
    """Descobre a categoria real do produto a partir do próprio JSON da Amazon."""
    ptype = (product.get("_productType") or "").upper()
    if ptype in PRODUCT_TYPE_TO_SLUG:
        return PRODUCT_TYPE_TO_SLUG[ptype]

    symbol = (product.get("_productSymbol") or "").lower()
    if symbol in PRODUCT_SYMBOL_TO_SLUG:
        return PRODUCT_SYMBOL_TO_SLUG[symbol]

    for dept in reversed(product.get("_departmentIds") or []):
        if dept in DEPT_ID_TO_SLUG:
            return DEPT_ID_TO_SLUG[dept]

    return None


# ─────────────────────────────────────────────────────────────────────────
# Parsers
# ─────────────────────────────────────────────────────────────────────────
def _brl_to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"R\$\s*([\d.]+(?:,\d{2})?)", raw)
    if not m:
        return None
    return amazon_service._parse_brl_float(m.group(1))


def _parse_bestsellers_card(card_html: str) -> dict | None:
    m_asin = re.search(r'data-asin="([A-Z0-9]{10})"', card_html)
    if not m_asin:
        return None
    asin = m_asin.group(1)

    m_title = re.search(
        r'class="p13n-sc-truncate[^"]*"[^>]*>(.*?)</div>', card_html, re.DOTALL
    )
    title = re.sub(r"<[^>]+>", "", m_title.group(1)).strip() if m_title else None

    m_price = re.search(
        r'class="_cDEzb_p13n-sc-price_[^"]*">\s*(R\$[\s\d.,]+)</span>', card_html
    )
    current_price = _brl_to_float(m_price.group(1)) if m_price else None

    m_rating = re.search(
        r'<span class="a-icon-alt">([\d,]+) de 5 estrelas</span>', card_html
    )
    rating = float(m_rating.group(1).replace(",", ".")) if m_rating else None

    m_reviews = re.search(r'<span class="a-size-small">([\d.]+)</span>', card_html)
    reviews = int(m_reviews.group(1).replace(".", "")) if m_reviews else 0

    image_url = None
    m_img = re.search(r'data-a-dynamic-image="([^"]+)"', card_html)
    if m_img:
        try:
            dyn = json.loads(m_img.group(1).replace("&quot;", '"'))
            if isinstance(dyn, dict) and dyn:
                image_url = max(dyn.keys(), key=lambda k: dyn[k][0] * dyn[k][1])
        except Exception:
            pass
    if not image_url:
        m_src = re.search(r'<img[^>]+src="([^"]+)"', card_html)
        if m_src:
            image_url = m_src.group(1)

    if not title or not current_price:
        return None

    return {
        "asin": asin,
        "title": title,
        "image_url": image_url,
        "current_price": current_price,
        "original_price": None,
        "discount_pct": None,
        "rating": rating,
        "reviews": reviews,
        "product_url": f"https://www.amazon.com.br/dp/{asin}",
    }


def _parse_bestsellers_carousels(html_text: str) -> dict[str, list[dict]]:
    """
    Retorna {slug_categoria: [cards]}
    Detecta carrosséis via data-reftagprefix="zg_bs_c_{slug}"
    """
    result: dict[str, list[dict]] = {}
    for m in re.finditer(r'data-reftagprefix="zg_bs_c_([a-z0-9-]+)"', html_text):
        slug = m.group(1)
        end = html_text.find('data-reftagprefix="zg_bs_c_', m.end())
        chunk = html_text[m.end() : end if end != -1 else len(html_text)]
        cards = []
        for cm in re.finditer(
            r'<div data-asin="([A-Z0-9]{10})">(.*?)(?=<div data-asin=|<li aria-roledescription|$)',
            chunk,
            re.DOTALL,
        ):
            parsed = _parse_bestsellers_card(cm.group(0))
            if parsed:
                cards.append(parsed)
        if cards:
            result[slug] = cards
    return result


def _parse_deals_json(html_text: str) -> list[dict]:
    """Extrai productSearchResponse.products + metadados de categoria."""
    marker = "assets.mountWidget('slot-7', "
    idx = html_text.find(marker)
    if idx == -1:
        return []
    start = idx + len(marker)
    try:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(html_text, start)
    except Exception as e:
        logger.warning(f"Falha parse JSON deals: {e}")
        return []

    products = data.get("productSearchResponse", {}).get("products", [])
    out = []
    for p in products:
        asin = p.get("asin")
        if not asin:
            continue

        img = p.get("image", {})
        img_hi = img.get("hiRes", {}).get("baseUrl")
        img_ext = img.get("hiRes", {}).get("extension", "jpg")
        image_url = (
            f"{img_hi}.{img_ext}" if img_hi else img.get("lowRes", {}).get("baseUrl")
        )

        price_block = p.get("price", {})
        cur = price_block.get("priceToPay", {}).get("price")
        orig = price_block.get("basisPrice", {}).get("price")
        current_price = float(cur) if cur else None
        original_price = float(orig) if orig else None

        disc_frags = (
            p.get("dealBadge", {})
            .get("label", {})
            .get("content", {})
            .get("fragments", [])
        )
        discount_pct = None
        for frag in disc_frags:
            m = re.search(r"(\d+)%", frag.get("text", ""))
            if m:
                discount_pct = int(m.group(1))
                break

        reviews_block = p.get("customerReviews", {})
        rating = None
        rs = reviews_block.get("rating", {}).get("shortDisplayString")
        if rs:
            try:
                rating = float(rs.replace(",", "."))
            except Exception:
                pass
        reviews = reviews_block.get("count", {}).get("value", 0)

        meta = p.get("meta", {})
        pc = p.get("productCategory", {}) or {}

        out.append(
            {
                "asin": asin,
                "title": p.get("title"),
                "image_url": image_url,
                "current_price": current_price,
                "original_price": original_price,
                "discount_pct": discount_pct,
                "rating": rating,
                "reviews": reviews,
                "product_url": f"https://www.amazon.com.br/dp/{asin}",
                "_productType": pc.get("productType"),
                "_productSymbol": pc.get("symbol"),
                "_departmentIds": meta.get("departmentIds") or [],
            }
        )
    return out


# ─────────────────────────────────────────────────────────────────────────
# Coleta
# ─────────────────────────────────────────────────────────────────────────
def fetch_bestsellers_candidates(selected_slugs: list[str]) -> list[dict]:
    headers = amazon_service._build_amazon_headers(with_cookie=True)
    url = get_config("amazon_bestsellers_url") or BESTSELLERS_URL
    try:
        resp = requests.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Bestsellers fetch falhou: {e}")
        return []

    by_cat = _parse_bestsellers_carousels(resp.text)
    out = []
    best_slugs = {
        AMAZON_CATEGORIES[s]["best"]
        for s in selected_slugs
        if s in AMAZON_CATEGORIES and AMAZON_CATEGORIES[s]["best"]
    }
    for slug, cards in by_cat.items():
        if slug not in best_slugs:
            continue
        unified = next(
            (k for k, v in AMAZON_CATEGORIES.items() if v["best"] == slug),
            slug,
        )
        for c in cards:
            c["category_slug"] = unified
            c["source"] = "bestsellers"
            out.append(c)
    return out


def fetch_deals_candidates(selected_slugs: list[str]) -> list[dict]:
    """
    Busca Deals UMA ÚNICA VEZ e filtra localmente pela categoria real do produto.
    Se a URL configurada (ex: /blackfriday fora de época) não trouxer o slot-7,
    tenta automaticamente a URL oficial /deals como fallback!
    """
    headers = amazon_service._build_amazon_headers(with_cookie=True)
    url = get_config("amazon_deals_url") or DEALS_URL
    raw = []

    for candidate_url in dict.fromkeys([url, DEALS_URL]):
        try:
            resp = requests.get(candidate_url, headers=headers, timeout=25)
            if resp.status_code == 200:
                raw = _parse_deals_json(resp.text)
                if raw:
                    break
        except Exception as e:
            logger.warning(f"Deals erro ({candidate_url}): {e}")

    logger.info(f"Deals: {len(raw)} produtos no catálogo bruto")

    selected_set = set(selected_slugs) if selected_slugs else None
    out = []
    unmatched = 0
    for p in raw:
        cat = _infer_deals_category(p)
        if not cat:
            unmatched += 1
            continue
        if selected_set and cat not in selected_set:
            continue
        p["category_slug"] = cat
        p["source"] = "deals"
        out.append(p)

    logger.info(
        f"Deals: {len(out)} produtos após filtro de categoria "
        f"({unmatched} sem categoria inferida)"
    )
    return out


def enrich_bestsellers(candidates: list[dict], max_workers: int = 5) -> list[dict]:
    """Preenche original_price, discount_pct, installments via /dp/{asin}."""

    def _enrich_one(c: dict) -> dict:
        try:
            headers = amazon_service._build_amazon_headers(with_cookie=True)
            resp = requests.get(c["product_url"], headers=headers, timeout=15)
            if (
                resp.status_code == 200
                and "api-services-support@amazon.com" not in resp.text
            ):
                info = amazon_service.scrape_amazon_product_html(
                    resp.text, asin=c["asin"]
                )
                if info.get("current_price"):
                    c["current_price"] = info["current_price"]
                if info.get("original_price"):
                    c["original_price"] = info["original_price"]
                if info.get("discount_pct"):
                    c["discount_pct"] = info["discount_pct"]
                if info.get("installments_text"):
                    c["installments_text"] = info["installments_text"]
                if info.get("image_url"):
                    c["image_url"] = info["image_url"]
        except Exception as e:
            logger.debug(f"Enrich {c['asin']} falhou: {e}")
        return c

    enriched = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_enrich_one, c) for c in candidates]
        for f in as_completed(futures):
            try:
                enriched.append(f.result())
            except Exception:
                pass
    return enriched


# ─────────────────────────────────────────────────────────────────────────
# Simulação (dry-run)
# ─────────────────────────────────────────────────────────────────────────
def simulate_amazon_offers_selection(
    count: int = 10,
    deals_pct: int | None = None,
    selected_categories: list[str] | None = None,
) -> dict:
    t0 = time.perf_counter()

    if deals_pct is None:
        deals_pct = int(get_config("amazon_bestsellers_pct") or "60")

    if selected_categories is None:
        raw = get_config("amazon_target_categories") or DEFAULT_CATEGORIES
        selected_categories = [s.strip() for s in raw.split(",") if s.strip()]

    deals_pct = max(0, min(100, deals_pct))
    target_deals = round(count * deals_pct / 100)
    if deals_pct > 0 and count >= 2 and target_deals == 0:
        target_deals = 1
    target_best = max(0, count - target_deals)

    deals_raw = fetch_deals_candidates(selected_categories)
    best_raw = fetch_bestsellers_candidates(selected_categories)

    if best_raw:
        best_raw = enrich_bestsellers(best_raw)

    accepted_cats: dict[str, int] = {}
    for c in deals_raw + best_raw:
        accepted_cats[c["category_slug"]] = accepted_cats.get(c["category_slug"], 0) + 1

    dedup_days = int(
        get_config("amazon_history_days") or get_config("dedup_history_days") or "7"
    )
    preloaded = offer_comparator.load_recent_queue_profiles(days=dedup_days)
    dedup_thresh = float(get_config("dedup_similarity_threshold") or "60")

    random.shuffle(deals_raw)
    random.shuffle(best_raw)

    seen_asins: set[str] = set()
    accepted_titles: list[str] = []
    skipped = 0

    def _pick(pool, needed):
        nonlocal skipped
        picked = []
        for item in pool:
            if len(picked) >= needed:
                break
            if item["asin"] in seen_asins:
                continue
            seen_asins.add(item["asin"])
            is_dup, _ = offer_comparator.is_product_recently_used_or_blocked(
                candidate_title=item["title"],
                candidate_url=item["product_url"],
                days=dedup_days,
                threshold=dedup_thresh,
                extra_accepted_titles=accepted_titles,
                preloaded_db_profiles=preloaded,
            )
            if is_dup:
                skipped += 1
                continue
            accepted_titles.append(item["title"])
            item["preview_message"] = amazon_service.format_product_message(
                {
                    "title": item["title"],
                    "current_price": item["current_price"],
                    "original_price": item.get("original_price"),
                    "discount_pct": item.get("discount_pct"),
                    "installments_text": item.get("installments_text"),
                    "affiliate_url": "https://www.amazon.com.br/dp/PREVIA_SIMULACAO",
                }
            )
            picked.append(item)
        return picked

    chosen_deals = _pick(deals_raw, target_deals)
    chosen_best = _pick(best_raw, target_best)

    total = len(chosen_deals) + len(chosen_best)
    if total < count:
        missing = count - total
        chosen_best.extend(_pick(best_raw, missing))
        total = len(chosen_deals) + len(chosen_best)
        if total < count:
            chosen_deals.extend(_pick(deals_raw, count - total))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "elapsed_ms": elapsed_ms,
        "raw_deals_count": len(deals_raw),
        "raw_bestsellers_count": len(best_raw),
        "accepted_categories": accepted_cats,
        "blocked_categories": [],
        "skipped_duplicates": skipped,
        "chosen_deals_count": len(chosen_deals),
        "chosen_bestsellers_count": len(chosen_best),
        "items": chosen_deals + chosen_best,
    }


# ─────────────────────────────────────────────────────────────────────────
# Conversão + Enfileiramento (com detecção real de fallback de link longo)
# ─────────────────────────────────────────────────────────────────────────
_last_fallbacks: list[str] = []


def get_last_fallbacks() -> list[str]:
    return list(_last_fallbacks)


def clear_last_fallbacks() -> None:
    global _last_fallbacks
    _last_fallbacks = []


def convert_and_enqueue_amazon_item(
    asin: str,
    target_date: str,
    category_name: str = "Amazon: Auto",
    fallback_data: dict | None = None,
    priority: int = 0,
) -> bool:
    """
    Converte um ASIN em oferta (SiteStripe → link curto) e enfileira.
    Se o SiteStripe falhar, cai para link longo com tag e registra o ASIN em `_last_fallbacks`.
    Se o parser não trouxer preço, mescla com `fallback_data` (dados do lab).
    """
    global _last_fallbacks
    product_url = f"https://www.amazon.com.br/dp/{asin}"

    try:
        offer = amazon_service._build_amazon_offer_sync(product_url)
    except Exception as e:
        logger.error(f"_build_amazon_offer_sync falhou para {asin}: {e}")
        offer = None

    # ─── Caso 1: Falha total ao montar dict `offer` → fallback completo ───
    if not offer or not offer.get("affiliate_url"):
        partner_tag = amazon_service.get_partner_tag()
        fallback_url = f"https://www.amazon.com.br/dp/{asin}?tag={partner_tag}"
        base = fallback_data or {}
        msg = amazon_service.format_product_message(
            {
                "title": base.get("title", f"Produto Amazon ({asin})"),
                "current_price": base.get("current_price"),
                "original_price": base.get("original_price"),
                "discount_pct": base.get("discount_pct"),
                "installments_text": base.get("installments_text"),
                "coupon_badge": base.get("coupon_badge"),
                "affiliate_url": fallback_url,
            }
        )
        add_to_queue(
            store="Amazon",
            product_url=product_url,
            message=msg,
            media_url=base.get("image_url"),
            media_type="image",
            priority=priority,
            target_date=target_date,
            category=f"{category_name} (fallback)",
        )
        _last_fallbacks.append(asin)
        return True

    # ─── Caso 2: Mescla com fallback_data se algum dado da página veio vazio ───
    if fallback_data:
        merged_fields = []
        for field in (
            "title",
            "current_price",
            "original_price",
            "discount_pct",
            "installments_text",
            "coupon_badge",
            "image_url",
        ):
            if not offer.get(field) and fallback_data.get(field):
                offer[field] = fallback_data[field]
                merged_fields.append(field)
        if merged_fields:
            logger.info(
                f"🔀 Amazon {asin}: campos preenchidos via fallback_data → {merged_fields}"
            )

    if offer.get("current_price") is None:
        logger.warning(
            f"⚠️ Amazon {asin}: sem current_price mesmo após merge. "
            f"title={offer.get('title')!r}"
        )

    # ─── Verifica se o link gerado foi realmente encurtado (amzn.to) ou caiu em link longo ───
    if not amazon_service.is_short_amazon_link(offer.get("affiliate_url")):
        _last_fallbacks.append(asin)
        category_name = f"{category_name} (fallback)"

    msg = amazon_service.format_product_message(offer)
    add_to_queue(
        store="Amazon",
        product_url=product_url,
        message=msg,
        media_url=offer.get("image_url") or (fallback_data or {}).get("image_url"),
        media_type="image",
        priority=priority,
        target_date=target_date,
        category=category_name,
    )
    return True


def generate_amazon_auto_offers_to_queue(
    count: int = 10, target_date: str | None = None
) -> dict:
    """Chamado pelo orquestrador e/ou auto-refill. Retorna {'added': int, 'fallbacks': [...]}"""
    clear_last_fallbacks()

    if not target_date:
        target_date = datetime.now(CUIABA_TZ).strftime("%Y-%m-%d")

    if get_config("amazon_auto_enabled") != "1":
        return {"added": 0, "fallbacks": []}

    sim = simulate_amazon_offers_selection(count=count + 3)
    added = 0
    for cand in sim["items"]:
        if added >= count:
            break
        ok = convert_and_enqueue_amazon_item(
            asin=cand["asin"],
            target_date=target_date,
            category_name=f"Amazon: {AMAZON_CATEGORIES.get(cand['category_slug'], {}).get('label', 'Geral')}",
            fallback_data=cand,
            priority=0,
        )
        if ok:
            added += 1

    logger.info(
        f"✅ Amazon auto: {added}/{count} enfileiradas. "
        f"Fallbacks (link longo): {len(_last_fallbacks)}"
    )
    return {"added": added, "fallbacks": list(_last_fallbacks)}
