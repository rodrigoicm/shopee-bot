"""
Serviço de Copywriting Inteligente via IA (Google Gemini Flash API).
Gera chamadas curtas, persuasivas e únicas para o topo das ofertas (Shopee, ML e Amazon),
calculando valor unitário em kits e destacando a diferença para o preço médio de mercado.
"""

import html
import logging
import os
import re
import socket
import sqlite3
from contextlib import contextmanager
import requests
from dotenv import load_dotenv
from database import DB_NAME, get_config

load_dotenv()

logger = logging.getLogger(__name__)

_ACTIVE_GEMINI_MODEL: str | None = None


@contextmanager
def _force_ipv4_context():
    """
    Força resolução DNS via IPv4 temporariamente durante a chamada ao Google APIs.
    Evita o bug clássico de Read Timeout em redes Windows onde a rota IPv6 para
    generativelanguage.googleapis.com congela após o handshake TLS.
    """
    orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = orig_getaddrinfo


def ensure_ai_configs() -> None:
    """Garante os parâmetros da IA na tabela config do SQLite."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    defaults = {
        "ai_copy_enabled": "0",  # 0 = Desligado por padrão; 1 = Ligado
        "ai_copy_style": "urgencia_comparativo",  # urgencia_comparativo | achadinho_amigavel | calculo_unitario
        "ai_copy_custom_prompt": "",
    }
    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


ensure_ai_configs()


def _discover_available_flash_models(api_key: str) -> list[str]:
    """
    Consulta diretamente na API do Google quais modelos 'flash' estão ativos
    para esta chave de API, ordenando pelos mais rápidos e recentes.
    """
    env_model = os.environ.get("GEMINI_MODEL", "").strip()
    if env_model:
        return [env_model.replace("models/", "")]

    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {
        "x-goog-api-key": api_key,
        "Connection": "close",
    }
    try:
        with _force_ipv4_context():
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            models_data = resp.json().get("models") or []
            flash_models = []
            for m in models_data:
                name = (m.get("name") or "").replace("models/", "")
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                if (
                    "flash" in name.lower()
                    and "tts" not in name.lower()
                    and "image" not in name.lower()
                ):
                    flash_models.append(name)

            # Prioriza modelos flash-lite ou flash padrão mais novos
            flash_models.sort(
                key=lambda x: (
                    0 if "lite" in x else 1,
                    0 if "3." in x or "2.0" in x else 1,
                    x,
                )
            )
            if flash_models:
                logger.info(
                    f"🤖 Modelos Gemini Flash detectados na conta: {flash_models[:4]}"
                )
                return flash_models[:4]
    except Exception as e:
        logger.warning(f"⚠️ Não foi possível listar modelos Gemini dinamicamente: {e}")

    return [
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]


def generate_ai_hook(
    title: str,
    current_price: float | str | None,
    original_price: float | str | None = None,
    discount_pct: int | str | None = None,
    store: str = "Shopee",
    sales: int | str | None = None,
    force: bool = False,
) -> str:
    """
    Consulta a IA para criar uma chamada curta (1 a 2 linhas) para o topo da oferta.
    Retorna string formatada em HTML (ex: '<b>🚨 Corre que baixou muito!...</b>')
    ou '' (vazio) se a IA estiver desativada ou sem chave configurada.
    """
    global _ACTIVE_GEMINI_MODEL

    if not force and get_config("ai_copy_enabled") != "1":
        return ""

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip("'\"")
    if not api_key or not title:
        return ""

    style = get_config("ai_copy_style") or "urgencia_comparativo"
    custom_instructions = (get_config("ai_copy_custom_prompt") or "").strip()

    style_guidelines = {
        "urgencia_comparativo": (
            "Use tom de urgência e oportunidade real ('Corre que tá muito barato', 'Preço despencou', 'Vai acabar rápido'). "
            "Compare com o preço normal de mercado ou destaque a economia real sem inventar números absurdos."
        ),
        "achadinho_amigavel": (
            "Use tom de recomendação entusiasmada de grupo VIP de promoções ('Olha esse achadinho', 'Vale cada centavo', 'Compensa demais')."
        ),
        "calculo_unitario": (
            "Se for um Kit, Pack, Fralda ou conjunto com várias unidades, calcule e destaque quanto sai cada unidade! "
            "Se for produto único, destaque o custo-benefício pelo preço atual."
        ),
    }.get(style, "Use tom de urgência e destaque o custo-benefício.")

    price_info = (
        f"R$ {float(current_price):.2f}".replace(".", ",")
        if current_price
        else "em promoção"
    )
    orig_info = (
        f"R$ {float(original_price):.2f}".replace(".", ",")
        if original_price
        else "não informado"
    )
    disc_info = f"{discount_pct}%" if discount_pct else "não informado"
    sales_info = f"{sales} vendidos" if sales else ""

    prompt = (
        "Você é um copywriter brasileiro de grupos de promoções no WhatsApp.\n"
        "Escreva APENAS 1 frase curta e magnética (máximo 135 caracteres) para o topo desta oferta:\n"
        f"- Produto: {title}\n"
        f"- Loja: {store} | Preço Agora: {price_info} | Preço Antigo: {orig_info} | Desconto: {disc_info} {sales_info}\n"
        f"- Estilo: {style_guidelines}\n"
        "- Se for Kit/Pack com várias unidades, calcule e cite quanto sai cada unidade.\n"
        f"{f'- Extra: {custom_instructions}' if custom_instructions else ''}\n"
        "Regras: Comece com 1 emoji (🚨, 😱, ⚡, 💥). Não repita o nome inteiro do produto. Sem links, sem hashtags e sem aspas."
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 200,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Connection": "close",
    }

    models_to_try = (
        [_ACTIVE_GEMINI_MODEL]
        if _ACTIVE_GEMINI_MODEL
        else _discover_available_flash_models(api_key)
    )

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            with _force_ipv4_context():
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=(5, 12)
                )

            if resp.status_code == 404:
                m_sug = re.search(r"models/(gemini-[a-zA-Z0-9.-]+)", resp.text)
                if m_sug:
                    suggested = m_sug.group(1)
                    if suggested not in models_to_try:
                        models_to_try.append(suggested)
                continue

            if resp.status_code != 200:
                logger.warning(
                    f"⚠️ Aviso API Gemini ({model_name} - HTTP {resp.status_code}): {resp.text[:180]}"
                )
                continue

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                continue

            parts = (candidates[0].get("content") or {}).get("parts") or []
            raw_hook = "".join(
                p.get("text", "") for p in parts if not p.get("thought")
            ).strip()

            raw_hook = raw_hook.strip("\"'").replace("**", "").replace("*", "").strip()
            raw_hook = " ".join(raw_hook.splitlines()).strip()

            if not raw_hook:
                continue

            _ACTIVE_GEMINI_MODEL = model_name

            return (
                f"💬 <i>{html.escape(raw_hook)}</i>"
                if not re.match(r"^[^\w\s]", raw_hook)
                else f"<b>{html.escape(raw_hook)}</b>"
            )
        except Exception as e:
            logger.warning(f"⚠️ Falha ao gerar gancho com IA ({model_name}): {e}")
            continue

    return ""


def prepend_ai_hook_to_message(
    base_message: str,
    title: str,
    current_price: float | str | None,
    original_price: float | str | None = None,
    discount_pct: int | str | None = None,
    store: str = "Shopee",
    sales: int | str | None = None,
    force: bool = False,
) -> str:
    """
    Gera o gancho com IA (se ativo) e o insere no topo da mensagem formatada.
    Se a mensagem já tiver um gancho ou se a IA estiver desligada, retorna a mensagem original intacta.
    """
    hook = generate_ai_hook(
        title=title,
        current_price=current_price,
        original_price=original_price,
        discount_pct=discount_pct,
        store=store,
        sales=sales,
        force=force,
    )
    if not hook:
        return base_message
    return f"{hook}\n\n{base_message}"
