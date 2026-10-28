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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Portal de Automação de Ofertas")

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip(
    "/"
)
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


def ensure_portal_defaults():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    defaults = {
        "shopee_general_pct": "30",
        "shopee_target_categories": "Roupas Femininas, Moda Infantil, Sapatos Femininos",
    }
    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


ensure_media_type_column()
offer_comparator.ensure_comparator_configs()
ensure_portal_defaults()


def format_brl(val: float) -> str:
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def convert_html_to_whatsapp(text: str) -> str:
    if not text:
        return ""
    import re

    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def trigger_whatsapp_dispatch(
    target_group: str,
    message: str,
    media_url: str | None = None,
    media_type: str = "image",
) -> bool:
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        return False
    final_message = convert_html_to_whatsapp(message)
    try:
        headers = {"apikey": EVOLUTION_API_KEY}
        if media_url:
            try:
                media_resp = requests.get(media_url, timeout=25)
                if media_resp.status_code == 200:
                    url_api = (
                        f"{EVOLUTION_API_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
                    )
                    if media_type == "video":
                        files = {
                            "file": ("oferta.mp4", media_resp.content, "video/mp4")
                        }
                        data = {
                            "number": target_group,
                            "mediatype": "video",
                            "caption": final_message,
                            "fileName": "oferta.mp4",
                        }
                    else:
                        files = {
                            "file": ("oferta.jpg", media_resp.content, "image/jpeg")
                        }
                        data = {
                            "number": target_group,
                            "mediatype": "image",
                            "caption": final_message,
                            "fileName": "oferta.jpg",
                        }
                    res = requests.post(
                        url_api, data=data, files=files, headers=headers, timeout=45
                    )
                    if res.status_code in [200, 201]:
                        return True
            except Exception:
                pass

        url_api = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        payload = {"number": target_group, "text": final_message, "delay": 1200}
        headers["Content-Type"] = "application/json"
        res = requests.post(url_api, json=payload, headers=headers, timeout=10)
        return res.status_code in [200, 201]
    except Exception:
        return False


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
        aff_link = api.generate_short_link(full_url) or product_url
        msg = f"🔥 <b>PRODUTO EM DESTAQUE NA SHOPEE</b>\n\n🏪 Loja: <b>Shopee</b>\n\n🚚 <b>Aproveite os cupons de FRETE GRÁTIS!</b>\n\n👉 {aff_link}"
        return msg, None


@app.post("/validate-video")
async def validate_video(product_url: str = Form(...)):
    """Testa e valida a extração do vídeo (.mp4) da Shopee sem salvar na fila."""
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


