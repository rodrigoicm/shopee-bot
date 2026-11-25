from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from shopee_service import ShopeeAffiliateAPI
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
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    comp_search: str = Query(""),
    comp_page: int = Query(1),
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
    synced_cats: int = Query(None),
    synced_store: str = Query("Shopee"),
    deleted_pending: int = Query(None),
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
    if cfg_sub == "mix":
        cfg_sub = "geral"
    if cfg_sub not in ("geral", "shopee", "ml", "amazon", "ia", "dedup"):
        cfg_sub = "geral"
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
    ai_model = configs.get("ai_copy_model", "gemini-2.5-flash")
    ai_models_updated_at = configs.get("ai_models_updated_at", "Ainda não testado")
    ai_models_list = ai_copy_service.get_cached_gemini_models()
    ai_model_options_html = "".join(
        f'<option value="{html.escape(m["id"])}" {"selected" if m["id"] == ai_model else ""}>'
        f'{html.escape(m.get("label") or m["id"])}</option>'
        for m in ai_models_list
    )

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

    # ───────────── Configs Globais / Anti-Repetição ─────────────
    dedup_days_cfg = int(configs.get("dedup_history_days", "7"))
    dedup_thresh_cfg = float(configs.get("dedup_similarity_threshold", "60"))
    blocked_terms_cfg = configs.get("dedup_blocked_terms", "")

    # ───────────── Configs Shopee ─────────────
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
    shopee_cats_updated_at = configs.get(
        "shopee_categories_updated_at", "Ainda não sincronizado (usando padrão)"
    )

    available_shopee_cats = ShopeeAffiliateAPI.get_available_shopee_categories()

    shopee_cat_checkboxes_html = ""
    shopee_cat_lab_html = ""
    known_shopee_cats_lower = set()
    for idx, (cat_label, cat_meta) in enumerate(available_shopee_cats.items()):
        known_shopee_cats_lower.add(cat_label.lower())
        is_checked = "checked" if cat_label.lower() in shopee_target_cats_lower else ""
        sub_list = (
            (cat_meta.get("subterms") or []) if isinstance(cat_meta, dict) else []
        )
        sub_preview = ", ".join(sub_list[:6])
        shopee_cat_checkboxes_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check" title="Subcategorias: {html.escape(sub_preview)}">
                <input class="form-check-input" type="checkbox" name="shopee_cat_checks" value="{html.escape(cat_label)}" id="shpCat{idx}" {is_checked}>
                <label class="form-check-label small" for="shpCat{idx}">{html.escape(cat_label)} <span class="text-muted" style="font-size:0.7rem;">({len(sub_list)})</span></label>
            </div>
        </div>
        """
        shopee_cat_lab_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check" title="Subcategorias: {html.escape(sub_preview)}">
                <input class="form-check-input" type="checkbox" name="cats" value="{html.escape(cat_label)}" id="labShpCat{idx}" {is_checked}>
                <label class="form-check-label small" for="labShpCat{idx}">{html.escape(cat_label)} <span class="text-muted" style="font-size:0.7rem;">({len(sub_list)})</span></label>
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

    # ───────────── Configs Mercado Livre (Categorias + Subcategorias Expansíveis) ─────────────
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

    ml_target_cats_raw = configs.get(
        "ml_target_categories", ml_auto.DEFAULT_ML_TARGET_CATEGORIES
    )
    ml_target_cats_list = ml_auto.parse_ml_categories_list(ml_target_cats_raw)
    ml_target_cats_norm = {ml_auto._normalize_text(x) for x in ml_target_cats_list}

    ml_target_subcats_raw = configs.get("ml_target_subcategories", None)
    ml_has_subcats_saved = ml_target_subcats_raw is not None
    ml_target_subcats_set = {
        s.strip() for s in (ml_target_subcats_raw or "").split(";") if s.strip()
    }

    ml_cats_updated_at = configs.get(
        "ml_categories_updated_at", "Ainda não sincronizado (usando padrão)"
    )
    excl_offers_raw = configs.get("ml_excluded_categories_offers", "")

    available_ml_cats = ml_auto.get_available_ml_categories()
    ml_cat_tree_html = ""
    ml_cat_lab_html = ""
    known_norm_set = set()

    for idx, (cat_name, cat_meta) in enumerate(available_ml_cats.items()):
        c_norm = ml_auto._normalize_text(cat_name)
        known_norm_set.add(c_norm)
        is_parent_checked = c_norm in ml_target_cats_norm
        children = (cat_meta or {}).get("children") or []

        sub_items_html = ""
        checked_sub_count = 0
        for s_idx, ch in enumerate(children):
            ch_id = str(ch.get("id") or "").strip()
            ch_name = str(ch.get("name") or "").strip()
            if not ch_id or not ch_name:
                continue
            if ml_has_subcats_saved:
                sub_checked = ch_id in ml_target_subcats_set
            else:
                sub_checked = is_parent_checked
            if sub_checked:
                checked_sub_count += 1
            sub_items_html += f"""
            <div class="col-6">
                <div class="form-check">
                    <input class="form-check-input ml-sub-check-{idx}" type="checkbox" name="ml_subcat_checks" value="{html.escape(ch_id)}" id="mlSub{idx}_{s_idx}" data-parent-idx="{idx}" onchange="syncMlParentFromChild(this)" {"checked" if sub_checked else ""}>
                    <label class="form-check-label" style="font-size:0.78rem;" for="mlSub{idx}_{s_idx}">{html.escape(ch_name)}</label>
                </div>
            </div>
            """

        if children:
            toggle_btn_html = f"""
            <button type="button" class="btn btn-sm btn-link p-0 text-decoration-none text-secondary" data-bs-toggle="collapse" data-bs-target="#mlSubCollapse{idx}" style="font-size:0.75rem;">
                ▾ Subcategorias (<span id="mlSubCount{idx}">{checked_sub_count}/{len(children)}</span>)
            </button>
            """
            collapse_box_html = f"""
            <div class="collapse mt-2 pt-2 border-top" id="mlSubCollapse{idx}">
                <div class="row g-1 ps-2">{sub_items_html}</div>
            </div>
            """
        else:
            toggle_btn_html = '<span class="text-muted" style="font-size:0.72rem;">(sincronize para ver subcategorias)</span>'
            collapse_box_html = ""

        ml_cat_tree_html += f"""
        <div class="col-md-6">
            <div class="p-2 bg-white border rounded h-100">
                <div class="d-flex justify-content-between align-items-center gap-2">
                    <div class="form-check mb-0">
                        <input class="form-check-input" type="checkbox" name="ml_cat_checks" value="{html.escape(cat_name)}" id="mlCatCheck{idx}" data-cat-idx="{idx}" onchange="toggleMlParent(this)" {"checked" if is_parent_checked else ""}>
                        <label class="form-check-label small fw-semibold" for="mlCatCheck{idx}">{html.escape(cat_name)}</label>
                    </div>
                    {toggle_btn_html}
                </div>
                {collapse_box_html}
            </div>
        </div>
        """

        ml_cat_lab_html += f"""
        <div class="col-md-3 col-sm-6">
            <div class="form-check">
                <input class="form-check-input" type="checkbox" name="cats" value="{html.escape(cat_name)}" id="labMlCat{idx}" {"checked" if is_parent_checked else ""}>
                <label class="form-check-label small" for="labMlCat{idx}">{html.escape(cat_name)}</label>
            </div>
        </div>
        """

    custom_ml_cats_str = "; ".join(
        c
        for c in ml_target_cats_list
        if ml_auto._normalize_text(c) not in known_norm_set
    )

    # ───────────── Configs Amazon ─────────────
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
    elif deleted_pending is not None:
        alert_html = f'<div class="alert alert-danger shadow-sm border-0 py-2">🗑️ <b>{deleted_pending} oferta(s) pendente(s)</b> de {current_date} foram excluídas da fila!</div>'
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
        alert_html = f'<div class="alert alert-success shadow-sm border-0 py-2">✅ <b>{ml_approved} oferta(s) ({label})</b> convertida(s) e adicionada(s) à fila de {current_date}!</div>'
    elif saved_cfg:
        alert_html = '<div class="alert alert-success shadow-sm border-0 py-2">💾 Configurações salvas com sucesso!</div>'
    elif synced_cats is not None:
        s_lbl = html.escape(synced_store or "Plataforma")
        if synced_cats > 0:
            alert_html = f'<div class="alert alert-success shadow-sm border-0 py-2">🔄 <b>{synced_cats} categorias oficiais ({s_lbl})</b> foram buscadas e atualizadas com sucesso!</div>'
        else:
            alert_html = f'<div class="alert alert-warning shadow-sm border-0 py-2">⚠️ Não foi possível atualizar as categorias de {s_lbl} agora; mantendo lista em cache.</div>'
    elif ai_updated is not None:
        if ai_updated > 0:
            alert_html = f'<div class="alert alert-info shadow-sm border-0 py-2">✨ <b>Copy com IA gerada e aplicada em {ai_updated} oferta(s)!</b> Clique em "👁️ Ver" na fila para conferir.</div>'
        else:
            alert_html = '<div class="alert alert-warning shadow-sm border-0 py-2">⚠️ Não foi possível gerar o gancho da IA. Verifique a chave GEMINI_API_KEY no .env.</div>'

    now_ts = time.time()

    # Comparador (cache 30s)
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
        "geral": ("", ""),
        "shopee": ("", ""),
        "ml": ("", ""),
        "amazon": ("", ""),
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
            elif "ml:" in cat_low or "ml ⚡" in cat_low or "amazon:" in cat_low:
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
                        data-return-tab="fila"
                        data-return-lab="shopee"
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

    # ───────────── HTML: LABORATÓRIO SHOPEE ─────────────
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
                                    data-return-tab="laboratorio"
                                    data-return-lab="shopee"
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
            sug_kw = _suggest_first_keyword(cand.get("title", ""))

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
                                <button type="button" class="btn btn-sm btn-outline-danger"
                                    data-offer-id="0"
                                    data-suggested="{html.escape(sug_kw)}"
                                    data-title="{c_title}"
                                    data-return-tab="laboratorio"
                                    data-return-lab="amazon"
                                    onclick="blockTermFromQueueBtn(this)"
                                    title="Bloquear termo indesejado">
                                    🚫 Termo
                                </button>
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
                <p class="mb-0 text-muted small">Marque as categorias desejadas acima e clique em <b>"🔍 Simular ML"</b> para coletar produtos.</p>
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
                f"<span class='badge bg-secondary'>{html.escape(c)}</span>"
                for c in ml_lab_data["blocked_categories"]
            )
            or "<span class='text-muted small'>Nenhuma ignorada</span>"
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
            sug_kw = _suggest_first_keyword(cand.get("title", ""))

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
                                <button type="button" class="btn btn-sm btn-outline-danger"
                                    data-offer-id="0"
                                    data-suggested="{html.escape(sug_kw)}"
                                    data-title="{c_title}"
                                    data-return-tab="laboratorio"
                                    data-return-lab="ml"
                                    onclick="blockTermFromQueueBtn(this)"
                                    title="Bloquear termo indesejado">
                                    🚫 Termo
                                </button>
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
                        <div class="mt-1"><b>✅ Categorias Lidas:</b> {acc_cats_badges}</div>
                        <div class="mt-1"><b>⏭️ Categorias Não Selecionadas:</b> {blk_cats_badges}</div>
                    </div>
                    <div class="col-md-3 text-end">
                        <form action="/ml-lab/approve-all" method="POST">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-warning fw-bold w-100" {"disabled" if not ml_lab_data.get('items') else ""}>🚀 Enfileirar Todos ({len(ml_lab_data['items'])})</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3">{ml_cards_html}</div>
        """

    # ───────────── HTML: COMPARADOR ─────────────
    comp_search_clean = (comp_search or "").strip().lower()
    all_clusters = comp_report["clusters"]
    if comp_search_clean:
        filtered_clusters = [
            cl
            for cl in all_clusters
            if comp_search_clean in (cl["canonical_title"] or "").lower()
            or comp_search_clean in (cl["signature"] or "").lower()
            or any(
                comp_search_clean in (it.get("title") or "").lower()
                or comp_search_clean in (it.get("product_url") or "").lower()
                for it in cl["items"]
            )
        ]
    else:
        filtered_clusters = all_clusters

    comp_per_page = 10
    comp_filtered_total = len(filtered_clusters)
    comp_total_pages = max(
        1, (comp_filtered_total + comp_per_page - 1) // comp_per_page
    )
    comp_page = max(1, min(comp_page, comp_total_pages))
    page_clusters = filtered_clusters[
        (comp_page - 1) * comp_per_page : comp_page * comp_per_page
    ]

    clusters_html = ""
    if not page_clusters:
        clusters_html = """
        <div class="card border-0 shadow-sm text-center py-5">
            <div class="card-body">
                <div class="fs-2 mb-2">🎉</div>
                <h5 class="fw-bold text-success mb-1">Nenhuma oferta repetida/similar encontrada com este filtro!</h5>
            </div>
        </div>
        """
    else:
        for cl in page_clusters:
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

    import_options_html = "".join(
        f'<option value="{imp[2]}">[{imp[3]} - {imp[1]}] {imp[2]} (ID: {imp[0]})</option>'
        for imp in available_to_import
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "tab": tab,
            "lab_sub": lab_sub,
            "cfg_sub": cfg_sub,
            "current_date": current_date,
            "today_str": today_str,
            "configs": configs,
            "alert_html": alert_html,
            "total_offers": total_offers,
            "sent_offers": sent_offers,
            "pending_offers": pending_offers,
            "remaining_seconds": remaining_seconds,
            "timer_status_text": timer_status_text,
            "queue_rows_html": queue_rows_html,
            "import_options_html": import_options_html,
            # IA
            "ai_enabled": ai_enabled,
            "ai_style": ai_style,
            "ai_custom_prompt": ai_custom_prompt,
            "ai_models_updated_at": ai_models_updated_at,
            "ai_model_options_html": ai_model_options_html,
            # Mix / Geral
            "ml_auto_enabled": ml_auto_enabled,
            "amazon_enabled": amazon_enabled,
            "amazon_available": AMAZON_AVAILABLE,
            "mix_shopee_pct": mix_shopee_pct,
            "mix_amazon_pct": mix_amazon_pct,
            "mix_ml_pct": mix_ml_pct,
            "mix_label": mix_label,
            # Shopee
            "shopee_gen_pct": shopee_gen_pct,
            "shopee_cat_pct": shopee_cat_pct,
            "available_shopee_cats_count": len(available_shopee_cats),
            "shopee_cats_updated_at": shopee_cats_updated_at,
            "shopee_blocked_terms_raw": shopee_blocked_terms_raw,
            "shopee_cat_checkboxes_html": shopee_cat_checkboxes_html,
            "shopee_cat_lab_html": shopee_cat_lab_html,
            "shopee_custom_cats_str": shopee_custom_cats_str,
            "shopee_lab_results_html": shopee_lab_results_html,
            # Mercado Livre
            "ml_best_pct": ml_best_pct,
            "ml_offers_pct": ml_offers_pct,
            "ml_hist_days": ml_hist_days,
            "ml_bestsellers_url": ml_bestsellers_url,
            "ml_offers_url": ml_offers_url,
            "ml_min_price": ml_min_price,
            "ml_max_price": ml_max_price,
            "ml_min_discount": ml_min_discount,
            "ml_pages_per_run": ml_pages_per_run,
            "ml_max_page_pool": ml_max_page_pool,
            "available_ml_cats_count": len(available_ml_cats),
            "ml_cats_updated_at": ml_cats_updated_at,
            "excl_offers_raw": excl_offers_raw,
            "ml_cat_tree_html": ml_cat_tree_html,
            "ml_cat_lab_html": ml_cat_lab_html,
            "custom_ml_cats_str": custom_ml_cats_str,
            "ml_lab_results_html": ml_lab_results_html,
            # Amazon
            "amazon_deals_pct": amazon_deals_pct,
            "amazon_bestsellers_pct": amazon_bestsellers_pct,
            "amazon_hist_days": amazon_hist_days,
            "amazon_bestsellers_url": amazon_bestsellers_url,
            "amazon_deals_url": amazon_deals_url,
            "amazon_cat_config_html": amazon_cat_config_html,
            "amazon_cat_lab_html": amazon_cat_lab_html,
            "amazon_cat_extra_str": amazon_cat_extra_str,
            "amazon_lab_results_html": amazon_lab_results_html,
            # Comparador / Anti-Repetição
            "scan_days": scan_days,
            "comp_store": comp_store,
            "comp_search": comp_search or "",
            "comp_page": comp_page,
            "comp_total_pages": comp_total_pages,
            "comp_filtered_total": comp_filtered_total,
            "comp_report": comp_report,
            "clusters_html": clusters_html,
            "dedup_days_cfg": dedup_days_cfg,
            "dedup_thresh_cfg": dedup_thresh_cfg,
            "blocked_terms_cfg": blocked_terms_cfg,
            # Vendas
            "sales_days": sales_days,
            "sales_platform": sales_platform,
            "sales_status": sales_status,
            "sales_data": sales_data,
            "sales_gmv_brl": format_brl(sales_data["total_gmv"]),
            "sales_comm_brl": format_brl(sales_data["total_commission"]),
            "sales_completed_comm_brl": format_brl(sales_data["completed_commission"]),
            "sales_rows_html": sales_rows_html,
        },
    )


