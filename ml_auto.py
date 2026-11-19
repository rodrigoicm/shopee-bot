"""
Motor de Busca Automática e Laboratório de Validação do Mercado Livre.
100% parametrizado via banco de dados (Portal Web) — sem filtros fixos no código.
"""

import html as html_lib
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from database import DB_NAME, add_to_queue, get_config, update_config
from ml_affiliate import clean_url, create_affiliate_link_full
from ml_social import build_ml_image_url
import ml_offer
import offer_comparator
from shopee_service import ShopeeAffiliateAPI

load_dotenv()

logger = logging.getLogger(__name__)
CUIABA_TZ = timezone(timedelta(hours=-4))

KNOWN_ML_CATEGORIES = [
    "Games",
    "Informática",
    "Acessórios para Veículos",
    "Alimentos e Bebidas",
    "Arte, Papelaria e Armarinho",
    "Bebês",
    "Beleza e Cuidado Pessoal",
    "Brinquedos e Hobbies",
    "Calçados, Roupas e Bolsas",
    "Casa, Móveis e Decoração",
    "Celulares e Telefones",
    "Construção",
    "Câmeras e Acessórios",
    "Eletrodomésticos",
    "Eletrônicos, Áudio e Vídeo",
    "Esportes e Fitness",
    "Ferramentas",
    "Festas e Lembrancinhas",
    "Indústria e Comércio",
    "Instrumentos Musicais",
    "Joias e Relógios",
    "Livros, Revistas e Comics",
    "Pet Shop",
    "Saúde",
]


def ensure_ml_auto_schema() -> None:
    """Garante a tabela ml_history e inicializa as configurações no banco (editáveis no Portal)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ml_history (
            item_key TEXT PRIMARY KEY,
            product_url TEXT,
            title TEXT,
            source_type TEXT,
            category_name TEXT,
            created_at TEXT
        )
        """)

    defaults = {
        "ml_auto_enabled": "0",
        "mix_shopee_pct": "60",
        "ml_best_sellers_pct": "30",
        "ml_history_days": "7",
        "ml_excluded_categories_bestsellers": "Games, Informática",
        "ml_excluded_categories_offers": "",
        "ml_bestsellers_url": "https://www.mercadolivre.com.br/mais-vendidos",
        "ml_offers_url": (
            "https://www.mercadolivre.com.br/ofertas?promotion_type=lightning\n"
            "https://www.mercadolivre.com.br/ofertas"
        ),
        "ml_min_price": "20",
        "ml_max_price": "350",
        "ml_min_discount": "12",
        "ml_pages_per_run": "4",
        "ml_max_page_pool": "15",
    }

    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )

    conn.commit()
    conn.close()


ensure_ml_auto_schema()


def _build_ml_headers() -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    cookie_env = (os.environ.get("ML_AFFILIATE_COOKIE") or "").strip().strip("'\"")
    if cookie_env:
        headers["Cookie"] = cookie_env
    return headers


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def parse_csv_list(raw_csv: str | None) -> list[str]:
    if not raw_csv:
        return []
    return [item.strip() for item in re.split(r"[,;\n]+", raw_csv) if item.strip()]


def extract_ml_item_key(url: str, metadata: dict | None = None) -> str:
    if metadata:
        for k in ("product_id", "user_product_id", "id", "pid"):
            val = metadata.get(k)
            if val:
                return str(val).upper()

    if not url:
        return ""

    m_cat = re.search(r"/p/(MLB\d+)", url, re.IGNORECASE)
    if m_cat:
        return m_cat.group(1).upper()

    m_up = re.search(r"/up/(MLBU\d+)", url, re.IGNORECASE)
    if m_up:
        return m_up.group(1).upper()

    m_item = re.search(r"(MLB-?\d+)", url, re.IGNORECASE)
    if m_item:
        return m_item.group(1).replace("-", "").upper()

    return clean_url(url)


