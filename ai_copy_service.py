import json
import logging
import os
import sqlite3
import time
import re
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from database import DB_NAME, get_config, update_config

load_dotenv()

logger = logging.getLogger(__name__)
CUIABA_TZ = timezone(timedelta(hours=-4))
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_FALLBACK_MODELS = []


def ensure_ai_configs() -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    defaults = {
        "ai_copy_enabled": "0",
        "ai_copy_style": "urgencia_comparativo",
        "ai_copy_custom_prompt": "",
        "ai_copy_model": "gemini-2.5-flash",
        "ai_available_models_json": "[]",
        "ai_models_updated_at": "Ainda não testado",
    }
    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


ensure_ai_configs()


def get_cached_gemini_models() -> list[dict]:
    try:
        raw = get_config("ai_available_models_json") or "[]"
        data = json.loads(raw)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return [
        {"id": m, "status": "unknown", "label": f"{m} (não testado)"}
        for m in DEFAULT_FALLBACK_MODELS
    ]


def _ping_gemini_model(api_key: str, model_id: str) -> tuple[str, str]:
    """Faz um ping mínimo no modelo para confirmar se está respondendo 200 OK."""
    url = f"{GEMINI_BASE_URL}/models/{model_id}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": "Responda apenas OK"}]}],
        "generationConfig": {"maxOutputTokens": 5, "temperature": 0.0},
    }
    try:
        resp = requests.post(url, json=payload, timeout=12)
        if resp.status_code == 200:
            return "ok", "✅ Funcionando"
        if resp.status_code == 429:
            return "rate_limit", "⏳ Cota/Rate Limit (429)"
        if resp.status_code == 404:
            return "not_found", "❌ Indisponível (404)"
        return "error", f"❌ Erro HTTP {resp.status_code}"
    except Exception as e:
        return "error", f"❌ Falha: {str(e)[:40]}"


