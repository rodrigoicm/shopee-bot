from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
import sqlite3
import os
import re
import time
import logging
import asyncio
import html
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from database import DB_NAME, get_config, update_config, add_to_queue, delete_offer
from shopee_service import ShopeeAffiliateAPI, SHOPEE_CATEGORIES
import ml_offer
import offer_comparator
import ml_auto
import ai_copy_service
from whatsapp_service import (
    send_whatsapp_message as trigger_whatsapp_dispatch,
    WHATSAPP_GROUP_PRINCIPAL,
    WHATSAPP_GROUP_PARCERIA,
)

try:
    import amazon_auto

    AMAZON_AVAILABLE = True
except ImportError:
    amazon_auto = None
    AMAZON_AVAILABLE = False

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PromoCuiabá — Plataforma de Automação de Ofertas")

offer_comparator.ensure_comparator_configs()
ml_auto.ensure_ml_auto_schema()
ai_copy_service.ensure_ai_configs()


def format_brl(val: float) -> str:
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _strip_existing_ai_hook(formatted_msg: str) -> str:
    if not formatted_msg:
        return ""
    blocks = formatted_msg.strip().split("\n\n")
    if len(blocks) >= 2 and blocks[1].lstrip().startswith("🔥"):
        return "\n\n".join(blocks[1:])
    return formatted_msg.strip()


def _suggest_first_keyword(title: str) -> str:
    """Extrai a primeira palavra relevante do título (ex: 'almofada') para sugerir no bloqueio."""
    stopwords = {
        "kit",
        "par",
        "jogo",
        "conjunto",
        "pack",
        "combo",
        "caixa",
        "super",
        "oferta",
        "promoção",
        "promocao",
        "original",
        "novo",
        "nova",
        "mini",
    }
    words = re.findall(r"[A-Za-zÀ-ÿ]{3,}", title or "")
    for w in words:
        if w.lower() not in stopwords:
            return w.lower()
    return words[0].lower() if words else ""


def apply_ai_hook_to_existing_offer(offer_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT store, formatted_message FROM queue WHERE id = ?", (offer_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    store, formatted_msg = row
    if not formatted_msg:
        conn.close()
        return False
    base_clean_msg = _strip_existing_ai_hook(formatted_msg)
    title = offer_comparator.extract_title_from_formatted_message(base_clean_msg)
    price = offer_comparator.extract_price_from_formatted_message(base_clean_msg)
    m_disc = re.search(r"(\d+)%\s*OFF", base_clean_msg, re.IGNORECASE)
    disc_pct = m_disc.group(1) if m_disc else None

    is_starting = "a partir de" in base_clean_msg.lower()
    m_max = re.search(r"até\s*R\$\s*([\d.]+,\d{2})", base_clean_msg, re.IGNORECASE)
    max_p = float(m_max.group(1).replace(".", "").replace(",", ".")) if m_max else None

    hook = ai_copy_service.generate_ai_hook(
        title=title,
        current_price=price,
        original_price=None,
        discount_pct=disc_pct,
        store=store or "Shopee",
        force=True,
        max_price=max_p,
        is_starting_price=is_starting,
    )
    if not hook:
        conn.close()
        return False
    new_msg = f"{hook}\n\n{base_clean_msg}"
    cursor.execute(
        "UPDATE queue SET formatted_message = ? WHERE id = ?",
        (new_msg, offer_id),
    )
    conn.commit()
    conn.close()
    return True


def refresh_offer_data(product_url: str) -> tuple[str, str | None]:
    low = product_url.lower()
    if "amazon." in low or "amzn.to" in low or "a.co" in low or "link.amazon" in low:
        try:
            import amazon_service

            offer = amazon_service._build_amazon_offer_sync(product_url)
            if offer and offer.get("affiliate_url"):
                msg = amazon_service.format_product_message(offer)
                return msg, offer.get("image_url")
        except Exception as e:
            logger.warning(f"Erro ao atualizar oferta Amazon: {e}")
        return f"🔥 Oferta imperdível:\n👉 {product_url}", None

    if "mercadolivre" in low or "meli.la" in low:
        try:
            offer = ml_offer._build_offer_sync(product_url)
            if offer and offer.get("affiliate_url"):
                msg = ml_offer.format_product_message(offer)
                return msg, offer.get("image_url")
        except Exception as e:
            logger.warning(f"Erro ao atualizar oferta do ML no portal: {e}")
        return f"🔥 Oferta imperdível:\n👉 {product_url}", None

    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    if not app_id or not app_secret:
        return f"🔥 Oferta imperdível:\n👉 {product_url}", None
    api = ShopeeAffiliateAPI(app_id, app_secret)
    full_url = api.resolve_short_link(product_url)
    item_id = api.extract_item_id(full_url) or api.extract_item_id(product_url)
    offer = api.get_product_offer(item_id) if item_id else None
    if offer:
        aff_link = offer.get("offerLink") or api.generate_short_link(full_url)
        msg = api.format_product_message(offer, aff_link)
        return msg, offer.get("imageUrl")
    clean_dest = api.clean_shopee_url_for_affiliate(product_url)
    aff_link = (
        api.generate_short_link(clean_dest)
        or api.generate_short_link(full_url)
        or product_url
    )
    return api.format_coupon_page_message(aff_link), None


@app.post("/validate-video")
async def validate_video(
    product_url: str = Form(""),
    manual_video_url: str = Form(""),
):
    candidate_mp4 = (manual_video_url or "").strip() or (product_url or "").strip()
    if (
        ".mp4" in candidate_mp4.lower()
        or "vod.susercontent.com" in candidate_mp4.lower()
    ):
        clean_mp4 = candidate_mp4.split("?")[0]
        return JSONResponse(
            {
                "success": True,
                "video_url": clean_mp4,
                "resolved_url": product_url or clean_mp4,
                "message": "✅ Link direto de vídeo (.mp4) reconhecido e pronto para uso!",
            }
        )
    if not product_url.strip():
        return JSONResponse(
            {
                "success": False,
                "message": "⚠️ Informe o link do produto da Shopee ou o link direto .mp4!",
            }
        )
    app_id = os.environ.get("SHOPEE_APP_ID", "")
    app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
    api = ShopeeAffiliateAPI(app_id, app_secret)
    full_url = await asyncio.to_thread(api.resolve_short_link, product_url.strip())
    try:
        from bot import ml_state

        if not ml_state.browser:
            return JSONResponse(
                {
                    "success": False,
                    "message": "❌ O Browser do Playwright não está ativo. Rode pelo app_runner.py!",
                }
            )
        vid_url = await api.extract_shopee_video_fast(ml_state.browser, full_url)
        if vid_url:
            return JSONResponse(
                {
                    "success": True,
                    "video_url": vid_url,
                    "resolved_url": full_url,
                    "message": "✅ Vídeo (.mp4) extraído automaticamente com sucesso!",
                }
            )
        return JSONResponse(
            {
                "success": False,
                "resolved_url": full_url,
                "message": "⚠️ A Shopee bloqueou a extração automática neste anúncio. Cole o link direto (.mp4) no campo ao lado!",
            }
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"❌ Erro ao validar vídeo: {e}"}
        )


