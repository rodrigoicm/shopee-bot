"""
Serviço de Copywriting Inteligente via IA (Google Gemini Flash API) e Limpeza de Títulos.
Gera chamadas curtas de 1 linha (máx 85 chars) sem repetir o preço que já vai abaixo,
e limpa palavras de spam/tradução automática dos títulos dos produtos.
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

_CACHED_MODELS_LIST: list[str] | None = None
_LAST_WORKING_MODEL: str | None = None

# Termos de spam de vendedores (especialmente Shopee internacional) para remover do título
JUNK_TITLE_PATTERNS = [
    r"\b202[2-7]\b",
    r"\b(?:inverno\s+quente|quente\s+novo|novo\s+quente|anti-susto)\b",
    r"\b(?:envio\s+imediato|pronta\s+entrega|envio\s+full|envio\s+r[aá]pido|envio\s+brasil)\b",
    r"\b(?:promo[cç][aã]o|oferta\s+rel[aâ]mpago|queima\s+de\s+estoque|super\s+oferta|lan[cç]amento)\b",
    r"【[^】]*】",
    r"\[[^\]]*\]",
]


def clean_product_title(raw_title: str, max_len: int = 78) -> str:
    """
    Remove termos de spam/SEO ('2025 Inverno Quente Novo anti-Susto', 'Envio Imediato', etc.)
    e corta o título de forma elegante em até `max_len` caracteres.
    """
    if not raw_title:
        return "Produto em Oferta"

    text = html.unescape(str(raw_title))
    for pat in JUNK_TITLE_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)

    # Remove barras/traços soltos no início ou fim e espaços duplicados
    text = re.sub(r"\s+", " ", text).strip(" -–|/.,;:")

    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(" -–|/.,;:") + "..."

    return text or "Produto em Oferta"


@contextmanager
def _force_ipv4_context():
    orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = orig_getaddrinfo


def ensure_ai_configs() -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    defaults = {
        "ai_copy_enabled": "0",
        "ai_copy_style": "urgencia_comparativo",
        "ai_copy_custom_prompt": "",
    }
    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


ensure_ai_configs()


def _get_ordered_flash_models(api_key: str) -> list[str]:
    global _CACHED_MODELS_LIST, _LAST_WORKING_MODEL
    env_model = os.environ.get("GEMINI_MODEL", "").strip()
    if env_model:
        return [env_model.replace("models/", "")]

    if _CACHED_MODELS_LIST:
        if _LAST_WORKING_MODEL and _LAST_WORKING_MODEL in _CACHED_MODELS_LIST:
            return [_LAST_WORKING_MODEL] + [
                m for m in _CACHED_MODELS_LIST if m != _LAST_WORKING_MODEL
            ]
        return _CACHED_MODELS_LIST

    preferred_priority = [
        "gemini-2.5-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.0-flash",
    ]

    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {"x-goog-api-key": api_key, "Connection": "close"}

    try:
        with _force_ipv4_context():
            resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            models_data = resp.json().get("models") or []
            available = set()
            for m in models_data:
                name = (m.get("name") or "").replace("models/", "")
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    if (
                        "flash" in name.lower()
                        and "tts" not in name.lower()
                        and "image" not in name.lower()
                    ):
                        available.add(name)
            final_list = [p for p in preferred_priority if p in available]
            for other in sorted(available):
                if other not in final_list:
                    final_list.append(other)
            if final_list:
                _CACHED_MODELS_LIST = final_list[:6]
                return _CACHED_MODELS_LIST
    except Exception:
        pass

    return preferred_priority[:5]


def _detect_multi_kit_title(title: str) -> list[int]:
    if not title:
        return []
    m = re.search(
        r"\b(?:kit|pack|combo|com|leve)?\s*(\d{1,3}(?:\s*[/|-]\s*\d{1,3})+|\d{1,3}\s+ou\s+\d{1,3})\b",
        title,
        re.IGNORECASE,
    )
    if not m:
        return []
    nums = [int(n) for n in re.findall(r"\d+", m.group(1)) if 1 <= int(n) <= 500]
    return sorted(set(nums)) if len(nums) >= 2 else []


def _calculate_exact_unit_price_in_python(
    title: str, price: float | None
) -> tuple[int | None, str | None]:
    """
    Calcula o preço por unidade no Python com precisão matemática exata
    para impedir que a IA erre contas de divisão de cabeça (ex: Kit 12 por R$ 79,99 = R$ 6,67/un).
    """
    if not title or not price or price <= 0:
        return None, None

    # Se tiver múltiplos kits no título (ex: Kit 5/10), não calcula valor único
    if _detect_multi_kit_title(title):
        return None, None

    patterns = [
        r"\b(?:kit|pack|caixa|cx|combo|fardo|leve|pague)\s*(?:com\s*|c/\s*)?(\d{1,4})\b",
        r"\b(\d{1,4})\s*(?:unidades|unidade|unid\.?|un\.?|peças|pecas|pares|fraldas|tiras|cápsulas|capsulas|sachês|saches|latas|garrafas|pacotes|rolos|folhas)\b",
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            qty = int(m.group(1))
            # Evita confundir ml/gramas/voltagem (ex: 250ml, 110v) com quantidade de itens
            if 2 <= qty <= 600:
                unit_val = float(price) / qty
                if unit_val >= 0.05:
                    return qty, f"R$ {unit_val:.2f}".replace(".", ",")
    return None, None


def generate_ai_hook(
    title: str,
    current_price: float | str | None,
    original_price: float | str | None = None,
    discount_pct: int | str | None = None,
    store: str = "Shopee",
    sales: int | str | None = None,
    force: bool = False,
    max_price: float | str | None = None,
    is_starting_price: bool = False,
) -> str:
    global _LAST_WORKING_MODEL

    if not force and get_config("ai_copy_enabled") != "1":
        return ""

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip("'\"")
    if not api_key or not title:
        return ""

    style = get_config("ai_copy_style") or "urgencia_comparativo"
    custom_instructions = (get_config("ai_copy_custom_prompt") or "").strip()

    has_price = bool(current_price)
    p_val = float(current_price) if has_price else 0.0
    multi_kit_nums = _detect_multi_kit_title(title)
    has_range = bool(
        is_starting_price
        or (max_price and has_price and abs(float(max_price) - p_val) > 0.05)
    )

    # Calcula o preço unitário no Python com 100% de precisão!
    exact_qty, exact_unit_str = (
        _calculate_exact_unit_price_in_python(title, p_val)
        if (has_price and not has_range and not multi_kit_nums)
        else (None, None)
    )

    rules = [
        "- MÁXIMO de 80 caracteres (1 linha rápida para leitura instantânea no celular).",
        "- NÃO repita o nome completo do produto e NÃO repita o preço total (pois o preço total já está logo abaixo).",
    ]

    if exact_qty and exact_unit_str and p_val <= 250.0:
        style_hint = f"Destaque que neste kit com {exact_qty} unidades cada unidade sai por APENAS {exact_unit_str}!"
        rules.append(
            f"- ⚠️ MATEMÁTICA EXATA JÁ CALCULADA: São {exact_qty} unidades e cada unidade sai por EXATAMENTE {exact_unit_str}. "
            f"Se citar valor por unidade, use OBRIGATORIAMENTE {exact_unit_str} (PROIBIDO inventar outro valor!)."
        )
    else:
        style_hint = "Crie 1 frase curtíssima e magnética focada no benefício e urgência da oferta."
        rules.append(
            "- ⚠️ REGRA CRÍTICA: PROIBIDO citar qualquer valor em R$ e PROIBIDO fazer contas de divisão! Foque 100% na qualidade/oportunidade do produto."
        )

    prompt = (
        "Você é um copywriter de grupos VIP de ofertas no WhatsApp.\n"
        "Escreva APENAS 1 chamada curta (máximo 80 caracteres) para o topo desta oferta:\n"
        f"- Produto: {clean_product_title(title, 80)}\n"
        f"- Diretriz: {style_hint}\n"
        + "\n".join(rules)
        + (f"\n- Extra: {custom_instructions}" if custom_instructions else "")
        + "\nComece com 1 emoji (🚨, 😱, ⚡, 💥, 😍). Sem links, sem hashtags e sem aspas."
    )

    payload_fast = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.45,
            "maxOutputTokens": 100,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Connection": "close",
    }
    models_to_try = _get_ordered_flash_models(api_key)

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            with _force_ipv4_context():
                resp = requests.post(
                    url, headers=headers, json=payload_fast, timeout=(4, 8)
                )
            if resp.status_code != 200:
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

            _LAST_WORKING_MODEL = model_name
            return f"<b>{html.escape(raw_hook)}</b>"
        except Exception:
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
    max_price: float | str | None = None,
    is_starting_price: bool = False,
) -> str:
    hook = generate_ai_hook(
        title=title,
        current_price=current_price,
        original_price=original_price,
        discount_pct=discount_pct,
        store=store,
        sales=sales,
        force=force,
        max_price=max_price,
        is_starting_price=is_starting_price,
    )
    if not hook:
        return base_message
    return f"{hook}\n\n{base_message}"