_PORTAL_CACHE = {
    "sales": {"ts": 0.0, "key": "", "data": None},
    "comp": {"ts": 0.0, "key": "", "data": None},
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
        "SELECT id, store, product_url, target_date FROM queue WHERE target_date != ? GROUP BY product_url ORDER BY id DESC LIMIT 100",
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
        timer_status_text = f"Fora do horário ({start_str} às {end_str})"
    elif pending_offers == 0 and current_date == today_str:
        timer_status_text = "Sem ofertas pendentes hoje"
    elif next_dispatch_str:
        try:
            next_dt = datetime.strptime(next_dispatch_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=cuiaba_tz
            )
            diff = int((next_dt - now_dt).total_seconds())
            if diff > 0:
                remaining_seconds = diff
                timer_status_text = f"Próximo às {next_dt.strftime('%H:%M:%S')}"
            else:
                timer_status_text = "Disparando em instantes..."
        except Exception:
            timer_status_text = "Aguardando ciclo..."
    elif pending_offers > 0 and current_date == today_str:
        timer_status_text = "Pronto para iniciar (< 30s)"

    alert_html = ""
    if video_msg == "ok":
        alert_html = '<div class="alert alert-success">🎬 Oferta salva com <b>VÍDEO (.mp4)</b> com sucesso!</div>'
    elif video_msg == "fallback":
        alert_html = '<div class="alert alert-warning">⚠️ Não foi possível extrair o vídeo automaticamente; a oferta foi salva usando a <b>Imagem</b> padrão da API.</div>'
    elif cleaned_count is not None:
        alert_html = f'<div class="alert alert-success">🧹 Limpeza concluída! <b>{cleaned_count} ofertas duplicadas pendentes</b> foram removidas da fila.</div>'
    elif blocked_term:
        alert_html = f'<div class="alert alert-warning">🚫 O termo <b>"{html.escape(blocked_term)}"</b> foi adicionado à lista de bloqueio e não será mais buscado automaticamente!</div>'

    # --- Proporções Shopee (30% Geral / 70% Categorias) ---
    shopee_gen_pct = int(configs.get("shopee_general_pct", "30"))
    shopee_cat_pct = 100 - shopee_gen_pct
    shopee_target_cats = configs.get(
        "shopee_target_categories",
        "Roupas Femininas, Moda Infantil, Sapatos Femininos",
    )

    # --- 1. COMPARADOR COM CACHE DE 30 SEGUNDOS ---
    dedup_days_cfg = int(configs.get("dedup_history_days", "7"))
    dedup_thresh_cfg = float(configs.get("dedup_similarity_threshold", "60"))
    blocked_terms_cfg = configs.get("dedup_blocked_terms", "")
    scan_days = comp_days if comp_days is not None else max(14, dedup_days_cfg)

    comp_cache_key = f"{scan_days}_{dedup_thresh_cfg}_{comp_store}_{total_offers}"
    now_ts = time.time()
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

    # --- 2. RELATÓRIO DE VENDAS (SÓ CONSULTA A API DA SHOPEE SE ABRIR A ABA VENDAS OU CACHE < 120s) ---
    sales_data = _PORTAL_CACHE["sales"]["data"] or {
        "days": sales_days,
        "total_orders": 0,
        "pending_orders": 0,
        "completed_orders": 0,
        "cancelled_orders": 0,
        "total_gmv": 0.0,
        "total_commission": 0.0,
        "completed_commission": 0.0,
        "pending_commission": 0.0,
        "orders": [],
    }
    sales_cache_key = f"{sales_days}_{sales_platform}"
    if tab == "vendas":
        if (
            _PORTAL_CACHE["sales"]["data"] is None
            or _PORTAL_CACHE["sales"]["key"] != sales_cache_key
            or (now_ts - _PORTAL_CACHE["sales"]["ts"]) > 120
        ):
            app_id = os.environ.get("SHOPEE_APP_ID")
            app_secret = os.environ.get("SHOPEE_APP_SECRET")
            if app_id and app_secret and sales_platform in ("all", "shopee"):
                try:
                    api_sales = ShopeeAffiliateAPI(app_id, app_secret)
                    sales_data = await asyncio.to_thread(
                        api_sales.get_sales_report, sales_days
                    )
                    _PORTAL_CACHE["sales"] = {
                        "ts": now_ts,
                        "key": sales_cache_key,
                        "data": sales_data,
                    }
                except Exception as e:
                    logger.error(f"Erro ao carregar relatório de vendas Shopee: {e}")

    # Filtra lista de pedidos por status na visualização, se solicitado
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

    # Controle de aba ativa
    active_fila = "show active" if tab == "fila" else ""
    active_btn_fila = "active" if tab == "fila" else ""
    active_comp = "show active" if tab == "comparador" else ""
    active_btn_comp = "active" if tab == "comparador" else ""
    active_vendas = "show active" if tab == "vendas" else ""
    active_btn_vendas = "active" if tab == "vendas" else ""

    # Monta o HTML dos Clusters do Comparador
    clusters_html = ""
    if not comp_report["clusters"]:
        clusters_html = """
        <div class="alert alert-success text-center py-4">
            <h5 class="mb-1">🎉 Nenhuma oferta repetida/similar detectada neste período!</h5>
            <small>Todos os produtos na fila possuem títulos distintos dentro do limite de similaridade configurado.</small>
        </div>
        """
    else:
        for cl in comp_report["clusters"]:
            c_title = html.escape(cl["canonical_title"])
            c_sig = html.escape(cl["signature"])
            c_img = cl["media_url"]
            img_tag = (
                f"<img src='{c_img}' style='width:64px;height:64px;object-fit:cover;' class='rounded border me-3'>"
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
                g_title = html.escape(g_item["title"])
                g_url = g_item["product_url"]
                g_date = g_item["target_date"]
                g_sim = g_item["similarity"]
                g_store = g_item["store"]
                is_suggested_del = gid in cl["suggested_delete_ids"]

                st_badge = (
                    "<span class='badge bg-warning text-dark'>Pendente</span>"
                    if g_item["status"] == "pending"
                    else "<span class='badge bg-success'>Enviado</span>"
                )
                prio_badge = (
                    "<span class='badge bg-danger'>Manual</span>"
                    if g_item["priority"] == 1
                    else "<span class='badge bg-secondary'>Auto</span>"
                )
                sug_badge = (
                    " <span class='badge bg-danger-subtle text-danger border border-danger'>Duplicata Pendente</span>"
                    if is_suggested_del
                    else ""
                )

                rows_html += f"""
                <tr class="{'table-warning' if is_suggested_del else ''}">
                    <td><b>#{gid}</b></td>
                    <td>{g_store}</td>
                    <td><span class="badge bg-dark">{g_date}</span></td>
                    <td><span class="badge bg-info text-dark">{g_sim}%</span></td>
                    <td>
                        <div class="fw-semibold">{g_title}{sug_badge}</div>
                        <a href="{g_url}" target="_blank" class="small text-muted text-truncate d-inline-block" style="max-width: 420px;">{g_url}</a>
                    </td>
                    <td>{prio_badge}</td>
                    <td>{st_badge}</td>
                    <td class="text-end">
                        <form action="/comparator/delete-item/{gid}" method="POST" class="d-inline">
                            <input type="hidden" name="comp_days" value="{scan_days}">
                            <input type="hidden" name="comp_store" value="{comp_store}">
                            <button type="submit" class="btn btn-sm btn-outline-danger" title="Excluir esta oferta">🗑️</button>
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
                            <span class="badge bg-danger me-1">🔁 {cl['total_occurrences']}x Repetido</span>
                            <span class="badge bg-success me-1">{cl['sent_count']} Enviados</span>
                            <span class="badge bg-warning text-dark me-2">{cl['pending_count']} Pendentes</span>
                            <strong class="fs-6">{c_title}</strong>
                            <div class="small text-muted mt-1">
                                🔑 Assinatura detectada: <code>{c_sig}</code> | 📅 Datas: {dates_badges}
                            </div>
                        </div>
                    </div>
                    <div class="d-flex gap-2">
                        <form action="/comparator/block-term" method="POST" class="d-inline">
                            <input type="hidden" name="term" value="{c_sig}">
                            <input type="hidden" name="comp_days" value="{scan_days}">
                            <button type="submit" class="btn btn-sm btn-dark">
                                🚫 Bloquear "{c_sig}"
                            </button>
                        </form>
                    </div>
                </div>
                <div class="card-body p-0">
                    <div class="table-responsive">
                        <table class="table table-sm table-hover align-middle mb-0">
                            <thead class="table-light">
                                <tr>
                                    <th>ID</th>
                                    <th>Loja</th>
                                    <th>Data</th>
                                    <th>Similaridade</th>
                                    <th>Título do Anúncio / Link Diferente</th>
                                    <th>Tipo</th>
                                    <th>Status</th>
                                    <th class="text-end">Ação</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows_html}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """

    # Monta as linhas da tabela de Vendas (Pedidos Shopee)
    sales_rows_html = ""
    if not filtered_orders:
        sales_rows_html = """
        <tr>
            <td colspan="7" class="text-center py-4 text-muted">
                Nenhum pedido encontrado para os filtros selecionados neste período.
            </td>
        </tr>
        """
    else:
        for ord_item in filtered_orders:
            st_lbl = ord_item["status_label"]
            if st_lbl == "Concluído":
                st_badge = "<span class='badge bg-success'>✅ Concluído</span>"
            elif st_lbl == "Cancelado":
                st_badge = "<span class='badge bg-secondary'>❌ Cancelado</span>"
            else:
                st_badge = "<span class='badge bg-warning text-dark'>⏳ Pendente</span>"

            plat_badge = (
                "<span class='badge' style='background-color:#ee4d2d;'>Shopee</span>"
            )
            sales_rows_html += f"""
            <tr>
                <td><span class="fw-semibold">{ord_item['purchase_time']}</span></td>
                <td>{plat_badge}</td>
                <td><code>{html.escape(ord_item['order_id'])}</code></td>
                <td style="max-width: 380px;">
                    <div class="text-truncate" title="{html.escape(ord_item['items_summary'])}">
                        {html.escape(ord_item['items_summary'])}
                    </div>
                </td>
                <td class="text-center">{ord_item['items_count']}</td>
                <td class="fw-semibold">{format_brl(ord_item['order_amount'])}</td>
                <td class="fw-bold text-success">{format_brl(ord_item['commission'])}</td>
                <td>{st_badge}</td>
            </tr>
            """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Portal de Automação - Ofertas & Vendas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
        <div class="container py-4">
            <h1 class="mb-3">🚀 Painel de Controle de Ofertas (Cuiabá MT -04:00)</h1>
            {alert_html}

            <!-- Cards Superiores da Fila -->
            <div class="row g-3 mb-4">
                <div class="col-md-3">
                    <div class="card text-center shadow-sm border-primary h-100">
                        <div class="card-body">
                            <h6 class="text-muted mb-1">📋 Total na Data ({current_date})</h6>
                            <h2 class="fw-bold text-primary mb-0">{total_offers}</h2>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card text-center shadow-sm border-success h-100">
                        <div class="card-body">
                            <h6 class="text-muted mb-1">✅ Ofertas Enviadas</h6>
                            <h2 class="fw-bold text-success mb-0">{sent_offers}</h2>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card text-center shadow-sm border-warning h-100">
                        <div class="card-body">
                            <h6 class="text-muted mb-1">⏳ Ofertas Pendentes</h6>
                            <h2 class="fw-bold text-warning mb-0">{pending_offers}</h2>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card text-center shadow-sm border-dark bg-dark text-white h-100">
                        <div class="card-body">
                            <h6 class="text-light mb-1">⏱️ Próximo Disparo</h6>
                            <h2 class="fw-bold text-info mb-0" id="countdown-timer">--:--</h2>
                            <small class="text-secondary d-block mt-1">{timer_status_text}</small>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Menu de Abas Principal -->
            <ul class="nav nav-tabs mb-4" id="portalTabs" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="nav-link fw-bold {active_btn_fila}" id="fila-tab" data-bs-toggle="tab" data-bs-target="#pane-fila" type="button" role="tab">
                        📋 Fila de Ofertas & Operação
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link fw-bold {active_btn_comp}" id="comp-tab" data-bs-toggle="tab" data-bs-target="#pane-comp" type="button" role="tab">
                        🔍 Comparador de Ofertas Repetidas
                        <span class="badge bg-danger ms-1">{comp_report['total_clusters']} grupos</span>
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="nav-link fw-bold {active_btn_vendas}" id="vendas-tab" data-bs-toggle="tab" data-bs-target="#pane-vendas" type="button" role="tab">
                        💰 Resultado de Vendas (Comissões)
                        <span class="badge bg-success ms-1">{sales_data['total_orders']} pedidos</span>
                    </button>
                </li>
            </ul>

            <div class="tab-content">
                <!-- ================= ABA 1: FILA E OPERAÇÃO ================= -->
                <div class="tab-pane fade {active_fila}" id="pane-fila" role="tabpanel">
                    <div class="card mb-4 shadow-sm">
                        <div class="card-header bg-primary text-white">⚙️ Parâmetros de Envio & Mix de Categorias da Shopee (30% Geral / 70% Categorias)</div>
                        <div class="card-body">
                            <form action="/update-config" method="POST" class="row g-3">
                                <div class="col-md-2">
                                    <label class="form-label">Início dos Envios</label>
                                    <input type="time" name="start_time" value="{configs.get('start_time', '08:00')}" class="form-control" required>
                                </div>
                                <div class="col-md-2">
                                    <label class="form-label">Fim dos Envios</label>
                                    <input type="time" name="end_time" value="{configs.get('end_time', '22:00')}" class="form-control" required>
                                </div>
                                <div class="col-md-2">
                                    <label class="form-label">Intervalo (Min)</label>
                                    <input type="number" name="interval_minutes" value="{configs.get('interval_minutes', '30')}" class="form-control" required>
                                </div>
                                <div class="col-md-2">
                                    <label class="form-label">Qtd. por Lote Auto</label>
                                    <input type="number" name="auto_refill_count" value="{configs.get('auto_refill_count', '15')}" class="form-control" required>
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label fw-bold">% Modo Geral (Mais Vendidos/Top)</label>
                                    <div class="input-group">
                                        <input type="number" min="0" max="100" name="shopee_general_pct" value="{shopee_gen_pct}" class="form-control" required>
                                        <span class="input-group-text">% Geral ({shopee_cat_pct}% Categorias)</span>
                                    </div>
                                </div>

                                <div class="col-md-6">
                                    <label class="form-label fw-bold">👗 Categorias-Alvo ({shopee_cat_pct}% das Ofertas da Shopee)</label>
                                    <input type="text" name="shopee_target_categories" value="{html.escape(shopee_target_cats)}" class="form-control" required>
                                    <small class="text-muted">Padrão: Roupas Femininas, Moda Infantil, Sapatos Femininos</small>
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label">Palavras-Chave Extras p/ os {shopee_gen_pct}% Gerais (Opcional)</label>
                                    <input type="text" name="auto_keywords" value="{configs.get('auto_keywords', '')}" placeholder="Deixe vazio para Mais Vendidos Gerais + Top Performing" class="form-control">
                                </div>

                                <div class="col-md-8 d-flex align-items-center">
                                    <div class="form-check form-switch">
                                        <input class="form-check-input" type="checkbox" name="auto_refill_enabled" value="1" id="autoRefillSwitch" {"checked" if configs.get('auto_refill_enabled', '1') == '1' else ""}>
                                        <label class="form-check-label fw-bold" for="autoRefillSwitch">
                                            🔄 Auto-Abastecer Fila quando esvaziar ({shopee_gen_pct}% Geral + {shopee_cat_pct}% Categorias [{html.escape(shopee_target_cats)}])
                                        </label>
                                    </div>
                                </div>
                                <div class="col-md-4 d-flex align-items-end">
                                    <button type="submit" class="btn btn-success w-100 fw-bold">Salvar Parâmetros</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card mb-4 shadow-sm border-info">
                        <div class="card-header bg-info text-white">📅 Calendário de Ofertas (Data Selecionada: {current_date})</div>
                        <div class="card-body">
                            <form method="GET" action="/" class="row g-3 align-items-end">
                                <div class="col-md-4">
                                    <label class="form-label fw-bold">Escolha a Data de Organização:</label>
                                    <input type="date" name="selected_date" value="{current_date}" class="form-control" onchange="this.form.submit()">
                                </div>
                                <div class="col-md-8">
                                    <a href="/?selected_date={today_str}" class="btn btn-outline-dark">Ir para Hoje ({today_str})</a>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card mb-4 shadow-sm border-success">
                        <div class="card-header bg-success text-white">
                            🤖 Gerar Lista Automática da Shopee ({shopee_gen_pct}% Mais Vendidos Geral + {shopee_cat_pct}% Categorias: {html.escape(shopee_target_cats)})
                        </div>
                        <div class="card-body">
                            <form action="/generate-auto-offers" method="POST" class="row g-3">
                                <input type="hidden" name="target_date" value="{current_date}">
                                <div class="col-md-12">
                                    <button type="submit" class="btn btn-success w-100 fw-bold">
                                        🎲 Gerar {configs.get('auto_refill_count', '15')} Ofertas ({shopee_gen_pct}% Geral / {shopee_cat_pct}% Moda & Calçados) para {current_date}
                                    </button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card mb-4 shadow-sm">
                        <div class="card-header bg-dark text-white">➕ Cadastrar Nova Oferta para {current_date}</div>
                        <div class="card-body">
                            <form action="/add-offer" method="POST" class="row g-3" id="addOfferForm">
                                <input type="hidden" name="target_date" value="{current_date}">
                                <div class="col-md-12">
                                    <label class="form-label">Cole o Link do Produto (Shopee ou Mercado Livre)</label>
                                    <input type="url" name="product_url" id="productUrlInput" placeholder="https://..." class="form-control" required>
                                </div>
                                <div class="col-md-6 d-flex align-items-center">
                                    <div class="form-check">
                                        <input class="form-check-input" type="checkbox" name="extract_video" value="1" id="extractVideoCheck">
                                        <label class="form-check-label fw-bold" for="extractVideoCheck">
                                            🎬 Tentar extrair vídeo (.mp4) automaticamente (Shopee)
                                        </label>
                                    </div>
                                </div>
                                <div class="col-md-6">
                                    <input type="url" name="manual_video_url" id="manualVideoInput" placeholder="Ou cole o Link Direto do Vídeo (.mp4) Opcional" class="form-control">
                                </div>

                                <div class="col-md-12" id="videoValidationBox" style="display: none;">
                                    <div class="p-3 border rounded bg-white">
                                        <div id="videoValidationStatus" class="mb-2 fw-bold"></div>
                                        <video id="videoPreviewPlayer" class="rounded" style="max-height: 280px; display: none;" controls></video>
                                    </div>
                                </div>

                                <div class="col-md-12 d-flex gap-2">
                                    <button type="button" class="btn btn-outline-secondary" id="btnValidateVideo" onclick="validateShopeeVideo()">
                                        🔍 Testar / Validar Vídeo Antes de Salvar
                                    </button>
                                    <button type="submit" class="btn btn-primary flex-grow-1">
                                        Converter e Salvar para {current_date}
                                    </button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card mb-4 shadow-sm">
                        <div class="card-header bg-secondary text-white">📥 Importar Oferta de Outra Data para {current_date}</div>
                        <div class="card-body">
                            <form action="/import-offer" method="POST" class="row g-3">
                                <input type="hidden" name="target_date" value="{current_date}">
                                <div class="col-md-9">
                                    <select name="product_url" class="form-control" required>
                                        <option value="">Selecione uma oferta cadastrada anteriormente...</option>
    """

    for imp in available_to_import:
        html_content += (
            f'<option value="{imp[2]}">[{imp[3]}] {imp[2]} (ID: {imp[0]})</option>'
        )

    html_content += f"""
                                    </select>
                                </div>
                                <div class="col-md-3">
                                    <button type="submit" class="btn btn-warning w-100">Importar para {current_date}</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card shadow-sm">
                        <div class="card-header bg-secondary text-white">📋 Fila de Ofertas para o dia {current_date}</div>
                        <div class="card-body">
                            <table class="table table-striped align-middle">
                                <thead>
                                    <tr>
                                        <th>ID</th>
                                        <th>Loja</th>
                                        <th>Categoria</th>
                                        <th>Link</th>
                                        <th>Ações (Atualiza na Hora do Envio)</th>
                                        <th>Prioridade</th>
                                        <th>Status</th>
                                        <th>Criado em</th>
                                    </tr>
                                </thead>
                                <tbody>
    """

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
        badge_category = f"<span class='badge bg-light text-dark border'>{html.escape(category_name or 'Geral')}</span>"

        if media_type == "video" and media_url:
            media_preview_html = f"<video src='{media_url}' class='img-fluid mb-3 rounded w-100' controls></video>"
        elif media_url:
            media_preview_html = (
                f"<img src='{media_url}' class='img-fluid mb-3 rounded' alt='Foto'>"
            )
        else:
            media_preview_html = ""

        html_content += f"""
                                    <tr>
                                        <td>{item_id}</td>
                                        <td>{store}{badge_media}</td>
                                        <td>{badge_category}</td>
                                        <td><a href="{prod_url}" target="_blank" class="text-truncate d-inline-block" style="max-width: 130px;">{prod_url}</a></td>
                                        <td>
                                            <button class="btn btn-sm btn-outline-info mb-1" data-bs-toggle="modal" data-bs-target="#modalMsg{item_id}">👁️ Ver</button>

                                            <div class="btn-group mb-1" role="group">
                                                <form action="/send-now/{item_id}/all" method="POST" class="d-inline">
                                                    <button type="submit" class="btn btn-sm btn-success" title="Atualiza preço e envia">🚀 Todos</button>
                                                </form>
                                                <form action="/send-now/{item_id}/principal" method="POST" class="d-inline ms-1">
                                                    <button type="submit" class="btn btn-sm btn-outline-success">Principal</button>
                                                </form>
                                                <form action="/send-now/{item_id}/parceria" method="POST" class="d-inline ms-1">
                                                    <button type="submit" class="btn btn-sm btn-outline-secondary">Parceria</button>
                                                </form>
                                            </div>

                                            <form action="/delete/{item_id}" method="POST" class="d-inline" onsubmit="return confirm('Deseja excluir esta oferta?')">
                                                <input type="hidden" name="current_date" value="{current_date}">
                                                <button type="submit" class="btn btn-sm btn-danger mb-1">🗑️</button>
                                            </form>

                                            <div class="modal fade" id="modalMsg{item_id}" tabindex="-1">
                                              <div class="modal-dialog">
                                                <div class="modal-content">
                                                  <div class="modal-header">
                                                    <h5 class="modal-title">Pré-visualização da Oferta #{item_id}</h5>
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
                                        <td>{badge_prio}</td>
                                        <td>{badge_status}</td>
                                        <td>{created_at}</td>
                                    </tr>
        """

    html_content += f"""
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- ================= ABA 2: COMPARADOR DE OFERTAS REPETIDAS ================= -->
                <div class="tab-pane fade {active_comp}" id="pane-comp" role="tabpanel">
                    <div class="row g-3 mb-4">
                        <div class="col-md-3">
                            <div class="card text-center border-secondary h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">🔎 Ofertas Analisadas ({scan_days}d)</h6>
                                    <h3 class="fw-bold mb-0">{comp_report['total_scanned']}</h3>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-danger h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">📦 Produtos Que Se Repetiram</h6>
                                    <h3 class="fw-bold text-danger mb-0">{comp_report['total_clusters']}</h3>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-warning h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">📑 Total de Anúncios Envolvidos</h6>
                                    <h3 class="fw-bold text-warning mb-0">{comp_report['total_duplicated_items']}</h3>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center border-success h-100">
                                <div class="card-body d-flex flex-column justify-content-center">
                                    <h6 class="text-muted mb-2">🧹 Duplicatas Pendentes p/ Limpar</h6>
                                    <form action="/comparator/clean-duplicates" method="POST">
                                        <input type="hidden" name="comp_days" value="{scan_days}">
                                        <input type="hidden" name="comp_store" value="{comp_store}">
                                        <button type="submit" class="btn btn-sm btn-danger fw-bold w-100" {"disabled" if comp_report['total_suggested_deletes'] == 0 else ""}>
                                            🗑️ Excluir {comp_report['total_suggested_deletes']} Pendentes Repetidas
                                        </button>
                                    </form>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="card mb-4 shadow-sm border-dark">
                        <div class="card-header bg-dark text-white fw-bold">
                            🛡️ Regras Anti-Repetição (Aplicadas na Coleta Automática da Shopee e futuras plataformas)
                        </div>
                        <div class="card-body">
                            <form action="/comparator/update-settings" method="POST" class="row g-3">
                                <div class="col-md-3">
                                    <label class="form-label fw-bold">Janela de Histórico (Dias)</label>
                                    <input type="number" min="1" max="60" name="dedup_history_days" value="{dedup_days_cfg}" class="form-control" required>
                                    <small class="text-muted">Não repetir produtos similares enviados nos últimos X dias.</small>
                                </div>
                                <div class="col-md-3">
                                    <label class="form-label fw-bold">Sensibilidade de Similaridade (%)</label>
                                    <input type="number" min="35" max="95" name="dedup_similarity_threshold" value="{int(dedup_thresh_cfg)}" class="form-control" required>
                                    <small class="text-muted">Padrão: 60%. Menor = mais rigoroso; Maior = só títulos quase idênticos.</small>
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">🚫 Produtos / Termos Bloqueados na Coleta (separados por vírgula)</label>
                                    <input type="text" name="dedup_blocked_terms" value="{html.escape(blocked_terms_cfg)}" placeholder="Ex: cueca boxer, fone tws, mini processador" class="form-control">
                                    <small class="text-muted">Qualquer anúncio contendo estes termos será ignorado na geração automática.</small>
                                </div>
                                <div class="col-md-12 text-end">
                                    <button type="submit" class="btn btn-primary fw-bold">💾 Salvar Regras Anti-Repetição</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <div class="card mb-3 shadow-sm">
                        <div class="card-body py-2">
                            <form method="GET" action="/" class="row g-2 align-items-center">
                                <input type="hidden" name="tab" value="comparador">
                                <div class="col-auto">
                                    <label class="fw-bold">Filtrar diagnóstico:</label>
                                </div>
                                <div class="col-auto">
                                    <select name="comp_days" class="form-select form-select-sm" onchange="this.form.submit()">
                                        <option value="7" {"selected" if scan_days == 7 else ""}>Últimos 7 dias</option>
                                        <option value="14" {"selected" if scan_days == 14 else ""}>Últimos 14 dias</option>
                                        <option value="30" {"selected" if scan_days == 30 else ""}>Últimos 30 dias</option>
                                    </select>
                                </div>
                                <div class="col-auto">
                                    <select name="comp_store" class="form-select form-select-sm" onchange="this.form.submit()">
                                        <option value="all" {"selected" if comp_store == "all" else ""}>🌐 Todas as Plataformas</option>
                                        <option value="shopee" {"selected" if comp_store == "shopee" else ""}>🟠 Apenas Shopee</option>
                                        <option value="mercado" {"selected" if comp_store == "mercado" else ""}>🟡 Apenas Mercado Livre</option>
                                    </select>
                                </div>
                            </form>
                        </div>
                    </div>

                    {clusters_html}
                </div>

                <!-- ================= ABA 3: RESULTADO DE VENDAS (COMISSÕES) ================= -->
                <div class="tab-pane fade {active_vendas}" id="pane-vendas" role="tabpanel">
                    <!-- Filtro de Período e Plataforma -->
                    <div class="card mb-4 shadow-sm border-success">
                        <div class="card-body py-3">
                            <form method="GET" action="/" class="row g-3 align-items-end">
                                <input type="hidden" name="tab" value="vendas">
                                <div class="col-md-3">
                                    <label class="form-label fw-bold">📅 Período de Apuração:</label>
                                    <select name="sales_days" class="form-select" onchange="this.form.submit()">
                                        <option value="7" {"selected" if sales_days == 7 else ""}>Últimos 7 dias</option>
                                        <option value="15" {"selected" if sales_days == 15 else ""}>Últimos 15 dias</option>
                                        <option value="30" {"selected" if sales_days == 30 else ""}>Últimos 30 dias</option>
                                        <option value="60" {"selected" if sales_days == 60 else ""}>Últimos 60 dias</option>
                                    </select>
                                </div>
                                <div class="col-md-3">
                                    <label class="form-label fw-bold">🏪 Plataforma:</label>
                                    <select name="sales_platform" class="form-select" onchange="this.form.submit()">
                                        <option value="all" {"selected" if sales_platform == "all" else ""}>🌐 Todas as Plataformas</option>
                                        <option value="shopee" {"selected" if sales_platform == "shopee" else ""}>🟠 Shopee</option>
                                        <option value="mercadolivre" {"selected" if sales_platform == "mercadolivre" else ""}>🟡 Mercado Livre (Em breve)</option>
                                    </select>
                                </div>
                                <div class="col-md-3">
                                    <label class="form-label fw-bold">📌 Status do Pedido:</label>
                                    <select name="sales_status" class="form-select" onchange="this.form.submit()">
                                        <option value="all" {"selected" if sales_status == "all" else ""}>Todos os Status</option>
                                        <option value="pending" {"selected" if sales_status == "pending" else ""}>⏳ Apenas Pendentes</option>
                                        <option value="completed" {"selected" if sales_status == "completed" else ""}>✅ Apenas Concluídos</option>
                                        <option value="cancelled" {"selected" if sales_status == "cancelled" else ""}>❌ Apenas Cancelados</option>
                                    </select>
                                </div>
                                <div class="col-md-3">
                                    <button type="submit" class="btn btn-success w-100 fw-bold">🔄 Atualizar Relatório</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <!-- Cards Executivos de Vendas e Comissões -->
                    <div class="row g-3 mb-4">
                        <div class="col-md-3">
                            <div class="card text-center shadow-sm border-primary h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">🛍️ Total de Pedidos ({sales_days}d)</h6>
                                    <h2 class="fw-bold text-primary mb-0">{sales_data['total_orders']}</h2>
                                    <small class="text-muted">
                                        ✅ {sales_data['completed_orders']} concl. | ⏳ {sales_data['pending_orders']} pend. | ❌ {sales_data['cancelled_orders']} canc.
                                    </small>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center shadow-sm border-info h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">🛒 Volume Vendido (GMV Válido)</h6>
                                    <h2 class="fw-bold text-dark mb-0">{format_brl(sales_data['total_gmv'])}</h2>
                                    <small class="text-muted">Soma das compras válidas</small>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center shadow-sm border-warning h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">💵 Comissão Total Estimada</h6>
                                    <h2 class="fw-bold text-warning mb-0">{format_brl(sales_data['total_commission'])}</h2>
                                    <small class="text-muted">Pendentes ({format_brl(sales_data['pending_commission'])}) + Concluídos</small>
                                </div>
                            </div>
                        </div>
                        <div class="col-md-3">
                            <div class="card text-center shadow-sm border-success h-100">
                                <div class="card-body">
                                    <h6 class="text-muted mb-1">✅ Comissão Concluída / Aprovada</h6>
                                    <h2 class="fw-bold text-success mb-0">{format_brl(sales_data['completed_commission'])}</h2>
                                    <small class="text-muted">Pedidos já finalizados na Shopee</small>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Tabela de Pedidos -->
                    <div class="card shadow-sm">
                        <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
                            <span>📊 Detalhamento de Pedidos Realizados ({len(filtered_orders)} exibidos)</span>
                            <span class="badge bg-secondary">Fonte: Shopee Affiliate API (conversionReport)</span>
                        </div>
                        <div class="card-body p-0">
                            <div class="table-responsive">
                                <table class="table table-striped table-hover align-middle mb-0">
                                    <thead class="table-light">
                                        <tr>
                                            <th>Data / Hora</th>
                                            <th>Plataforma</th>
                                            <th>ID do Pedido</th>
                                            <th>Itens Comprados</th>
                                            <th class="text-center">Qtd</th>
                                            <th>Valor da Compra</th>
                                            <th>Sua Comissão</th>
                                            <th>Status</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {sales_rows_html}
                                    </tbody>
                                </table>
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
                    const resp = await fetch('/validate-video', {{
                        method: 'POST',
                        body: formData
                    }});
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
    return RedirectResponse(url="/?tab=comparador", status_code=303)


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
    return RedirectResponse(
        url=f"/?tab=comparador&comp_days={comp_days}&comp_store={comp_store}",
        status_code=303,
    )


@app.post("/generate-auto-offers")
async def generate_auto_offers(target_date: str = Form(...)):
    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    if not app_id or not app_secret:
        return RedirectResponse(
            url=f"/?selected_date={target_date}&gen_count=0", status_code=303
        )

    count = int(get_config("auto_refill_count") or "15")
    keywords_csv = get_config("auto_keywords") or ""

    api = ShopeeAffiliateAPI(app_id, app_secret)
    added_count = await asyncio.to_thread(
        api.generate_auto_offers_to_queue, count, target_date, keywords_csv
    )
    return RedirectResponse(
        url=f"/?selected_date={target_date}&gen_count={added_count}", status_code=303
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
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{offer_id}")
async def remove_offer(offer_id: int, current_date: str = Form(...)):
    delete_offer(offer_id)
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
        else:
            aff_link = api.generate_short_link(full_url) or product_url
            msg = f"🔥 <b>PRODUTO EM DESTAQUE NA SHOPEE</b>\n\n🏪 Loja: <b>Shopee</b>\n\n🚚 <b>Aproveite os cupons de FRETE GRÁTIS!</b>\n\n👉 {aff_link}"
            media_url = video_url_found

        store_name = "Shopee"

    media_type = (
        "video" if ("video_url_found" in locals() and video_url_found) else "image"
    )

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cuiaba_tz = timezone(timedelta(hours=-4))
    now_cuiaba = datetime.now(cuiaba_tz).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        INSERT INTO queue (store, product_url, formatted_message, media_url, media_type, priority, status, target_date, scheduled_at, created_at)
        VALUES (?, ?, ?, ?, ?, 1, 'pending', ?, ?, ?)
        """,
        (
            store_name,
            product_url,
            msg,
            media_url,
            media_type,
            target_date,
            now_cuiaba,
            now_cuiaba,
        ),
    )
    conn.commit()
    conn.close()

    status_param = video_status_param if "video_status_param" in locals() else ""
    return RedirectResponse(
        url=f"/?selected_date={target_date}{status_param}", status_code=303
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
    )
    return RedirectResponse(url=f"/?selected_date={target_date}", status_code=303)
