"""
Motor Comparador de Ofertas Otimizado (Alta Performance).
Compara por ID único da plataforma (MLB / ASIN / Shopee ID), URL limpa e Similaridade Semântica de Título,
ignorando ganchos da IA no topo da mensagem.
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


def extract_platform_item_id(url: str) -> str:
    """
    Extrai o identificador único do produto na URL (MLB do Mercado Livre, ASIN da Amazon ou ID da Shopee).
    Isso garante que mesmo que a URL mude parâmetros, o produto seja reconhecido como o mesmo!
    """
    if not url:
        return ""
    # 1. Mercado Livre (MLB / MLBU)
    m_ml = (
        re.search(r"/(?:p|up)/(MLBU?\d+)", url, re.IGNORECASE)
        or re.search(r"[?&]wid=(MLB\d+)", url, re.IGNORECASE)
        or re.search(r"(MLBU?-?\d{7,14})", url, re.IGNORECASE)
    )
    if m_ml:
        return m_ml.group(1).replace("-", "").upper()

    # 2. Amazon (ASIN de 10 caracteres)
    m_amz = re.search(
        r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?&#]|$)", url, re.IGNORECASE
    )
    if m_amz:
        return f"AMZ_{m_amz.group(1).upper()}"

    # 3. Shopee (product/shop/item ou -i.shop.item)
    m_shp = re.search(r"(?:/product/\d+/|-i\.\d+\.)(\d+)", url)
    if m_shp:
        return f"SHP_{m_shp.group(1)}"

    return ""


def extract_title_from_formatted_message(formatted_msg: str) -> str:
    """
    Extrai o título limpo do produto a partir da mensagem formatada,
    mesmo quando há um gancho persuasivo da IA na primeira linha (inclusive se o gancho usar 🔥).
    """
    if not formatted_msg:
        return "Sem título"

    # Se houver blocos separados por linha em branco e o 2º bloco começar com 🔥 <b>,
    # o 1º bloco é SEMPRE o gancho da IA e o 2º bloco é SEMPRE o título real do produto!
    blocks = [b.strip() for b in formatted_msg.strip().split("\n\n") if b.strip()]
    if len(blocks) >= 2:
        second_block_first_line = blocks[1].splitlines()[0].strip()
        second_no_tags = re.sub(r"<[^>]+>", "", second_block_first_line).strip()
        if (
            second_block_first_line.startswith("🔥")
            and "off" not in second_no_tags.lower()[:25]
        ):
            cleaned_b2 = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨\s]+", "", second_no_tags).strip()
            if cleaned_b2:
                return cleaned_b2

    lines = [ln.strip() for ln in formatted_msg.strip().splitlines() if ln.strip()]
    if not lines:
        return "Sem título"

    # Procura todas as linhas que começam com 🔥 (que não sejam "% OFF")
    fire_candidates = []
    for ln in lines:
        no_tags = re.sub(r"<[^>]+>", "", ln).strip()
        if ln.startswith("🔥") and "off" not in no_tags.lower()[:25]:
            cleaned = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨\s]+", "", no_tags).strip()
            if cleaned:
                fire_candidates.append(cleaned)

    # Se houver 2 linhas com 🔥 (ex: 1ª foi gancho da IA com 🔥 e a 2ª é o título real), pega a 2ª!
    if len(fire_candidates) >= 2:
        return fire_candidates[1]
    if len(fire_candidates) == 1:
        return fire_candidates[0]

    first_no_tags = re.sub(r"<[^>]+>", "", lines[0]).strip()
    if len(lines) > 1 and re.match(r"^[🚨😱💬⚡🎯💥🤩👀🏃]", first_no_tags):
        second_no_tags = re.sub(r"<[^>]+>", "", lines[1]).strip()
        cleaned_second = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨\s]+", "", second_no_tags).strip()
        if cleaned_second:
            return cleaned_second

    cleaned = re.sub(r"^[🔥🏷️🎟️⚡💥🛒✨🚨😱💬🎯\s]+", "", first_no_tags).strip()
    return cleaned or "Produto sem título"


def extract_price_from_formatted_message(formatted_msg: str) -> float | None:
    if not formatted_msg:
        return None
    m_por = re.search(
        r"por\s*(?:<b>)?R\$\s*([\d.]+,\d{2})", formatted_msg, re.IGNORECASE
    )
    if m_por:
        try:
            return float(m_por.group(1).replace(".", "").replace(",", "."))
        except Exception:
            pass

    m_any = re.search(r"R\$\s*([\d.]+,\d{2})", formatted_msg)
    if m_any:
        try:
            return float(m_any.group(1).replace(".", "").replace(",", "."))
        except Exception:
            pass
    return None


def normalize_and_tokenize(title: str) -> tuple[str, list[str], set[str]]:
    if not title:
        return "", [], set()

    nfkd = unicodedata.normalize("NFKD", title)
    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()

    clean_str = re.sub(r"[^a-z0-9\s]", " ", no_accent)
    clean_str = re.sub(
        r"\b\d+(?:ml|l|g|kg|w|v|cm|mm|m|gb|tb|pol|un|pcs|pecas|x)\b", " ", clean_str
    )
    extra_noise = {
        "preto",
        "preta",
        "branco",
        "branca",
        "azul",
        "rosa",
        "vermelho",
        "vermelha",
        "cinza",
        "bege",
        "verde",
        "amarelo",
        "amarela",
        "roxo",
        "roxa",
        "marrom",
        "inox",
        "dourado",
        "prata",
        "bivolt",
        "110v",
        "220v",
        "127v",
    }
    raw_tokens = [t for t in clean_str.split() if t not in extra_noise]

    unique_ordered = []
    seen = set()
    for t in raw_tokens:
        if len(t) <= 2 and not any(ch.isdigit() for ch in t):
            continue
        if t in STOPWORDS_PT:
            continue
        if t.isdigit() and int(t) < 50:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        if t not in seen:
            seen.add(t)
            unique_ordered.append(t)

    norm_joined = " ".join(unique_ordered)
    return norm_joined, unique_ordered, seen


def _build_token_profile(title: str) -> dict:
    norm_str, token_list, token_set = normalize_and_tokenize(title)
    return {
        "title": title,
        "norm": norm_str,
        "list": token_list,
        "set": token_set,
        "head_set": set(token_list[:5]),
        "top3_list": token_list[:3],
        "top3_set": set(token_list[:3]),
        "head_sig": " ".join(token_list[:3]) if token_list else norm_str[:25],
    }


def _fast_similarity_from_profiles(prof_a: dict, prof_b: dict) -> float:
    set_a = prof_a["set"]
    set_b = prof_b["set"]
    if not set_a or not set_b:
        return 0.0

    inter_len = len(set_a & set_b)
    if inter_len == 0:
        return 0.0

    if len(prof_a["top3_list"]) >= 3 and len(prof_b["top3_list"]) >= 3:
        if prof_a["top3_list"] == prof_b["top3_list"]:
            return 88.0
        if prof_a["top3_set"] == prof_b["top3_set"]:
            return 82.0

    union_len = len(set_a | set_b)
    jaccard = inter_len / union_len if union_len else 0.0

    min_len = min(len(set_a), len(set_b))
    overlap = inter_len / min_len if min_len else 0.0

    head_a = prof_a["head_set"]
    head_b = prof_b["head_set"]
    head_inter = len(head_a & head_b)
    head_union = len(head_a | head_b)
    head_score = head_inter / head_union if head_union else 0.0

    partial_score = (head_score * 0.40) + (overlap * 0.30) + (jaccard * 0.15)
    if partial_score < 0.22:
        return round(partial_score * 100.0, 1)

    seq_ratio = SequenceMatcher(None, prof_a["norm"], prof_b["norm"]).ratio()
    final_score = (partial_score + (seq_ratio * 0.15)) * 100.0
    return round(min(100.0, final_score), 1)


def calculate_product_similarity(title_a: str, title_b: str) -> float:
    return _fast_similarity_from_profiles(
        _build_token_profile(title_a), _build_token_profile(title_b)
    )


def load_recent_queue_profiles(days: int = 7) -> list[dict]:
    """
    Carrega do SQLite todos os itens da fila (`queue`) e também do histórico ML (`ml_history`)
    dos últimos N dias JÁ PRÉ-TOKENIZADOS em memória.
    """
    cutoff_date = (datetime.now(CUIABA_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, product_url, formatted_message, target_date FROM queue WHERE target_date >= ?",
        (cutoff_date,),
    )
    rows = cursor.fetchall()

    # Também carrega da tabela ml_history caso algum item do ML tenha sido registrado lá
    ml_hist_rows = []
    try:
        cursor.execute(
            "SELECT item_key, product_url, title, created_at FROM ml_history WHERE substr(created_at, 1, 10) >= ?",
            (cutoff_date,),
        )
        ml_hist_rows = cursor.fetchall()
    except Exception:
        pass

    conn.close()

    profiles = []
    for q_id, p_url, f_msg, t_date in rows:
        title = extract_title_from_formatted_message(f_msg)
        prof = _build_token_profile(title)
        prof["id"] = q_id
        prof["product_url"] = (p_url or "").strip()
        prof["item_key"] = extract_platform_item_id(p_url or "")
        prof["target_date"] = t_date
        profiles.append(prof)

    for item_key, p_url, title, c_at in ml_hist_rows:
        if not title:
            continue
        prof = _build_token_profile(title)
        prof["id"] = f"MLH_{item_key}"
        prof["product_url"] = (p_url or "").strip()
        prof["item_key"] = (item_key or "").strip().upper()
        prof["target_date"] = (c_at or "")[:10]
        profiles.append(prof)

    return profiles


def is_product_recently_used_or_blocked(
    candidate_title: str,
    candidate_url: str = "",
    days: int | None = None,
    threshold: float | None = None,
    extra_accepted_titles: list[str] | None = None,
    preloaded_db_profiles: list[dict] | None = None,
    candidate_item_key: str = "",
) -> tuple[bool, str]:
    """
    Verifica rapidamente se um produto candidato é repetido (por ID MLB/ASIN/Shopee, URL ou Título)
    ou se contém termos bloqueados.
    """
    if days is None:
        days = int(get_config("dedup_history_days") or "7")
    if threshold is None:
        threshold = float(get_config("dedup_similarity_threshold") or "60")

    blocked_csv = get_config("dedup_blocked_terms") or ""
    blocked_terms = [t.strip().lower() for t in blocked_csv.split(",") if t.strip()]

    cand_prof = _build_token_profile(candidate_title)
    raw_cand_lower = (candidate_title or "").lower()

    for b_term in blocked_terms:
        nfkd_b = unicodedata.normalize("NFKD", b_term)
        clean_b = (
            "".join(c for c in nfkd_b if not unicodedata.combining(c)).lower().strip()
        )
        if clean_b and (clean_b in raw_cand_lower or clean_b in cand_prof["norm"]):
            return True, f"Termo bloqueado: '{b_term}'"

    if extra_accepted_titles:
        for prev_title in extra_accepted_titles:
            sim_batch = _fast_similarity_from_profiles(
                cand_prof, _build_token_profile(prev_title)
            )
            if sim_batch >= threshold:
                return True, f"Duplicado no mesmo lote ({sim_batch}%)"

    db_profiles = (
        preloaded_db_profiles
        if preloaded_db_profiles is not None
        else load_recent_queue_profiles(days=days)
    )
    cand_url_clean = (candidate_url or "").split("?")[0].split("#")[0].strip()
    cand_key = (
        (candidate_item_key or extract_platform_item_id(candidate_url)).strip().upper()
    )

    for existing_prof in db_profiles:
        # 1. Checagem exata pelo ID da plataforma (ex: MLB23037572, AMZ_B09K2Y4BFC)
        existing_key = existing_prof.get("item_key") or ""
        if cand_key and existing_key and cand_key == existing_key:
            return (
                True,
                f"Mesmo código ({cand_key}) já usado em {existing_prof['target_date']}",
            )

        # 2. Checagem pela URL base sem parâmetros
        existing_url_base = (
            (existing_prof["product_url"] or "").split("?")[0].split("#")[0].strip()
        )
        if cand_url_clean and existing_url_base and cand_url_clean == existing_url_base:
            return True, f"Mesma URL já na fila #{existing_prof['id']}"

        # 3. Checagem semântica pelo título do produto
        sim = _fast_similarity_from_profiles(cand_prof, existing_prof)
        if sim >= threshold:
            return (
                True,
                f"Similar ({sim}%) ao item #{existing_prof['id']} ({existing_prof['target_date']})",
            )

    return False, ""


def analyze_repeated_offers(
    days: int = 14, threshold: float = 60.0, store_filter: str = "all"
) -> dict:

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if days and days > 0:
        cutoff_date = (datetime.now(CUIABA_TZ) - timedelta(days=days)).strftime(
            "%Y-%m-%d"
        )
        cursor.execute(
            """
            SELECT id, store, product_url, formatted_message, media_url, status, priority, target_date, created_at
            FROM queue
            WHERE target_date >= ?
            ORDER BY target_date DESC, id DESC
            """,
            (cutoff_date,),
        )
    else:
        cursor.execute("""
            SELECT id, store, product_url, formatted_message, media_url, status, priority, target_date, created_at
            FROM queue
            ORDER BY target_date DESC, id DESC
            """)
    rows = cursor.fetchall()
    conn.close()

    items = []
    for r in rows:
        q_id, store, prod_url, f_msg, media_url, status, prio, t_date, c_at = r
        if store_filter != "all" and store_filter.lower() not in (store or "").lower():
            continue

        title = extract_title_from_formatted_message(f_msg)
        prof = _build_token_profile(title)
        items.append(
            {
                "id": q_id,
                "store": store,
                "product_url": (prod_url or "").strip(),
                "item_key": extract_platform_item_id(prod_url or ""),
                "title": title,
                "norm_title": prof["norm"],
                "head_signature": prof["head_sig"],
                "profile": prof,
                "media_url": media_url,
                "status": status,
                "priority": prio,
                "target_date": t_date,
                "created_at": c_at,
            }
        )

    clusters = []
    visited = set()
    n_items = len(items)

    for i in range(n_items):
        item_a = items[i]
        if item_a["id"] in visited:
            continue

        prof_a = item_a["profile"]
        url_a = (item_a["product_url"] or "").split("?")[0].split("#")[0]
        key_a = item_a["item_key"]

        group = [
            {k: v for k, v in item_a.items() if k != "profile"} | {"similarity": 100.0}
        ]
        visited.add(item_a["id"])

        for j in range(i + 1, n_items):
            item_b = items[j]
            if item_b["id"] in visited:
                continue

            url_b = (item_b["product_url"] or "").split("?")[0].split("#")[0]
            key_b = item_b["item_key"]

            if (key_a and key_a == key_b) or (url_a and url_a == url_b):
                sim = 100.0
            else:
                sim = _fast_similarity_from_profiles(prof_a, item_b["profile"])

            if sim >= threshold:
                group.append(
                    {k: v for k, v in item_b.items() if k != "profile"}
                    | {"similarity": sim}
                )
                visited.add(item_b["id"])

        if len(group) > 1:
            dates_set = sorted({g["target_date"] for g in group}, reverse=True)
            pending_ids = [g["id"] for g in group if g["status"] == "pending"]
            sent_count = sum(1 for g in group if g["status"] == "sent")

            if sent_count > 0:
                suggested_delete_ids = pending_ids
            else:
                suggested_delete_ids = pending_ids[:-1] if len(pending_ids) > 1 else []

            clusters.append(
                {
                    "canonical_title": item_a["title"],
                    "signature": item_a["head_signature"],
                    "media_url": item_a["media_url"],
                    "total_occurrences": len(group),
                    "sent_count": sent_count,
                    "pending_count": len(pending_ids),
                    "dates": dates_set,
                    "items": group,
                    "suggested_delete_ids": suggested_delete_ids,
                }
            )

    clusters.sort(
        key=lambda c: (c["total_occurrences"], c["pending_count"]), reverse=True
    )

    total_suggested_deletes = sum(len(c["suggested_delete_ids"]) for c in clusters)

    return {
        "total_scanned": n_items,
        "total_clusters": len(clusters),
        "total_duplicated_items": sum(c["total_occurrences"] for c in clusters),
        "total_suggested_deletes": total_suggested_deletes,
        "clusters": clusters,
    }