def _extract_product_core_signature(title: str) -> str:
    """
    Extrai as 4 primeiras palavras substantivas do título (ignorando voltagem, cor e medidas)
    para barrar o mesmo produto anunciado por vendedores diferentes ou em variações 110v/220v.
    """
    norm = _normalize_text(title)
    norm = re.sub(r"\b\d+(?:ml|l|g|kg|w|v|cm|mm|m|un|pcs|pecas|x|bivolt)\b", " ", norm)
    norm = re.sub(r"[^a-z0-9\s]", " ", norm)
    stopwords = {
        "kit",
        "com",
        "para",
        "por",
        "mais",
        "super",
        "oferta",
        "original",
        "novo",
        "nova",
        "promocao",
        "barato",
        "envio",
        "full",
        "rapido",
        "preto",
        "branco",
        "azul",
        "rosa",
        "vermelho",
        "cinza",
        "inox",
        "bivolt",
        "110v",
        "220v",
        "127v",
        "masculino",
        "feminino",
        "unissex",
        "de",
        "da",
        "do",
        "em",
        "no",
        "na",
        "dos",
        "das",
        "e",
        "ou",
    }
    words = [
        w
        for w in norm.split()
        if len(w) >= 3 and w not in stopwords and not w.isdigit()
    ]
    return " ".join(words[:4])


