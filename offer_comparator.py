"""
Motor Comparador de Ofertas Otimizado (Alta Performance).
Pré-tokeniza os títulos uma única vez e aplica filtro rápido de interseção,
reduzindo o tempo de análise de ~21.000ms para < 40ms.
"""

import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from database import DB_NAME, get_config

CUIABA_TZ = timezone(timedelta(hours=-4))

STOPWORDS_PT = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "para",
    "com",
    "sem",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "por",
    "um",
    "uma",
    "uns",
    "umas",
    "e",
    "ou",
    "ao",
    "aos",
    "pelo",
    "pela",
    "original",
    "oficial",
    "promocao",
    "oferta",
    "imperdivel",
    "queima",
    "estoque",
    "envio",
    "imediato",
    "rapido",
    "full",
    "brasil",
    "pronta",
    "entrega",
    "barato",
    "lancamento",
    "novo",
    "nova",
    "super",
    "mega",
    "top",
    "melhor",
    "qualidade",
    "premium",
    "profissional",
    "alta",
    "alto",
    "resistente",
    "universal",
    "portatil",
    "cores",
    "cor",
    "sortidas",
    "sortido",
    "varias",
    "escolha",
    "seu",
    "sua",
    "unidades",
    "unidade",
    "pecas",
    "peca",
    "un",
    "pc",
    "pcs",
    "kit",
    "conjunto",
    "combo",
    "par",
    "pares",
    "caixa",
    "pacote",
    "atacado",
    "revenda",
    "brinde",
    "frete",
    "gratis",
    "desconto",
    "cupom",
    "loja",
    "garantia",
    "nf",
    "nota",
    "fiscal",
    "masculino",
    "feminino",
    "unissex",
    "adulto",
    "infantil",
    "luxo",
}


def ensure_comparator_configs() -> None:
    """Garante os parâmetros padrão do comparador e das categorias Shopee na tabela config."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    defaults = {
        "dedup_history_days": "7",
        "dedup_similarity_threshold": "60",
        "dedup_blocked_terms": "",
        "shopee_general_pct": "30",
        "shopee_target_categories": "Roupas Femininas, Moda Infantil, Sapatos Femininos",
    }
    for k, v in defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


ensure_comparator_configs()


def extract_title_from_formatted_message(formatted_msg: str) -> str:
    """
    Extrai o título limpo do produto a partir da mensagem formatada,
    mesmo quando há um gancho persuasivo da IA na primeira linha.
    """
    if not formatted_msg:
        return "Sem título"

    lines = [ln.strip() for ln in formatted_msg.strip().splitlines() if ln.strip()]
    if not lines:
        return "Sem título"

    # 1. Procura a linha padrão de título de produto (que começa com 🔥 e não é linha de % OFF)
    for ln in lines:
        no_tags = re.sub(r"<[^>]+>", "", ln).strip()
        if ln.startswith("🔥") and "off" not in no_tags.lower()[:25]:
            cleaned = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨\s]+", "", no_tags).strip()
            if cleaned:
                return cleaned

    # 2. Fallback: se a 1ª linha for um gancho curto da IA (🚨, 😱, 💬, ⚡, 🎯, 💥) e houver outra linha abaixo
    first_no_tags = re.sub(r"<[^>]+>", "", lines[0]).strip()
    if len(lines) > 1 and re.match(r"^[🚨😱💬⚡🎯💥🤩👀🏃]", first_no_tags):
        second_no_tags = re.sub(r"<[^>]+>", "", lines[1]).strip()
        cleaned_second = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨\s]+", "", second_no_tags).strip()
        if cleaned_second:
            return cleaned_second

    # 3. Fallback padrão (1ª linha)
    cleaned = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨🚨😱💬🎯\s]+", "", first_no_tags).strip()
    return cleaned or "Produto sem título"


def extract_price_from_formatted_message(formatted_msg: str) -> float | None:
    """Extrai o preço promocional principal de uma mensagem já formatada para uso na IA."""
    if not formatted_msg:
        return None
    # Procura por "por R$ X" ou "Preço: R$ X" ou "A partir de R$ X"
    m_por = re.search(
        r"por\s*(?:<b>)?R\$\s*([\d.]+,\d{2})", formatted_msg, re.IGNORECASE
    )
    if m_por:
        try:
            return float(m_por.group(1).replace(".", "").replace(",", "."))
        except Exception:
            pass

    m_any = re.search
