"""
Motor de Busca Automática e Laboratório de Validação do Mercado Livre
(Mais Vendidos + Ofertas < R$ 100) e Orquestrador de Mesclagem (60% Shopee / 40% ML).
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
import ml_offer
import offer_comparator
from shopee_service import ShopeeAffiliateAPI

load_dotenv()

logger = logging.getLogger(__name__)
CUIABA_TZ = timezone(timedelta(hours=-4))

DEFAULT_ML_MAIS_VENDIDOS_URL = "https://www.mercadolivre.com.br/mais-vendidos"
DEFAULT_ML_OFERTAS_URL = (
    "https://www.mercadolivre.com.br/ofertas"
    "?container_id=MLB779362-1&price=0.0-100.0"
)

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
    """Garante a tabela ml_history e as chaves de configuração do Mercado Livre."""
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
        "ml_auto_enabled": "0",  # 0 = Só Shopee no auto-refill (até validar no Laboratório); 1 = Mix 60/40 ativo
        "mix_shopee_pct": "60",  # 60% Shopee / 40% Mercado Livre
        "ml_best_sellers_pct": "30",  # 30% Mais Vendidos / 70% Ofertas (< R$ 100)
        "ml_history_days": "7",  # Não repetir produtos do ML em 7 dias
        "ml_excluded_categories_bestsellers": "Games, Informática",
        "ml_excluded_categories_offers": "",
        "ml_offers_url": DEFAULT_ML_OFERTAS_URL,
    }

    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )

    conn.commit()
    conn.close()


ensure_ml_auto_schema()


def _build_ml_headers() -> dict:
    """Monta os headers HTTP incluindo o cookie de sessão do .env para evitar bloqueios/páginas vazias."""
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
    return [item.strip() for item in raw_csv.split(",") if item.strip()]


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


def _extract_nordic_ctx(html_text: str) -> dict | None:
    """Extrai o JSON de _n.ctx.r presente no HTML do Mercado Livre."""
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
    """Converte um objeto polycard do JSON do ML em dicionário padronizado de oferta."""
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
    image_url = (
        f"https://http2.mlstatic.com/D_Q_NP_2X_{pic_id}-AB.webp" if pic_id else None
    )

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
            title = (comp.get("title") or {}).get("text")

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
    """Busca recursivamente todos os carrosséis de categorias dentro do JSON de Mais Vendidos."""
    if isinstance(node, dict):
        # Se este dicionário tem 'title' e 'polycards' (ou 'items' -> 'result' -> 'polycards')
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
    """Busca recursivamente todos os polycards dentro do JSON da página de Ofertas."""
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
    """
    Coleta os produtos da página Mais Vendidos do ML filtrando as categorias excluídas.
    Retorna: (lista_candidatos, categorias_bloqueadas, contagem_por_categoria_aceita)
    """
    if excluded_categories is None:
        excluded_categories = parse_csv_list(
            get_config("ml_excluded_categories_bestsellers") or "Games, Informática"
        )
    excluded_norm = [_normalize_text(c) for c in excluded_categories if c.strip()]

    session = requests.Session()
    resp = session.get(
        DEFAULT_ML_MAIS_VENDIDOS_URL, headers=_build_ml_headers(), timeout=20
    )
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
            if parsed:
                candidates.append(parsed)
                count_ok += 1

        if count_ok > 0:
            accepted_cats[cat_clean] = accepted_cats.get(cat_clean, 0) + count_ok

    return candidates, blocked_cats, accepted_cats


def fetch_ml_offers_candidates(
    offers_url: str | None = None,
    excluded_terms: list[str] | None = None,
    max_pages: int = 2,
) -> list[dict]:
    """
    Coleta produtos da página de Ofertas (ex: Menos de R$ 100), buscando até `max_pages` páginas.
    """
    base_url = (
        offers_url or get_config("ml_offers_url") or DEFAULT_ML_OFERTAS_URL
    ).strip()
    base_url_clean = base_url.split("#")[0]

    if excluded_terms is None:
        excluded_terms = parse_csv_list(
            get_config("ml_excluded_categories_offers") or ""
        )
    excluded_norm = [_normalize_text(t) for t in excluded_terms if t.strip()]

    session = requests.Session()
    headers = _build_ml_headers()
    candidates: list[dict] = []
    seen_keys: set[str] = set()

    for page_num in range(1, max_pages + 1):
        sep = "&" if "?" in base_url_clean else "?"
        page_url = (
            base_url_clean if page_num == 1 else f"{base_url_clean}{sep}page={page_num}"
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

            for card in raw_cards:
                parsed = _parse_polycard(
                    card,
                    source_type="offers",
                    category_name="ML: Ofertas (< R$100)",
                )
                if not parsed:
                    continue
                if parsed["item_key"] in seen_keys:
                    continue
                seen_keys.add(parsed["item_key"])

                if excluded_norm:
                    t_norm = _normalize_text(parsed["title"])
                    u_norm = _normalize_text(parsed["clean_url"])
                    if any(ex in t_norm or ex in u_norm for ex in excluded_norm if ex):
                        continue

                candidates.append(parsed)
        except Exception as e:
            logger.warning(f"Erro ao ler página {page_num} de Ofertas ML: {e}")
            break

    return candidates


def simulate_ml_offers_selection(count: int = 10) -> dict:
    """
    Executa a busca completa do Mercado Livre em modo SIMULAÇÃO (Dry-Run / Laboratório).
    Filtra categorias excluídas, aplica deduplicação semântica de 7 dias e sorteia
    respeitando os 30% Mais Vendidos e 70% Ofertas (< R$ 100), SEM gastar conversão de link.
    """
    t0 = time.perf_counter()

    best_pct = int(get_config("ml_best_sellers_pct") or "30")
    best_pct = max(0, min(100, best_pct))
    dedup_days = int(get_config("ml_history_days") or "7")
    dedup_threshold = float(get_config("dedup_similarity_threshold") or "60")

    target_best = round(count * (best_pct / 100.0))
    if best_pct > 0 and count >= 2 and target_best == 0:
        target_best = 1
    target_offers = max(0, count - target_best)

    best_raw, blocked_cats, accepted_cats = fetch_ml_best_sellers_candidates()
    offers_raw = fetch_ml_offers_candidates(max_pages=2)

    preloaded_profiles = offer_comparator.load_recent_queue_profiles(days=dedup_days)

    random.shuffle(best_raw)
    random.shuffle(offers_raw)

    accepted_titles: list[str] = []
    seen_keys: set[str] = set()
    skipped_duplicates = 0

    def _pick_from_pool(pool: list[dict], needed: int) -> list[dict]:
        nonlocal skipped_duplicates
        picked = []
        for item in pool:
            if len(picked) >= needed:
                break
            ik = item["item_key"]
            if ik in seen_keys:
                continue
            seen_keys.add(ik)

            is_dup, reason = offer_comparator.is_product_recently_used_or_blocked(
                candidate_title=item["title"],
                candidate_url=item["clean_url"],
                days=dedup_days,
                threshold=dedup_threshold,
                extra_accepted_titles=accepted_titles,
                preloaded_db_profiles=preloaded_profiles,
            )
            if is_dup:
                skipped_duplicates += 1
                continue

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

    # Completa caso uma das fontes tenha menos itens
    total_got = len(chosen_best) + len(chosen_offers)
    if total_got < count:
        missing = count - total_got
        extra = _pick_from_pool(offers_raw + best_raw, missing)
        chosen_offers.extend(extra)

    # Intercala os itens
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
    """
    Converte 1 produto aprovado do Mercado Livre em link oficial de afiliado (meli.la),
    formata a mensagem com a função única `ml_offer.format_product_message` e salva na fila.
    """
    try:
        offer = ml_offer._build_offer_sync(product_url)
        if not offer or not offer.get("affiliate_url"):
            return False

        # Se algum dado veio mais rico no card da listagem (ex: discount_label), preserva como fallback
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

        msg = ml_offer.format_product_message(offer, offer["affiliate_url"])
        media_url = offer.get("image_url")

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
        return True
    except Exception as e:
        logger.error(f"Erro ao converter e enfileirar item ML ({product_url}): {e}")
        return False


def _interleave_pending_auto_queue(target_date: str) -> None:
    """
    Reorganiza os itens automáticos pendentes (priority=0, status='pending') da data alvo
    para que Shopee e Mercado Livre fiquem perfeitamente intercalados na ordem de envio (id ASC).
    """
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

    ids_ordered = [r[0] for r in rows]
    shopee_items = [r[1:] for r in rows if "shopee" in (r[1] or "").lower()]
    ml_items = [r[1:] for r in rows if "mercado" in (r[1] or "").lower()]
    other_items = [
        r[1:]
        for r in rows
        if "shopee" not in (r[1] or "").lower()
        and "mercado" not in (r[1] or "").lower()
    ]

    # Se só houver uma loja na fila pendente, não precisa reordenar
    if not shopee_items or not ml_items:
        conn.close()
        return

    # Intercala proporcionalmente (Bresenham / distribuição uniforme exata)
    interleaved_payloads = []
    total_s = len(shopee_items)
    total_m = len(ml_items)
    total_mix = total_s + total_m

    idx_s = 0
    idx_m = 0

    for step in range(1, total_mix + 1):
        # Decide qual loja está "atrasada" em relação à sua proporção ideal no lote
        ratio_s = idx_s / total_s if total_s else 1.0
        ratio_m = idx_m / total_m if total_m else 1.0

        if idx_s < total_s and (idx_m >= total_m or ratio_s <= ratio_m):
            interleaved_payloads.append(shopee_items[idx_s])
            idx_s += 1
        elif idx_m < total_m:
            interleaved_payloads.append(ml_items[idx_m])
            idx_m += 1

    interleaved_payloads.extend(other_items)

    # Atualiza os registros mantendo exatamente os mesmos IDs (em ordem crescente),
    # mas com os conteúdos intercalados!
    for target_id, payload in zip(ids_ordered, interleaved_payloads):
        cursor.execute(
            """
            UPDATE queue
            SET store = ?, product_url = ?, formatted_message = ?, media_url = ?,
                media_type = ?, priority = ?, status = ?, target_date = ?,
                scheduled_at = ?, created_at = ?, category = ?
            WHERE id = ?
            """,
            (*payload, target_id),
        )

    conn.commit()
    conn.close()
    logger.info(
        f"🔀 Fila de {target_date} intercalada com sucesso ({total_s} Shopee ⇄ {total_m} Mercado Livre)!"
    )


def generate_mixed_auto_offers_to_queue(
    total_count: int, target_date: str, keywords_csv: str = ""
) -> dict:
    """
    Orquestrador de Geração Automática:
    - Se `ml_auto_enabled == '1'`, divide o lote em 60% Shopee e 40% Mercado Livre (configurável)
      e intercala os itens na fila para alternar os disparos entre Shopee e ML.
    - Se `ml_auto_enabled == '0'`, gera 100% Shopee no automático.
    """
    ml_enabled = get_config("ml_auto_enabled") == "1"
    shopee_pct = int(get_config("mix_shopee_pct") or "60") if ml_enabled else 100
    shopee_pct = max(0, min(100, shopee_pct))

    shopee_target = round(total_count * (shopee_pct / 100.0))
    ml_target = max(0, total_count - shopee_target)

    ml_added = 0
    if ml_target > 0:
        sim = simulate_ml_offers_selection(count=ml_target + 3)
        for cand in sim["items"]:
            if ml_added >= ml_target:
                break
            ok = convert_and_enqueue_ml_item(
                product_url=cand["product_url"],
                target_date=target_date,
                category_name=cand.get("category_name", "ML: Automático"),
                priority=0,
                fallback_card_data=cand,
            )
            if ok:
                ml_added += 1

    # Compensa com Shopee se faltou algum item do ML
    shopee_actual_target = shopee_target + max(0, ml_target - ml_added)
    shopee_added = 0

    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    if shopee_actual_target > 0 and app_id and app_secret:
        api = ShopeeAffiliateAPI(app_id, app_secret)
        shopee_added = api.generate_auto_offers_to_queue(
            count=shopee_actual_target,
            target_date=target_date,
            keywords_csv=keywords_csv,
        )

    # Intercala os itens pendentes na fila para alternar Shopee e Mercado Livre!
    if shopee_added > 0 and ml_added > 0:
        _interleave_pending_auto_queue(target_date)

    return {
        "total_added": shopee_added + ml_added,
        "shopee_added": shopee_added,
        "ml_added": ml_added,
    }
