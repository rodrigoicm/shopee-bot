import json
import os
import re
from urllib.parse import urlparse, parse_qs, urlencode, urljoin, urldefrag

import requests
from dotenv import load_dotenv

from ml_social import fetch_social_product, SocialPageBlocked  # noqa: F401

load_dotenv()

CREATE_LINK_URL = (
    "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink"
)
REFERER = "https://www.mercadolivre.com.br/afiliados/linkbuilder"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# O que vale a pena manter ao limpar o link (o resto é rastreio/recomendação)
KEEP_QUERY = ("pdp_filters", "searchVariation")
KEEP_FRAGMENT = ("wid", "searchVariation")


class AffiliateSessionExpired(Exception):
    """Cookies/CSRF do afiliado expiraram: faça login de novo e atualize o .env."""


# ---------------------------------------------------------------------------
# 1. Expandir link (curto, de outros grupos, etc.)
# ---------------------------------------------------------------------------
def _unwrap_verification(url: str) -> str | None:
    """O ML às vezes redireciona para /gz/account-verification?go=<url real>."""
    parsed = urlparse(url)
    if "account-verification" in parsed.path:
        go = (parse_qs(parsed.query).get("go") or [None])[0]
        if go:
            return go + (("#" + parsed.fragment) if parsed.fragment else "")
    return None


def _has_product_id(url: str) -> bool:
    p = urlparse(url)
    pdp = " ".join(parse_qs(p.query).get("pdp_filters", []))
    return bool(re.search(r"MLBU?-?\d+", f"{p.path} {p.fragment} {pdp}", re.I))


def expand_url(url: str, max_hops: int = 8) -> str:
    """Segue os redirecionamentos 1 a 1 (sem baixar a página) até achar a URL do produto."""
    current = url.strip()
    for _ in range(max_hops):
        unwrapped = _unwrap_verification(current)
        if unwrapped:
            return unwrapped

        if _has_product_id(current):
            return current

        base, frag = urldefrag(current)
        try:
            r = requests.get(
                base,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=15,
                stream=True,
            )
            location = r.headers.get("Location")
            r.close()
        except Exception as e:
            print(f"   ⚠️ Erro ao expandir {base}: {e}")
            return current

        if not location:
            return current

        nxt = urljoin(base, location)
        if "#" not in nxt and frag:
            nxt += "#" + frag
        current = nxt
    return current


# ---------------------------------------------------------------------------
# 2. Limpar link (remove rastreio e parâmetros de outros afiliados)
# ---------------------------------------------------------------------------
def clean_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    frag = parse_qs(parsed.fragment)

    query = urlencode({k: qs[k][0] for k in KEEP_QUERY if k in qs})
    fragment = urlencode({k: frag[k][0] for k in KEEP_FRAGMENT if k in frag})
    return parsed._replace(query=query, fragment=fragment).geturl()


# ---------------------------------------------------------------------------
# 3. Converter em link de afiliado (endpoint interno do linkbuilder)
# ---------------------------------------------------------------------------
def _collect_urls(obj, found: list):
    if isinstance(obj, str):
        if obj.startswith("http"):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_urls(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, found)


def _extract_link(data, original: str) -> str:
    urls = data.get("urls") if isinstance(data, dict) else None
    if urls and isinstance(urls, list) and isinstance(urls[0], dict):
        first = urls[0]
        if first.get("error_code"):
            raise ValueError(
                f"O ML recusou a URL ({first.get('message')}, "
                f"error_code={first.get('error_code')}): {first.get('origin_url')}"
            )
        if first.get("short_url"):
            return first["short_url"]

    found: list = []
    _collect_urls(data, found)
    candidates = [u for u in found if u.split("#")[0] != original.split("#")[0]]

    for u in candidates:
        if "meli.la" in u or "/sec/" in u:
            return u
    if candidates:
        return candidates[0]
    raise ValueError(f"Não encontrei o link de afiliado na resposta: {str(data)[:300]}")