def _load_ml_history_blocklist(days: int = 7) -> tuple[set[str], set[str], list[str]]:
    """Lê ml_history e queue dos últimos N dias para impedir repetição de produtos no ML."""
    cutoff = (datetime.now(CUIABA_TZ) - timedelta(days=max(1, days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    used_keys: set[str] = set()
    used_cores: set[str] = set()
    used_titles: list[str] = []

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    try:
        rows_h = cur.execute(
            "SELECT item_key, title, product_url FROM ml_history WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        for ik, title, purl in rows_h:
            if ik:
                used_keys.add(str(ik).upper())
            if purl:
                used_keys.add(extract_ml_item_key(purl))
            if title:
                used_titles.append(title)
                core = _extract_product_core_signature(title)
                if len(core.split()) >= 3:
                    used_cores.add(core)
    except Exception as e:
        logger.debug(f"Aviso ao ler ml_history: {e}")

    try:
        rows_q = cur.execute(
            "SELECT product_url, formatted_message FROM queue WHERE created_at >= ?",
            (cutoff.split(" ")[0],),
        ).fetchall()
        for purl, msg in rows_q:
            if purl:
                ik = extract_ml_item_key(purl)
                if ik:
                    used_keys.add(ik)
            if msg:
                m_t = re.search(r"🔥\s*(?:<b>|\*)?([^\n<*]+)", msg)
                if m_t:
                    t_clean = m_t.group(1).strip()
                    used_titles.append(t_clean)
                    core = _extract_product_core_signature(t_clean)
                    if len(core.split()) >= 3:
                        used_cores.add(core)
    except Exception as e:
        logger.debug(f"Aviso ao ler queue para ml_history: {e}")

    conn.close()
    return used_keys, used_cores, used_titles


def _extract_nordic_ctx(html_text: str) -> dict | None:
    for marker in ("_n.ctx.r=", "_n.ctx.r ="):
        idx = html_text.find(marker)
        if idx != -1:
            start = html_text.find("{", idx)
            if start != -1:
                try:
                    decoder = json.JSONDecoder()
                    data, _ = decoder.raw_decode(html_text, start)
                    return data
                except Exception as e:
                    logger.warning(f"Falha ao decodificar _n.ctx.r: {e}")
    return None


def _parse_polycard(
    polycard: dict, source_type: str = "offers", category_name: str = ""
) -> dict | None:
    if not isinstance(polycard, dict):
        return None

    metadata = polycard.get("metadata") or {}
    raw_url = metadata.get("url") or ""
    if not raw_url:
        return None

    url_params = metadata.get("url_params") or ""
    full_url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
    cleaned_url = clean_url(full_url)
    if "searchVariation" in url_params and "?" not in cleaned_url:
        product_url_full = f"{cleaned_url}{url_params}"
    else:
        product_url_full = cleaned_url

    item_key = extract_ml_item_key(cleaned_url, metadata)

    pic_id = None
    pics_list = (polycard.get("pictures") or {}).get("pictures") or []
    if pics_list and isinstance(pics_list, list):
        pic_id = pics_list[0].get("id")
    image_url = build_ml_image_url(pic_id)

    title = None
    seller = None
    current_price = None
    original_price = None
    regular_price = None
    pix_price = None
    price_note = None
    discount_label = None
    installments_text = None
    installments_no_interest = None
    has_shipping_info = True

    for comp in polycard.get("components") or []:
        if not isinstance(comp, dict):
            continue
        c_type = comp.get("type")

        if c_type == "title":
            raw_t = (comp.get("title") or {}).get("text")
            title = html_lib.unescape(raw_t) if raw_t else None

        elif c_type == "seller":
            seller_obj = comp.get("seller") or {}
            seller_text = seller_obj.get("text") or ""
            for v in seller_obj.get("values") or []:
                if v.get("key") == "label":
                    seller = (v.get("label") or {}).get("text")
                    break
            if not seller and seller_text:
                clean_s = re.sub(r"\{[^}]+\}", "", seller_text).strip()
                if clean_s:
                    seller = clean_s

        elif c_type == "price":
            p_obj = comp.get("price") or {}
            cur_obj = p_obj.get("current_price") or {}
            current_price = cur_obj.get("value")

            for pl in p_obj.get("price_labels") or []:
                for val in pl.get("values") or []:
                    if val.get("key") == "previous_price":
                        original_price = (val.get("price") or {}).get("value")

            disc_poly = p_obj.get("discount_polylabel") or {}
            for val in disc_poly.get("values") or []:
                pill_text = (val.get("pill") or {}).get("text")
                if pill_text:
                    discount_label = pill_text.strip()

            unit_desc = (
                (p_obj.get("unit_description") or {}).get("text") or ""
            ).strip()

            inst_obj = p_obj.get("installments") or {}
            inst_template = inst_obj.get("text") or ""
            inst_values = inst_obj.get("values") or []

            if "pix" in unit_desc.lower():
                pix_price = current_price
                price_note = "no Pix"
                regular_price = current_price
                for v in inst_values:
                    if v.get("key") == "price_total":
                        regular_price = (v.get("price") or {}).get(
                            "value"
                        ) or current_price
            else:
                regular_price = current_price
                if unit_desc:
                    price_note = unit_desc

                if inst_template and "outros meios" not in inst_template.lower():
                    rendered = inst_template
                    for v in inst_values:
                        k = v.get("key")
                        if v.get("type") == "price":
                            val_num = (v.get("price") or {}).get("value")
                            if val_num is not None:
                                brl_str = (
                                    f"R$ {val_num:,.2f}".replace(",", "X")
                                    .replace(".", ",")
                                    .replace("X", ".")
                                )
                                rendered = rendered.replace(f"{{{k}}}", brl_str)
                        elif v.get("type") == "label":
                            lbl_str = (v.get("label") or {}).get("text") or ""
                            rendered = rendered.replace(f"{{{k}}}", lbl_str)
                    installments_text = re.sub(r"\s+", " ", rendered).strip()
                    installments_no_interest = bool(inst_obj.get("no_interest"))

        elif c_type == "shipping_v2":
            if comp.get("shipping_v2") is None and source_type == "best_sellers":
                has_shipping_info = False

    if source_type == "best_sellers" and not has_shipping_info:
        return None

    if not title or current_price is None:
        return None

    return {
        "item_key": item_key,
        "product_url": product_url_full,
        "clean_url": cleaned_url,
        "affiliate_url": None,
        "image_url": image_url,
        "title": title,
        "current_price": float(current_price),
        "original_price": float(original_price) if original_price else None,
        "regular_price": (
            float(regular_price) if regular_price else float(current_price)
        ),
        "pix_price": float(pix_price) if pix_price else None,
        "price_note": price_note,
        "discount_label": discount_label,
        "installments_text": installments_text,
        "installments_no_interest": installments_no_interest,
        "seller": seller,
        "source_type": source_type,
        "category_name": category_name or "Mercado Livre",
    }


def _find_carousels_recursively(node, found_list: list):
    if isinstance(node, dict):
        polycards = (
            ((node.get("items") or {}).get("result") or {}).get("polycards")
            if isinstance(node.get("items"), dict)
            else None
        )
        if polycards and isinstance(polycards, list):
            found_list.append(
                {
                    "title": node.get("title") or "",
                    "polycards": polycards,
                }
            )
        else:
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _find_carousels_recursively(v, found_list)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _find_carousels_recursively(item, found_list)


def _find_offer_cards_recursively(node, found_cards: list):
    if isinstance(node, dict):
        if (
            "metadata" in node
            and "components" in node
            and isinstance(node["metadata"], dict)
        ):
            found_cards.append(node)
            return
        for v in node.values():
            if isinstance(v, (dict, list)):
                _find_offer_cards_recursively(v, found_cards)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _find_offer_cards_recursively(item, found_cards)


def fetch_ml_best_sellers_candidates(
    excluded_categories: list[str] | None = None,
) -> tuple[list[dict], list[str], dict[str, int]]:
    if excluded_categories is None:
        excluded_categories = parse_csv_list(
            get_config("ml_excluded_categories_bestsellers") or ""
        )
    excluded_norm = [_normalize_text(c) for c in excluded_categories if c.strip()]

    bestsellers_url = (
        get_config("ml_bestsellers_url")
        or "https://www.mercadolivre.com.br/mais-vendidos"
    ).strip()
    min_price = float(get_config("ml_min_price") or "0")
    max_price = float(get_config("ml_max_price") or "0")

    session = requests.Session()
    resp = session.get(bestsellers_url, headers=_build_ml_headers(), timeout=20)
    resp.raise_for_status()

    ctx = _extract_nordic_ctx(resp.text)
    if not ctx:
        logger.warning("⚠️ _n.ctx.r não encontrado em Mais Vendidos.")
        return [], [], {}

    carousels = []
    _find_carousels_recursively(ctx, carousels)

    candidates: list[dict] = []
    blocked_cats: list[str] = []
    accepted_cats: dict[str, int] = {}

    for car in carousels:
        raw_title = car.get("title") or ""
        cat_clean = re.sub(
            r"^Mais vendidos em\s+", "", raw_title, flags=re.IGNORECASE
        ).strip()
        if not cat_clean:
            cat_clean = "Geral"
        cat_norm = _normalize_text(cat_clean)

        if any(excl in cat_norm or cat_norm in excl for excl in excluded_norm if excl):
            if cat_clean not in blocked_cats:
                blocked_cats.append(cat_clean)
            continue

        count_ok = 0
        for card in car.get("polycards") or []:
            parsed = _parse_polycard(
                card,
                source_type="best_sellers",
                category_name=f"ML: {cat_clean}",
            )
            if not parsed:
                continue

            price_val = parsed.get("current_price") or 0
            if min_price > 0 and price_val < min_price:
                continue
            if max_price > 0 and price_val > max_price:
                continue

            candidates.append(parsed)
            count_ok += 1

        if count_ok > 0:
            accepted_cats[cat_clean] = accepted_cats.get(cat_clean, 0) + count_ok

    return candidates, blocked_cats, accepted_cats


def fetch_ml_offers_candidates(
    offers_url: str | None = None,
    excluded_terms: list[str] | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """
    Coleta produtos das páginas de Ofertas configuradas no Portal:
    - Suporta múltiplas URLs em `ml_offers_url` (separadas por linha ou vírgula)
    - Sorteia `ml_pages_per_run` páginas dentro do alcance `ml_max_page_pool` (ex: páginas de 1 até 20)
    - Se a página não retornar itens (fim da paginação), interrompe aquela URL automaticamente.
    """
    raw_urls_cfg = (offers_url or get_config("ml_offers_url") or "").strip()
    base_urls = [u.strip() for u in re.split(r"[,;\n\r]+", raw_urls_cfg) if u.strip()]
    if not base_urls:
        logger.warning("⚠️ Nenhuma URL configurada em ml_offers_url no Portal.")
        return []

    min_price = float(get_config("ml_min_price") or "0")
    max_price = float(get_config("ml_max_price") or "0")
    min_discount = float(get_config("ml_min_discount") or "0")

    pages_per_run = int(
        max_pages if max_pages is not None else (get_config("ml_pages_per_run") or "4")
    )
    max_page_pool = int(get_config("ml_max_page_pool") or "15")
    pages_per_run = max(1, min(30, pages_per_run))
    max_page_pool = max(pages_per_run, min(50, max_page_pool))

    if excluded_terms is None:
        excluded_terms = parse_csv_list(
            get_config("ml_excluded_categories_offers") or ""
        )
    excluded_norm = [_normalize_text(t) for t in excluded_terms if t.strip()]

    session = requests.Session()
    headers = _build_ml_headers()
    candidates: list[dict] = []
    seen_keys: set[str] = set()

    for base_url in base_urls:
        base_url_clean = base_url.split("#")[0]
        is_lightning = "lightning" in base_url_clean.lower()
        cat_label = "ML: Oferta Relâmpago ⚡" if is_lightning else "ML: Ofertas"

        # Garante sempre a página 1 + sorteia o restante no universo de 2 até max_page_pool
        if max_page_pool <= pages_per_run:
            pages_to_fetch = list(range(1, max_page_pool + 1))
        else:
            extra_pages = random.sample(
                range(2, max_page_pool + 1), k=max(0, pages_per_run - 1)
            )
            pages_to_fetch = [1] + sorted(extra_pages)

        for page_num in pages_to_fetch:
            sep = "&" if "?" in base_url_clean else "?"
            page_url = (
                base_url_clean
                if page_num == 1
                else f"{base_url_clean}{sep}page={page_num}"
            )
            try:
                resp = session.get(page_url, headers=headers, timeout=20)
                if resp.status_code != 200:
                    break
                ctx = _extract_nordic_ctx(resp.text)
                if not ctx:
                    break

                raw_cards = []
                _find_offer_cards_recursively(ctx, raw_cards)
                if not raw_cards:
                    # Fim das páginas disponíveis para esta URL
                    break

                for card in raw_cards:
                    parsed = _parse_polycard(
                        card,
                        source_type="offers",
                        category_name=cat_label,
                    )
                    if not parsed:
                        continue
                    if parsed["item_key"] in seen_keys:
                        continue

                    price_val = parsed.get("current_price") or 0
                    if min_price > 0 and price_val < min_price:
                        continue
                    if max_price > 0 and price_val > max_price:
                        continue

                    orig_val = parsed.get("original_price") or 0
                    if min_discount > 0 and orig_val > price_val:
                        disc_pct = ((orig_val - price_val) / orig_val) * 100.0
                        if disc_pct < min_discount:
                            continue

                    seen_keys.add(parsed["item_key"])

                    if excluded_norm:
                        t_norm = _normalize_text(parsed["title"])
                        u_norm = _normalize_text(parsed["clean_url"])
                        if any(
                            ex in t_norm or ex in u_norm for ex in excluded_norm if ex
                        ):
                            continue

                    candidates.append(parsed)
            except Exception as e:
                logger.warning(f"Erro ao ler página {page_num} de Ofertas ML: {e}")
                continue

    return candidates


def simulate_ml_offers_selection(count: int = 10) -> dict:
    t0 = time.perf_counter()

    best_pct = int(get_config("ml_best_sellers_pct") or "30")
    best_pct = max(0, min(100, best_pct))
    dedup_days = max(
        int(get_config("ml_history_days") or "7"),
        int(get_config("dedup_history_days") or "7"),
    )
    dedup_threshold = float(get_config("dedup_similarity_threshold") or "60")

    target_best = round(count * (best_pct / 100.0))
    if best_pct > 0 and count >= 2 and target_best == 0:
        target_best = 1
    target_offers = max(0, count - target_best)

    best_raw, blocked_cats, accepted_cats = fetch_ml_best_sellers_candidates()
    offers_raw = fetch_ml_offers_candidates()

    preloaded_profiles = offer_comparator.load_recent_queue_profiles(days=dedup_days)
    used_ml_keys, used_ml_cores, used_ml_titles = _load_ml_history_blocklist(
        days=dedup_days
    )

    random.shuffle(best_raw)
    random.shuffle(offers_raw)

    accepted_titles: list[str] = list(used_ml_titles)
    seen_keys: set[str] = set(used_ml_keys)
    seen_cores: set[str] = set(used_ml_cores)
    skipped_duplicates = 0

    def _pick_from_pool(pool: list[dict], needed: int) -> list[dict]:
        nonlocal skipped_duplicates
        picked = []
        for item in pool:
            if len(picked) >= needed:
                break
            ik = str(item["item_key"]).upper()
            if ik in seen_keys:
                skipped_duplicates += 1
                continue

            core_sig = _extract_product_core_signature(item["title"])
            if len(core_sig.split()) >= 3 and core_sig in seen_cores:
                skipped_duplicates += 1
                continue

            is_dup, reason = offer_comparator.is_product_recently_used_or_blocked(
                candidate_title=item["title"],
                candidate_url=item["clean_url"],
                days=dedup_days,
                threshold=dedup_threshold,
                extra_accepted_titles=accepted_titles,
                preloaded_db_profiles=preloaded_profiles,
                candidate_item_key=ik,
            )
            if is_dup:
                skipped_duplicates += 1
                continue

            seen_keys.add(ik)
            if len(core_sig.split()) >= 3:
                seen_cores.add(core_sig)
            accepted_titles.append(item["title"])

            item_preview = {
                **item,
                "affiliate_url": "https://meli.la/PREVIA_SIMULACAO",
            }
            item["preview_message"] = ml_offer.format_product_message(item_preview)
            picked.append(item)
        return picked

    chosen_best = _pick_from_pool(best_raw, target_best)
    chosen_offers = _pick_from_pool(offers_raw, target_offers)

    total_got = len(chosen_best) + len(chosen_offers)
    if total_got < count:
        missing = count - total_got
        extra = _pick_from_pool(offers_raw + best_raw, missing)
        chosen_offers.extend(extra)

    final_items = []
    cb = list(chosen_best)
    co = list(chosen_offers)
    while cb or co:
        for _ in range(2):
            if co:
                final_items.append(co.pop(0))
        if cb:
            final_items.append(cb.pop(0))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "elapsed_ms": elapsed_ms,
        "blocked_categories": blocked_cats,
        "accepted_categories": accepted_cats,
        "raw_best_count": len(best_raw),
        "raw_offers_count": len(offers_raw),
        "skipped_duplicates": skipped_duplicates,
        "chosen_best_count": len(chosen_best),
        "chosen_offers_count": len(chosen_offers),
        "items": final_items,
    }


def convert_and_enqueue_ml_item(
    product_url: str,
    target_date: str,
    category_name: str = "ML: Automático",
    priority: int = 0,
    fallback_card_data: dict | None = None,
) -> bool:
    try:
        offer = ml_offer._build_offer_sync(product_url)
        if not offer or not offer.get("affiliate_url"):
            return False

        if fallback_card_data:
            for k in (
                "title",
                "current_price",
                "original_price",
                "pix_price",
                "discount_label",
                "image_url",
            ):
                if not offer.get(k) and fallback_card_data.get(k):
                    offer[k] = fallback_card_data[k]

        offer["image_url"] = build_ml_image_url(offer.get("image_url"))
        media_url = offer.get("image_url")
        msg = ml_offer.format_product_message(offer, offer["affiliate_url"])

        add_to_queue(
            store="Mercado Livre",
            product_url=clean_url(product_url),
            message=msg,
            media_url=media_url,
            media_type="image",
            priority=priority,
            target_date=target_date,
            category=category_name,
        )

        # Grava no ml_history TANTO o item_key do card QUANTO o item_id final (caso seja /p/ redirecionado)
        try:
            keys_to_save = set()
            if fallback_card_data and fallback_card_data.get("item_key"):
                keys_to_save.add(str(fallback_card_data["item_key"]).upper())
            if offer.get("item_id"):
                keys_to_save.add(str(offer["item_id"]).upper())
            ext_k = extract_ml_item_key(product_url)
            if ext_k:
                keys_to_save.add(ext_k.upper())

            now_str = datetime.now(CUIABA_TZ).strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_NAME)
            for k_val in keys_to_save:
                if not k_val:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ml_history
                    (item_key, product_url, title, source_type, category_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        k_val,
                        clean_url(product_url),
                        offer.get("title") or "",
                        (fallback_card_data or {}).get("source_type", "auto"),
                        category_name,
                        now_str,
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as e_hist:
            logger.debug(f"Aviso ao gravar ml_history: {e_hist}")

        return True
    except Exception as e:
        logger.error(f"Erro ao converter e enfileirar item ML ({product_url}): {e}")
        return False


def _interleave_pending_auto_queue(target_date: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, store, product_url, formatted_message, media_url, media_type,
               priority, status, target_date, scheduled_at, created_at, category
        FROM queue
        WHERE target_date = ? AND status = 'pending' AND priority = 0
        ORDER BY id ASC
        """,
        (target_date,),
    )
    rows = cursor.fetchall()

    if len(rows) <= 1:
        conn.close()
        return

    buckets: dict[str, list] = {"shopee": [], "amazon": [], "mercado": [], "other": []}
    for r in rows:
        s = (r[1] or "").lower()
        if "shopee" in s:
            buckets["shopee"].append(r)
        elif "amazon" in s:
            buckets["amazon"].append(r)
        elif "mercado" in s:
            buckets["mercado"].append(r)
        else:
            buckets["other"].append(r)

    active = [k for k in ("shopee", "amazon", "mercado") if buckets[k]]
    if len(active) < 2:
        conn.close()
        return

    ids_ordered = [r[0] for r in rows]
    totals = {k: len(buckets[k]) for k in active}
    idxs = {k: 0 for k in active}
    payloads = []

    while any(idxs[k] < totals[k] for k in active):
        best_k, best_ratio = None, 2.0
        for k in active:
            if idxs[k] >= totals[k]:
                continue
            ratio = idxs[k] / totals[k]
            if ratio < best_ratio:
                best_ratio, best_k = ratio, k
        if best_k is None:
            break
        payloads.append(buckets[best_k][idxs[best_k]][1:])
        idxs[best_k] += 1

    payloads.extend(r[1:] for r in buckets["other"])

    for target_id, payload in zip(ids_ordered, payloads):
        cursor.execute(
            """
            UPDATE queue SET store=?, product_url=?, formatted_message=?, media_url=?,
                media_type=?, priority=?, status=?, target_date=?, scheduled_at=?,
                created_at=?, category=? WHERE id=?
            """,
            (*payload, target_id),
        )
    conn.commit()
    conn.close()
    logger.info(f"🔀 Fila de {target_date} intercalada ({totals}).")


def generate_mixed_auto_offers_to_queue(
    total_count: int, target_date: str, keywords_csv: str = ""
) -> dict:
    ml_enabled = get_config("ml_auto_enabled") == "1"
    amazon_enabled = get_config("amazon_auto_enabled") == "1"

    shopee_pct = int(get_config("mix_shopee_pct") or "60")
    amazon_pct = int(get_config("mix_amazon_pct") or "0") if amazon_enabled else 0
    ml_pct = 100 - shopee_pct - amazon_pct
    if ml_pct < 0:
        amazon_pct = max(0, 100 - shopee_pct)
        ml_pct = 0
    if not ml_enabled:
        shopee_pct += ml_pct
        ml_pct = 0
    if not amazon_enabled:
        shopee_pct += amazon_pct
        amazon_pct = 0

    total_pct = shopee_pct + amazon_pct + ml_pct or 1
    shopee_target = round(total_count * shopee_pct / total_pct)
    amazon_target = round(total_count * amazon_pct / total_pct)
    ml_target = max(0, total_count - shopee_target - amazon_target)

    amazon_added = 0
    amazon_fallbacks: list[str] = []
    if amazon_target > 0:
        from amazon_auto import generate_amazon_auto_offers_to_queue

        res = generate_amazon_auto_offers_to_queue(amazon_target, target_date)
        amazon_added = res["added"]
        amazon_fallbacks = res["fallbacks"]

    ml_added = 0
    if ml_target > 0:
        sim = simulate_ml_offers_selection(count=ml_target + 3)
        for cand in sim["items"]:
            if ml_added >= ml_target:
                break
            if convert_and_enqueue_ml_item(
                cand["product_url"],
                target_date,
                cand.get("category_name", "ML: Automático"),
                0,
                cand,
            ):
                ml_added += 1

    missing = (amazon_target - amazon_added) + (ml_target - ml_added)
    shopee_actual = shopee_target + missing
    shopee_added = 0
    if shopee_actual > 0:
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")
        if app_id and app_secret:
            api = ShopeeAffiliateAPI(app_id, app_secret)
            shopee_added = api.generate_auto_offers_to_queue(
                count=shopee_actual,
                target_date=target_date,
                keywords_csv=keywords_csv,
            )

    if sum([shopee_added, amazon_added, ml_added]) > 1:
        _interleave_pending_auto_queue(target_date)

    return {
        "total_added": shopee_added + amazon_added + ml_added,
        "shopee_added": shopee_added,
        "amazon_added": amazon_added,
        "ml_added": ml_added,
        "amazon_fallbacks": amazon_fallbacks,
    }
