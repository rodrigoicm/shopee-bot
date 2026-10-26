"""
Lê os dados do produto no Mercado Livre diretamente do HTML (_n.ctx.r / JSON-LD),
funcionando tanto para páginas /social/<perfil> (links de afiliados) quanto para
páginas diretas de produto (PDP), sem usar a API oficial e sem abrir navegador.
"""

import html as html_lib
import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class SocialPageBlocked(Exception):
    """O ML devolveu uma página de verificação em vez da página do perfil/produto."""


def _brl_str(val: float) -> str:
    return "R$ " + f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _extract_ctx(html: str) -> dict | None:
    m = re.search(r"_n\.ctx\.r\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end() :])
        return obj
    except ValueError:
        return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_featured_polycard(data: dict, shared_ids: list[str]) -> dict | None:
    """Busca primeiro o polycard dentro do componente 'card-featured' ou pelo shared_id."""
    # 1. Procura direto no componente de destaque 'card-featured'
    for comp in data.get("components") or []:
        if isinstance(comp, dict) and comp.get("id") == "card-featured":
            pcs = (
                (comp.get("recommendation_data") or {}).get("recommendation_info") or {}
            ).get("polycards") or []
            if pcs and isinstance(pcs[0], dict):
                return pcs[0]

    # 2. Procura em todos os polycards comparando id, user_product_id e product_id
    all_cards = []
    for node in _walk(data):
        pcs = node.get("polycards")
        if isinstance(pcs, list):
            all_cards.extend(c for c in pcs if isinstance(c, dict))

    for sid in shared_ids:
        for c in all_cards:
            meta = c.get("metadata") or {}
            if sid in (
                meta.get("id"),
                meta.get("user_product_id"),
                meta.get("product_id"),
                meta.get("pid"),
            ):
                return c

    return all_cards[0] if all_cards else None


def _components(card: dict) -> dict:
    out = {}
    for c in card.get("components") or []:
        if isinstance(c, dict) and c.get("type"):
            out[c["type"]] = c.get(c["type"]) or {}
    return out


def _meta_tag(data: dict, tag_type: str) -> str | None:
    for tag in (data.get("header") or {}).get("meta_tags") or []:
        if isinstance(tag, dict) and tag.get("type") == tag_type:
            return tag.get("content")
    return None


def _format_interpolated_text(raw_text: str, values_list: list) -> str:
    """Substitui placeholders como {price_total} ou {0} pelos valores reais do JSON."""
    if not raw_text:
        return ""
    result = raw_text
    for item in values_list or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        val_str = ""
        if item.get("type") == "price" and isinstance(item.get("price"), dict):
            v = item["price"].get("value")
            if v is not None:
                val_str = _brl_str(float(v))
        elif "text" in item:
            val_str = str(item["text"])
        if val_str:
            result = result.replace(f"{{{key}}}", val_str)
    return re.sub(r"\s+", " ", result).strip()


def _info_from_polycard(card: dict) -> dict:
    meta = card.get("metadata") or {}
    comps = _components(card)
    price = comps.get("price") or {}

    current = (price.get("current_price") or {}).get("value")
    previous = (price.get("previous_price") or {}).get("value")
    label = (price.get("discount_label") or {}).get("text") or ""
    is_pix = "pix" in label.lower()

    # Analisa o bloco 'installments' (pode ser "ou R$ X em outros meios" OU "em 10x R$ X sem juros")
    inst = price.get("installments") or {}
    inst_raw_text = inst.get("text") or ""
    inst_values = inst.get("values") or []
    inst_no_interest = inst.get("no_interest")  # True = sem juros, False = com juros

    other_means_price = None
    installments_text = None

    if inst_raw_text:
        formatted_inst = _format_interpolated_text(inst_raw_text, inst_values)
        if "outros meios" in inst_raw_text.lower():
            # É o preço normal fora do Pix ("ou R$ 329,99 em outros meios")
            for v in inst_values:
                if (
                    isinstance(v, dict)
                    and (v.get("price") or {}).get("value") is not None
                ):
                    other_means_price = float(v["price"]["value"])
                    break
        else:
            # É informação de parcelamento real (ex: "em 10x R$ 32,99 sem juros")
            installments_text = formatted_inst
            if (
                inst_no_interest is True
                and "sem juros" not in installments_text.lower()
            ):
                installments_text += " sem juros"
            elif inst_no_interest is False and "juros" not in installments_text.lower():
                installments_text += " com juros"

    pics = (card.get("pictures") or {}).get("pictures") or []
    pic_id = pics[0].get("id") if pics and isinstance(pics[0], dict) else None

    url = meta.get("url")
    product_url = None
    if url:
        clean_u = url.replace("\\u002F", "/").lstrip("/")
        if not clean_u.startswith("http"):
            clean_u = "https://" + clean_u
        product_url = (
            clean_u + (meta.get("url_params") or "") + (meta.get("url_fragments") or "")
        )

    seller = (comps.get("seller") or {}).get("text")
    if seller:
        seller = re.sub(r"\s*\{[^}]*\}", "", seller).strip()

    regular_price = (
        other_means_price if (is_pix and other_means_price is not None) else current
    )
    pix_price = current if is_pix else None

    return {
        "itemId": meta.get("id")
        or meta.get("user_product_id")
        or meta.get("product_id"),
        "title": (comps.get("title") or {}).get("text"),
        "price": regular_price,
        "pix_price": pix_price,
        "original_price": previous,
        "discount_label": label or None,
        "installments_text": installments_text,
        "installments_no_interest": inst_no_interest,
        "thumbnail": (
            f"https://http2.mlstatic.com/D_NQ_NP_{pic_id}-O.jpg" if pic_id else None
        ),
        "seller": seller,
        "product_url": product_url,
    }


def _parse_direct_product_html(html: str, final_url: str = "") -> dict | None:
    """Fallback para quando a página aberta já é a página direta do produto (PDP)."""
    title = None
    image_url = None
    price = None
    pix_price = None
    original_price = None
    discount_label = None
    installments_text = None
    installments_no_interest = None
    item_id = None

    # 1. Tenta JSON-LD oficial presente na página do produto
    ld_matches = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    for ld_raw in ld_matches:
        try:
            ld_data = json.loads(ld_raw.strip())
            if isinstance(ld_data, dict) and ld_data.get("@type") == "Product":
                title = ld_data.get("name")
                img = ld_data.get("image")
                image_url = img[0] if isinstance(img, list) and img else img
                offers = ld_data.get("offers") or {}
                if isinstance(offers, dict) and offers.get("price") is not None:
                    price = float(offers["price"])
                item_id = ld_data.get("sku") or ld_data.get("productID")
                break
        except Exception:
            pass

    # 2. Meta tags OpenGraph
    if not title:
        m_t = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if m_t:
            title = html_lib.unescape(m_t.group(1)).split(" - R$")[0].strip()

    if not image_url:
        m_i = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m_i:
            image_url = m_i.group(1)

    # 3. Inspeciona o _n.ctx.r da página de produto (PDP) para pegar original_price, Pix e parcelas
    ctx = _extract_ctx(html)
    if ctx:
        for node in _walk(ctx):
            # Preço original riscado na PDP
            if original_price is None and isinstance(
                node.get("original_price"), (int, float)
            ):
                original_price = float(node["original_price"])
            if (
                price is None
                and isinstance(node.get("price"), (int, float))
                and node.get("currency_id") == "BRL"
            ):
                price = float(node["price"])

    if not title and price is None:
        return None

    return {
        "itemId": item_id,
        "title": title,
        "price": price,
        "pix_price": pix_price,
        "original_price": original_price,
        "discount_label": discount_label,
        "installments_text": installments_text,
        "installments_no_interest": installments_no_interest,
        "thumbnail": image_url,
        "seller": None,
        "product_url": final_url or None,
    }


def parse_social_html(html: str, final_url: str = "") -> dict | None:
    ctx = _extract_ctx(html)
    data = (((ctx or {}).get("appProps") or {}).get("pageProps") or {}).get(
        "data"
    ) or {}

    shared_ids = [
        s.get("id")
        for s in (data.get("shared_items") or [])
        if isinstance(s, dict) and s.get("id")
    ]
    if not shared_ids:
        m = re.search(r'"shared_item"\s*:\s*\{\s*"id"\s*:\s*"(MLBU?\d+)"', html)
        if m:
            shared_ids = [m.group(1)]

    card = _find_featured_polycard(data, shared_ids)

    if card:
        info = _info_from_polycard(card)
        info["title"] = info.get("title") or _meta_tag(data, "title")
        info["thumbnail"] = info.get("thumbnail") or _meta_tag(data, "image")
        if not info.get("product_url") and shared_ids:
            sid = shared_ids[0]
            if sid.startswith("MLB") and not sid.startswith("MLBU"):
                info["product_url"] = (
                    f"https://produto.mercadolivre.com.br/MLB-{sid[3:]}"
                )
        return info

    # Se não for uma página /social/ com polycard, tenta ler como página direta do produto (PDP)
    return _parse_direct_product_html(html, final_url=final_url)


def fetch_social_product(url: str, debug: bool = False) -> dict:
    """Baixa a página (/social/... ou PDP) e devolve os dados estruturados do produto."""
    cookie = os.environ.get("ML_AFFILIATE_COOKIE")
    headers = {
        "user-agent": USER_AGENT,
        "accept": "text/html,application/xhtml+xml",
        "accept-language": "pt-BR,pt;q=0.9",
    }
    if cookie:
        # Remove aspas simples extras caso estejam salvas no .env
        headers["cookie"] = cookie.strip().strip("'")

    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    if debug:
        print(f"   GET status={r.status_code} final={r.url[:110]} bytes={len(r.text)}")

    if "account-verification" in r.url or "webdevice/config" in r.url:
        raise SocialPageBlocked(
            "O ML pediu verificação em vez de mostrar a página "
            f"(redirecionou para: {r.url[:100]})."
        )

    info = parse_social_html(r.text, final_url=r.url)
    if not info:
        raise ValueError(
            "Não achei os dados do produto nessa página (pode ser um perfil sem produto "
            "ou uma página de verificação do ML)."
        )
    return info


if __name__ == "__main__":
    import sys

    link = (
        sys.argv[1]
        if len(sys.argv) > 1
        else input("🔗 Link /social/ ou arquivo HTML: ").strip()
    )
    if os.path.exists(link):
        with open(link, "r", encoding="utf-8") as f:
            print(json.dumps(parse_social_html(f.read()), ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                fetch_social_product(link, debug=True), ensure_ascii=False, indent=2
            )
        )