# ─────────── Rotas: Sincronizar Categorias Dinâmicas ───────────
@app.post("/shopee/sync-categories")
async def sync_shopee_categories_route(
    selected_date: str = Form(""),
    return_tab: str = Form("config"),
):
    res = await asyncio.to_thread(ShopeeAffiliateAPI.fetch_dynamic_shopee_categories)
    if isinstance(res, tuple):
        cats_dict, ok = res
        count_synced = len(cats_dict) if ok else 0
    elif isinstance(res, dict):
        count_synced = len(res)
    else:
        count_synced = 0

    if count_synced > 0:
        cuiaba_tz = timezone(timedelta(hours=-4))
        update_config(
            "shopee_categories_updated_at",
            datetime.now(cuiaba_tz).strftime("%d/%m/%Y %H:%M"),
        )

    if return_tab == "laboratorio":
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=shopee&selected_date={selected_date}&synced_cats={count_synced}&synced_store=Shopee",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/?tab=config&cfg_sub=shopee&selected_date={selected_date}&synced_cats={count_synced}&synced_store=Shopee",
        status_code=303,
    )


@app.post("/ml/sync-categories")
async def sync_ml_categories_route(
    selected_date: str = Form(""),
    return_tab: str = Form("config"),
):
    cats_dict, ok = await asyncio.to_thread(ml_auto.fetch_dynamic_ml_categories)
    count_synced = len(cats_dict) if ok else 0
    if return_tab == "laboratorio":
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub=ml&selected_date={selected_date}&synced_cats={count_synced}&synced_store=Mercado%20Livre",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/?tab=config&cfg_sub=ml&selected_date={selected_date}&synced_cats={count_synced}&synced_store=Mercado%20Livre",
        status_code=303,
    )