def create_affiliate_link_full(
    product_url: str, tag: str | None = None, debug: bool = False
) -> dict:
    """Cria o link de afiliado e devolve {'short_url', 'long_url', 'text'}."""
    cookie = os.environ.get("ML_AFFILIATE_COOKIE")
    csrf = os.environ.get("ML_AFFILIATE_CSRF")
    tag = tag or os.environ.get("ML_AFFILIATE_TAG")
    apikey = os.environ.get("ML_AFFILIATE_APIKEY", "mude-me")

    if not (cookie and csrf and tag):
        raise RuntimeError(
            "Defina ML_AFFILIATE_COOKIE, ML_AFFILIATE_CSRF e ML_AFFILIATE_TAG no .env"
        )

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "apikey": apikey,
        "content-type": "application/json",
        "origin": "https://www.mercadolivre.com.br",
        "referer": REFERER,
        "user-agent": USER_AGENT,
        "x-csrf-token": csrf,
        "cookie": cookie,
    }

    resp = requests.post(
        CREATE_LINK_URL,
        headers=headers,
        json={"urls": [product_url], "tag": tag},
        timeout=20,
    )

    if debug:
        print(f"   createLink status={resp.status_code} body={resp.text[:500]}")

    if resp.status_code in (401, 403):
        raise AffiliateSessionExpired(
            f"Sessão do afiliado recusada (HTTP {resp.status_code}). Atualize cookie/CSRF no .env."
        )
    resp.raise_for_status()

    data = resp.json()
    short_url = _extract_link(
        data, product_url
    )  # valida e levanta erro claro se o ML recusar

    first = {}
    if isinstance(data, dict) and isinstance(data.get("urls"), list) and data["urls"]:
        first = data["urls"][0] if isinstance(data["urls"][0], dict) else {}

    return {
        "short_url": short_url,
        "long_url": first.get("long_url"),
        "text": first.get("text"),
    }


def create_affiliate_link(
    product_url: str, tag: str | None = None, debug: bool = False
) -> str:
    return create_affiliate_link_full(product_url, tag=tag, debug=debug)["short_url"]


# ---------------------------------------------------------------------------
# Fluxo completo
# ---------------------------------------------------------------------------
def ensure_product_url(url: str):
    """Garante que a URL aponta para um produto (e não para perfil/vitrine de afiliado)."""
    if not _has_product_id(url):
        raise ValueError(
            "O link não aponta para um produto (provavelmente uma página de perfil/vitrine "
            f"de afiliado, ex.: /social/...): {url}"
        )


def resolve_product_url(url: str, debug: bool = False):
    """Expande o link e devolve (expandido, url_do_produto, info).

    Para links de outros afiliados (que abrem /social/<perfil>), lê o produto
    compartilhado dentro da própria página. Nos demais casos, info = None.
    """
    expanded = expand_url(url)
    info = None
    if "/social/" in urlparse(expanded).path:
        info = fetch_social_product(expanded, debug=debug)
        product_url = info["product_url"]
    else:
        product_url = expanded
    return expanded, product_url, info


def process_link(url: str, debug: bool = False) -> dict:
    _, product_url, info = resolve_product_url(url, debug=debug)
    cleaned = clean_url(product_url)
    ensure_product_url(cleaned)
    affiliate = create_affiliate_link(cleaned, debug=debug)
    return {"product_url": cleaned, "affiliate_url": affiliate, "info": info}


if __name__ == "__main__":
    import sys

    link = sys.argv[1] if len(sys.argv) > 1 else input("🔗 Link: ").strip()
    try:
        expanded, product_url, info = resolve_product_url(link, debug=True)
        print(f"\n1) Expandido: {expanded}")
        if info:
            print(
                f"   Produto lido da página: {info.get('title')} ({info.get('itemId')})"
            )
            print(
                f"   Preço: {info.get('price')} | Pix: {info.get('pix_price')} "
                f"| Antes: {info.get('original_price')}"
            )
        cleaned = clean_url(product_url)
        print(f"2) Limpo:     {cleaned}")
        ensure_product_url(cleaned)
        affiliate = create_affiliate_link(cleaned, debug=True)
        print(f"\n3) Afiliado:  {affiliate}")
    except AffiliateSessionExpired as e:
        print(f"\n❌ {e}")
    except Exception as e:
        print(f"\n❌ Erro: {e}")
