from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
import sqlite3
import os
import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from database import DB_NAME, get_config, update_config, add_to_queue, delete_offer
import asyncio
import ml_offer
from shopee_service import ShopeeAffiliateAPI

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
    """Garante que a coluna media_type exista na tabela queue do SQLite."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE queue ADD COLUMN media_type TEXT DEFAULT 'image'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


ensure_media_type_column()


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
                    "message": "⚠️ Nenhum vídeo (.mp4) foi detectado antes do bloqueio/login (ou o anúncio não possui vídeo). Você ainda pode colar o link direto do vídeo no campo ao lado ou salvar apenas com foto.",
                }
            )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"❌ Erro ao validar vídeo: {e}"}
        )


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    selected_date: str = Query(None),
    video_msg: str = Query(None),
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
        "SELECT id, store, product_url, formatted_message, media_url, status, priority, target_date, created_at, COALESCE(media_type, 'image') FROM queue WHERE target_date = ? ORDER BY priority DESC, id DESC",
        (current_date,),
    )
    queue_items = cursor.fetchall()

    cursor.execute(
        "SELECT id, store, product_url, target_date FROM queue WHERE target_date != ? GROUP BY product_url ORDER BY id DESC",
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
        alert_html = '<div class="alert alert-success">🎬 Oferta salva com <b>VÍDEO (.mp4)</b> com sucesso! Clique em "👁️ Ver" na tabela para assistir.</div>'
    elif video_msg == "fallback":
        alert_html = '<div class="alert alert-warning">⚠️ Não foi possível extrair o vídeo automaticamente; a oferta foi salva usando a <b>Imagem</b> padrão da API.</div>'

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Portal de Automação - Ofertas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
        <div class="container py-5">
            <h1 class="mb-4">🚀 Painel de Controle de Ofertas (Cuiabá MT -04:00)</h1>
            {alert_html}

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

            <div class="card mb-4 shadow-sm">
                <div class="card-header bg-primary text-white">⚙️ Parâmetros de Envio & Auto-Abastecimento</div>
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
                            <label class="form-label">Palavras-Chave (Mais Vendidos - Opcional)</label>
                            <input type="text" name="auto_keywords" value="{configs.get('auto_keywords', '')}" placeholder="Ex: cozinha, ferramentas, fone" class="form-control">
                        </div>
                        <div class="col-md-8 d-flex align-items-center">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" name="auto_refill_enabled" value="1" id="autoRefillSwitch" {"checked" if configs.get('auto_refill_enabled', '1') == '1' else ""}>
                                <label class="form-check-label fw-bold" for="autoRefillSwitch">
                                    🔄 Auto-Abastecer Fila quando esvaziar (Mescla TOP_PERFORMING + Mais Vendidos automaticamente)
                                </label>
                            </div>
                        </div>
                        <div class="col-md-4 d-flex align-items-end">
                            <button type="submit" class="btn btn-success w-100">Salvar Parâmetros</button>
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
                <div class="card-header bg-success text-white">🤖 Gerar Lista Automática da Shopee (Mescla TOP_PERFORMING + Mais Vendidos)</div>
                <div class="card-body">
                    <form action="/generate-auto-offers" method="POST" class="row g-3">
                        <input type="hidden" name="target_date" value="{current_date}">
                        <div class="col-md-12">
                            <button type="submit" class="btn btn-success w-100">🎲 Gerar {configs.get('auto_refill_count', '15')} Ofertas (TOP_PERFORMING + Mais Vendidos) para {current_date}</button>
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
        ) = item
        badge_status = (
            f"<span class='badge bg-warning'>Pendente</span>"
            if status == "pending"
            else f"<span class='badge bg-success'>Enviado</span>"
        )
        badge_prio = (
            f"<span class='badge bg-danger'>Manual</span>"
            if priority == 1
            else f"<span class='badge bg-secondary'>Auto</span>"
        )
        badge_media = (
            " <span class='badge bg-info text-dark'>🎬 Vídeo</span>"
            if media_type == "video"
            else ""
        )

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
    added_count = api.generate_auto_offers_to_queue(
        count=count, target_date=target_date, keywords_csv=keywords_csv
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
    auto_keywords: str = Form(""),
    auto_refill_enabled: str = Form(None),
):
    update_config("start_time", start_time)
    update_config("end_time", end_time)
    update_config("interval_minutes", interval_minutes)
    update_config("auto_refill_count", auto_refill_count)
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
        if store.lower() == "shopee" and product_url:
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