# ─────────── Rota: Bloquear Termo Direto da Tela de Fila / Laboratório ───────────
@app.post("/queue/block-term")
async def block_term_from_queue(
    term: str = Form(...),
    offer_id: int = Form(0),
    current_date: str = Form(...),
    return_tab: str = Form("fila"),
    return_lab_sub: str = Form("shopee"),
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

        for lab_key in ("shopee_lab", "amazon_lab", "ml_lab"):
            lab_cache = _PORTAL_CACHE.get(lab_key)
            if lab_cache and lab_cache.get("items"):
                lab_cache["items"] = [
                    it
                    for it in lab_cache["items"]
                    if clean_t not in (it.get("title") or "").lower()
                ]

    _PORTAL_CACHE["comp"]["ts"] = 0.0
    if return_tab == "laboratorio":
        return RedirectResponse(
            url=f"/?tab=laboratorio&lab_sub={return_lab_sub}&selected_date={current_date}&blocked_term={clean_t}&purged_count={purged}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={current_date}&blocked_term={clean_t}&purged_count={purged}",
        status_code=303,
    )


# ─────────── Rotas Shopee Lab ───────────
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
async def ml_lab_simulate(
    selected_date: str = Form(...),
    sim_count: int = Form(10),
    cats: list[str] = Form([]),
):
    if cats:
        update_config("ml_target_categories", "; ".join(cats))
    sim_data = await asyncio.to_thread(
        ml_auto.simulate_ml_offers_selection,
        max(2, min(30, sim_count)),
        cats if cats else None,
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


# ─────────── Rotas Config (Separadas por Loja) ───────────
@app.post("/update-config")
async def update_parameters(
    start_time: str = Form(...),
    end_time: str = Form(...),
    interval_minutes: str = Form(...),
    auto_refill_count: str = Form("15"),
    auto_refill_enabled: str = Form(None),
    amazon_auto_enabled: str = Form(None),
    ml_auto_enabled: str = Form(None),
    mix_shopee_pct: str = Form("60"),
    mix_amazon_pct: str = Form("0"),
):
    update_config("start_time", start_time)
    update_config("end_time", end_time)
    update_config("interval_minutes", interval_minutes)
    update_config("auto_refill_count", auto_refill_count)
    update_config("auto_refill_enabled", "1" if auto_refill_enabled == "1" else "0")
    update_config("amazon_auto_enabled", "1" if amazon_auto_enabled == "1" else "0")
    update_config("ml_auto_enabled", "1" if ml_auto_enabled == "1" else "0")
    update_config("mix_shopee_pct", str(max(0, min(100, int(mix_shopee_pct or 60)))))
    update_config("mix_amazon_pct", str(max(0, min(100, int(mix_amazon_pct or 0)))))
    return RedirectResponse(
        url="/?tab=config&cfg_sub=geral&saved_cfg=1", status_code=303
    )


@app.post("/update-shopee-config")
async def update_shopee_config(
    request: Request,
    shopee_general_pct: str = Form("30"),
    shopee_custom_categories: str = Form(""),
    shopee_blocked_terms: str = Form(""),
    auto_keywords: str = Form(""),
):
    form = await request.form()
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

    return RedirectResponse(
        url="/?tab=config&cfg_sub=shopee&saved_cfg=1", status_code=303
    )


@app.post("/update-ml-config")
async def update_ml_config(
    request: Request,
    ml_best_sellers_pct: str = Form("30"),
    ml_history_days: str = Form("7"),
    ml_bestsellers_url: str = Form("https://www.mercadolivre.com.br/mais-vendidos"),
    ml_offers_url: str = Form("https://www.mercadolivre.com.br/ofertas"),
    ml_min_price: str = Form("20"),
    ml_max_price: str = Form("350"),
    ml_min_discount: str = Form("12"),
    ml_pages_per_run: str = Form("4"),
    ml_max_page_pool: str = Form("15"),
    custom_ml_categories: str = Form(""),
    ml_excluded_categories_offers: str = Form(""),
):
    form = await request.form()
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

    checked_ml = form.getlist("ml_cat_checks")
    all_ml_target = list(checked_ml)
    for extra in ml_auto.parse_ml_categories_list(custom_ml_categories):
        if extra not in all_ml_target:
            all_ml_target.append(extra)
    update_config("ml_target_categories", "; ".join(all_ml_target))

    checked_ml_subs = form.getlist("ml_subcat_checks")
    update_config("ml_target_subcategories", "; ".join(checked_ml_subs))
    update_config(
        "ml_excluded_categories_offers", ml_excluded_categories_offers.strip()
    )

    return RedirectResponse(url="/?tab=config&cfg_sub=ml&saved_cfg=1", status_code=303)


@app.post("/update-amazon-config")
async def update_amazon_config(
    request: Request,
    amazon_bestsellers_pct: str = Form("60"),
    amazon_bestsellers_url: str = Form("https://www.amazon.com.br/gp/bestsellers"),
    amazon_deals_url: str = Form("https://www.amazon.com.br/deals"),
    amazon_history_days: str = Form("7"),
    custom_categories: str = Form(""),
):
    form = await request.form()
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

    return RedirectResponse(
        url="/?tab=config&cfg_sub=amazon&saved_cfg=1", status_code=303
    )


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
    ai_copy_model: str = Form("gemini-2.5-flash"),
):
    update_config("ai_copy_enabled", "1" if ai_copy_enabled == "1" else "0")
    update_config("ai_copy_style", ai_copy_style.strip())
    update_config("ai_copy_custom_prompt", ai_copy_custom_prompt.strip())
    update_config("ai_copy_model", ai_copy_model.strip())
    return RedirectResponse(url="/?tab=config&cfg_sub=ia&saved_cfg=1", status_code=303)


@app.post("/ai/sync-models")
async def sync_ai_models():
    await asyncio.to_thread(ai_copy_service.discover_and_test_gemini_models)
    return RedirectResponse(url="/?tab=config&cfg_sub=ia&saved_cfg=1", status_code=303)


@app.post("/delete/{offer_id}")
async def remove_offer(offer_id: int, current_date: str = Form(...)):
    delete_offer(offer_id)
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={current_date}", status_code=303
    )


@app.post("/delete-pending")
async def remove_all_pending(target_date: str = Form(...)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM queue WHERE target_date = ? AND status = 'pending'",
        (target_date,),
    )
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(
        url=f"/?tab=fila&selected_date={target_date}&deleted_pending={deleted_count}",
        status_code=303,
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