def discover_and_test_gemini_models() -> dict:
    """
    Lista os modelos disponíveis na API do Gemini, testa os principais candidatos,
    salva o resultado no banco e seleciona automaticamente um modelo funcional.
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {
            "ok": False,
            "error": "GEMINI_API_KEY não configurada no .env",
            "models": [],
        }

    candidate_ids: list[str] = []
    ignore_tokens = (
        "embedding",
        "aqa",
        "imagen",
        "veo",
        "tts",
        "audio",
        "robotics",
        "computer-use",
        "deep-research",
        "vision",
    )

    try:
        resp = requests.get(f"{GEMINI_BASE_URL}/models?key={api_key}", timeout=15)
        if resp.status_code == 200:
            for m in resp.json().get("models") or []:
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                name = str(m.get("name") or "").replace("models/", "").strip()
                low = name.lower()
                if not low.startswith("gemini"):
                    continue
                if any(tok in low for tok in ignore_tokens):
                    continue
                candidate_ids.append(name)
    except Exception as e:
        logger.warning(f"⚠️ Erro ao listar modelos Gemini: {e}")

    if not candidate_ids:
        candidate_ids = list(DEFAULT_FALLBACK_MODELS)

    # Prioriza modelos flash/lite (mais rápidos e maior cota gratuita)
    def _sort_key(mid: str):
        low = mid.lower()
        is_flash = 0 if "flash" in low else 1
        is_exp = 1 if ("exp" in low or "preview" in low) else 0
        return (is_flash, is_exp, low)

    candidate_ids = sorted(set(candidate_ids), key=_sort_key)[:10]

    tested: list[dict] = []
    working_ids: list[str] = []
    for mid in candidate_ids:
        status, status_text = _ping_gemini_model(api_key, mid)
        tested.append(
            {
                "id": mid,
                "status": status,
                "label": f"{mid} — {status_text}",
            }
        )
        if status == "ok":
            working_ids.append(mid)
        time.sleep(0.35)

    update_config("ai_available_models_json", json.dumps(tested, ensure_ascii=False))
    update_config(
        "ai_models_updated_at", datetime.now(CUIABA_TZ).strftime("%d/%m/%Y %H:%M")
    )

    current_model = (get_config("ai_copy_model") or "").strip()
    selected_model = current_model

    # Se o modelo atual não está funcionando, troca automaticamente pelo 1º funcional
    if working_ids and current_model not in working_ids:
        selected_model = working_ids[0]
        update_config("ai_copy_model", selected_model)

    return {
        "ok": len(working_ids) > 0,
        "working_count": len(working_ids),
        "total_tested": len(tested),
        "selected_model": selected_model,
        "models": tested,
    }


def _call_gemini_text(
    api_key: str, model_id: str, prompt: str
) -> tuple[str | None, int]:
    url = f"{GEMINI_BASE_URL}/models/{model_id}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 90},
    }
    try:
        resp = requests.post(url, json=payload, timeout=25)
        if resp.status_code != 200:
            logger.warning(
                f"⚠️ Gemini [{model_id}] retornou HTTP {resp.status_code}: {resp.text[:180]}"
            )
            return None, resp.status_code
        data = resp.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get(
            "parts"
        ) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None, 200
    except Exception as e:
        logger.warning(f"⚠️ Erro na chamada Gemini [{model_id}]: {e}")
        return None, 0


def generate_ai_hook(
    title: str,
    current_price: float | None = None,
    original_price: float | None = None,
    discount_pct: str | int | None = None,
    store: str = "Shopee",
    force: bool = False,
    max_price: float | None = None,
    is_starting_price: bool = False,
) -> str:
    if not force and get_config("ai_copy_enabled") != "1":
        return ""

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return ""

    style = get_config("ai_copy_style") or "urgencia_comparativo"
    custom_prompt = (get_config("ai_copy_custom_prompt") or "").strip()
    primary_model = (get_config("ai_copy_model") or "").strip()
    if not primary_model:
        return ""

    style_map = {
        "urgencia_comparativo": "Use gatilho de urgência e destaque o desconto/custo-benefício.",
        "calculo_unitario": "Se for kit/pack, destaque o valor por unidade ou economia real.",
        "achadinho_amigavel": "Use tom amigável de achadinho VIP que acabou de aparecer.",
    }
    style_hint = style_map.get(style, style_map["urgencia_comparativo"])

    try:
        cur_p = (
            float(str(current_price).replace("R$", "").replace(",", ".").strip())
            if current_price not in (None, "")
            else None
        )
    except (ValueError, TypeError):
        cur_p = None

    try:
        max_p = (
            float(str(max_price).replace("R$", "").replace(",", ".").strip())
            if max_price not in (None, "")
            else None
        )
    except (ValueError, TypeError):
        max_p = None

    price_info = f"R$ {cur_p:.2f}" if cur_p is not None else "preço promocional"
    if is_starting_price and cur_p is not None:
        price_info = f"a partir de {price_info}"
    if max_p is not None and (cur_p is None or max_p > cur_p):
        price_info += f" (até R$ {max_p:.2f})"
    disc_info = f"{discount_pct}% OFF" if discount_pct else ""

    prompt = (
        f"Crie UMA única frase curta e chamativa (máx 110 caracteres) em português do Brasil "
        f"com 1 emoji no início para vender este produto da {store} no WhatsApp.\n"
        f"Produto: {title}\nPreço: {price_info} {disc_info}\n"
        f"Estilo: {style_hint}\n"
        f"{f'Instrução extra: {custom_prompt}' if custom_prompt else ''}\n"
        f"Responda APENAS com a frase pronta, sem aspas e sem explicações."
    )

    # 1. Tenta o modelo principal (com 1 retry de 2s em caso de 503 ou 429)
    for attempt in range(2):
        text, status = _call_gemini_text(api_key, primary_model, prompt)
        if text:
            clean = text.replace('"', "").replace("\n", " ").strip()
            return f"<b>{clean}</b>"
        if status in (429, 503) and attempt == 0:
            time.sleep(2.0)
            continue
        break

    # 2. Fallback SOMENTE para outros modelos que passaram no teste (status == "ok")
    ok_backups = [
        m["id"]
        for m in get_cached_gemini_models()
        if m.get("status") == "ok" and m.get("id") != primary_model
    ]
    for backup_id in ok_backups[:2]:
        text, _ = _call_gemini_text(api_key, backup_id, prompt)
        if text:
            clean = text.replace('"', "").replace("\n", " ").strip()
            return f"<b>{clean}</b>"

    return ""


def clean_product_title(title: str, max_len: int = 85) -> str:
    """Limpa espaços extras e corta títulos muito longos sem quebrar palavras."""
    if not title:
        return ""
    clean = re.sub(r"\s+", " ", str(title)).strip()
    if len(clean) <= max_len:
        return clean
    truncated = clean[:max_len].rsplit(" ", 1)[0].strip(" -|,;/")
    return f"{truncated}..." if truncated else f"{clean[:max_len]}..."


def prepend_ai_hook_to_message(
    message: str = "",
    title: str = "",
    current_price: float | None = None,
    original_price: float | None = None,
    discount_pct: str | int | None = None,
    store: str = "Shopee",
    force: bool = False,
    max_price: float | None = None,
    is_starting_price: bool = False,
    **kwargs,
) -> str:
    """Gera o gancho com IA e coloca no topo da mensagem formatada, se estiver ativo."""
    msg = (
        message
        or kwargs.get("base_message")
        or kwargs.get("formatted_message")
        or kwargs.get("msg")
        or ""
    )
    if not msg:
        return ""
    if current_price is None and "price" in kwargs:
        current_price = kwargs.get("price")
    if discount_pct is None and "discount" in kwargs:
        discount_pct = kwargs.get("discount")

    hook = generate_ai_hook(
        title=title,
        current_price=current_price,
        original_price=original_price,
        discount_pct=discount_pct,
        store=store,
        force=force,
        max_price=max_price,
        is_starting_price=is_starting_price,
    )
    if not hook:
        return msg
    return f"{hook}\n\n{msg}"
