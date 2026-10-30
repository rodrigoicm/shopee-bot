from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
import sqlite3
import os
import requests
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
from whatsapp_service import (
    send_whatsapp_message as trigger_whatsapp_dispatch,
    WHATSAPP_GROUP_PRINCIPAL,
    WHATSAPP_GROUP_PARCERIA,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Portal de Automação de Ofertas")

EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "chip_01")
WHATSAPP_GROUP_PRINCIPAL = os.environ.get(
    "WHATSAPP_GROUP_PRINCIPAL", "120363421300618666@g.us"
)
WHATSAPP_GROUP_PARCERIA = os.environ.get(
    "WHATSAPP_GROUP_PARCERIA", "556596440424-1585878265@g.us"
)


def ensure_media_type_column():
    """Garante que as colunas media_type e category existam na tabela queue do SQLite."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE queue ADD COLUMN media_type TEXT DEFAULT 'image'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE queue ADD COLUMN category TEXT DEFAULT 'Geral'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


ensure_media_type_column()
offer_comparator.ensure_comparator_configs()
ml_auto.ensure_ml_auto_schema()


def format_brl(val: float) -> str:
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def refresh_offer_data(product_url: str) -> tuple[str, str | None]:
    """Reconsulta a API/Serviço na hora do envio para atualizar preço, título e link."""
    if "mercadolivre" in product_url.lower() or "meli.la" in product_url.lower():
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
        media_url = offer.get("imageUrl")
        return msg, media_url
    else:
        clean_dest = api.clean_shopee_url_for_affiliate(product_url)
        aff_link = (
            api.generate_short_link(clean_dest)
            or api.generate_short_link(full_url)
            or product_url
        )
        msg = api.format_coupon_page_message(aff_link)
        return msg, None


@app.post("/validate-video")
async def validate_video(product_url: str = Form(...)):
    app_id = os.environ.get("SHOPEE_APP_ID", "")
    app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
    api = ShopeeAffiliateAPI(app_id, app_secret)

    full_url = api.resolve_short_link(product_url)

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
                    "message": "✅ Vídeo (.mp4) encontrado e validado com sucesso!",
                }
            )
        else:
            return JSONResponse(
                {
                    "success": False,
                    "resolved_url": full_url,
                    "message": "⚠️ Nenhum vídeo (.mp4) foi detectado antes do bloqueio/login (ou o anúncio não possui vídeo).",
                }
            )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"❌ Erro ao validar vídeo: {e}"}
        )


# Cache em memória para navegação rápida e armazenamento da última simulação do Laboratório ML
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
    "ml_lab": None,
}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    selected_date: str = Query(None),
    video_msg: str = Query(None),
    tab: str = Query("fila"),
    comp_days: int = Query(None),
    comp_store: str = Query("all"),
    cleaned_count: int = Query(None),
    blocked_term: str = Query(None),
    sales_days: int = Query(15),
    sales_platform: str = Query("all"),
    sales_status: str = Query("all"),
    gen_shopee: int = Query(None),
    gen_ml: int = Query(None),
    ml_approved: int = Query(None),
    saved_cfg: str = Query(None),
):
    cuiaba_tz = timezone(timedelta(hours=-4))
    now_dt = datetime.now(cuiaba_tz)
    today_str = now_dt.strftime("%Y-%m-%d")

    current_date = selected_date if selected_date else today_str

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT key, value FROM config")
    configs = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT id, store, product_url, formatted_message, media_url, status, priority, target_date, created_at, COALESCE(media_type, 'image'), COALESCE(category, 'Geral') FROM queue WHERE target_date = ? ORDER BY priority DESC, id DESC",
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

    # Alertas de Feedback
    alert_html = ""
    if video_msg == "ok":
        alert_html = '<div class="alert alert-success py-2">🎬 Oferta salva com <b>VÍDEO (.mp4)</b> com sucesso!</div>'
    elif video_msg == "fallback":
        alert_html = '<div class="alert alert-warning py-2">⚠️ Vídeo não detectado; oferta salva com a <b>Imagem</b> padrão.</div>'
    elif cleaned_count is not None:
        alert_html = f'<div class="alert alert-success py-2">🧹 Limpeza concluída: <b>{cleaned_count} ofertas duplicadas pendentes</b> removidas!</div>'
    elif blocked_term:
        alert_html = f'<div class="alert alert-warning py-2">🚫 Termo <b>"{html.escape(blocked_term)}"</b> bloqueado com sucesso!</div>'
    elif gen_shopee is not None or gen_ml is not None:
        alert_html = f'<div class="alert alert-success py-2">🎲 Lote gerado para {current_date}: <b>{gen_shopee or 0} Shopee</b> + <b>{gen_ml or 0} Mercado Livre</b>!</div>'
    elif ml_approved is not None:
        alert_html = f'<div class="alert alert-success py-2">✅ <b>{ml_approved} oferta(s) do Mercado Livre</b> convertida(s) em link de afiliado e adicionada(s) à fila de {current_date}!</div>'
    elif saved_cfg:
        alert_html = '<div class="alert alert-success py-2">💾 Configurações salvas com sucesso!</div>'

    # --- Parâmetros Gerais, Shopee e ML ---
    shopee_gen_pct = int(configs.get("shopee_general_pct", "30"))
    shopee_cat_pct = 100 - shopee_gen_pct
    shopee_target_cats = configs.get(
        "shopee_target_categories",
        "Roupas Femininas, Moda Infantil, Sapatos Femininos",
    )

    ml_auto_enabled = configs.get("ml_auto_enabled", "0") == "1"
    mix_shopee_pct = int(configs.get("mix_shopee_pct", "60"))
    mix_ml_pct = 100 - mix_shopee_pct
    ml_best_pct = int(configs.get("ml_best_sellers_pct", "30"))
    ml_offers_pct = 100 - ml_best_pct
    ml_hist_days = configs.get("ml_history_days", "7")
    ml_offers_url = configs.get("ml_offers_url", ml_auto.DEFAULT_ML_OFERTAS_URL)
    excl_bestsellers_raw = configs.get(
        "ml_excluded_categories_bestsellers", "Games, Informática"
    )
    excl_bestsellers_list = ml_auto.parse_csv_list(excl_bestsellers_raw)
    excl_bestsellers_norm = {ml_auto._normalize_text(x) for x in excl_bestsellers_list}
    excl_offers_raw = configs.get("ml_excluded_categories_offers", "")

    now_ts = time.time()

    # --- 1. COMPARADOR (CACHE 30s) ---
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

    # --- 2. VENDAS (LAZY LOAD SÓ NA ABA VENDAS) ---
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

    # Controle de Abas Ativas
    tabs_map = {
        "fila": ("show active", "active"),
        "ml_lab": ("", ""),
        "comparador": ("", ""),
        "vendas": ("", ""),
        "config": ("", ""),
    }
    if tab in tabs_map:
        for k in tabs_map:
            tabs_map[k] = ("show active", "active") if k == tab else ("", "")

    # --- HTML: LINHAS DA FILA DO DIA ---
    queue_rows_html = ""
    if not queue_items:
        queue_rows_html = """
        <tr>
            <td colspan="8" class="text-center py-4 text-muted">
                Nenhuma oferta cadastrada para esta data. Use os botões acima para gerar ou adicionar ofertas!
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
            badge_status = (
                "<span class='badge bg-warning text-dark'>Pendente</span>"
                if status == "pending"
                else "<span class='badge bg-success'>Enviado</span>"
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
            badge_store = (
                "<span class='badge bg-warning text-dark'>Mercado Livre</span>"
                if "mercado" in (store or "").lower()
                else "<span class='badge' style='background-color:#ee4d2d;'>Shopee</span>"
            )

            cat_str = category_name or "Geral"
            cat_low = cat_str.lower()
            if "feminina" in cat_low or "sapato" in cat_low:
                badge_cat = f"<span class='badge bg-danger-subtle text-danger border border-danger-subtle'>{html.escape(cat_str)}</span>"
            elif "infantil" in cat_low:
                badge_cat = f"<span class='badge bg-info-subtle text-info-emphasis border border-info-subtle'>{html.escape(cat_str)}</span>"
            elif "ml:" in cat_low:
                badge_cat = f"<span class='badge bg-warning-subtle text-dark border border-warning'>{html.escape(cat_str)}</span>"
            elif "mais vendidos" in cat_low or "top" in cat_low:
                badge_cat = f"<span class='badge bg-primary-subtle text-primary border border-primary-subtle'>{html.escape(cat_str)}</span>"
            else:
                badge_cat = f"<span class='badge bg-light text-dark border'>{html.escape(cat_str)}</span>"

            thumb_html = (
                f"<img src='{media_url}' style='width:46px;height:46px;object-fit:cover;' class='rounded border me-2'>"
                if media_url and media_type != "video"
                else "<div style='width:46px;height:46px;' class='rounded border bg-light d-flex align-items-center justify-content-center me-2'>📦</div>"
            )

            if media_type == "video" and media_url:
                media_preview_html = f"<video src='{media_url}' class='img-fluid mb-3 rounded w-100' controls></video>"
            elif media_url:
                media_preview_html = (
                    f"<img src='{media_url}' class='img-fluid mb-3 rounded' alt='Foto'>"
                )
            else:
                media_preview_html = ""

            queue_rows_html += f"""
            <tr>
                <td class="fw-bold text-muted">#{item_id}</td>
                <td>
                    <div class="d-flex align-items-center">
                        {thumb_html}
                        <div style="max-width: 310px;">
                            <div class="fw-semibold text-truncate" title="{html.escape(title_extracted)}">{html.escape(title_extracted)}</div>
                            <a href="{prod_url}" target="_blank" class="small text-muted text-truncate d-block">{prod_url}</a>
                        </div>
                    </div>
                </td>
                <td>{badge_store}{badge_media}</td>
                <td>{badge_cat}</td>
                <td>{badge_prio}</td>
                <td>{badge_status}</td>
                <td>
                    <button class="btn btn-sm btn-outline-primary" data-bs-toggle="modal" data-bs-target="#modalMsg{item_id}">👁️ Ver</button>
                    <div class="btn-group ms-1" role="group">
                        <form action="/send-now/{item_id}/all" method="POST" class="d-inline">
                            <button type="submit" class="btn btn-sm btn-success" title="Atualiza preço e envia para todos">🚀 Todos</button>
                        </form>
                        <form action="/send-now/{item_id}/principal" method="POST" class="d-inline ms-1">
                            <button type="submit" class="btn btn-sm btn-outline-success">Principal</button>
                        </form>
                        <form action="/send-now/{item_id}/parceria" method="POST" class="d-inline ms-1">
                            <button type="submit" class="btn btn-sm btn-outline-secondary">Parceria</button>
                        </form>
                    </div>
                    <form action="/delete/{item_id}" method="POST" class="d-inline ms-1" onsubmit="return confirm('Excluir esta oferta?')">
                        <input type="hidden" name="current_date" value="{current_date}">
                        <button type="submit" class="btn btn-sm btn-outline-danger">🗑️</button>
                    </form>

                    <div class="modal fade" id="modalMsg{item_id}" tabindex="-1">
                      <div class="modal-dialog">
                        <div class="modal-content">
                          <div class="modal-header">
                            <h5 class="modal-title">Oferta #{item_id} — {html.escape(cat_str)}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                          </div>
                          <div class="modal-body">
                            {media_preview_html}
                            <div class="p-3 bg-light border rounded" style="white-space: pre-wrap;">{formatted_msg}</div>
                          </div>
                        </div>
                      </div>
                    </div>
                </td>
            </tr>
            """

    # --- HTML: ABA 2 - LABORATÓRIO MERCADO LIVRE (DRY-RUN / VALIDAÇÃO) ---
    ml_lab_data = _PORTAL_CACHE.get("ml_lab")
    ml_lab_html = ""
    if not ml_lab_data:
        ml_lab_html = """
        <div class="alert alert-secondary text-center py-4">
            <h5 class="mb-1">🧪 Nenhuma simulação carregada no momento</h5>
            <p class="mb-0 text-muted small">Clique no botão <b>"🔍 Buscar e Simular Ofertas ML Agora"</b> acima para coletar os produtos de <i>Mais Vendidos</i> (excluindo Games/Informática) e <i>Ofertas até R$ 100</i> e validar antes de colocar na fila!</p>
        </div>
        """
    else:
        acc_cats_badges = " ".join(
            f"<span class='badge bg-success-subtle text-success border'>{ html.escape(k) }: {v}</span>"
            for k, v in ml_lab_data["accepted_categories"].items()
        )
        blk_cats_badges = (
            " ".join(
                f"<span class='badge bg-danger'>{html.escape(c)}</span>"
                for c in ml_lab_data["blocked_categories"]
            )
            or "<span class='text-muted small'>Nenhuma encontrada</span>"
        )

        cards_html = ""
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

            cards_html += f"""
            <div class="col-md-6">
                <div class="card h-100 shadow-sm border-warning">
                    <div class="card-body d-flex gap-3">
                        <img src="{c_img}" style="width:90px;height:90px;object-fit:contain;" class="rounded border bg-white p-1">
                        <div class="flex-grow-1">
                            <div class="mb-1">
                                <span class="badge bg-warning text-dark">{c_cat}</span>
                                {c_disc}{c_pix}
                            </div>
                            <h6 class="fw-bold mb-1">{c_title}</h6>
                            <div class="fs-5 fw-bold text-primary mb-2">{c_cur}{c_orig}</div>
                            <div class="d-flex gap-2 flex-wrap">
                                <button type="button" class="btn btn-sm btn-outline-dark" data-bs-toggle="modal" data-bs-target="#modalLab{idx}">
                                    📝 Ver Mensagem
                                </button>
                                <a href="{c_url}" target="_blank" class="btn btn-sm btn-outline-secondary">🔗 Ver no ML</a>
                                <form action="/ml-lab/approve-one" method="POST" class="d-inline">
                                    <input type="hidden" name="item_index" value="{idx}">
                                    <input type="hidden" name="target_date" value="{current_date}">
                                    <button type="submit" class="btn btn-sm btn-success fw-bold">
                                        ✅ Converter e Enfileirar
                                    </button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="modal fade" id="modalLab{idx}" tabindex="-1">
                  <div class="modal-dialog">
                    <div class="modal-content">
                      <div class="modal-header">
                        <h5 class="modal-title">Prévia da Mensagem ML #{idx+1}</h5>
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

        ml_lab_html = f"""
        <div class="card mb-3 border-info shadow-sm">
            <div class="card-body py-2">
                <div class="row align-items-center">
                    <div class="col-md-9 small">
                        <div><b>⏱️ Tempo da Coleta:</b> {ml_lab_data['elapsed_ms']} ms | <b>📦 Pool Mais Vendidos:</b> {ml_lab_data['raw_best_count']} itens | <b>📦 Pool Ofertas (&lt; R$100):</b> {ml_lab_data['raw_offers_count']} itens | <b>🛡️ Repetidos Barrados (7d):</b> {ml_lab_data['skipped_duplicates']}</div>
                        <div class="mt-1"><b>🚫 Categorias Bloqueadas:</b> {blk_cats_badges}</div>
                        <div class="mt-1"><b>✅ Categorias Lidas em Mais Vendidos:</b> {acc_cats_badges}</div>
                    </div>
                    <div class="col-md-3 text-end">
                        <form action="/ml-lab/approve-all" method="POST">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-warning fw-bold w-100">
                                🚀 Converter e Enfileirar Todos ({len(ml_lab_data['items'])})
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
        <div class="row g-3">
            {cards_html}
        </div>
        """

    # --- HTML: ABA 3 - COMPARADOR ---
    clusters_html = ""
    if not comp_report["clusters"]:
        clusters_html = """
        <div class="alert alert-success text-center py-4">
            <h5 class="mb-1">🎉 Nenhuma oferta repetida/similar detectada neste período!</h5>
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
            <div class="card mb-3 shadow-sm border-danger">
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
                    <table class="table table-sm align-middle mb-0">
                        <tbody>{rows_html}</tbody>
                    </table>
                </div>
            </div>
            """

    # --- HTML: ABA 4 - VENDAS ---
    sales_rows_html = ""
    if not filtered_orders:
        sales_rows_html = '<tr><td colspan="8" class="text-center py-4 text-muted">Nenhum pedido encontrado neste período. Clique em Atualizar Relatório.</td></tr>'
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

    # --- HTML: CHECKBOXES DE CATEGORIAS DO ML NA ABA 5 ---
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
        [
            c
            for c in excl_bestsellers_list
            if ml_auto._normalize_text(c) not in known_norm_set
        ]
    )

    import_options_html = "".join(
        f'<option value="{imp[2]}">[{imp[3]} - {imp[1]}] {imp[2]} (ID: {imp[0]})</option>'
        for imp in available_to_import
    )

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Portal de Ofertas — PromoCuiabá</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
        <!-- BARRA DE TOPO COMPACTA -->
        <nav class="navbar navbar-dark bg-dark shadow-sm mb-3 py-2">
            <div class="container-fluid px-4 d-flex flex-wrap justify-content-between align-items-center gap-2">
                <div class="d-flex align-items-center gap-3">
                    <span class="navbar-brand fw-bold mb-0 fs-5">🚀 PromoCuiabá</span>
                    <form method="GET" action="/" class="d-flex align-items-center gap-2 m-0">
                        <input type="date" name="selected_date" value="{current_date}" class="form-control form-control-sm" onchange="this.form.submit()">
                        <a href="/?selected_date={today_str}" class="btn btn-sm btn-outline-light text-nowrap">Hoje</a>
                    </form>
                </div>

                <div class="d-flex align-items-center gap-2 flex-wrap">
                    <span class="badge bg-primary fs-6">📋 Total: {total_offers}</span>
                    <span class="badge bg-success fs-6">✅ Enviadas: {sent_offers}</span>
                    <span class="badge bg-warning text-dark fs-6">⏳ Pendentes: {pending_offers}</span>
                    <div class="bg-secondary bg-opacity-25 border border-secondary rounded px-3 py-1 text-white d-flex align-items-center gap-2">
                        <span>⏱️ <strong id="countdown-timer" class="text-info">--:--</strong></span>
                        <small class="text-light opacity-75">({timer_status_text})</small>
                    </div>
                </div>
            </div>
        </nav>

        <div class="container-fluid px-4 pb-5">
            {alert_html}

           <!-- NAVEGAÇÃO EM 5 ABAS ESPECIALIZADAS (100% SINCRONIZADA VIA URL) -->
            <ul class="nav nav-tabs mb-3" id="portalTabs" role="tablist">
                <li class="nav-item" role="presentation">
                    <a class="nav-link fw-bold {tabs_map['fila'][1]}" href="/?tab=fila&selected_date={current_date}">
                        📋 Fila do Dia ({current_date})
                    </a>
                </li>
                <li class="nav-item" role="presentation">
                    <a class="nav-link fw-bold {tabs_map['ml_lab'][1]}" href="/?tab=ml_lab&selected_date={current_date}">
                        🧪 Laboratório Mercado Livre
                        <span class="badge bg-warning text-dark ms-1">Validação</span>
                    </a>
                </li>
                <li class="nav-item" role="presentation">
                    <a class="nav-link fw-bold {tabs_map['comparador'][1]}" href="/?tab=comparador&selected_date={current_date}&comp_days={scan_days}&comp_store={comp_store}">
                        🔍 Comparador de Repetidas
                        <span class="badge bg-danger ms-1">{comp_report['total_clusters']}</span>
                    </a>
                </li>
                <li class="nav-item" role="presentation">
                    <a class="nav-link fw-bold {tabs_map['vendas'][1]}" href="/?tab=vendas&selected_date={current_date}&sales_days={sales_days}&sales_platform={sales_platform}&sales_status={sales_status}">
                        💰 Vendas & Comissões
                    </a>
                </li>
                <li class="nav-item" role="presentation">
                    <a class="nav-link fw-bold {tabs_map['config'][1]}" href="/?tab=config&selected_date={current_date}">
                        ⚙️ Configurações
                    </a>
                </li>
            </ul>

            <div class="tab-content">
                <!-- ================= ABA 1: FILA DO DIA ================= -->
                <div class="tab-pane fade {tabs_map['fila'][0]}" id="pane-fila" role="tabpanel">
                    <!-- Barra de Ações Rápidas da Fila -->
                    <div class="d-flex flex-wrap gap-2 mb-3 align-items-center justify-content-between bg-white p-3 rounded shadow-sm border">
                        <div class="d-flex flex-wrap gap-2">
                            <button class="btn btn-dark fw-bold" type="button" data-bs-toggle="collapse" data-bs-target="#collapseAddManual">
                                ➕ Nova Oferta Manual
                            </button>
                            <button class="btn btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#collapseImport">
                                📥 Importar de Outra Data
                            </button>
                        </div>

                        <form action="/generate-auto-offers" method="POST" class="m-0">
                            <input type="hidden" name="target_date" value="{current_date}">
                            <button type="submit" class="btn btn-success fw-bold">
                                🎲 Gerar {configs.get('auto_refill_count', '15')} Ofertas Automáticas ({'60% Shopee / 40% ML' if ml_auto_enabled else f'Shopee {shopee_gen_pct}% Geral / {shopee_cat_pct}% Categorias'})
                            </button>
                        </form>
                    </div>

                    <!-- Painel Retrátil: Cadastrar Nova Oferta Manual -->
                    <div class="collapse mb-3" id="collapseAddManual">
                        <div class="card card-body border-dark shadow-sm">
                            <h6 class="fw-bold mb-3">➕ Cadastrar Link Manual (Shopee ou Mercado Livre)</h6>
                            <form action="/add-offer" method="POST" class="row g-3">
                                <input type="hidden" name="target_date" value="{current_date}">
                                <div class="col-md-12">
                                    <input type="url" name="product_url" id="productUrlInput" placeholder="Cole o link https://..." class="form-control" required>
                                </div>
                                <div class="col-md-6 d-flex align-items-center">
                                    <div class="form-check">
                                        <input class="form-check-input" type="checkbox" name="extract_video" value="1" id="extractVideoCheck">
                                        <label class="form-check-label fw-bold" for="extractVideoCheck">🎬 Extrair vídeo (.mp4) automaticamente (Shopee)</label>
                                    </div>
                                </div>
                                <div class="col-md-6">
                                    <input type="url" name="manual_video_url" id="manualVideoInput" placeholder="Ou cole o Link Direto do Vídeo (.mp4)" class="form-control">
                                </div>
                                <div class="col-md-12" id="videoValidationBox" style="display: none;">
                                    <div class="p-3 border rounded bg-light">
                                        <div id="videoValidationStatus" class="mb-2 fw-bold"></div>
                                        <video id="videoPreviewPlayer" class="rounded" style="max-height: 240px; display: none;" controls></video>
                                    </div>
                                </div>
                                <div class="col-md-12 d-flex gap-2">
                                    <button type="button" class="btn btn-outline-secondary" onclick="validateShopeeVideo()">🔍 Testar Vídeo</button>
                                    <button type="submit" class="btn btn-primary flex-grow-1 fw-bold">Converter e Salvar na Fila</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <!-- Painel Retrátil: Importar de Outra Data -->
                    <div class="collapse mb-3" id="collapseImport">
                        <div class="card card-body border-secondary shadow-sm">
                            <form action="/import-offer" method="POST" class="row g-2 align-items-center">
                                <input type="hidden" name="target_date" value="{current_date}">
                                <div class="col-md-9">
                                    <select name="product_url" class="form-select" required>
                                        <option value="">Selecione uma oferta anterior...</option>
                                        {import_options_html}
                                    </select>
                                </div>
                                <div class="col-md-3">
                                    <button type="submit" class="btn btn-warning w-100 fw-bold">Importar para {current_date}</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <!-- Tabela Principal da Fila -->
                    <div class="card shadow-sm">
                        <div class="card-body p-0">
                            <div class="table-responsive">
                                <table class="table table-hover align-middle mb-0">
                                    <thead class="table-light">
                                        <tr>
                                            <th>ID</th>
                                            <th>Produto</th>
                                            <th>Loja</th>
                                            <th>Categoria / Origem</th>
                                            <th>Tipo</th>
                                            <th>Status</th>
                                            <th>Ações</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {queue_rows_html}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ================= ABA 2: LABORATÓRIO MERCADO LIVRE ================= -->
                <div class="tab-pane fade {tabs_map['ml_lab'][0]}" id="pane-mllab" role="tabpanel">
                    <div class="card mb-3 shadow-sm border-warning">
                        <div class="card-body d-flex flex-wrap justify-content-between align-items-center gap-3">
                            <div>
                                <h5 class="fw-bold mb-1">🧪 Ambiente de Validação — Busca Automática Mercado Livre</h5>
                                <div class="small text-muted">
                                    Proporção configurada: <b>{ml_best_pct}% Mais Vendidos</b> (excluindo: <code>{html.escape(excl_bestsellers_raw)}</code>) + <b>{ml_offers_pct}% Ofertas (&lt; R$ 100)</b> | Filtro anti-repetição: <b>{ml_hist_days} dias</b>.
                                </div>
                            </div>
                            <form action="/ml-lab/simulate" method="POST" class="d-flex gap-2 align-items-center m-0">
                                <input type="hidden" name="selected_date" value="{current_date}">
                                <input type="number" name="sim_count" value="10" min="2" max="30" class="form-control form-control-sm" style="width: 80px;" title="Quantidade para simular">
                                <button type="submit" class="btn btn-warning fw-bold text-nowrap">
                                    🔍 Buscar e Simular Ofertas ML Agora
                                </button>
                            </form>
                        </div>
                    </div>

                    {ml_lab_html}
                </div>

                <!-- ================= ABA 3: COMPARADOR DE REPETIDAS ================= -->
                <div class="tab-pane fade {tabs_map['comparador'][0]}" id="pane-comp" role="tabpanel">
                    <div class="d-flex flex-wrap justify-content-between align-items-center bg-white p-3 rounded border shadow-sm mb-3 gap-2">
                        <form method="GET" action="/" class="d-flex flex-wrap gap-2 align-items-center m-0">
                            <input type="hidden" name="tab" value="comparador">
                            <span class="fw-bold">Período:</span>
                            <select name="comp_days" class="form-select form-select-sm" style="width:auto;" onchange="this.form.submit()">
                                <option value="7" {"selected" if scan_days == 7 else ""}>Últimos 7 dias</option>
                                <option value="14" {"selected" if scan_days == 14 else ""}>Últimos 14 dias</option>
                                <option value="30" {"selected" if scan_days == 30 else ""}>Últimos 30 dias</option>
                            </select>
                            <select name="comp_store" class="form-select form-select-sm" style="width:auto;" onchange="this.form.submit()">
                                <option value="all" {"selected" if comp_store == "all" else ""}>🌐 Todas as Lojas</option>
                                <option value="shopee" {"selected" if comp_store == "shopee" else ""}>🟠 Shopee</option>
                                <option value="mercado" {"selected" if comp_store == "mercado" else ""}>🟡 Mercado Livre</option>
                            </select>
                            <span class="badge bg-secondary ms-2">Analisados: {comp_report['total_scanned']}</span>
                            <span class="badge bg-danger">Grupos Repetidos: {comp_report['total_clusters']}</span>
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

                <!-- ================= ABA 4: VENDAS & COMISSÕES ================= -->
                <div class="tab-pane fade {tabs_map['vendas'][0]}" id="pane-vendas" role="tabpanel">
                    <div class="card mb-3 shadow-sm border-success">
                        <div class="card-body py-2">
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
                                    <button type="submit" class="btn btn-sm btn-success w-100 fw-bold">🔄 Atualizar Vendas</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="row g-3 mb-3">
                        <div class="col-md-3">
                            <div class="card text-center border-primary h-100">
                                <div class="card-body py-2">
                                    <small class="text-muted">🛍️ Pedidos ({sales_days}d)</small>
                                    <h3 class="fw-bold text-primary mb-0">{sales_data['total_orders']}</h3>
                                    <small class="text-muted">✅ {sales_data['completed_orders']} | ⏳ {sales_data['pending_orders']} | ❌ {sales_data['cancelled_orders']}</small>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-info h-100">
                                <div class="card-body py-2">
                                    <small class="text-muted">🛒 Volume Vendido (GMV)</small>
                                    <h3 class="fw-bold text-dark mb-0">{format_brl(sales_data['total_gmv'])}</h3>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-warning h-100">
                                <div class="card-body py-2">
                                    <small class="text-muted">💵 Comissão Estimada</small>
                                    <h3 class="fw-bold text-warning mb-0">{format_brl(sales_data['total_commission'])}</h3>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-success h-100">
                                <div class="card-body py-2">
                                    <small class="text-muted">✅ Comissão Concluída</small>
                                    <h3 class="fw-bold text-success mb-0">{format_brl(sales_data['completed_commission'])}</h3>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="card shadow-sm">
                        <div class="card-body p-0">
                            <div class="table-responsive">
                                <table class="table table-striped table-sm align-middle mb-0">
                                    <thead class="table-light">
                                        <tr>
                                            <th>Data/Hora</th>
                                            <th>Loja</th>
                                            <th>Pedido</th>
                                            <th>Itens</th>
                                            <th class="text-center">Qtd</th>
                                            <th>Valor Compra</th>
                                            <th>Comissão</th>
                                            <th>Status</th>
                                        </tr>
                                    </thead>
                                    <tbody>{sales_rows_html}</tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ================= ABA 5: CONFIGURAÇÕES CENTRALIZADAS ================= -->
                <div class="tab-pane fade {tabs_map['config'][0]}" id="pane-config" role="tabpanel">
                    <div class="row g-4">
                        <!-- Bloco 1: Horários e Shopee -->
                        <div class="col-md-6">
                            <div class="card shadow-sm h-100 border-primary">
                                <div class="card-header bg-primary text-white fw-bold">⚙️ 1. Horários de Disparo & Regras Shopee (30% / 70%)</div>
                                <div class="card-body">
                                    <form action="/update-config" method="POST" class="row g-3">
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">Início</label>
                                            <input type="time" name="start_time" value="{configs.get('start_time', '08:00')}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">Fim</label>
                                            <input type="time" name="end_time" value="{configs.get('end_time', '22:00')}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">Intervalo (Min)</label>
                                            <input type="number" name="interval_minutes" value="{configs.get('interval_minutes', '30')}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-6">
                                            <label class="form-label small fw-bold">Qtd. por Lote Automático</label>
                                            <input type="number" name="auto_refill_count" value="{configs.get('auto_refill_count', '15')}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-6">
                                            <label class="form-label small fw-bold">% Modo Geral na Shopee</label>
                                            <div class="input-group input-group-sm">
                                                <input type="number" min="0" max="100" name="shopee_general_pct" value="{shopee_gen_pct}" class="form-control" required>
                                                <span class="input-group-text">% ({shopee_cat_pct}% Categorias)</span>
                                            </div>
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold">👗 Categorias-Alvo Shopee ({shopee_cat_pct}%)</label>
                                            <input type="text" name="shopee_target_categories" value="{html.escape(shopee_target_cats)}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold">Palavras-Chave Extras Shopee (Opcional)</label>
                                            <input type="text" name="auto_keywords" value="{configs.get('auto_keywords', '')}" class="form-control form-control-sm">
                                        </div>
                                        <div class="col-md-12">
                                            <div class="form-check form-switch">
                                                <input class="form-check-input" type="checkbox" name="auto_refill_enabled" value="1" id="autoRefillSwitch" {"checked" if configs.get('auto_refill_enabled', '1') == '1' else ""}>
                                                <label class="form-check-label fw-bold small" for="autoRefillSwitch">🔄 Auto-Abastecer Fila quando zerar</label>
                                            </div>
                                        </div>
                                        <div class="col-md-12">
                                            <button type="submit" class="btn btn-primary btn-sm w-100 fw-bold">💾 Salvar Horários & Shopee</button>
                                        </div>
                                    </form>
                                </div>
                            </div>
                        </div>

                        <!-- Bloco 2: Anti-Repetição -->
                        <div class="col-md-6">
                            <div class="card shadow-sm h-100 border-dark">
                                <div class="card-header bg-dark text-white fw-bold">🛡️ 2. Motor Anti-Repetição & Termos Bloqueados</div>
                                <div class="card-body">
                                    <form action="/comparator/update-settings" method="POST" class="row g-3">
                                        <div class="col-md-6">
                                            <label class="form-label small fw-bold">Janela sem Repetir (Dias)</label>
                                            <input type="number" min="1" max="60" name="dedup_history_days" value="{dedup_days_cfg}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-6">
                                            <label class="form-label small fw-bold">Sensibilidade Similaridade (%)</label>
                                            <input type="number" min="35" max="95" name="dedup_similarity_threshold" value="{int(dedup_thresh_cfg)}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold">🚫 Termos / Produtos Bloqueados (separados por vírgula)</label>
                                            <textarea name="dedup_blocked_terms" rows="3" class="form-control form-control-sm" placeholder="Ex: cueca boxer, mini processador">{html.escape(blocked_terms_cfg)}</textarea>
                                        </div>
                                        <div class="col-md-12">
                                            <button type="submit" class="btn btn-dark btn-sm w-100 fw-bold">💾 Salvar Regras Anti-Repetição</button>
                                        </div>
                                    </form>
                                </div>
                            </div>
                        </div>

                        <!-- Bloco 3: Mercado Livre e Mix -->
                        <div class="col-md-12">
                            <div class="card shadow-sm border-warning">
                                <div class="card-header bg-warning text-dark fw-bold">🟡 3. Parametrização Mercado Livre & Mesclagem Automática (60% Shopee / 40% ML)</div>
                                <div class="card-body">
                                    <form action="/update-ml-params" method="POST" class="row g-3">
                                        <div class="col-md-12">
                                            <div class="form-check form-switch p-3 bg-warning-subtle border border-warning rounded">
                                                <input class="form-check-input ms-0 me-2" type="checkbox" name="ml_auto_enabled" value="1" id="mlAutoSwitch" {"checked" if ml_auto_enabled else ""}>
                                                <label class="form-check-label fw-bold" for="mlAutoSwitch">
                                                    🚀 Ativar Mesclagem Automática ({mix_shopee_pct}% Shopee + {mix_ml_pct}% Mercado Livre) na Geração da Fila
                                                </label>
                                                <div class="small text-muted">Enquanto estiver desmarcado, você pode testar e aprovar ofertas do ML manualmente na aba <b>🧪 Laboratório Mercado Livre</b>.</div>
                                            </div>
                                        </div>
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">Proporção Shopee no Mix (%)</label>
                                            <div class="input-group input-group-sm">
                                                <input type="number" min="0" max="100" name="mix_shopee_pct" value="{mix_shopee_pct}" class="form-control" required>
                                                <span class="input-group-text">% Shopee ({mix_ml_pct}% ML)</span>
                                            </div>
                                        </div>
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">% Mais Vendidos dentro do ML</label>
                                            <div class="input-group input-group-sm">
                                                <input type="number" min="0" max="100" name="ml_best_sellers_pct" value="{ml_best_pct}" class="form-control" required>
                                                <span class="input-group-text">% Mais Vendidos ({ml_offers_pct}% Ofertas)</span>
                                            </div>
                                        </div>
                                        <div class="col-md-4">
                                            <label class="form-label small fw-bold">Histórico sem Repetir ML (Dias)</label>
                                            <input type="number" min="1" max="60" name="ml_history_days" value="{ml_hist_days}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold">URL da Página de Ofertas do ML (Até R$ 100)</label>
                                            <input type="url" name="ml_offers_url" value="{html.escape(ml_offers_url)}" class="form-control form-control-sm" required>
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold text-danger">🚫 Categorias Excluídas em "Mais Vendidos" do ML:</label>
                                            <div class="row g-2 p-2 bg-white border rounded mb-2">
                                                {cat_checkboxes_html}
                                            </div>
                                            <input type="text" name="custom_excl_bestsellers" value="{html.escape(custom_bestsellers_str)}" placeholder="Outras categorias p/ excluir em Mais Vendidos (separadas por vírgula)" class="form-control form-control-sm">
                                        </div>
                                        <div class="col-md-12">
                                            <label class="form-label small fw-bold">🚫 Termos/Categorias Excluídas na Página de Ofertas ML (Opcional):</label>
                                            <input type="text" name="ml_excluded_categories_offers" value="{html.escape(excl_offers_raw)}" placeholder="Ex: gift card, digital" class="form-control form-control-sm">
                                        </div>
                                        <div class="col-md-12">
                                            <button type="submit" class="btn btn-warning fw-bold w-100">💾 Salvar Parâmetros do Mercado Livre</button>
                                        </div>
                                    </form>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            async function validateShopeeVideo() {{
                const urlInput = document.getElementById('productUrlInput');
                const box = document.getElementById('videoValidationBox');
                const statusEl = document.getElementById('videoValidationStatus');
                const player = document.getElementById('videoPreviewPlayer');
                const manualInput = document.getElementById('manualVideoInput');
                const check = document.getElementById('extractVideoCheck');

                if (!urlInput.value) {{
                    alert('Cole o link do produto primeiro!');
                    return;
                }}

                box.style.display = 'block';
                player.style.display = 'none';
                statusEl.innerHTML = '⏳ Validando e buscando vídeo... aguarde.';

                const formData = new FormData();
                formData.append('product_url', urlInput.value);

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

            let secondsLeft = {remaining_seconds};
            const timerEl = document.getElementById('countdown-timer');

            function updateCountdown() {{
                if (secondsLeft <= 0) {{
                    timerEl.textContent = "00:00";
                    return;
                }}
                const mins = Math.floor(secondsLeft / 60);
                const secs = secondsLeft % 60;
                timerEl.textContent = String(mins).padStart(2, '0') + ":" + String(secs).padStart(2, '0');
                secondsLeft--;
                if (secondsLeft === 0) {{
                    setTimeout(() => window.location.reload(), 1000);
                }}
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


# --- ROTAS DO LABORATÓRIO MERCADO LIVRE ---
@app.post("/ml-lab/simulate")
async def ml_lab_simulate(
    selected_date: str = Form(...),
    sim_count: int = Form(10),
):
    sim_data = await asyncio.to_thread(
        ml_auto.simulate_ml_offers_selection, max(2, min(30, sim_count))
    )
    _PORTAL_CACHE["ml_lab"] = sim_data
    return RedirectResponse(
        url=f"/?tab=ml_lab&selected_date={selected_date}", status_code=303
    )


@app.post("/ml-lab/approve-one")
async def ml_lab_approve_one(
    item_index: int = Form(...),
    target_date: str = Form(...),
):
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
        url=f"/?tab=ml_lab&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


@app.post("/ml-lab/approve-all")
async def ml_lab_approve_all(target_date: str = Form(...)):
    lab_data = _PORTAL_CACHE.get("ml_lab")
    approved = 0
    if lab_data and lab_data.get("items"):
        items_copy = list(lab_data["items"])
        for cand in items_copy:
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
        url=f"/?tab=fila&selected_date={target_date}&ml_approved={approved}",
        status_code=303,
    )


@app.post("/update-ml-params")
async def update_ml_parameters(
    request: Request,
    ml_auto_enabled: str = Form(None),
    mix_shopee_pct: str = Form("60"),
    ml_best_sellers_pct: str = Form("30"),
    ml_history_days: str = Form("7"),
    ml_offers_url: str = Form(ml_auto.DEFAULT_ML_OFERTAS_URL),
    custom_excl_bestsellers: str = Form(""),
    ml_excluded_categories_offers: str = Form(""),
):
    form_data = await request.form()
    checked_cats = form_data.getlist("excl_cat_checks")

    all_bestsellers_excl = list(checked_cats)
    for extra in ml_auto.parse_csv_list(custom_excl_bestsellers):
        if extra not in all_bestsellers_excl:
            all_bestsellers_excl.append(extra)

    update_config("ml_auto_enabled", "1" if ml_auto_enabled == "1" else "0")
    update_config("mix_shopee_pct", str(max(0, min(100, int(mix_shopee_pct or 60)))))
    update_config(
        "ml_best_sellers_pct", str(max(0, min(100, int(ml_best_sellers_pct or 30))))
    )
    update_config("ml_history_days", str(max(1, int(ml_history_days or 7))))
    update_config("ml_offers_url", ml_offers_url.strip())
    update_config("ml_excluded_categories_bestsellers", ", ".join(all_bestsellers_excl))
    update_config(
        "ml_excluded_categories_offers", ml_excluded_categories_offers.strip()
    )
    return RedirectResponse(url="/?tab=config&saved_cfg=1", status_code=303)


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
    return RedirectResponse(url="/?tab=config&saved_cfg=1", status_code=303)


@app.post("/comparator/block-term")
async def comparator_block_term(
    term: str = Form(...),
    comp_days: int = Form(14),
):
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
    comp_days: int = Form(14),
    comp_store: str = Form("all"),
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
    offer_id: int,
    comp_days: int = Form(14),
    comp_store: str = Form("all"),
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
    return RedirectResponse(
        url=f"/?selected_date={target_date}&gen_shopee={res['shopee_added']}&gen_ml={res['ml_added']}",
        status_code=303,
    )


@app.post("/update-config")
async def update_parameters(
    start_time: str = Form(...),
    end_time: str = Form(...),
    interval_minutes: str = Form(...),
    auto_refill_count: str = Form("15"),
    shopee_general_pct: str = Form("30"),
    shopee_target_categories: str = Form(
        "Roupas Femininas, Moda Infantil, Sapatos Femininos"
    ),
    auto_keywords: str = Form(""),
    auto_refill_enabled: str = Form(None),
):
    update_config("start_time", start_time)
    update_config("end_time", end_time)
    update_config("interval_minutes", interval_minutes)
    update_config("auto_refill_count", auto_refill_count)
    update_config(
        "shopee_general_pct", str(max(0, min(100, int(shopee_general_pct or 30))))
    )
    update_config("shopee_target_categories", shopee_target_categories.strip())
    update_config("auto_keywords", auto_keywords.strip())
    update_config("auto_refill_enabled", "1" if auto_refill_enabled == "1" else "0")
    return RedirectResponse(url="/?tab=config&saved_cfg=1", status_code=303)


@app.post("/delete/{offer_id}")
async def remove_offer(offer_id: int, current_date: str = Form(...)):
    delete_offer(offer_id)
    _PORTAL_CACHE["comp"]["ts"] = 0.0
    return RedirectResponse(url=f"/?selected_date={current_date}", status_code=303)


@app.post("/send-now/{offer_id}/{target}")
async def send_now(offer_id: int, target: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT store, product_url, formatted_message, media_url, COALESCE(media_type, 'image') FROM queue WHERE id = ?",
        (offer_id,),
    )
    row = cursor.fetchone()

    if row:
        store, product_url, saved_msg, saved_media, media_type = row
        if product_url:
            formatted_message, fresh_image = refresh_offer_data(product_url)
            media_url = (
                saved_media if media_type == "video" and saved_media else fresh_image
            )
            cursor.execute(
                "UPDATE queue SET formatted_message = ?, media_url = ? WHERE id = ?",
                (formatted_message, media_url, offer_id),
            )
            conn.commit()
        else:
            formatted_message, media_url = saved_msg, saved_media

        success = False
        if target == "all":
            s1 = trigger_whatsapp_dispatch(
                WHATSAPP_GROUP_PRINCIPAL, formatted_message, media_url, media_type
            )
            time.sleep(1)
            s2 = trigger_whatsapp_dispatch(
                WHATSAPP_GROUP_PARCERIA, formatted_message, media_url, media_type
            )
            success = s1 or s2
        elif target == "principal":
            success = trigger_whatsapp_dispatch(
                WHATSAPP_GROUP_PRINCIPAL, formatted_message, media_url, media_type
            )
        elif target == "parceria":
            success = trigger_whatsapp_dispatch(
                WHATSAPP_GROUP_PARCERIA, formatted_message, media_url, media_type
            )

        if success:
            cursor.execute("UPDATE queue SET status = 'sent' WHERE id = ?", (offer_id,))
            conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/add-offer")
async def add_offer(
    product_url: str = Form(...),
    target_date: str = Form(...),
    extract_video: str = Form(None),
    manual_video_url: str = Form(None),
):
    is_ml = "mercadolivre" in product_url.lower() or "meli.la" in product_url.lower()

    if is_ml:
        offer = await ml_offer.get_ml_offer(product_url)
        if offer and offer.get("affiliate_url"):
            msg = ml_offer.format_product_message(offer)
            media_url = offer.get("image_url")
        else:
            msg = f"🔥 Oferta do Mercado Livre:\n👉 {product_url}"
            media_url = None
        store_name = "Mercado Livre"
        cat_name = "Manual ML"
        video_status_param = ""
        media_type = "image"
    else:
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")

        api = ShopeeAffiliateAPI(app_id, app_secret) if app_id and app_secret else None

        if not api:
            add_to_queue(
                store="Shopee",
                product_url=product_url,
                message=f"🔥 Oferta imperdível:\n👉 {product_url}",
                priority=1,
                target_date=target_date,
                category="Manual Shopee",
            )
            return RedirectResponse(
                url=f"/?selected_date={target_date}", status_code=303
            )

        full_url = api.resolve_short_link(product_url)
        item_id = api.extract_item_id(full_url) or api.extract_item_id(product_url)

        video_url_found = None
        video_status_param = ""

        if manual_video_url and manual_video_url.strip().startswith("http"):
            video_url_found = manual_video_url.strip()
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

        offer = api.get_product_offer(item_id) if item_id else None

        if offer:
            aff_link = offer.get("offerLink") or api.generate_short_link(full_url)
            msg = api.format_product_message(offer, aff_link)
            media_url = video_url_found if video_url_found else offer.get("imageUrl")
            cat_name = "Manual Shopee"
        else:
            clean_dest = api.clean_shopee_url_for_affiliate(product_url)
            aff_link = (
                api.generate_short_link(clean_dest)
                or api.generate_short_link(full_url)
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
        url=f"/?selected_date={target_date}{video_status_param}", status_code=303
    )


@app.post("/import-offer")
async def import_offer(product_url: str = Form(...), target_date: str = Form(...)):
    is_ml = "mercadolivre" in product_url.lower() or "meli.la" in product_url.lower()

    if is_ml:
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
            full_url = api.resolve_short_link(product_url)
            item_id = api.extract_item_id(full_url) or api.extract_item_id(product_url)
            offer = api.get_product_offer(item_id) if item_id else None
            if offer:
                aff_link = offer.get("offerLink") or api.generate_short_link(full_url)
                msg = api.format_product_message(offer, aff_link)
                media_url = offer.get("imageUrl")
            else:
                aff_link = api.generate_short_link(full_url) or product_url
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
    return RedirectResponse(url=f"/?selected_date={target_date}", status_code=303)