_PORTAL_CACHE = {
    "sales": {
        "ts": 0.0,
        "key": "",
        "data": {
            "days": 15,
            "total_orders": 0,
            "pending_orders": 0,
            "completed_orders": 0,
            "cancelled_orders": 0,
            "total_gmv": 0.0,
            "total_commission": 0.0,
            "completed_commission": 0.0,
            "pending_commission": 0.0,
            "orders": [],
        },
    },
    "comp": {"ts": 0.0, "key": "", "data": None},
    "shopee_lab": None,
    "ml_lab": None,
    "amazon_lab": None,
}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    selected_date: str = Query(None),
    video_msg: str = Query(None),
    tab: str = Query("fila"),
    lab_sub: str = Query("shopee"),
    cfg_sub: str = Query("geral"),
    comp_days: int = Query(None),
    comp_store: str = Query("all"),
    cleaned_count: int = Query(None),
    blocked_term: str = Query(None),
    purged_count: int = Query(None),
    sales_days: int = Query(15),
    sales_platform: str = Query("all"),
    sales_status: str = Query("all"),
    gen_shopee: int = Query(None),
    gen_amazon: int = Query(None),
    gen_ml: int = Query(None),
    fallback_count: int = Query(None),
    ml_approved: int = Query(None),
    saved_cfg: str = Query(None),
    ai_updated: int = Query(None),
):
    cuiaba_tz = timezone(timedelta(hours=-4))
    now_dt = datetime.now(cuiaba_tz)
    today_str = now_dt.strftime("%Y-%m-%d")
    current_date = selected_date if selected_date else today_str

    if tab == "ml_lab":
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=ml&selected_date={current_date}",
            status_code=303,
        )
    if cfg_sub == "ml":
        cfg_sub = "mix"
    if lab_sub not in ("shopee", "amazon", "ml"):
        lab_sub = "shopee"

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM config")
    configs = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT id, store, product_url, formatted_message, media_url, status, priority, target_date, created_at, COALESCE(media_type, 'image'), COALESCE(category, 'Geral') FROM queue WHERE target_date = ? ORDER BY priority DESC, id ASC",
        (current_date,),
    )
    queue_items = cursor.fetchall()

    cursor.execute(
        "SELECT id, store, product_url, target_date FROM queue WHERE target_date != ? GROUP BY product_url ORDER BY id DESC LIMIT 80",
        (current_date,),
    )
    available_to_import = cursor.fetchall()
    conn.close()

    total_offers = len(queue_items)
    sent_offers = sum(1 for item in queue_items if item[5] == "sent")
    pending_offers = sum(1 for item in queue_items if item[5] == "pending")

    remaining_seconds = 0
    timer_status_text = "Fila vazia / Aguardando"
    next_dispatch_str = configs.get("next_dispatch_at", "")
    start_str = configs.get("start_time", "08:00")
    end_str = configs.get("end_time", "22:00")
    in_window = start_str <= now_dt.strftime("%H:%M") <= end_str

    ai_enabled = configs.get("ai_copy_enabled", "0") == "1"
    ai_style = configs.get("ai_copy_style", "urgencia_comparativo")
    ai_custom_prompt = configs.get("ai_copy_custom_prompt", "")

    if not in_window:
        timer_status_text = f"Fora do horário ({start_str}–{end_str})"
    elif pending_offers == 0 and current_date == today_str:
        timer_status_text = "Sem pendentes hoje"
    elif next_dispatch_str:
        try:
            next_dt = datetime.strptime(next_dispatch_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=cuiaba_tz
            )
            diff = int((next_dt - now_dt).total_seconds())
            if diff > 0:
                remaining_seconds = diff
                timer_status_text = f"Disparo às {next_dt.strftime('%H:%M:%S')}"
            else:
                timer_status_text = "Disparando..."
        except Exception:
            timer_status_text = "Aguardando ciclo..."
    elif pending_offers > 0 and current_date == today_str:
        timer_status_text = "Pronto (< 30s)"

    # ───────────── Configs Shopee (Categorias + Termos Excluídos + Lab) ─────────────
    shopee_gen_pct = int(configs.get("shopee_general_pct", "30"))
    shopee_cat_pct = 100 - shopee_gen_pct
    shopee_target_cats_raw = configs.get(
        "shopee_target_categories",
        "Roupas Femininas, Moda Infantil, Sapatos Femininos",
    )
    shopee_target_cats_list = [
        c.strip() for c in shopee_target_cats_raw.split(",") if c.strip()
    ]
    shopee_target_cats_lower = {c.lower() for c in shopee_target_cats_list}
    shopee_blocked_terms_raw = configs.get("shopee_blocked_terms", "")

    shopee_cat_checkboxes_html = ""
    shopee_cat_lab_html = ""
    known_shopee_cats_lower = set()
    for idx, cat_label in enumerate(SHOPEE_CATEGORIES.keys()):
        known_shopee_cats_lower.add(cat_label.lower())
        is_checked = "checked" if cat_label.lower() in shopee_target_cats_lower else ""
        shopee_cat_checkboxes_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="shopee_cat_checks" value="{html.escape(cat_label)}" id="shpCat{idx}" {is_checked}>
                <label class="form-check-label small" for="shpCat{idx}">{html.escape(cat_label)}</label>
            </div>
        </div>
        """
        shopee_cat_lab_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="cats" value="{html.escape(cat_label)}" id="labShpCat{idx}" {is_checked}>
                <label class="form-check-label small" for="labShpCat{idx}">{html.escape(cat_label)}</label>
            </div>
        </div>
        """
    shopee_custom_cats_str = ", ".join(
        c for c in shopee_target_cats_list if c.lower() not in known_shopee_cats_lower
    )

    ml_auto_enabled = configs.get("ml_auto_enabled", "0") == "1"
    amazon_enabled = configs.get("amazon_auto_enabled", "0") == "1"
    mix_shopee_pct = int(configs.get("mix_shopee_pct", "60"))
    mix_amazon_pct = int(configs.get("mix_amazon_pct", "0"))
    mix_ml_pct = max(0, 100 - mix_shopee_pct - mix_amazon_pct)

    mix_parts = [f"{mix_shopee_pct}% S"]
    if amazon_enabled and mix_amazon_pct > 0:
        mix_parts.append(f"{mix_amazon_pct}% A")
    if ml_auto_enabled and mix_ml_pct > 0:
        mix_parts.append(f"{mix_ml_pct}% ML")
    mix_label = " / ".join(mix_parts)

    # Configs Mercado Livre
    ml_best_pct = int(configs.get("ml_best_sellers_pct", "30"))
    ml_offers_pct = 100 - ml_best_pct
    ml_hist_days = configs.get("ml_history_days", "7")
    ml_bestsellers_url = configs.get(
        "ml_bestsellers_url", "https://www.mercadolivre.com.br/mais-vendidos"
    )
    ml_offers_url = configs.get(
        "ml_offers_url",
        "https://www.mercadolivre.com.br/ofertas?promotion_type=lightning\nhttps://www.mercadolivre.com.br/ofertas",
    )
    ml_min_price = configs.get("ml_min_price", "20")
    ml_max_price = configs.get("ml_max_price", "350")
    ml_min_discount = configs.get("ml_min_discount", "12")
    ml_pages_per_run = configs.get("ml_pages_per_run", "4")
    ml_max_page_pool = configs.get("ml_max_page_pool", "15")

    excl_bestsellers_raw = configs.get(
        "ml_excluded_categories_bestsellers", "Games, Informática"
    )
    excl_bestsellers_list = ml_auto.parse_csv_list(excl_bestsellers_raw)
    excl_bestsellers_norm = {ml_auto._normalize_text(x) for x in excl_bestsellers_list}
    excl_offers_raw = configs.get("ml_excluded_categories_offers", "")

    # Configs Amazon
    amazon_deals_pct = int(configs.get("amazon_bestsellers_pct", "60"))
    amazon_bestsellers_pct = 100 - amazon_deals_pct
    amazon_hist_days = configs.get("amazon_history_days", "7")
    amazon_bestsellers_url = configs.get(
        "amazon_bestsellers_url", "https://www.amazon.com.br/gp/bestsellers"
    )
    amazon_deals_url = configs.get(
        "amazon_deals_url", "https://www.amazon.com.br/deals"
    )
    amazon_target_cats_raw = configs.get("amazon_target_categories", "")
    amazon_target_cats_set = {
        c.strip() for c in amazon_target_cats_raw.split(",") if c.strip()
    }

    amazon_categories = amazon_auto.AMAZON_CATEGORIES if AMAZON_AVAILABLE else {}
    amazon_cat_extra_str = ", ".join(
        c for c in amazon_target_cats_set if c not in amazon_categories
    )

    amazon_cat_config_html = ""
    for idx, (slug, meta) in enumerate(amazon_categories.items()):
        is_checked = "checked" if slug in amazon_target_cats_set else ""
        amazon_cat_config_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="amazon_cat_checks" value="{slug}" id="amzCat{idx}" {is_checked}>
                <label class="form-check-label small" for="amzCat{idx}">{html.escape(meta['label'])}</label>
            </div>
        </div>
        """

    amazon_cat_lab_html = ""
    for idx, (slug, meta) in enumerate(amazon_categories.items()):
        is_checked = "checked" if slug in amazon_target_cats_set else ""
        amazon_cat_lab_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="cats" value="{slug}" id="labAmzCat{idx}" {is_checked}>
                <label class="form-check-label small" for="labAmzCat{idx}">{html.escape(meta['label'])}</label>
            </div>
        </div>
        """

    # ───────────── Alertas ─────────────
    alert_html = ""
    if video_msg == "ok":
        alert_html = '<div class="alert alert-success shadow-sm border-0 py-2">🎬 Oferta salva com <b>VÍDEO (.mp4)</b> com sucesso!</div>'
    elif video_msg == "fallback":
        alert_html = '<div class="alert alert-warning shadow-sm border-0 py-2">⚠️ Vídeo não detectado; oferta salva com a <b>Imagem</b> padrão.</div>'
    elif cleaned_count is not None:
        alert_html = f'<div class="alert alert-success shadow-sm border-0 py-2">🧹 Limpeza concluída: <b>{cleaned_count} ofertas duplicadas pendentes</b> removidas!</div>'
    elif blocked_term:
        extra_purge = (
            f" e <b>{purged_count} oferta(s) pendente(s)</b> com esse termo foram removidas da fila!"
            if purged_count is not None
            else "!"
        )
        alert_html = f'<div class="alert alert-warning shadow-sm border-0 py-2">🚫 Termo <b>"{html.escape(blocked_term)}"</b> adicionado à lista de exclusão{extra_purge}</div>'
    elif gen_shopee is not None or gen_ml is not None or gen_amazon is not None:
        alert_html = f'<div class="alert alert-success shadow-sm border-0 py-2">🎲 Lote gerado e intercalado para {current_date}: <b>{gen_shopee or 0} Shopee</b> + <b>{gen_amazon or 0} Amazon</b> + <b>{gen_ml or 0} Mercado Livre</b>!</div>'
        if fallback_count and fallback_count > 0:
            alert_html += f'<div class="alert alert-warning shadow-sm border-0 py-2 mt-2">⚠️ <b>{fallback_count} oferta(s) Amazon</b> usaram <b>fallback</b> (cookie expirado). Rode <code>curl_amazon_to_env.py</code> para renovar!</div>'
    elif ml_approved is not None:
        label_map = {"shopee": "Shopee", "amazon": "Amazon", "ml": "Mercado Livre"}
        label = label_map.get(lab_sub, "Laboratório")
        alert_html = f'<div class="alert alert-success shadow-sm border-0 py-2">✅ <b>{ml_approved} oferta(s) da {label}</b> convertida(s) e adicionada(s) à fila de {current_date}!</div>'
    elif saved_cfg:
        alert_html = '<div class="alert alert-success shadow-sm border-0 py-2">💾 Configurações salvas com sucesso!</div>'
    elif ai_updated is not None:
        if ai_updated > 0:
            alert_html = f'<div class="alert alert-info shadow-sm border-0 py-2">✨ <b>Copy com IA gerada e aplicada em {ai_updated} oferta(s)!</b> Clique em "👁️ Ver" na fila para conferir.</div>'
        else:
            alert_html = '<div class="alert alert-warning shadow-sm border-0 py-2">⚠️ Não foi possível gerar o gancho da IA. Verifique a chave GEMINI_API_KEY no .env.</div>'

    now_ts = time.time()

    # Comparador (cache 30s)
    dedup_days_cfg = int(configs.get("dedup_history_days", "7"))
    dedup_thresh_cfg = float(configs.get("dedup_similarity_threshold", "60"))
    blocked_terms_cfg = configs.get("dedup_blocked_terms", "")
    scan_days = comp_days if comp_days is not None else max(14, dedup_days_cfg)
    comp_cache_key = (
        f"{scan_days}_{dedup_thresh_cfg}_{comp_store}_{total_offers}_{cleaned_count}"
    )

    if (
        _PORTAL_CACHE["comp"]["data"] is not None
        and _PORTAL_CACHE["comp"]["key"] == comp_cache_key
        and (now_ts - _PORTAL_CACHE["comp"]["ts"]) < 30
    ):
        comp_report = _PORTAL_CACHE["comp"]["data"]
    else:
        comp_report = offer_comparator.analyze_repeated_offers(
            days=scan_days,
            threshold=dedup_thresh_cfg,
            store_filter=comp_store or "all",
        )
        _PORTAL_CACHE["comp"] = {
            "ts": now_ts,
            "key": comp_cache_key,
            "data": comp_report,
        }

    # Vendas (lazy load)
    sales_cache_key = f"{sales_days}_{sales_platform}"
    if tab == "vendas" and (
        _PORTAL_CACHE["sales"]["key"] != sales_cache_key
        or (now_ts - _PORTAL_CACHE["sales"]["ts"]) > 120
    ):
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")
        if app_id and app_secret and sales_platform in ("all", "shopee"):
            try:
                api_sales = ShopeeAffiliateAPI(app_id, app_secret)
                fetched_sales = await asyncio.to_thread(
                    api_sales.get_sales_report, sales_days
                )
                _PORTAL_CACHE["sales"] = {
                    "ts": now_ts,
                    "key": sales_cache_key,
                    "data": fetched_sales,
                }
            except Exception as e:
                logger.error(f"Erro ao carregar vendas Shopee: {e}")

    sales_data = _PORTAL_CACHE["sales"]["data"]
    filtered_orders = sales_data["orders"]
    if sales_status == "pending":
        filtered_orders = [
            o for o in filtered_orders if o["status_label"] == "Pendente"
        ]
    elif sales_status == "completed":
        filtered_orders = [
            o for o in filtered_orders if o["status_label"] == "Concluído"
        ]
    elif sales_status == "cancelled":
        filtered_orders = [
            o for o in filtered_orders if o["status_label"] == "Cancelado"
        ]

    # Mapas de abas
    tabs_map = {
        "fila": ("show active", "active"),
        "cadastrar": ("", ""),
        "laboratorio": ("", ""),
        "comparador": ("", ""),
        "vendas": ("", ""),
        "config": ("", ""),
    }
    if tab in tabs_map:
        for k in tabs_map:
            tabs_map[k] = ("show active", "active") if k == tab else ("", "")

    lab_sub_map = {
        "shopee": ("show active", "active"),
        "amazon": ("", ""),
        "ml": ("", ""),
    }
    for k in lab_sub_map:
        lab_sub_map[k] = ("show active", "active") if k == lab_sub else ("", "")

    cfg_sub_map = {
        "geral": ("show active", "active"),
        "mix": ("", ""),
        "ia": ("", ""),
        "dedup": ("", ""),
    }
    for k in cfg_sub_map:
        cfg_sub_map[k] = ("show active", "active") if k == cfg_sub else ("", "")

    # ───────────── HTML: LINHAS DA FILA ─────────────
    queue_rows_html = ""
    if not queue_items:
        queue_rows_html = """
        <tr>
            <td colspan="7" class="text-center py-5 text-muted">
                <div class="fs-4 mb-2">📭</div>
                Nenhuma oferta na fila para esta data.<br>
                <small>Clique em <b>"🎲 Gerar Lote Automático"</b> acima ou acesse <b>"➕ Cadastrar Oferta"</b> no menu lateral.</small>
            </td>
        </tr>
        """
    else:
        for item in queue_items:
            (
                item_id,
                store,
                prod_url,
                formatted_msg,
                media_url,
                status,
                priority,
                t_date,
                created_at,
                media_type,
                category_name,
            ) = item
            title_extracted = offer_comparator.extract_title_from_formatted_message(
                formatted_msg
            )
            suggested_term = _suggest_first_keyword(title_extracted)
            has_ai_hook = len(
                (formatted_msg or "").strip().split("\n\n")
            ) >= 2 and not (formatted_msg or "").lstrip().startswith("🔥")

            badge_status = (
                "<span class='badge rounded-pill bg-warning text-dark'>⏳ Pendente</span>"
                if status == "pending"
                else "<span class='badge rounded-pill bg-success'>✅ Enviado</span>"
            )
            badge_prio = (
                "<span class='badge bg-danger'>Manual</span>"
                if priority == 1
                else "<span class='badge bg-secondary'>Auto</span>"
            )
            badge_media = (
                " <span class='badge bg-info text-dark'>🎬 Vídeo</span>"
                if media_type == "video"
                else ""
            )
            badge_ai = (
                " <span class='badge bg-primary-subtle text-primary border border-primary-subtle' title='Possui gancho gerado por IA'>✨ IA</span>"
                if has_ai_hook
                else ""
            )
            s_low = (store or "").lower()
            if "mercado" in s_low:
                badge_store = (
                    "<span class='badge bg-warning text-dark'>Mercado Livre</span>"
                )
            elif "amazon" in s_low:
                badge_store = "<span class='badge bg-dark'>Amazon</span>"
            else:
                badge_store = "<span class='badge' style='background-color:#ee4d2d;'>Shopee</span>"

            cat_str = category_name or "Geral"
            cat_low = cat_str.lower()
            if "feminina" in cat_low or "sapato" in cat_low:
                badge_cat = f"<span class='badge bg-danger-subtle text-danger border border-danger-subtle'>{html.escape(cat_str)}</span>"
            elif "infantil" in cat_low:
                badge_cat = f"<span class='badge bg-info-subtle text-info-emphasis border border-info-subtle'>{html.escape(cat_str)}</span>"
            elif "ml:" in cat_low or "amazon:" in cat_low:
                badge_cat = f"<span class='badge bg-warning-subtle text-dark border border-warning'>{html.escape(cat_str)}</span>"
            elif "mais vendidos" in cat_low or "top" in cat_low:
                badge_cat = f"<span class='badge bg-primary-subtle text-primary border border-primary-subtle'>{html.escape(cat_str)}</span>"
            else:
                badge_cat = f"<span class='badge bg-light text-dark border'>{html.escape(cat_str)}</span>"

            if media_type == "video" and media_url:
                thumb_html = "<div style='width:46px;height:46px;' class='rounded border bg-info-subtle text-info-emphasis d-flex align-items-center justify-content-center me-2 fw-bold' title='Vídeo .mp4 anexado'>🎬</div>"
                media_preview_html = f"<video src='{media_url}' class='img-fluid mb-3 rounded w-100' controls></video>"
            elif media_url:
                thumb_html = f"<img src='{media_url}' style='width:46px;height:46px;object-fit:cover;' class='rounded border me-2'>"
                media_preview_html = (
                    f"<img src='{media_url}' class='img-fluid mb-3 rounded' alt='Foto'>"
                )
            else:
                thumb_html = "<div style='width:46px;height:46px;' class='rounded border bg-light d-flex align-items-center justify-content-center me-2'>📦</div>"
                media_preview_html = ""

            queue_rows_html += f"""
            <tr>
                <td class="fw-bold text-muted">#{item_id}</td>
                <td>
                    <div class="d-flex align-items-center">
                        {thumb_html}
                        <div style="max-width: 320px;">
                            <div class="fw-semibold text-truncate" title="{html.escape(title_extracted)}">{html.escape(title_extracted)}</div>
                            <a href="{prod_url}" target="_blank" class="small text-muted text-truncate d-block">{prod_url}</a>
                        </div>
                    </div>
                </td>
                <td>{badge_store}{badge_media}{badge_ai}</td>
                <td>{badge_cat}</td>
                <td>{badge_prio}</td>
                <td>{badge_status}</td>
                <td class="text-nowrap">
                    <button class="btn btn-sm btn-outline-primary" data-bs-toggle="modal" data-bs-target="#modalMsg{item_id}" title="Ver copy completa">👁️ Ver</button>
                    <form action="/ai/regenerate/{item_id}" method="POST" class="d-inline ms-1">
                        <input type="hidden" name="current_date" value="{current_date}">
                        <button type="submit" class="btn btn-sm btn-outline-info text-dark fw-semibold" title="Gerar/Reescrever gancho persuasivo com IA para esta oferta">✨ IA</button>
                    </form>
                    <div class="btn-group ms-1" role="group">
                        <form action="/send-now/{item_id}/all" method="POST" class="d-inline">
                            <button type="submit" class="btn btn-sm btn-success" title="Disparar agora para Todos os Grupos">🚀 Todos</button>
                        </form>
                        <form action="/send-now/{item_id}/principal" method="POST" class="d-inline ms-1">
                            <button type="submit" class="btn btn-sm btn-outline-success">Principal</button>
                        </form>
                        <form action="/send-now/{item_id}/parceria" method="POST" class="d-inline ms-1">
                            <button type="submit" class="btn btn-sm btn-outline-secondary">Parceria</button>
                        </form>
                    </div>
                    <button type="button" class="btn btn-sm btn-outline-dark ms-1"
                        data-offer-id="{item_id}"
                        data-suggested="{html.escape(suggested_term)}"
                        data-title="{html.escape(title_extracted)}"
                        onclick="blockTermFromQueueBtn(this)"
                        title="Adicionar palavra à lista de exclusão e remover da fila">
                        🚫 Termo
                    </button>
                    <form action="/delete/{item_id}" method="POST" class="d-inline ms-1" onsubmit="return confirm('Excluir esta oferta?')">
                        <input type="hidden" name="current_date" value="{current_date}">
                        <button type="submit" class="btn btn-sm btn-outline-danger" title="Excluir oferta">🗑️</button>
                    </form>
                    <div class="modal fade" id="modalMsg{item_id}" tabindex="-1">
                      <div class="modal-dialog">
                        <div class="modal-content">
                          <div class="modal-header">
                            <h5 class="modal-title">Oferta #{item_id} — {html.escape(cat_str)}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                          </div>
                          <div class="modal-body text-wrap">
                            {media_preview_html}
                            <div class="p-3 bg-light border rounded" style="white-space: pre-wrap;">{formatted_msg}</div>
                          </div>
                          <div class="modal-footer justify-content-between">
                            <form action="/ai/regenerate/{item_id}" method="POST" class="m-0">
                                <input type="hidden" name="current_date" value="{current_date}">
                                <button type="submit" class="btn btn-sm btn-info fw-bold">✨ Gerar Novo Gancho com IA</button>
                            </form>
                            <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Fechar</button>
                          </div>
                        </div>
                      </div>
                    </div>
                </td>
            </tr>
            """

    # ───────────── HTML: LABORATÓRIO SHOPEE (NOVO!) ─────────────
    shopee_lab_data = _PORTAL_CACHE.get("shopee_lab")
    shopee_lab_results_html = ""
    if not shopee_lab_data:
        shopee_lab_results_html = """
        <div class="card border-0 shadow-sm text-center py-5">
            <div class="card-body">
                <div class="fs-2 mb-2">🧪</div>
                <h5 class="fw-bold mb-1">Nenhuma simulação Shopee carregada</h5>
                <p class="mb-0 text-muted small">Marque as categorias desejadas acima e clique em <b>"🔍 Simular Shopee"</b> para pré-visualizar as ofertas.</p>
            </div>
        </div>
        """
    else:
        shp_cards_html = ""
        for idx, cand in enumerate(shopee_lab_data.get("items", [])):
            c_title = html.escape(cand.get("title", "Produto Shopee"))
            c_cat_label = html.escape(cand.get("category_name", "Shopee"))
            c_img = cand.get("image_url") or ""
            c_url = cand.get("product_url") or "#"
            p_min = cand.get("price_min") or 0
            p_max = cand.get("price_max") or p_min
            c_cur = (
                f"A partir de {format_brl(p_min)}"
                if p_max > p_min
                else format_brl(p_min)
            )
            c_disc = (
                f"<span class='badge bg-success ms-1'>{cand['discount_pct']}% OFF</span>"
                if cand.get("discount_pct")
                else ""
            )
            c_meta = (
                f"⭐ {cand.get('rating', 0):.1f} | 🔥 {cand.get('sales', 0)} vendidos"
            )
            sug_kw = _suggest_first_keyword(cand.get("title", ""))

            shp_cards_html += f"""
            <div class="col-md-6">
                <div class="card h-100 shadow-sm border-0 border-start border-danger border-4">
                    <div class="card-body d-flex gap-3">
                        <img src="{c_img}" style="width:90px;height:90px;object-fit:contain;" class="rounded border bg-white p-1">
                        <div class="flex-grow-1">
                            <div class="mb-1">
                                <span class="badge" style="background-color:#ee4d2d;">{c_cat_label}</span>
                                {c_disc}
                            </div>
                            <h6 class="fw-bold mb-1">{c_title}</h6>
                            <div class="fs-5 fw-bold text-primary mb-1">{c_cur}</div>
                            <small class="text-muted">{c_meta}</small>
                            <div class="d-flex gap-2 flex-wrap mt-2">
                                <button type="button" class="btn btn-sm btn-outline-dark" data-bs-toggle="modal" data-bs-target="#modalShpLab{idx}">
                                    📝 Ver Mensagem
                                </button>
                                <a href="{c_url}" target="_blank" class="btn btn-sm btn-outline-secondary">🔗 Ver na Shopee</a>
                                <button type="button" class="btn btn-sm btn-outline-danger"
                                    data-offer-id="0"
                                    data-suggested="{html.escape(sug_kw)}"
                                    data-title="{c_title}"
                                    onclick="blockTermFromQueueBtn(this)"
                                    title="Bloquear termo indesejado">
                                    🚫 Termo
                                </button>
                                <form action="/shopee-lab/approve-one" method="POST" class="d-inline">
                                    <input type="hidden" name="item_index" value="{idx}">
                                    <input type="hidden" name="target_date" value="{current_date}">
                                    <button type="submit" class="btn btn-sm btn-success fw-bold">✅ Enfileirar</button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal fade" id="modalShpLab{idx}" tabindex="-1">
                    <div class="modal-dialog">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">Prévia Shopee #{idx+1}</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                            </div>
                            <div class="modal-body">
                                <div class="p-3 bg-light border rounded" style="white-space: pre-wrap;">{cand.get('preview_message', '')}</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            """

        acc_cats = shopee_lab_data.get("accepted_categories", {})
        acc_badges = (
            " ".join(
                f"<span class='badge bg-success-subtle text-success border'>{html.escape(k)}: {v}</span>"
                for k, v in acc_cats.items()
            )
            or "<span class='text-muted small'>Nenhuma</span>"
        )

        shopee_lab_results_html = f"""
        <div class="card mb-3 border-0 shadow-sm">
            <div class="card-body py-3">
                <div class="row align-items-center">
                    <div class="col-md-9 small">
                        <div>
                            <b>⏱️ Tempo:</b> {shopee_lab_data.get('elapsed_ms', 0)} ms |
                            <b>📦 Pool Categorias:</b> {shopee_lab_data.get('raw_cat_count', 0)} |
                            <b>📦 Pool Geral:</b> {shopee_lab_data.get('raw_gen_count', 0)} |
                            <b>🛡️ Repetidos/Bloqueados Barrados:</b> {shopee_lab_data.get('skipped_duplicates', 0)}
                        </div>
                        <div class="mt-1"><b>✅ Categorias lidas:</b> {acc_badges}</div>
                    </div>
                    <div class="col-md-3 text-end">
                        <form action="/shopee-lab/approve-all" method="POST">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-warning fw-bold w-100" {"disabled" if not shopee_lab_data.get('items') else ""}>
                                🚀 Enfileirar Todos ({len(shopee_lab_data.get('items', []))})
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3">{shp_cards_html}</div>
        """

    # ───────────── HTML: LABORATÓRIO AMAZON ─────────────
    amazon_lab_data = _PORTAL_CACHE.get("amazon_lab")
    amazon_lab_results_html = ""
    if not AMAZON_AVAILABLE:
        amazon_lab_results_html = """
        <div class="alert alert-warning">⚠️ Módulo <code>amazon_auto.py</code> não instalado. Crie o arquivo para usar o laboratório.</div>
        """
    elif not amazon_lab_data:
        amazon_lab_results_html = """
        <div class="card border-0 shadow-sm text-center py-5">
            <div class="card-body">
                <div class="fs-2 mb-2">🧪</div>
                <h5 class="fw-bold mb-1">Nenhuma simulação Amazon carregada</h5>
                <p class="mb-0 text-muted small">Clique em <b>"🔍 Simular Amazon"</b> acima para coletar ofertas de <i>Deals</i> e <i>Bestsellers</i>.</p>
            </div>
        </div>
        """
    else:
        amz_cards_html = ""
        for idx, cand in enumerate(amazon_lab_data.get("items", [])):
            c_title = html.escape(cand.get("title", "Produto Amazon"))
            c_cat_slug = cand.get("category_slug", "")
            c_cat_label = amazon_categories.get(c_cat_slug, {}).get(
                "label", c_cat_slug or "Geral"
            )
            c_img = cand.get("image_url") or ""
            c_url = cand.get(
                "product_url", f"https://www.amazon.com.br/dp/{cand.get('asin')}"
            )
            c_cur = (
                format_brl(cand["current_price"]) if cand.get("current_price") else "—"
            )
            c_orig = (
                f"<s class='text-muted small ms-1'>{format_brl(cand['original_price'])}</s>"
                if cand.get("original_price")
                else ""
            )
            c_disc = (
                f"<span class='badge bg-success ms-1'>{cand['discount_pct']}% OFF</span>"
                if cand.get("discount_pct")
                else ""
            )
            c_source = (
                "<span class='badge bg-primary text-white ms-1'>Deals</span>"
                if cand.get("source") == "deals"
                else "<span class='badge bg-secondary ms-1'>Bestsellers</span>"
            )
            c_rating = (
                f"⭐ {cand['rating']:.1f} ({cand.get('reviews', 0)})"
                if cand.get("rating")
                else ""
            )

            amz_cards_html += f"""
            <div class="col-md-6">
                <div class="card h-100 shadow-sm border-0 border-start border-warning border-4">
                    <div class="card-body d-flex gap-3">
                        <img src="{c_img}" style="width:90px;height:90px;object-fit:contain;" class="rounded border bg-white p-1">
                        <div class="flex-grow-1">
                            <div class="mb-1">
                                <span class="badge bg-warning text-dark">{html.escape(c_cat_label)}</span>
                                {c_disc}{c_source}
                            </div>
                            <h6 class="fw-bold mb-1">{c_title}</h6>
                            <div class="fs-5 fw-bold text-primary mb-1">{c_cur}{c_orig}</div>
                            <small class="text-muted">{c_rating}</small>
                            <div class="d-flex gap-2 flex-wrap mt-2">
                                <button type="button" class="btn btn-sm btn-outline-dark" data-bs-toggle="modal" data-bs-target="#modalAmzLab{idx}">
                                    📝 Ver Mensagem
                                </button>
                                <a href="{c_url}" target="_blank" class="btn btn-sm btn-outline-secondary">🔗 Ver na Amazon</a>
                                <form action="/amazon-lab/approve-one" method="POST" class="d-inline">
                                    <input type="hidden" name="item_index" value="{idx}">
                                    <input type="hidden" name="target_date" value="{current_date}">
                                    <button type="submit" class="btn btn-sm btn-success fw-bold">✅ Enfileirar</button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal fade" id="modalAmzLab{idx}" tabindex="-1">
                    <div class="modal-dialog">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">Prévia Amazon #{idx+1}</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                            </div>
                            <div class="modal-body">
                                <div class="p-3 bg-light border rounded" style="white-space: pre-wrap;">{cand.get('preview_message', '')}</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            """

        acc_cats = amazon_lab_data.get("accepted_categories", {})
        acc_badges = (
            " ".join(
                f"<span class='badge bg-success-subtle text-success border'>{html.escape(amazon_categories.get(k, {}).get('label', k))}: {v}</span>"
                for k, v in acc_cats.items()
            )
            or "<span class='text-muted small'>Nenhuma</span>"
        )

        amazon_lab_results_html = f"""
        <div class="card mb-3 border-0 shadow-sm">
            <div class="card-body py-3">
                <div class="row align-items-center">
                    <div class="col-md-9 small">
                        <div>
                            <b>⏱️ Tempo:</b> {amazon_lab_data.get('elapsed_ms', 0)} ms |
                            <b>📦 Pool Deals:</b> {amazon_lab_data.get('raw_deals_count', 0)} |
                            <b>📦 Pool Bestsellers:</b> {amazon_lab_data.get('raw_bestsellers_count', 0)} |
                            <b>🛡️ Repetidos Barrados:</b> {amazon_lab_data.get('skipped_duplicates', 0)}
                        </div>
                        <div class="mt-1"><b>✅ Categorias lidas:</b> {acc_badges}</div>
                    </div>
                    <div class="col-md-3 text-end">
                        <form action="/amazon-lab/approve-all" method="POST">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-warning fw-bold w-100" {"disabled" if not amazon_lab_data.get('items') else ""}>
                                🚀 Enfileirar Todos ({len(amazon_lab_data.get('items', []))})
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3">{amz_cards_html}</div>
        """

    # ───────────── HTML: LABORATÓRIO ML ─────────────
    ml_lab_data = _PORTAL_CACHE.get("ml_lab")
    ml_lab_results_html = ""
    if not ml_lab_data:
        ml_lab_results_html = """
        <div class="card border-0 shadow-sm text-center py-5">
            <div class="card-body">
                <div class="fs-2 mb-2">🧪</div>
                <h5 class="fw-bold mb-1">Nenhuma simulação ML carregada</h5>
                <p class="mb-0 text-muted small">Clique em <b>"🔍 Simular ML"</b> acima para coletar produtos segundo os parâmetros configurados.</p>
            </div>
        </div>
        """
    else:
        acc_cats_badges = " ".join(
            f"<span class='badge bg-success-subtle text-success border'>{html.escape(k)}: {v}</span>"
            for k, v in ml_lab_data["accepted_categories"].items()
        )
        blk_cats_badges = (
            " ".join(
                f"<span class='badge bg-danger'>{html.escape(c)}</span>"
                for c in ml_lab_data["blocked_categories"]
            )
            or "<span class='text-muted small'>Nenhuma encontrada</span>"
        )
        ml_cards_html = ""
        for idx, cand in enumerate(ml_lab_data["items"]):
            c_title = html.escape(cand["title"])
            c_cat = html.escape(cand["category_name"])
            c_img = cand.get("image_url") or ""
            c_url = cand["product_url"]
            c_cur = format_brl(cand["current_price"])
            c_orig = (
                f"<s class='text-muted small ms-1'>{format_brl(cand['original_price'])}</s>"
                if cand.get("original_price")
                else ""
            )
            c_disc = (
                f"<span class='badge bg-success ms-1'>{html.escape(cand['discount_label'])}</span>"
                if cand.get("discount_label")
                else ""
            )
            c_pix = (
                "<span class='badge bg-info text-dark ms-1'>Pix</span>"
                if cand.get("pix_price")
                else ""
            )
            ml_cards_html += f"""
            <div class="col-md-6">
                <div class="card h-100 shadow-sm border-0 border-start border-warning border-4">
                    <div class="card-body d-flex gap-3">
                        <img src="{c_img}" style="width:90px;height:90px;object-fit:contain;" class="rounded border bg-white p-1">
                        <div class="flex-grow-1">
                            <div class="mb-1">
                                <span class="badge bg-warning text-dark">{c_cat}</span>{c_disc}{c_pix}
                            </div>
                            <h6 class="fw-bold mb-1">{c_title}</h6>
                            <div class="fs-5 fw-bold text-primary mb-2">{c_cur}{c_orig}</div>
                            <div class="d-flex gap-2 flex-wrap">
                                <button type="button" class="btn btn-sm btn-outline-dark" data-bs-toggle="modal" data-bs-target="#modalLab{idx}">📝 Ver Mensagem</button>
                                <a href="{c_url}" target="_blank" class="btn btn-sm btn-outline-secondary">🔗 Ver no ML</a>
                                <form action="/ml-lab/approve-one" method="POST" class="d-inline">
                                    <input type="hidden" name="item_index" value="{idx}">
                                    <input type="hidden" name="target_date" value="{current_date}">
                                    <button type="submit" class="btn btn-sm btn-success fw-bold">✅ Enfileirar</button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal fade" id="modalLab{idx}" tabindex="-1">
                    <div class="modal-dialog">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title">Prévia ML #{idx+1}</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                            </div>
                            <div class="modal-body">
                                <div class="p-3 bg-light border rounded" style="white-space: pre-wrap;">{cand.get('preview_message', '')}</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            """
        ml_lab_results_html = f"""
        <div class="card mb-3 border-0 shadow-sm">
            <div class="card-body py-3">
                <div class="row align-items-center">
                    <div class="col-md-9 small">
                        <div><b>⏱️ Tempo:</b> {ml_lab_data['elapsed_ms']} ms | <b>📦 Pool Mais Vendidos:</b> {ml_lab_data['raw_best_count']} | <b>📦 Pool Ofertas:</b> {ml_lab_data['raw_offers_count']} | <b>🛡️ Repetidos:</b> {ml_lab_data['skipped_duplicates']}</div>
                        <div class="mt-1"><b>🚫 Categorias Bloqueadas:</b> {blk_cats_badges}</div>
                        <div class="mt-1"><b>✅ Categorias Lidas:</b> {acc_cats_badges}</div>
                    </div>
                    <div class="col-md-3 text-end">
                        <form action="/ml-lab/approve-all" method="POST">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-warning fw-bold w-100">🚀 Enfileirar Todos ({len(ml_lab_data['items'])})</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3">{ml_cards_html}</div>
        """

    # ───────────── HTML: COMPARADOR ─────────────
    clusters_html = ""
    if not comp_report["clusters"]:
        clusters_html = """
        <div class="card border-0 shadow-sm text-center py-5">
            <div class="card-body">
                <div class="fs-2 mb-2">🎉</div>
                <h5 class="fw-bold text-success mb-1">Nenhuma oferta repetida/similar detectada neste período!</h5>
            </div>
        </div>
        """
    else:
        for cl in comp_report["clusters"]:
            c_title = html.escape(cl["canonical_title"])
            c_sig = html.escape(cl["signature"])
            c_img = cl["media_url"]
            img_tag = (
                f"<img src='{c_img}' style='width:56px;height:56px;object-fit:cover;' class='rounded border me-3'>"
                if c_img
                else ""
            )
            dates_badges = " ".join(
                f"<span class='badge bg-light text-dark border'>{d}</span>"
                for d in cl["dates"]
            )
            rows_html = ""
            for g_item in cl["items"]:
                gid = g_item["id"]
                is_sug = gid in cl["suggested_delete_ids"]
                st_b = (
                    "<span class='badge bg-warning text-dark'>Pendente</span>"
                    if g_item["status"] == "pending"
                    else "<span class='badge bg-success'>Enviado</span>"
                )
                rows_html += f"""
                <tr class="{'table-warning' if is_sug else ''}">
                    <td><b>#{gid}</b></td>
                    <td>{g_item['store']}</td>
                    <td><span class="badge bg-dark">{g_item['target_date']}</span></td>
                    <td><span class="badge bg-info text-dark">{g_item['similarity']}%</span></td>
                    <td>
                        <div class="fw-semibold">{html.escape(g_item['title'])}</div>
                        <a href="{g_item['product_url']}" target="_blank" class="small text-muted">{g_item['product_url']}</a>
                    </td>
                    <td>{st_b}</td>
                    <td class="text-end">
                        <form action="/comparator/delete-item/{gid}" method="POST" class="d-inline">
                            <input type="hidden" name="comp_days" value="{scan_days}">
                            <input type="hidden" name="comp_store" value="{comp_store}">
                            <button type="submit" class="btn btn-sm btn-outline-danger">🗑️</button>
                        </form>
                    </td>
                </tr>
                """
            clusters_html += f"""
            <div class="card mb-3 shadow-sm border-0 border-start border-danger border-4">
                <div class="card-header bg-white d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <div class="d-flex align-items-center">
                        {img_tag}
                        <div>
                            <span class="badge bg-danger me-1">🔁 {cl['total_occurrences']}x</span>
                            <strong class="fs-6">{c_title}</strong>
                            <div class="small text-muted">Assinatura: <code>{c_sig}</code> | Datas: {dates_badges}</div>
                        </div>
                    </div>
                    <form action="/comparator/block-term" method="POST" class="d-inline">
                        <input type="hidden" name="term" value="{c_sig}">
                        <input type="hidden" name="comp_days" value="{scan_days}">
                        <button type="submit" class="btn btn-sm btn-dark">🚫 Bloquear "{c_sig}"</button>
                    </form>
                </div>
                <div class="card-body p-0">
                    <table class="table table-sm align-middle mb-0"><tbody>{rows_html}</tbody></table>
                </div>
            </div>
            """

    # ───────────── HTML: VENDAS ─────────────
    sales_rows_html = ""
    if not filtered_orders:
        sales_rows_html = '<tr><td colspan="8" class="text-center py-4 text-muted">Nenhum pedido encontrado neste período.</td></tr>'
    else:
        for ord_item in filtered_orders:
            st_lbl = ord_item["status_label"]
            st_badge = (
                "<span class='badge bg-success'>✅ Concluído</span>"
                if st_lbl == "Concluído"
                else (
                    "<span class='badge bg-secondary'>❌ Cancelado</span>"
                    if st_lbl == "Cancelado"
                    else "<span class='badge bg-warning text-dark'>⏳ Pendente</span>"
                )
            )
            sales_rows_html += f"""
            <tr>
                <td>{ord_item['purchase_time']}</td>
                <td><span class='badge' style='background-color:#ee4d2d;'>Shopee</span></td>
                <td><code>{html.escape(ord_item['order_id'])}</code></td>
                <td style="max-width:360px;" class="text-truncate" title="{html.escape(ord_item['items_summary'])}">{html.escape(ord_item['items_summary'])}</td>
                <td class="text-center">{ord_item['items_count']}</td>
                <td>{format_brl(ord_item['order_amount'])}</td>
                <td class="fw-bold text-success">{format_brl(ord_item['commission'])}</td>
                <td>{st_badge}</td>
            </tr>
            """

    # ───────────── HTML: CHECKBOXES ML EXCLUSÃO ─────────────
    cat_checkboxes_html = ""
    known_norm_set = set()
    for idx, cat_name in enumerate(ml_auto.KNOWN_ML_CATEGORIES):
        c_norm = ml_auto._normalize_text(cat_name)
        known_norm_set.add(c_norm)
        is_checked = "checked" if c_norm in excl_bestsellers_norm else ""
        cat_checkboxes_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="excl_cat_checks" value="{cat_name}" id="catCheck{idx}" {is_checked}>
                <label class="form-check-label small" for="catCheck{idx}">{cat_name}</label>
            </div>
        </div>
        """
    custom_bestsellers_str = ", ".join(
        c
        for c in excl_bestsellers_list
        if ml_auto._normalize_text(c) not in known_norm_set
    )

    import_options_html = "".join(
        f'<option value="{imp[2]}">[{imp[3]} - {imp[1]}] {imp[2]} (ID: {imp[0]})</option>'
        for imp in available_to_import
    )

    # ───────────── HTML PRINCIPAL ─────────────
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>PromoCuiabá — Plataforma de Automação de Ofertas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #f4f6f9; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }}
            .app-wrapper {{ display: flex; min-height: 100vh; }}
            .sidebar {{ width: 265px; background: #111827; color: #f9fafb; flex-shrink: 0; display: flex; flex-direction: column; box-shadow: 2px 0 10px rgba(0,0,0,0.12); }}
            .sidebar-brand {{ padding: 1.25rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,0.08); }}
            .sidebar-nav {{ padding: 1rem 0.75rem; flex-grow: 1; }}
            .sidebar-link {{ display: flex; align-items: center; justify-content: space-between; padding: 0.7rem 1rem; color: #9ca3af; text-decoration: none; border-radius: 0.5rem; margin-bottom: 0.35rem; font-weight: 500; transition: all 0.15s ease; }}
            .sidebar-link:hover {{ background: rgba(255,255,255,0.06); color: #ffffff; }}
            .sidebar-link.active {{ background: #2563eb; color: #ffffff; font-weight: 600; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35); }}
            .sidebar-section-title {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: #6b7280; padding: 0.75rem 1rem 0.35rem; font-weight: 700; }}
            .main-area {{ flex-grow: 1; display: flex; flex-direction: column; min-width: 0; }}
            .topbar {{ background: #ffffff; border-bottom: 1px solid #e5e7eb; padding: 0.75rem 1.75rem; }}
            .stat-card {{ border: none; border-radius: 0.75rem; transition: transform 0.15s ease; }}
        </style>
    </head>
    <body>
        <div class="app-wrapper">
            <aside class="sidebar">
                <div class="sidebar-brand">
                    <div class="d-flex align-items-center gap-2">
                        <span class="fs-4">🚀</span>
                        <div>
                            <div class="fw-bold fs-5 lh-1 text-white">PromoCuiabá</div>
                            <small class="text-secondary" style="font-size: 0.75rem;">SaaS Affiliate Hub</small>
                        </div>
                    </div>
                </div>
                <div class="sidebar-nav">
                    <div class="sidebar-section-title">Operação Diária</div>
                    <a class="sidebar-link {tabs_map['fila'][1]}" href="/?tab=fila&selected_date={current_date}">
                        <span>📋 Fila de Disparos</span>
                        <span class="badge rounded-pill bg-light text-dark">{pending_offers}</span>
                    </a>
                    <a class="sidebar-link {tabs_map['cadastrar'][1]}" href="/?tab=cadastrar&selected_date={current_date}">
                        <span>➕ Cadastrar / Importar</span>
                    </a>

                    <div class="sidebar-section-title mt-2">Curadoria & Automação</div>
                    <a class="sidebar-link {tabs_map['laboratorio'][1]}" href="/?tab=laboratorio&lab_sub={lab_sub}&selected_date={current_date}">
                        <span>🧪 Laboratório</span>
                        <span class="badge bg-warning text-dark">Ativo</span>
                    </a>
                    <a class="sidebar-link {tabs_map['comparador'][1]}" href="/?tab=comparador&selected_date={current_date}&comp_days={scan_days}&comp_store={comp_store}">
                        <span>🛡️ Anti-Repetição</span>
                        <span class="badge rounded-pill bg-danger">{comp_report['total_clusters']}</span>
                    </a>

                    <div class="sidebar-section-title mt-2">Gestão & Sistema</div>
                    <a class="sidebar-link {tabs_map['vendas'][1]}" href="/?tab=vendas&selected_date={current_date}&sales_days={sales_days}&sales_platform={sales_platform}&sales_status={sales_status}">
                        <span>💰 Vendas & Comissões</span>
                    </a>
                    <a class="sidebar-link {tabs_map['config'][1]}" href="/?tab=config&selected_date={current_date}">
                        <span>⚙️ Configurações</span>
                    </a>
                </div>
                <div class="p-3 border-top border-secondary border-opacity-25 small">
                    <div class="d-flex justify-content-between align-items-center mb-1">
                        <span class="text-secondary">Copywriting IA:</span>
                        <span class="badge {'bg-info text-dark' if ai_enabled else 'bg-secondary'}">{'ATIVO ✨' if ai_enabled else 'OFF'}</span>
                    </div>
                    <div class="d-flex justify-content-between align-items-center">
                        <span class="text-secondary">Mix Automático:</span>
                        <span class="badge {'bg-warning text-dark' if (ml_auto_enabled or amazon_enabled) else 'bg-secondary'}">{mix_label if (ml_auto_enabled or amazon_enabled) else '100% Shopee'}</span>
                    </div>
                </div>
            </aside>

            <div class="main-area">
                <header class="topbar d-flex flex-wrap justify-content-between align-items-center gap-3 shadow-sm">
                    <div class="d-flex align-items-center gap-2">
                        <span class="fw-bold text-secondary small text-uppercase">📅 Data da Fila:</span>
                        <form method="GET" action="/" class="d-flex align-items-center gap-2 m-0">
                            <input type="hidden" name="tab" value="{tab}">
                            <input type="hidden" name="lab_sub" value="{lab_sub}">
                            <input type="date" name="selected_date" value="{current_date}" class="form-control form-control-sm fw-semibold" onchange="this.form.submit()">
                            <a href="/?tab={tab}&lab_sub={lab_sub}&selected_date={today_str}" class="btn btn-sm btn-outline-dark">Hoje</a>
                        </form>
                    </div>
                    <div class="d-flex align-items-center gap-2 flex-wrap">
                        <span class="badge bg-primary-subtle text-primary border border-primary-subtle px-3 py-2">📋 Total: {total_offers}</span>
                        <span class="badge bg-success-subtle text-success border border-success-subtle px-3 py-2">✅ Enviadas: {sent_offers}</span>
                        <span class="badge bg-warning-subtle text-dark border border-warning px-3 py-2">⏳ Pendentes: {pending_offers}</span>
                        <div class="bg-dark text-white rounded-3 px-3 py-1 d-flex align-items-center gap-2">
                            <span>⏱️ <strong id="countdown-timer" class="text-info">--:--</strong></span>
                            <small class="text-light opacity-75">({timer_status_text})</small>
                        </div>
                    </div>
                </header>

                <main class="container-fluid p-4">
                    {alert_html}

                    <!-- Form oculto para o botão 🚫 Termo direto da Fila ou Laboratório -->
                    <form id="quickBlockForm" action="/queue/block-term" method="POST" style="display:none;">
                        <input type="hidden" name="term" id="quickBlockTermInput">
                        <input type="hidden" name="offer_id" id="quickBlockOfferIdInput">
                        <input type="hidden" name="current_date" value="{current_date}">
                    </form>

                    <div class="tab-content">

                        <!-- ================= FILA ================= -->
                        <div class="tab-pane fade {tabs_map['fila'][0]}" role="tabpanel">
                            <div class="d-flex flex-wrap gap-2 mb-3 align-items-center justify-content-between bg-white p-3 rounded-3 shadow-sm">
                                <div>
                                    <h5 class="fw-bold mb-0">📋 Fila de Disparos ({current_date})</h5>
                                    <small class="text-muted">Use o botão <b>"🚫 Termo"</b> em qualquer oferta inútil (ex: almofada) para banir a palavra e limpar a fila.</small>
                                </div>
                                <div class="d-flex flex-wrap gap-2">
                                    <a href="/?tab=cadastrar&selected_date={current_date}" class="btn btn-outline-dark fw-semibold">➕ Nova Manual</a>
                                    <form action="/ai/regenerate-all-pending" method="POST" class="m-0">
                                        <input type="hidden" name="target_date" value="{current_date}">
                                        <button type="submit" class="btn btn-info fw-bold text-dark" {"disabled" if pending_offers == 0 else ""} title="Gera e aplica o gancho da IA nas ofertas pendentes desta data">
                                            ✨ Gerar IA nas Pendentes ({pending_offers})
                                        </button>
                                    </form>
                                    <form action="/generate-auto-offers" method="POST" class="m-0">
                                        <input type="hidden" name="target_date" value="{current_date}">
                                        <button type="submit" class="btn btn-success fw-bold">
                                            🎲 Gerar {configs.get('auto_refill_count', '15')} Automáticas ({mix_label if (ml_auto_enabled or amazon_enabled) else '100% Shopee'})
                                        </button>
                                    </form>
                                </div>
                            </div>
                            <div class="card border-0 shadow-sm rounded-3 overflow-hidden">
                                <div class="table-responsive">
                                    <table class="table table-hover align-middle mb-0">
                                        <thead class="table-light">
                                            <tr>
                                                <th>ID</th><th>Produto</th><th>Plataforma</th><th>Categoria / Origem</th><th>Modo</th><th>Status</th><th>Ações Rápidas</th>
                                            </tr>
                                        </thead>
                                        <tbody>{queue_rows_html}</tbody>
                                    </table>
                                </div>
                            </div>
                        </div>

                        <!-- ================= CADASTRAR ================= -->
                        <div class="tab-pane fade {tabs_map['cadastrar'][0]}" role="tabpanel">
                            <div class="row g-4">
                                <div class="col-lg-7">
                                    <div class="card border-0 shadow-sm rounded-3">
                                        <div class="card-header bg-dark text-white fw-bold py-3">➕ Cadastrar Nova Oferta Manual (Shopee, Amazon ou ML)</div>
                                        <div class="card-body p-4">
                                            <form action="/add-offer" method="POST" class="row g-3">
                                                <input type="hidden" name="target_date" value="{current_date}">
                                                <div class="col-md-12">
                                                    <label class="form-label fw-bold small">1. Link do Produto ou Página de Cupom</label>
                                                    <input type="url" name="product_url" id="productUrlInput" placeholder="https://s.shopee.com.br/... | https://amzn.to/... | https://mercadolivre.com/..." class="form-control" required>
                                                </div>
                                                <div class="col-md-12">
                                                    <label class="form-label fw-bold small">2. Vídeo do Produto (.mp4) — Opcional</label>
                                                    <input type="url" name="manual_video_url" id="manualVideoInput" placeholder="Cole o link direto https://down-ws-br.vod.susercontent.com/...mp4" class="form-control">
                                                    <div class="form-check mt-2">
                                                        <input class="form-check-input" type="checkbox" name="extract_video" value="1" id="extractVideoCheck">
                                                        <label class="form-check-label small fw-semibold" for="extractVideoCheck">🎬 Tentar extrair vídeo (.mp4) automaticamente via Browser (Shopee)</label>
                                                    </div>
                                                </div>
                                                <div class="col-md-12" id="videoValidationBox" style="display: none;">
                                                    <div class="p-3 border rounded bg-light">
                                                        <div id="videoValidationStatus" class="mb-2 fw-bold"></div>
                                                        <video id="videoPreviewPlayer" class="rounded" style="max-height: 240px; display: none;" controls></video>
                                                    </div>
                                                </div>
                                                <div class="col-md-12 d-flex gap-2 pt-2">
                                                    <button type="button" class="btn btn-outline-secondary fw-semibold" onclick="validateShopeeVideo()">🔍 Testar Vídeo</button>
                                                    <button type="submit" class="btn btn-primary flex-grow-1 fw-bold">🚀 Converter Link e Salvar na Fila ({current_date})</button>
                                                </div>
                                            </form>
                                        </div>
                                    </div>
                                </div>
                                <div class="col-lg-5">
                                    <div class="card border-0 shadow-sm rounded-3">
                                        <div class="card-header bg-secondary text-white fw-bold py-3">📥 Reaproveitar / Importar Oferta</div>
                                        <div class="card-body p-4">
                                            <p class="small text-muted">Selecione um produto de dias anteriores para revalidar preço e adicionar em <b>{current_date}</b>:</p>
                                            <form action="/import-offer" method="POST" class="row g-3">
                                                <input type="hidden" name="target_date" value="{current_date}">
                                                <div class="col-md-12">
                                                    <select name="product_url" class="form-select" required>
                                                        <option value="">Selecione...</option>
                                                        {import_options_html}
                                                    </select>
                                                </div>
                                                <div class="col-md-12">
                                                    <button type="submit" class="btn btn-warning w-100 fw-bold">📥 Importar para {current_date}</button>
                                                </div>
                                            </form>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- ================= LABORATÓRIO (Shopee | Amazon | ML) ================= -->
                        <div class="tab-pane fade {tabs_map['laboratorio'][0]}" role="tabpanel">
                            <div class="card mb-3 border-0 shadow-sm rounded-3">
                                <div class="card-header bg-white border-bottom pt-3 pb-0">
                                    <ul class="nav nav-pills mb-3 gap-2" role="tablist">
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {lab_sub_map['shopee'][1]}" href="/?tab=laboratorio&lab_sub=shopee&selected_date={current_date}">🟠 Shopee</a>
                                        </li>
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {lab_sub_map['amazon'][1]}" href="/?tab=laboratorio&lab_sub=amazon&selected_date={current_date}">🟠 Amazon</a>
                                        </li>
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {lab_sub_map['ml'][1]}" href="/?tab=laboratorio&lab_sub=ml&selected_date={current_date}">🟡 Mercado Livre</a>
                                        </li>
                                    </ul>
                                </div>
                                <div class="card-body tab-content">

                                    <!-- SHOPEE LAB (ATIVO!) -->
                                    <div class="tab-pane fade {lab_sub_map['shopee'][0]}">
                                        <div class="card mb-3 border-0 shadow-sm rounded-3">
                                            <div class="card-body d-flex flex-wrap justify-content-between align-items-center gap-3">
                                                <div>
                                                    <h5 class="fw-bold mb-1">🧪 Laboratório de Curadoria — Shopee</h5>
                                                    <div class="small text-muted">
                                                        Mix Shopee: <b>{shopee_cat_pct}% Categorias Selecionadas</b> + <b>{shopee_gen_pct}% Busca Geral</b> | Termos excluídos: <code>{html.escape(shopee_blocked_terms_raw or 'Nenhum')}</code>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        <form action="/shopee-lab/simulate" method="POST" class="row g-2 mb-3 p-3 bg-white rounded-3 shadow-sm">
                                            <input type="hidden" name="selected_date" value="{current_date}">
                                            <div class="col-md-2">
                                                <label class="small fw-bold">Qtd.</label>
                                                <input type="number" name="sim_count" value="10" min="2" max="30" class="form-control form-control-sm">
                                            </div>
                                            <div class="col-md-8">
                                                <label class="small fw-bold">Categorias Desejadas (marque para filtrar e salvar)</label>
                                                <div class="row g-1 p-2 bg-light border rounded" style="max-height: 130px; overflow-y: auto;">
                                                    {shopee_cat_lab_html}
                                                </div>
                                            </div>
                                            <div class="col-md-2 d-flex align-items-end">
                                                <button type="submit" class="btn fw-bold w-100 text-white" style="background-color:#ee4d2d;">🔍 Simular Shopee</button>
                                            </div>
                                        </form>
                                        {shopee_lab_results_html}
                                    </div>

                                    <!-- AMAZON LAB -->
                                    <div class="tab-pane fade {lab_sub_map['amazon'][0]}">
                                        <div class="card mb-3 border-0 shadow-sm rounded-3">
                                            <div class="card-body d-flex flex-wrap justify-content-between align-items-center gap-3">
                                                <div>
                                                    <h5 class="fw-bold mb-1">🧪 Laboratório de Curadoria — Amazon</h5>
                                                    <div class="small text-muted">
                                                        Fontes: <b>{amazon_deals_pct}% Deals</b> + <b>{amazon_bestsellers_pct}% Bestsellers</b> | Anti-repetição: <b>{amazon_hist_days} dias</b>.
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        <form action="/amazon-lab/simulate" method="POST" class="row g-2 mb-3 p-3 bg-white rounded-3 shadow-sm">
                                            <input type="hidden" name="selected_date" value="{current_date}">
                                            <div class="col-md-2">
                                                <label class="small fw-bold">Qtd.</label>
                                                <input type="number" name="sim_count" value="10" min="2" max="30" class="form-control form-control-sm">
                                            </div>
                                            <div class="col-md-8">
                                                <label class="small fw-bold">Categorias</label>
                                                <div class="row g-1 p-2 bg-light border rounded" style="max-height: 130px; overflow-y: auto;">
                                                    {amazon_cat_lab_html if amazon_cat_lab_html else '<span class="text-muted small">Nenhuma categoria configurada. Configure em ⚙️ Configurações → Mix & Lojas.</span>'}
                                                </div>
                                            </div>
                                            <div class="col-md-2 d-flex align-items-end">
                                                <button type="submit" class="btn btn-warning fw-bold w-100" {"disabled" if not AMAZON_AVAILABLE else ""}>🔍 Simular Amazon</button>
                                            </div>
                                        </form>
                                        {amazon_lab_results_html}
                                    </div>

                                    <!-- ML LAB -->
                                    <div class="tab-pane fade {lab_sub_map['ml'][0]}">
                                        <div class="card mb-3 border-0 shadow-sm rounded-3">
                                            <div class="card-body d-flex flex-wrap justify-content-between align-items-center gap-3">
                                                <div>
                                                    <h5 class="fw-bold mb-1">🧪 Laboratório de Curadoria — Mercado Livre</h5>
                                                    <div class="small text-muted">
                                                        Mix ML: <b>{ml_best_pct}% Mais Vendidos</b> + <b>{ml_offers_pct}% Ofertas</b> (Faixa: R$ {ml_min_price} a R$ {ml_max_price} | Desc. mín: {ml_min_discount}% | Págs: {ml_pages_per_run} de {ml_max_page_pool}) | Anti-repetição: <b>{ml_hist_days} dias</b>.
                                                    </div>
                                                </div>
                                                <form action="/ml-lab/simulate" method="POST" class="d-flex gap-2 align-items-center m-0">
                                                    <input type="hidden" name="selected_date" value="{current_date}">
                                                    <input type="number" name="sim_count" value="10" min="2" max="30" class="form-control form-control-sm" style="width: 80px;">
                                                    <button type="submit" class="btn btn-warning fw-bold text-nowrap">🔍 Simular ML</button>
                                                </form>
                                            </div>
                                        </div>
                                        {ml_lab_results_html}
                                    </div>

                                </div>
                            </div>
                        </div>

                        <!-- ================= COMPARADOR ================= -->
                        <div class="tab-pane fade {tabs_map['comparador'][0]}" role="tabpanel">
                            <div class="d-flex flex-wrap justify-content-between align-items-center bg-white p-3 rounded-3 shadow-sm mb-3 gap-2">
                                <form method="GET" action="/" class="d-flex flex-wrap gap-2 align-items-center m-0">
                                    <input type="hidden" name="tab" value="comparador">
                                    <input type="hidden" name="selected_date" value="{current_date}">
                                    <span class="fw-bold">Auditar Período:</span>
                                    <select name="comp_days" class="form-select form-select-sm" style="width:auto;" onchange="this.form.submit()">
                                        <option value="7" {"selected" if scan_days == 7 else ""}>Últimos 7 dias</option>
                                        <option value="14" {"selected" if scan_days == 14 else ""}>Últimos 14 dias</option>
                                        <option value="30" {"selected" if scan_days == 30 else ""}>Últimos 30 dias</option>
                                    </select>
                                    <select name="comp_store" class="form-select form-select-sm" style="width:auto;" onchange="this.form.submit()">
                                        <option value="all" {"selected" if comp_store == "all" else ""}>🌐 Todas as Lojas</option>
                                        <option value="shopee" {"selected" if comp_store == "shopee" else ""}>🟠 Shopee</option>
                                        <option value="amazon" {"selected" if comp_store == "amazon" else ""}>🟠 Amazon</option>
                                        <option value="mercado" {"selected" if comp_store == "mercado" else ""}>🟡 Mercado Livre</option>
                                    </select>
                                    <span class="badge bg-secondary ms-2">Analisados: {comp_report['total_scanned']}</span>
                                    <span class="badge bg-danger">Conflitos: {comp_report['total_clusters']}</span>
                                </form>
                                <form action="/comparator/clean-duplicates" method="POST" class="m-0">
                                    <input type="hidden" name="comp_days" value="{scan_days}">
                                    <input type="hidden" name="comp_store" value="{comp_store}">
                                    <button type="submit" class="btn btn-sm btn-danger fw-bold" {"disabled" if comp_report['total_suggested_deletes'] == 0 else ""}>
                                        🧹 Excluir {comp_report['total_suggested_deletes']} Duplicadas Pendentes
                                    </button>
                                </form>
                            </div>
                            {clusters_html}
                        </div>

                        <!-- ================= VENDAS ================= -->
                        <div class="tab-pane fade {tabs_map['vendas'][0]}" role="tabpanel">
                            <div class="card mb-3 border-0 shadow-sm rounded-3">
                                <div class="card-body py-3">
                                    <form method="GET" action="/" class="row g-2 align-items-center">
                                        <input type="hidden" name="tab" value="vendas">
                                        <div class="col-md-3">
                                            <select name="sales_days" class="form-select form-select-sm" onchange="this.form.submit()">
                                                <option value="7" {"selected" if sales_days == 7 else ""}>Últimos 7 dias</option>
                                                <option value="15" {"selected" if sales_days == 15 else ""}>Últimos 15 dias</option>
                                                <option value="30" {"selected" if sales_days == 30 else ""}>Últimos 30 dias</option>
                                                <option value="60" {"selected" if sales_days == 60 else ""}>Últimos 60 dias</option>
                                            </select>
                                        </div>
                                        <div class="col-md-3">
                                            <select name="sales_platform" class="form-select form-select-sm" onchange="this.form.submit()">
                                                <option value="all" {"selected" if sales_platform == "all" else ""}>🌐 Todas as Plataformas</option>
                                                <option value="shopee" {"selected" if sales_platform == "shopee" else ""}>🟠 Shopee</option>
                                            </select>
                                        </div>
                                        <div class="col-md-3">
                                            <select name="sales_status" class="form-select form-select-sm" onchange="this.form.submit()">
                                                <option value="all" {"selected" if sales_status == "all" else ""}>Todos os Status</option>
                                                <option value="pending" {"selected" if sales_status == "pending" else ""}>⏳ Pendentes</option>
                                                <option value="completed" {"selected" if sales_status == "completed" else ""}>✅ Concluídos</option>
                                                <option value="cancelled" {"selected" if sales_status == "cancelled" else ""}>❌ Cancelados</option>
                                            </select>
                                        </div>
                                        <div class="col-md-3">
                                            <button type="submit" class="btn btn-sm btn-success w-100 fw-bold">🔄 Atualizar</button>
                                        </div>
                                    </form>
                                </div>
                            </div>
                            <div class="row g-3 mb-3">
                                <div class="col-md-3">
                                    <div class="card stat-card shadow-sm text-center h-100">
                                        <div class="card-body py-3">
                                            <small class="text-muted fw-semibold">🛍️ Total de Pedidos ({sales_days}d)</small>
                                            <h3 class="fw-bold text-primary mb-1">{sales_data['total_orders']}</h3>
                                            <small class="text-muted">✅ {sales_data['completed_orders']} | ⏳ {sales_data['pending_orders']} | ❌ {sales_data['cancelled_orders']}</small>
                                        </div>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="card stat-card shadow-sm text-center h-100">
                                        <div class="card-body py-3">
                                            <small class="text-muted fw-semibold">🛒 GMV</small>
                                            <h3 class="fw-bold text-dark mb-0">{format_brl(sales_data['total_gmv'])}</h3>
                                        </div>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="card stat-card shadow-sm text-center h-100">
                                        <div class="card-body py-3">
                                            <small class="text-muted fw-semibold">💵 Comissão Estimada</small>
                                            <h3 class="fw-bold text-warning mb-0">{format_brl(sales_data['total_commission'])}</h3>
                                        </div>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="card stat-card shadow-sm text-center h-100">
                                        <div class="card-body py-3">
                                            <small class="text-muted fw-semibold">✅ Comissão Aprovada</small>
                                            <h3 class="fw-bold text-success mb-0">{format_brl(sales_data['completed_commission'])}</h3>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div class="card border-0 shadow-sm rounded-3 overflow-hidden">
                                <div class="table-responsive">
                                    <table class="table table-striped table-sm align-middle mb-0">
                                        <thead class="table-light">
                                            <tr><th>Data/Hora</th><th>Loja</th><th>Pedido</th><th>Itens</th><th class="text-center">Qtd</th><th>Valor</th><th>Comissão</th><th>Status</th></tr>
                                        </thead>
                                        <tbody>{sales_rows_html}</tbody>
                                    </table>
                                </div>
                            </div>
                        </div>

                        <!-- ================= CONFIGURAÇÕES ================= -->
                        <div class="tab-pane fade {tabs_map['config'][0]}" role="tabpanel">
                            <div class="card border-0 shadow-sm rounded-3">
                                <div class="card-header bg-white border-bottom pt-3 pb-0">
                                    <ul class="nav nav-pills mb-3 gap-2" role="tablist">
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {cfg_sub_map['geral'][1]}" href="/?tab=config&cfg_sub=geral&selected_date={current_date}">⏰ 1. Horários & Disparos</a>
                                        </li>
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {cfg_sub_map['mix'][1]}" href="/?tab=config&cfg_sub=mix&selected_date={current_date}">🎛️ 2. Mix & Lojas</a>
                                        </li>
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {cfg_sub_map['ia'][1]}" href="/?tab=config&cfg_sub=ia&selected_date={current_date}">🤖 3. Copy com IA</a>
                                        </li>
                                        <li class="nav-item">
                                            <a class="nav-link fw-semibold {cfg_sub_map['dedup'][1]}" href="/?tab=config&cfg_sub=dedup&selected_date={current_date}">🛡️ 4. Anti-Repetição</a>
                                        </li>
                                    </ul>
                                </div>
                                <div class="card-body p-4 tab-content">

                                    <!-- GERAL -->
                                    <div class="tab-pane fade {cfg_sub_map['geral'][0]}">
                                        <h6 class="fw-bold mb-3">⏰ Horários de Disparo & Janela Operacional</h6>
                                        <form action="/update-config" method="POST" class="row g-3">
                                            <div class="col-md-4">
                                                <label class="form-label small fw-bold">Início</label>
                                                <input type="time" name="start_time" value="{configs.get('start_time', '08:00')}" class="form-control" required>
                                            </div>
                                            <div class="col-md-4">
                                                <label class="form-label small fw-bold">Término</label>
                                                <input type="time" name="end_time" value="{configs.get('end_time', '22:00')}" class="form-control" required>
                                            </div>
                                            <div class="col-md-4">
                                                <label class="form-label small fw-bold">Intervalo (min)</label>
                                                <input type="number" name="interval_minutes" value="{configs.get('interval_minutes', '30')}" class="form-control" required>
                                            </div>
                                            <div class="col-md-6">
                                                <label class="form-label small fw-bold">Quantidade por Lote Automático</label>
                                                <input type="number" name="auto_refill_count" value="{configs.get('auto_refill_count', '15')}" class="form-control" required>
                                            </div>
                                            <div class="col-md-6 d-flex align-items-end">
                                                <div class="form-check form-switch">
                                                    <input class="form-check-input" type="checkbox" name="auto_refill_enabled" value="1" id="autoRefillSwitch" {"checked" if configs.get('auto_refill_enabled', '1') == '1' else ""}>
                                                    <label class="form-check-label fw-bold" for="autoRefillSwitch">🔄 Auto-Abastecer Fila quando zerar</label>
                                                </div>
                                            </div>
                                            <div class="col-md-12 pt-2">
                                                <button type="submit" class="btn btn-primary fw-bold px-4">💾 Salvar</button>
                                            </div>
                                        </form>
                                    </div>

                                    <!-- MIX & LOJAS -->
                                    <div class="tab-pane fade {cfg_sub_map['mix'][0]}">
                                        <form action="/update-mix" method="POST" class="row g-3">

                                            <!-- CABEÇALHO: MIX -->
                                            <div class="col-md-12">
                                                <h6 class="fw-bold mb-2">🎛️ Mix de Lojas na Geração Automática</h6>
                                                <div class="row g-2 p-3 bg-warning-subtle border border-warning rounded">
                                                    <div class="col-md-3">
                                                        <label class="small fw-bold">% Shopee</label>
                                                        <input type="number" min="0" max="100" name="mix_shopee_pct" value="{mix_shopee_pct}" class="form-control" required>
                                                    </div>
                                                    <div class="col-md-3">
                                                        <label class="small fw-bold">% Amazon</label>
                                                        <input type="number" min="0" max="100" name="mix_amazon_pct" value="{mix_amazon_pct}" class="form-control" required>
                                                    </div>
                                                    <div class="col-md-3">
                                                        <label class="small fw-bold">% ML (calc)</label>
                                                        <input type="number" value="{mix_ml_pct}" class="form-control" disabled>
                                                    </div>
                                                    <div class="col-md-3 d-flex align-items-end">
                                                        <button type="button" class="btn btn-outline-dark w-100" onclick="restaurarMixPadrao()">🔄 Restaurar 40/40/20</button>
                                                    </div>
                                                    <div class="col-md-6">
                                                        <div class="form-check form-switch">
                                                            <input class="form-check-input" type="checkbox" name="ml_auto_enabled" value="1" id="mlAutoSwitch" {"checked" if ml_auto_enabled else ""}>
                                                            <label class="form-check-label fw-bold" for="mlAutoSwitch">🟡 Incluir Mercado Livre no mix</label>
                                                        </div>
                                                    </div>
                                                    <div class="col-md-6">
                                                        <div class="form-check form-switch">
                                                            <input class="form-check-input" type="checkbox" name="amazon_auto_enabled" value="1" id="amazonAutoSwitch" {"checked" if amazon_enabled else ""} {"disabled" if not AMAZON_AVAILABLE else ""}>
                                                            <label class="form-check-label fw-bold" for="amazonAutoSwitch">🟠 Incluir Amazon no mix {"(módulo amazon_auto.py não instalado)" if not AMAZON_AVAILABLE else ""}</label>
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>

                                            <!-- SHOPEE -->
                                            <div class="col-md-12 mt-3">
                                                <h6 class="fw-bold mb-2">🟠 Shopee (Categorias Desejadas & Exclusão de Termos)</h6>
                                                <div class="row g-2 p-3 bg-light border rounded">
                                                    <div class="col-md-4">
                                                        <label class="small fw-bold">% Busca Geral (Top Ofertas)</label>
                                                        <input type="number" min="0" max="100" name="shopee_general_pct" value="{shopee_gen_pct}" class="form-control">
                                                        <small class="text-muted">{shopee_cat_pct}% virá das Categorias marcadas ao lado</small>
                                                    </div>
                                                    <div class="col-md-8">
                                                        <label class="small fw-bold">Palavras-chave extras para Busca Geral (opcional)</label>
                                                        <input type="text" name="auto_keywords" value="{html.escape(configs.get('auto_keywords', ''))}" placeholder="Ex: air fryer, kit ferramentas, tênis corrida" class="form-control">
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold text-success">✅ Categorias Desejadas na Shopee ({shopee_cat_pct}% do lote):</label>
                                                        <div class="row g-2 p-2 bg-white border rounded">
                                                            {shopee_cat_checkboxes_html}
                                                        </div>
                                                        <input type="text" name="shopee_custom_categories" value="{html.escape(shopee_custom_cats_str)}" placeholder="Outras categorias/nichos desejados (separados por vírgula)" class="form-control form-control-sm mt-2">
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold text-danger">🚫 Termos Excluídos na Shopee (ex: almofada, capa, pelúcia):</label>
                                                        <input type="text" name="shopee_blocked_terms" value="{html.escape(shopee_blocked_terms_raw)}" placeholder="Ex: almofada, capa de almofada, chaveiro, adesivo" class="form-control">
                                                    </div>
                                                </div>
                                            </div>

                                            <!-- AMAZON -->
                                            <div class="col-md-12 mt-3">
                                                <h6 class="fw-bold mb-2">🟠 Amazon</h6>
                                                <div class="row g-2 p-3 bg-light border rounded">
                                                    <div class="col-md-4">
                                                        <label class="small fw-bold">% Deals dentro da Amazon</label>
                                                        <input type="number" min="0" max="100" name="amazon_bestsellers_pct" value="{amazon_deals_pct}" class="form-control">
                                                        <small class="text-muted">{amazon_bestsellers_pct}% Bestsellers</small>
                                                    </div>
                                                    <div class="col-md-4">
                                                        <label class="small fw-bold">Histórico (dias)</label>
                                                        <input type="number" name="amazon_history_days" value="{amazon_hist_days}" class="form-control">
                                                    </div>
                                                    <div class="col-md-4"></div>
                                                    <div class="col-md-6">
                                                        <label class="small fw-bold">URL Bestsellers</label>
                                                        <input type="url" name="amazon_bestsellers_url" value="{html.escape(amazon_bestsellers_url)}" class="form-control">
                                                    </div>
                                                    <div class="col-md-6">
                                                        <label class="small fw-bold">URL Deals</label>
                                                        <input type="url" name="amazon_deals_url" value="{html.escape(amazon_deals_url)}" class="form-control">
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold text-success">✅ Categorias Amazon:</label>
                                                        <div class="row g-2 p-2 bg-white border rounded">
                                                            {amazon_cat_config_html if amazon_cat_config_html else '<span class="text-muted small">Módulo não instalado</span>'}
                                                        </div>
                                                        <input type="text" name="custom_categories" value="{html.escape(amazon_cat_extra_str)}" placeholder="Categorias extras (vírgula)" class="form-control form-control-sm mt-2">
                                                    </div>
                                                </div>
                                            </div>

                                            <!-- MERCADO LIVRE -->
                                            <div class="col-md-12 mt-3">
                                                <h6 class="fw-bold mb-2">🟡 Mercado Livre (100% Parametrizado)</h6>
                                                <div class="row g-2 p-3 bg-light border rounded">
                                                    <div class="col-md-3">
                                                        <label class="small fw-bold">% Mais Vendidos</label>
                                                        <input type="number" min="0" max="100" name="ml_best_sellers_pct" value="{ml_best_pct}" class="form-control">
                                                        <small class="text-muted">{ml_offers_pct}% Ofertas</small>
                                                    </div>
                                                    <div class="col-md-3">
                                                        <label class="small fw-bold">Histórico Anti-Repetição (dias)</label>
                                                        <input type="number" min="1" max="60" name="ml_history_days" value="{ml_hist_days}" class="form-control">
                                                    </div>
                                                    <div class="col-md-2">
                                                        <label class="small fw-bold">Preço Mín. (R$)</label>
                                                        <input type="number" min="0" step="1" name="ml_min_price" value="{ml_min_price}" class="form-control" title="0 = sem mínimo">
                                                    </div>
                                                    <div class="col-md-2">
                                                        <label class="small fw-bold">Preço Máx. (R$)</label>
                                                        <input type="number" min="0" step="1" name="ml_max_price" value="{ml_max_price}" class="form-control" title="0 = sem limite máximo">
                                                    </div>
                                                    <div class="col-md-2">
                                                        <label class="small fw-bold">Desc. Mín. (%)</label>
                                                        <input type="number" min="0" max="90" name="ml_min_discount" value="{ml_min_discount}" class="form-control" title="Desconto mínimo real exigido nas Ofertas">
                                                    </div>

                                                    <div class="col-md-6 mt-2">
                                                        <label class="small fw-bold">Páginas Sorteadas por Busca (por URL)</label>
                                                        <input type="number" min="1" max="30" name="ml_pages_per_run" value="{ml_pages_per_run}" class="form-control">
                                                        <small class="text-muted">Quantas páginas ler a cada simulação/lote (ex: 4 páginas).</small>
                                                    </div>
                                                    <div class="col-md-6 mt-2">
                                                        <label class="small fw-bold">Alcance Máximo de Páginas (Pool)</label>
                                                        <input type="number" min="1" max="50" name="ml_max_page_pool" value="{ml_max_page_pool}" class="form-control">
                                                        <small class="text-muted">Sorteia páginas entre 1 e este número (ex: 15 ou 25) para variar os produtos.</small>
                                                    </div>

                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold">URL Mais Vendidos ML</label>
                                                        <input type="url" name="ml_bestsellers_url" value="{html.escape(ml_bestsellers_url)}" class="form-control">
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold">URL(s) de Ofertas ML (aceita vários links: 1 por linha ou separados por vírgula)</label>
                                                        <textarea name="ml_offers_url" rows="3" class="form-control font-monospace small" placeholder="https://www.mercadolivre.com.br/ofertas?promotion_type=lightning&#10;https://www.mercadolivre.com.br/ofertas">{html.escape(ml_offers_url)}</textarea>
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold text-danger">🚫 Categorias excluídas em Mais Vendidos</label>
                                                        <div class="row g-2 p-2 bg-white border rounded">{cat_checkboxes_html}</div>
                                                        <input type="text" name="custom_excl_bestsellers" value="{html.escape(custom_bestsellers_str)}" placeholder="Extras" class="form-control form-control-sm mt-2">
                                                    </div>
                                                    <div class="col-md-12 mt-2">
                                                        <label class="small fw-bold">🚫 Termos excluídos em Ofertas</label>
                                                        <input type="text" name="ml_excluded_categories_offers" value="{html.escape(excl_offers_raw)}" placeholder="Ex: gift card, digital" class="form-control">
                                                    </div>
                                                </div>
                                            </div>

                                            <div class="col-md-12 pt-2">
                                                <button type="submit" class="btn btn-warning fw-bold px-4">💾 Salvar Mix & Lojas</button>
                                            </div>
                                        </form>
                                    </div>

                                    <!-- IA -->
                                    <div class="tab-pane fade {cfg_sub_map['ia'][0]}">
                                        <h6 class="fw-bold mb-3">🤖 Copywriting com IA (Google Gemini)</h6>
                                        <form action="/update-ai-config" method="POST" class="row g-3">
                                            <div class="col-md-12">
                                                <div class="form-check form-switch p-3 bg-info-subtle border border-info rounded">
                                                    <input class="form-check-input ms-0 me-2" type="checkbox" name="ai_copy_enabled" value="1" id="aiCopySwitch" {"checked" if ai_enabled else ""}>
                                                    <label class="form-check-label fw-bold" for="aiCopySwitch">✨ Ativar gancho automático com IA nas novas ofertas</label>
                                                </div>
                                            </div>
                                            <div class="col-md-6">
                                                <label class="form-label small fw-bold">Estilo</label>
                                                <select name="ai_copy_style" class="form-select">
                                                    <option value="urgencia_comparativo" {"selected" if ai_style == "urgencia_comparativo" else ""}>🚨 Urgência + Comparativo</option>
                                                    <option value="calculo_unitario" {"selected" if ai_style == "calculo_unitario" else ""}>🧮 Preço por Unidade (Kits)</option>
                                                    <option value="achadinho_amigavel" {"selected" if ai_style == "achadinho_amigavel" else ""}>😍 Achadinho VIP</option>
                                                </select>
                                            </div>
                                            <div class="col-md-6">
                                                <label class="form-label small fw-bold">Instrução customizada</label>
                                                <input type="text" name="ai_copy_custom_prompt" value="{html.escape(ai_custom_prompt)}" class="form-control">
                                            </div>
                                            <div class="col-md-12 pt-2">
                                                <button type="submit" class="btn btn-info fw-bold px-4">💾 Salvar</button>
                                            </div>
                                        </form>
                                    </div>

                                    <!-- DEDUP -->
                                    <div class="tab-pane fade {cfg_sub_map['dedup'][0]}">
                                        <h6 class="fw-bold mb-3">🛡️ Anti-Repetição & Blacklist Global</h6>
                                        <form action="/comparator/update-settings" method="POST" class="row g-3">
                                            <div class="col-md-6">
                                                <label class="form-label small fw-bold">Janela Histórica (dias)</label>
                                                <input type="number" min="1" max="60" name="dedup_history_days" value="{dedup_days_cfg}" class="form-control" required>
                                            </div>
                                            <div class="col-md-6">
                                                <label class="form-label small fw-bold">Similaridade mínima (%)</label>
                                                <input type="number" min="35" max="95" name="dedup_similarity_threshold" value="{int(dedup_thresh_cfg)}" class="form-control" required>
                                            </div>
                                            <div class="col-md-12">
                                                <label class="form-label small fw-bold">🚫 Termos bloqueados em Todas as Lojas (vírgula)</label>
                                                <textarea name="dedup_blocked_terms" rows="3" class="form-control">{html.escape(blocked_terms_cfg)}</textarea>
                                            </div>
                                            <div class="col-md-12 pt-2">
                                                <button type="submit" class="btn btn-dark fw-bold px-4">💾 Salvar</button>
                                            </div>
                                        </form>
                                    </div>

                                </div>
                            </div>
                        </div>

                    </div>
                </main>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            function blockTermFromQueueBtn(btn) {{
                const offerId = btn.getAttribute('data-offer-id');
                const suggested = btn.getAttribute('data-suggested') || '';
                const fullTitle = btn.getAttribute('data-title') || '';
                const term = prompt(
                    '🚫 Bloquear termo indesejado e remover da fila pendente:\\n\\nProduto: ' + fullTitle + '\\n\\nDigite a palavra ou termo que deseja banir (ex: almofada):',
                    suggested
                );
                if (term && term.trim().length >= 2) {{
                    document.getElementById('quickBlockTermInput').value = term.trim();
                    document.getElementById('quickBlockOfferIdInput').value = offerId;
                    document.getElementById('quickBlockForm').submit();
                }}
            }}

            async function validateShopeeVideo() {{
                const urlInput = document.getElementById('productUrlInput');
                const manualInput = document.getElementById('manualVideoInput');
                const box = document.getElementById('videoValidationBox');
                const statusEl = document.getElementById('videoValidationStatus');
                const player = document.getElementById('videoPreviewPlayer');
                const check = document.getElementById('extractVideoCheck');
                const prodVal = urlInput.value.trim();
                const manualVal = manualInput.value.trim();
                if (!prodVal && !manualVal) {{
                    alert('Cole o link do produto ou o link direto do vídeo (.mp4) primeiro!');
                    return;
                }}
                if (prodVal.toLowerCase().includes('.mp4') && !manualVal) {{
                    manualInput.value = prodVal;
                    urlInput.value = '';
                }}
                box.style.display = 'block';
                player.style.display = 'none';
                statusEl.innerHTML = '⏳ Validando vídeo... aguarde.';
                const formData = new FormData();
                formData.append('product_url', urlInput.value.trim());
                formData.append('manual_video_url', manualInput.value.trim());
                try {{
                    const resp = await fetch('/validate-video', {{ method: 'POST', body: formData }});
                    const data = await resp.json();
                    statusEl.innerHTML = data.message;
                    if (data.success && data.video_url) {{
                        player.src = data.video_url;
                        player.style.display = 'block';
                        manualInput.value = data.video_url;
                        check.checked = true;
                    }}
                }} catch (err) {{
                    statusEl.innerHTML = '❌ Erro na requisição: ' + err;
                }}
            }}

            function restaurarMixPadrao() {{
                const shopee = document.querySelector('input[name="mix_shopee_pct"]');
                const amazon = document.querySelector('input[name="mix_amazon_pct"]');
                const amazonCheck = document.querySelector('input[name="amazon_auto_enabled"]');
                const mlCheck = document.querySelector('input[name="ml_auto_enabled"]');
                if (shopee) shopee.value = 40;
                if (amazon) amazon.value = 40;
                if (amazonCheck) amazonCheck.checked = true;
                if (mlCheck) mlCheck.checked = true;
            }}

            let secondsLeft = {remaining_seconds};
            const timerEl = document.getElementById('countdown-timer');
            function updateCountdown() {{
                if (secondsLeft <= 0) {{ timerEl.textContent = "00:00"; return; }}
                const mins = Math.floor(secondsLeft / 60);
                const secs = secondsLeft % 60;
                timerEl.textContent = String(mins).padStart(2, '0') + ":" + String(secs).padStart(2, '0');
                secondsLeft--;
                if (secondsLeft === 0) {{ setTimeout(() => window.location.reload(), 1000); }}
            }}
            if (secondsLeft > 0) {{
                updateCountdown();
                setInterval(updateCountdown, 1000);
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# ─────────── Rota: Bloquear Termo Direto da Tela de Fila / Laboratório ───────────
@app.post("/queue/block-term")
async def block_term_from_queue(
    term: str = Form(...),
    offer_id: int = Form(0),
    current_date: str = Form(...),
):
    clean_t = term.strip().lower()
    purged = 0
    if clean_t:
        for cfg_key in ("dedup_blocked_terms", "shopee_blocked_terms"):
            current_csv = get_config(cfg_key) or ""
            terms_list = [t.strip() for t in current_csv.split(",") if t.strip()]
            if clean_t not in [x.lower() for x in terms_list]:
                terms_list.append(clean_t)
                update_config(cfg_key, ", ".join(terms_list))

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, formatted_message FROM queue WHERE status = 'pending'"
        ).fetchall()
        ids_to_remove = set()
        if offer_id > 0:
            ids_to_remove.add(offer_id)
        for rid, rmsg in rows:
            title_norm = offer_comparator.extract_title_from_formatted_message(
                rmsg or ""
            ).lower()
            if clean_t in title_norm or clean_t in (rmsg or "").lower():
                ids_to_remove.add(rid)
        conn.close()

        for del_id in ids_to_remove:
            delete_offer(del_id)
            purged += 1

        # Remove também de eventual simulação aberta no cache do Laboratório Shopee
        shp_lab = _PORTAL_CACHE.get("shopee_lab")
        if shp_lab and shp_lab.get("items"):
            shp_lab["items"] = [
                it
                for it in shp_lab["items"]
                if clean_t not in (it.get("title") or "").lower()
            ]

    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={current_date}&blocked_term={clean_t}&purged_count={purged}",
        status_code=303,
    )


# ─────────── Rotas Shopee Lab (NOVAS!) ───────────
@app.post("/shopee-lab/simulate")
async def shopee_lab_simulate(
    selected_date: str = Form(...),
    sim_count: int = Form(10),
    cats: list[str] = Form([]),
):
    if cats:
        update_config("shopee_target_categories", ", ".join(cats))

    app_id = os.environ.get("SHOPEE_APP_ID", "")
    app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
    if app_id and app_secret:
        api = ShopeeAffiliateAPI(app_id, app_secret)
        sim_data = await asyncio.to_thread(
            api.simulate_shopee_offers_selection,
            max(2, min(30, sim_count)),
            selected_date,
            cats if cats else None,
        )
        _PORTAL_CACHE["shopee_lab"] = sim_data

    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=shopee&selected_date={selected_date}",
        status_code=303,
    )


@app.post("/shopee-lab/approve-one")
async def shopee_lab_approve_one(
    item_index: int = Form(...), target_date: str = Form(...)
):
    lab = _PORTAL_CACHE.get("shopee_lab")
    approved = 0
    app_id = os.environ.get("SHOPEE_APP_ID", "")
    app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
    if lab and 0 <= item_index < len(lab["items"]) and app_id and app_secret:
        api = ShopeeAffiliateAPI(app_id, app_secret)
        cand = lab["items"][item_index]
        ok = await asyncio.to_thread(
            api.enqueue_simulated_shopee_item, cand, target_date, 0
        )
        if ok:
            approved = 1
            lab["items"].pop(item_index)
            _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=shopee&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


@app.post("/shopee-lab/approve-all")
async def shopee_lab_approve_all(target_date: str = Form(...)):
    lab = _PORTAL_CACHE.get("shopee_lab")
    approved = 0
    app_id = os.environ.get("SHOPEE_APP_ID", "")
    app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
    if lab and lab.get("items") and app_id and app_secret:
        api = ShopeeAffiliateAPI(app_id, app_secret)
        for cand in list(lab["items"]):
            ok = await asyncio.to_thread(
                api.enqueue_simulated_shopee_item, cand, target_date, 0
            )
            if ok:
                approved += 1
        _PORTAL_CACHE["shopee_lab"] = None
        _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=shopee&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


# ─────────── Rotas IA ───────────
@app.post("/ai/regenerate/{offer_id}")
async def ai_regenerate_single(offer_id: int, current_date: str = Form(...)):
    ok = await asyncio.to_thread(apply_ai_hook_to_existing_offer, offer_id)
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={current_date}&ai_updated={1 if ok else 0}",
        status_code=303,
    )


@app.post("/ai/regenerate-all-pending")
async def ai_regenerate_all_pending(target_date: str = Form(...)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM queue WHERE target_date = ? AND status = 'pending' ORDER BY id ASC",
        (target_date,),
    )
    ids = [r[0] for r in cursor.fetchall()]
    conn.close()
    updated = 0
    for oid in ids:
        ok = await asyncio.to_thread(apply_ai_hook_to_existing_offer, oid)
        if ok:
            updated += 1
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={target_date}&ai_updated={updated}",
        status_code=303,
    )


# ─────────── Rotas ML Lab ───────────
@app.post("/ml-lab/simulate")
async def ml_lab_simulate(selected_date: str = Form(...), sim_count: int = Form(10)):
    sim_data = await asyncio.to_thread(
        ml_auto.simulate_ml_offers_selection, max(2, min(30, sim_count))
    )
    _PORTAL_CACHE["ml_lab"] = sim_data
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=ml&selected_date={selected_date}",
        status_code=303,
    )


@app.post("/ml-lab/approve-one")
async def ml_lab_approve_one(item_index: int = Form(...), target_date: str = Form(...)):
    lab_data = _PORTAL_CACHE.get("ml_lab")
    approved = 0
    if lab_data and 0 <= item_index < len(lab_data["items"]):
        cand = lab_data["items"][item_index]
        ok = await asyncio.to_thread(
            ml_auto.convert_and_enqueue_ml_item,
            cand["product_url"],
            target_date,
            cand.get("category_name", "ML: Laboratório"),
            0,
            cand,
        )
        if ok:
            approved = 1
            lab_data["items"].pop(item_index)
            _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=ml&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


@app.post("/ml-lab/approve-all")
async def ml_lab_approve_all(target_date: str = Form(...)):
    lab_data = _PORTAL_CACHE.get("ml_lab")
    approved = 0
    if lab_data and lab_data.get("items"):
        for cand in list(lab_data["items"]):
            ok = await asyncio.to_thread(
                ml_auto.convert_and_enqueue_ml_item,
                cand["product_url"],
                target_date,
                cand.get("category_name", "ML: Laboratório"),
                0,
                cand,
            )
            if ok:
                approved += 1
        _PORTAL_CACHE["ml_lab"] = None
        _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=ml&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


# ─────────── Rotas Amazon Lab ───────────
@app.post("/amazon-lab/simulate")
async def amazon_lab_simulate(
    selected_date: str = Form(...),
    sim_count: int = Form(10),
    cats: list[str] = Form([]),
):
    if not AMAZON_AVAILABLE:
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={selected_date}",
            status_code=303,
        )
    if cats:
        update_config("amazon_target_categories", ", ".join(cats))

    data = await asyncio.to_thread(
        amazon_auto.simulate_amazon_offers_selection,
        max(2, min(30, sim_count)),
        None,
        cats if cats else None,
    )
    _PORTAL_CACHE["amazon_lab"] = data
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={selected_date}",
        status_code=303,
    )


@app.post("/amazon-lab/approve-one")
async def amazon_lab_approve_one(
    item_index: int = Form(...), target_date: str = Form(...)
):
    if not AMAZON_AVAILABLE:
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={target_date}",
            status_code=303,
        )
    lab = _PORTAL_CACHE.get("amazon_lab")
    approved = 0
    if lab and 0 <= item_index < len(lab["items"]):
        cand = lab["items"][item_index]
        ok = await asyncio.to_thread(
            amazon_auto.convert_and_enqueue_amazon_item,
            cand["asin"],
            target_date,
            f"Amazon: {cand.get('category_slug', 'Geral')}",
            cand,
            0,
        )
        if ok:
            approved = 1
            lab["items"].pop(item_index)
            _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


@app.post("/amazon-lab/approve-all")
async def amazon_lab_approve_all(target_date: str = Form(...)):
    if not AMAZON_AVAILABLE:
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={target_date}",
            status_code=303,
        )
    lab = _PORTAL_CACHE.get("amazon_lab")
    approved = 0
    if lab and lab.get("items"):
        for cand in list(lab["items"]):
            ok = await asyncio.to_thread(
                amazon_auto.convert_and_enqueue_amazon_item,
                cand["asin"],
                target_date,
                f"Amazon: {cand.get('category_slug', 'Geral')}",
                cand,
                0,
            )
            if ok:
                approved += 1
        _PORTAL_CACHE["amazon_lab"] = None
        _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=laboratorio&lab_sub=amazon&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


# ─────────── Rotas Config ───────────
@app.post("/update-config")
async def update_parameters(
    start_time: str = Form(...),
    end_time: str = Form(...),
    interval_minutes: str = Form(...),
    auto_refill_count: str = Form("15"),
    auto_refill_enabled: str = Form(None),
):
    update_config("start_time", start_time)
    update_config("end_time", end_time)
    update_config("interval_minutes", interval_minutes)
    update_config("auto_refill_count", auto_refill_count)
    update_config("auto_refill_enabled", "1" if auto_refill_enabled == "1" else "0")
    return RedirectResponse(
        url="/?tab=config&cfg_sub=geral&saved_cfg=1", status_code=303
    )


@app.post("/update-mix")
async def update_mix(
    request: Request,
    amazon_auto_enabled: str = Form(None),
    ml_auto_enabled: str = Form(None),
    mix_shopee_pct: str = Form("60"),
    mix_amazon_pct: str = Form("0"),
    shopee_general_pct: str = Form("30"),
    shopee_custom_categories: str = Form(""),
    shopee_blocked_terms: str = Form(""),
    auto_keywords: str = Form(""),
    amazon_bestsellers_pct: str = Form("60"),
    amazon_bestsellers_url: str = Form("https://www.amazon.com.br/gp/bestsellers"),
    amazon_deals_url: str = Form("https://www.amazon.com.br/deals"),
    amazon_history_days: str = Form("7"),
    custom_categories: str = Form(""),
    ml_best_sellers_pct: str = Form("30"),
    ml_history_days: str = Form("7"),
    ml_bestsellers_url: str = Form("https://www.mercadolivre.com.br/mais-vendidos"),
    ml_offers_url: str = Form("https://www.mercadolivre.com.br/ofertas"),
    ml_min_price: str = Form("20"),
    ml_max_price: str = Form("350"),
    ml_min_discount: str = Form("12"),
    ml_pages_per_run: str = Form("4"),
    ml_max_page_pool: str = Form("15"),
    custom_excl_bestsellers: str = Form(""),
    ml_excluded_categories_offers: str = Form(""),
):
    form = await request.form()

    # Mix
    update_config("amazon_auto_enabled", "1" if amazon_auto_enabled == "1" else "0")
    update_config("ml_auto_enabled", "1" if ml_auto_enabled == "1" else "0")
    update_config("mix_shopee_pct", str(max(0, min(100, int(mix_shopee_pct or 60)))))
    update_config("mix_amazon_pct", str(max(0, min(100, int(mix_amazon_pct or 0)))))

    # Shopee (Checkboxes + Extras + Termos Bloqueados)
    update_config(
        "shopee_general_pct", str(max(0, min(100, int(shopee_general_pct or 30))))
    )
    selected_shp_cats = form.getlist("shopee_cat_checks")
    extra_shp_cats = [
        c.strip() for c in shopee_custom_categories.split(",") if c.strip()
    ]
    all_shp_cats = list(selected_shp_cats) + [
        e
        for e in extra_shp_cats
        if e.lower() not in {x.lower() for x in selected_shp_cats}
    ]
    update_config("shopee_target_categories", ", ".join(all_shp_cats))
    update_config("shopee_blocked_terms", shopee_blocked_terms.strip())
    update_config("auto_keywords", auto_keywords.strip())

    if shopee_blocked_terms.strip():
        global_blk = get_config("dedup_blocked_terms") or ""
        g_list = [t.strip() for t in global_blk.split(",") if t.strip()]
        for st in [t.strip() for t in shopee_blocked_terms.split(",") if t.strip()]:
            if st.lower() not in [x.lower() for x in g_list]:
                g_list.append(st.lower())
        update_config("dedup_blocked_terms", ", ".join(g_list))

    # Amazon
    update_config(
        "amazon_bestsellers_pct",
        str(max(0, min(100, int(amazon_bestsellers_pct or 60)))),
    )
    update_config("amazon_bestsellers_url", amazon_bestsellers_url.strip())
    update_config("amazon_deals_url", amazon_deals_url.strip())
    update_config("amazon_history_days", str(max(1, int(amazon_history_days or 7))))

    selected_amz = form.getlist("amazon_cat_checks")
    extra_amz = [c.strip() for c in custom_categories.split(",") if c.strip()]
    all_amz = selected_amz + [e for e in extra_amz if e not in selected_amz]
    update_config("amazon_target_categories", ", ".join(all_amz))

    # ML Parametrizado
    update_config(
        "ml_best_sellers_pct", str(max(0, min(100, int(ml_best_sellers_pct or 30))))
    )
    update_config("ml_history_days", str(max(1, int(ml_history_days or 7))))
    update_config("ml_bestsellers_url", ml_bestsellers_url.strip())
    update_config("ml_offers_url", ml_offers_url.strip())
    update_config("ml_min_price", str(max(0, int(float(ml_min_price or 0)))))
    update_config("ml_max_price", str(max(0, int(float(ml_max_price or 0)))))
    update_config(
        "ml_min_discount", str(max(0, min(90, int(float(ml_min_discount or 0)))))
    )
    update_config("ml_pages_per_run", str(max(1, min(30, int(ml_pages_per_run or 4)))))
    update_config("ml_max_page_pool", str(max(1, min(50, int(ml_max_page_pool or 15)))))

    checked_ml = form.getlist("excl_cat_checks")
    all_ml_excl = list(checked_ml)
    for extra in ml_auto.parse_csv_list(custom_excl_bestsellers):
        if extra not in all_ml_excl:
            all_ml_excl.append(extra)
    update_config("ml_excluded_categories_bestsellers", ", ".join(all_ml_excl))
    update_config(
        "ml_excluded_categories_offers", ml_excluded_categories_offers.strip()
    )

    return RedirectResponse(url="/?tab=config&cfg_sub=mix&saved_cfg=1", status_code=303)


@app.post("/comparator/update-settings")
async def update_comparator_settings(
    dedup_history_days: str = Form("7"),
    dedup_similarity_threshold: str = Form("60"),
    dedup_blocked_terms: str = Form(""),
):
    update_config("dedup_history_days", str(max(1, int(dedup_history_days or 7))))
    update_config(
        "dedup_similarity_threshold",
        str(max(30, min(95, int(float(dedup_similarity_threshold or 60))))),
    )
    update_config("dedup_blocked_terms", dedup_blocked_terms.strip())
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url="/?tab=config&cfg_sub=dedup&saved_cfg=1", status_code=303
    )


@app.post("/comparator/block-term")
async def comparator_block_term(term: str = Form(...), comp_days: int = Form(14)):
    clean_t = term.strip().lower()
    current_csv = get_config("dedup_blocked_terms") or ""
    terms_list = [t.strip() for t in current_csv.split(",") if t.strip()]
    if clean_t and clean_t not in [x.lower() for x in terms_list]:
        terms_list.append(clean_t)
        update_config("dedup_blocked_terms", ", ".join(terms_list))
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=comparador&comp_days={comp_days}&blocked_term={clean_t}",
        status_code=303,
    )


@app.post("/comparator/clean-duplicates")
async def comparator_clean_duplicates(
    comp_days: int = Form(14), comp_store: str = Form("all")
):
    thresh = float(get_config("dedup_similarity_threshold") or "60")
    report = offer_comparator.analyze_repeated_offers(
        days=comp_days, threshold=thresh, store_filter=comp_store
    )
    deleted = 0
    for cl in report["clusters"]:
        for del_id in cl["suggested_delete_ids"]:
            delete_offer(del_id)
            deleted += 1
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=comparador&comp_days={comp_days}&comp_store={comp_store}&cleaned_count={deleted}",
        status_code=303,
    )


@app.post("/comparator/delete-item/{offer_id}")
async def comparator_delete_single(
    offer_id: int, comp_days: int = Form(14), comp_store: str = Form("all")
):
    delete_offer(offer_id)
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=comparador&comp_days={comp_days}&comp_store={comp_store}",
        status_code=303,
    )


@app.post("/generate-auto-offers")
async def generate_auto_offers(target_date: str = Form(...)):
    count = int(get_config("auto_refill_count") or "15")
    keywords_csv = get_config("auto_keywords") or ""
    res = await asyncio.to_thread(
        ml_auto.generate_mixed_auto_offers_to_queue, count, target_date, keywords_csv
    )
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    fallback_ct = len(res.get("amazon_fallbacks", []))
    return RedirectResponse(
        url=(
            f"/?tab=fila&selected_date={target_date}"
            f"&gen_shopee={res.get('shopee_added', 0)}"
            f"&gen_amazon={res.get('amazon_added', 0)}"
            f"&gen_ml={res.get('ml_added', 0)}"
            f"&fallback_count={fallback_ct}"
        ),
        status_code=303,
    )


@app.post("/update-ai-config")
async def update_ai_config(
    ai_copy_enabled: str = Form(None),
    ai_copy_style: str = Form("urgencia_comparativo"),
    ai_copy_custom_prompt: str = Form(""),
):
    update_config("ai_copy_enabled", "1" if ai_copy_enabled == "1" else "0")
    update_config("ai_copy_style", ai_copy_style.strip())
    update_config("ai_copy_custom_prompt", ai_copy_custom_prompt.strip())
    return RedirectResponse(url="/?tab=config&cfg_sub=ia&saved_cfg=1", status_code=303)


@app.post("/delete/{offer_id}")
async def remove_offer(offer_id: int, current_date: str = Form(...)):
    delete_offer(offer_id)
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={current_date}", status_code=303
    )


@app.post("/send-now/{offer_id}/{target}")
async def send_now(offer_id: int, target: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT store, product_url, formatted_message, media_url, COALESCE(media_type, 'image'), target_date FROM queue WHERE id = ?",
        (offer_id,),
    )
    row = cursor.fetchone()
    t_date = ""
    if row:
        store, product_url, saved_msg, saved_media, media_type, t_date = row
        formatted_message = saved_msg
        media_url = saved_media
        success = False
        if target == "all":
            s1 = await asyncio.to_thread(
                trigger_whatsapp_dispatch,
                WHATSAPP_GROUP_PRINCIPAL,
                formatted_message,
                media_url,
                media_type,
            )
            await asyncio.sleep(6)
            s2 = await asyncio.to_thread(
                trigger_whatsapp_dispatch,
                WHATSAPP_GROUP_PARCERIA,
                formatted_message,
                media_url,
                media_type,
            )
            success = s1 or s2
        elif target == "principal":
            success = await asyncio.to_thread(
                trigger_whatsapp_dispatch,
                WHATSAPP_GROUP_PRINCIPAL,
                formatted_message,
                media_url,
                media_type,
            )
        elif target == "parceria":
            success = await asyncio.to_thread(
                trigger_whatsapp_dispatch,
                WHATSAPP_GROUP_PARCERIA,
                formatted_message,
                media_url,
                media_type,
            )
        if success:
            cursor.execute("UPDATE queue SET status = 'sent' WHERE id = ?", (offer_id,))
            conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?tab=fila&selected_date={t_date}", status_code=303)


@app.post("/add-offer")
async def add_offer(
    product_url: str = Form(...),
    target_date: str = Form(...),
    extract_video: str = Form(None),
    manual_video_url: str = Form(None),
):
    product_url = product_url.strip()
    manual_video_url = (manual_video_url or "").strip()
    if (
        ".mp4" in product_url.lower() or "vod.susercontent.com" in product_url.lower()
    ) and not manual_video_url:
        manual_video_url = product_url.split("?")[0]

    low = product_url.lower()
    is_ml = "mercadolivre" in low or "meli.la" in low
    is_amazon = any(k in low for k in ("amazon.", "amzn.to", "a.co", "link.amazon"))

    if is_amazon:
        import amazon_service

        try:
            offer = await amazon_service.get_amazon_offer(product_url)
        except Exception:
            offer = None
        if offer and offer.get("affiliate_url"):
            msg = amazon_service.format_product_message(offer)
            media_url = manual_video_url if manual_video_url else offer.get("image_url")
        else:
            msg = f"🔥 Oferta Amazon:\n👉 {product_url}"
            media_url = manual_video_url or None
        store_name = "Amazon"
        cat_name = "Manual Amazon"
        video_status_param = "&video_msg=ok" if manual_video_url else ""
        media_type = "video" if manual_video_url else "image"

    elif is_ml:
        offer = await ml_offer.get_ml_offer(product_url)
        if offer and offer.get("affiliate_url"):
            msg = ml_offer.format_product_message(offer)
            media_url = manual_video_url if manual_video_url else offer.get("image_url")
        else:
            msg = f"🔥 Oferta do Mercado Livre:\n👉 {product_url}"
            media_url = manual_video_url or None
        store_name = "Mercado Livre"
        cat_name = "Manual ML"
        video_status_param = "&video_msg=ok" if manual_video_url else ""
        media_type = "video" if manual_video_url else "image"

    else:
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")
        api = ShopeeAffiliateAPI(app_id, app_secret) if app_id and app_secret else None
        if not api:
            add_to_queue(
                store="Shopee",
                product_url=product_url,
                message=f"🔥 Oferta imperdível:\n👉 {product_url}",
                media_url=manual_video_url or None,
                media_type="video" if manual_video_url else "image",
                priority=1,
                target_date=target_date,
                category="Manual Shopee",
            )
            return RedirectResponse(
                url=f"/?tab=fila&selected_date={target_date}", status_code=303
            )

        full_url = await asyncio.to_thread(api.resolve_short_link, product_url)
        item_id = api.extract_item_id(full_url) or api.extract_item_id(product_url)
        video_url_found = None
        video_status_param = ""
        if manual_video_url and manual_video_url.startswith("http"):
            video_url_found = manual_video_url.split("?")[0]
            video_status_param = "&video_msg=ok"
        elif extract_video == "1":
            try:
                from bot import ml_state

                if ml_state.browser:
                    video_url_found = await api.extract_shopee_video_fast(
                        ml_state.browser, full_url
                    )
            except Exception:
                video_url_found = None
            video_status_param = (
                "&video_msg=ok" if video_url_found else "&video_msg=fallback"
            )

        offer = (
            await asyncio.to_thread(api.get_product_offer, item_id) if item_id else None
        )
        if offer:
            aff_link = offer.get("offerLink") or await asyncio.to_thread(
                api.generate_short_link, full_url
            )
            msg = api.format_product_message(offer, aff_link)
            media_url = video_url_found if video_url_found else offer.get("imageUrl")
            cat_name = "Manual Shopee"
        else:
            clean_dest = await asyncio.to_thread(
                api.clean_shopee_url_for_affiliate, product_url
            )
            aff_link = (
                await asyncio.to_thread(api.generate_short_link, clean_dest)
                or await asyncio.to_thread(api.generate_short_link, full_url)
                or product_url
            )
            msg = api.format_coupon_page_message(aff_link)
            media_url = video_url_found
            cat_name = "Cupom Shopee"
        store_name = "Shopee"
        media_type = "video" if video_url_found else "image"

    add_to_queue(
        store=store_name,
        product_url=product_url,
        message=msg,
        media_url=media_url,
        media_type=media_type,
        priority=1,
        target_date=target_date,
        category=cat_name,
    )
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={target_date}{video_status_param}",
        status_code=303,
    )


@app.post("/import-offer")
async def import_offer(product_url: str = Form(...), target_date: str = Form(...)):
    low = product_url.lower()
    is_ml = "mercadolivre" in low or "meli.la" in low
    is_amazon = any(k in low for k in ("amazon.", "amzn.to", "a.co", "link.amazon"))

    if is_amazon:
        import amazon_service

        try:
            offer = await amazon_service.get_amazon_offer(product_url)
        except Exception:
            offer = None
        if offer and offer.get("affiliate_url"):
            msg = amazon_service.format_product_message(offer)
            media_url = offer.get("image_url")
        else:
            msg = f"🔥 Oferta Amazon:\n👉 {product_url}"
            media_url = None
        store_name = "Amazon"

    elif is_ml:
        offer = await ml_offer.get_ml_offer(product_url)
        if offer and offer.get("affiliate_url"):
            msg = ml_offer.format_product_message(offer)
            media_url = offer.get("image_url")
        else:
            msg = f"🔥 Oferta do Mercado Livre:\n👉 {product_url}"
            media_url = None
        store_name = "Mercado Livre"

    else:
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")
        api = ShopeeAffiliateAPI(app_id, app_secret) if app_id and app_secret else None
        if api:
            full_url = await asyncio.to_thread(api.resolve_short_link, product_url)
            item_id = api.extract_item_id(full_url) or api.extract_item_id(product_url)
            offer = (
                await asyncio.to_thread(api.get_product_offer, item_id)
                if item_id
                else None
            )
            if offer:
                aff_link = offer.get("offerLink") or await asyncio.to_thread(
                    api.generate_short_link, full_url
                )
                msg = api.format_product_message(offer, aff_link)
                media_url = offer.get("imageUrl")
            else:
                aff_link = (
                    await asyncio.to_thread(api.generate_short_link, full_url)
                    or product_url
                )
                msg = f"🔥 <b>PRODUTO EM DESTAQUE NA SHOPEE</b>\n\n👉 {aff_link}"
                media_url = None
        else:
            msg = f"🔥 Oferta importada:\n👉 {product_url}"
            media_url = None
        store_name = "Shopee"

    add_to_queue(
        store=store_name,
        product_url=product_url,
        message=msg,
        media_url=media_url,
        priority=0,
        target_date=target_date,
        category="Importada",
    )
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={target_date}", status_code=303
    )
