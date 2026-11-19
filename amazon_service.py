"""
Serviço completo da Amazon Brasil (Amazon Associados).
- Suporte total a amzn.to, a.co e link.amazon
- Detecção de preço 'Programe e Poupe' (Subscribe & Save)
- Extração blindada da imagem principal (#landingImage)
- Formatação Curta Escaneável (5 linhas) e Multi-Variação (M / G / XG / XXG)
"""

import asyncio
import html
import json
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import requests
from dotenv import load_dotenv
from ai_copy_service import clean_product_title, prepend_ai_hook_to_message

try:
    from curl_cffi import requests as cffi_requests

    CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    CFFI_AVAILABLE = False

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_AMAZON_TAG = "alinemoraeshe-20"
DEFAULT_MARKETPLACE_ID = "526970"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AMAZON_URL_PATTERN = (
    r"https?://(?:[a-zA-Z0-9-]+\.)?"
    r"(?:amazon\.com\.br|amazon\.com|amzn\.to|a\.co|link\.amazon(?:\.com\.br)?)/[^\s<>\"'\)]+"
)

SHORTENER_DOMAINS = ("amzn.to", "a.co", "link.amazon")


def get_partner_tag() -> str:
    return (
        (os.environ.get("AMAZON_PARTNER_TAG") or DEFAULT_AMAZON_TAG)
        .strip()
        .strip("'\"")
    )


def get_user_agent() -> str:
    return (
        (os.environ.get("AMAZON_USER_AGENT") or DEFAULT_USER_AGENT).strip().strip("'\"")
    )


def _build_amazon_headers(with_cookie: bool = True) -> dict:
    ua = get_user_agent()
    m_ver = re.search(r"Chrome/(\d+)", ua)
    chrome_ver = m_ver.group(1) if m_ver else "131"

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": f'"Not:A-Brand";v="8", "Chromium";v="{chrome_ver}", "Google Chrome";v="{chrome_ver}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if with_cookie:
        cookie = (os.environ.get("AMAZON_COOKIE") or "").strip().strip("'\"")
        if cookie:
            headers["Cookie"] = cookie
    return headers


def extract_asin(url: str) -> str | None:
    if not url:
        return None
    low = url.lower()
    if (
        any(short_dom in low for short_dom in SHORTENER_DOMAINS)
        and "/dp/" not in low
        and "/gp/" not in low
    ):
        return None

    patterns = [
        r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin|product)/([A-Z0-9]{10})(?:[/?&#]|$)",
        r"[?&]pd_rd_i=([A-Z0-9]{10})(?:[&#]|$)",
        r"/([B][A-Z0-9]{9})(?:[/?&#]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, url, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def resolve_amazon_url(url: str) -> str:
    url = url.strip()
    low = url.lower()
    is_short = any(d in low for d in SHORTENER_DOMAINS)

    if not is_short and extract_asin(url):
        return url

    try:
        if CFFI_AVAILABLE:
            resp = cffi_requests.get(
                url,
                headers=_build_amazon_headers(with_cookie=False),
                allow_redirects=True,
                impersonate="chrome",
                timeout=15,
            )
            final_url = str(resp.url)
        else:
            resp = requests.get(
                url,
                headers=_build_amazon_headers(with_cookie=False),
                allow_redirects=True,
                timeout=15,
            )
            final_url = resp.url

        if any(d in final_url.lower() for d in SHORTENER_DOMAINS) and resp.text:
            m_canonical = re.search(
                r'https?://(?:www\.)?amazon\.com\.br/[^\s"\'<>]+', resp.text
            )
            if m_canonical:
                return html.unescape(m_canonical.group(0))

        return final_url
    except Exception as e:
        logger.warning(f"Erro ao expandir link da Amazon ({url}): {e}")
        return url


def build_tagged_long_url(
    url_or_asin: str, tag: str | None = None
) -> tuple[str, str | None]:
    partner_tag = tag or get_partner_tag()
    asin = extract_asin(url_or_asin)
    link_id = uuid.uuid4().hex

    if asin:
        long_url = (
            f"https://www.amazon.com.br/dp/{asin}"
            f"?pd_rd_i={asin}&linkCode=sl2&tag={partner_tag}&linkId={link_id}&ref_=as_li_ss_tl"
        )
        return long_url, asin

    parsed = urlparse(url_or_asin)
    base_clean = f"https://www.amazon.com.br{parsed.path}"
    long_url = (
        f"{base_clean}?linkCode=sl2&tag={partner_tag}&linkId={link_id}&ref_=as_li_ss_tl"
    )
    return long_url, None


def generate_sitestripe_short_link(long_tagged_url: str, tag: str | None = None) -> str:
    cookie = (os.environ.get("AMAZON_COOKIE") or "").strip().strip("'\"")
    partner_tag = tag or get_partner_tag()
    store_id = (os.environ.get("AMAZON_STORE_ID") or partner_tag).strip().strip("'\"")
    asin = extract_asin(long_tagged_url)

    if not cookie or not store_id:
        return (
            f"https://www.amazon.com.br/dp/{asin}?tag={partner_tag}"
            if asin
            else long_tagged_url
        )

    mkt_id = (os.environ.get("AMAZON_MARKETPLACE_ID") or DEFAULT_MARKETPLACE_ID).strip()

    api_url = (
        "https://www.amazon.com.br/associates/sitestripe/getShortUrl"
        f"?longUrl={quote(long_tagged_url, safe='')}"
        f"&marketplaceId={mkt_id}"
        f"&storeId={store_id}"
    )

    ua = get_user_agent()
    m_ver = re.search(r"Chrome/(\d+)", ua)
    chrome_ver = m_ver.group(1) if m_ver else "131"

    referer_url = (
        f"https://www.amazon.com.br/dp/{asin}" if asin else "https://www.amazon.com.br/"
    )

    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "apikey": "mude-me",
        "priority": "u=1, i",
        "referer": referer_url,
        "sec-ch-ua": f'"Not:A-Brand";v="8", "Chromium";v="{chrome_ver}", "Google Chrome";v="{chrome_ver}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": ua,
        "x-requested-with": "XMLHttpRequest",
        "cookie": cookie,
    }

    try:
        if CFFI_AVAILABLE:
            resp = cffi_requests.get(
                api_url,
                headers=headers,
                impersonate="chrome",
                timeout=12,
            )
        else:
            resp = requests.get(api_url, headers=headers, timeout=12)

        if resp.status_code == 200:
            text_body = resp.text.strip()
            data = resp.json() if text_body.startswith("{") else {}
            short_url = data.get("shortUrl") or data.get("short_url") or data.get("url")
            if not short_url and ("amzn.to" in text_body or "link.amazon" in text_body):
                m_s = re.search(
                    r"https?://(?:amzn\.to|link\.amazon\.[a-z.]+|link\.amazon)/[A-Za-z0-9_-]+",
                    text_body,
                )
                if m_s:
                    short_url = m_s.group(0)

            if short_url and short_url.startswith("http"):
                return short_url.strip()
    except Exception as e:
        logger.warning(f"⚠️ Exceção no SiteStripe: {e}")

    if asin:
        return f"https://www.amazon.com.br/dp/{asin}?tag={partner_tag}"
    return long_tagged_url


def is_short_amazon_link(url: str | None) -> bool:
    if not url:
        return False
    low = url.lower()
    return any(d in low for d in SHORTENER_DOMAINS)


def _parse_brl_float(raw_str: str | None) -> float | None:
    if not raw_str:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2}|\d+,\d{2})", raw_str)
    if not m:
        return None
    val_str = m.group(1)
    if "," in val_str:
        val_str = val_str.replace(".", "").replace(",", ".")
    try:
        return float(val_str)
    except Exception:
        return None


def extract_variant_and_units(title: str, price: float | None = None) -> dict:
    if not title:
        return {
            "size": None,
            "units": None,
            "unit_price": None,
            "unit_price_str": None,
            "label_short": "",
            "summary_line": "",
        }

    m_size = (
        re.search(
            r"\b(?:Tamanho|Tam\.?)\s*(RN|XXXG|XXG|XG|SXG|EG|P|M|G)\b",
            title,
            re.IGNORECASE,
        )
        or re.search(r"\b(RN|XXXG|XXG|XG|SXG)\b", title)
        or re.search(
            r"(?:Fralda|Care|Pampers|Huggies|MamyPoko|Pom\s*Pom|Babysec|Turma).*?\b(P|M|G)\b",
            title,
            re.IGNORECASE,
        )
    )
    size = m_size.group(1).upper() if m_size else None

    units = None
    unit_patterns = [
        r"\b(\d{2,4})\s*(?:unidades|unidade|unid\.?|un\.?|fraldas|tiras|tirinhas|cápsulas|capsulas|sachês|saches|lenços|lencos|peças|pecas)\b",
        r"\b(?:com|c/|kit|pack|leve)\s*(\d{2,4})\b",
    ]
    for pat in unit_patterns:
        m_u = re.search(pat, title, re.IGNORECASE)
        if m_u:
            val_u = int(m_u.group(1))
            if 2 <= val_u <= 2000:
                units = val_u
                break

    unit_price = (
        round(float(price) / units, 2) if (price and units and units > 1) else None
    )
    unit_price_str = f"{unit_price:.2f}".replace(".", ",") if unit_price else None

    if size and units:
        label_short = f"{size} com {units}"
    elif units:
        label_short = f"Com {units} un"
    elif size:
        label_short = f"Tam. {size}"
    else:
        label_short = ""

    if label_short and unit_price_str:
        summary_line = f"{label_short} ({unit_price_str} a unidade)"
    else:
        summary_line = label_short

    return {
        "size": size,
        "units": units,
        "unit_price": unit_price,
        "unit_price_str": unit_price_str,
        "label_short": label_short,
        "summary_line": summary_line,
    }


def scrape_amazon_product_html(html_text: str, asin: str | None = None) -> dict:
    title = None
    regular_price = None
    sns_price = None
    original_price = None
    discount_pct = None
    installments_text = None
    coupon_badge = None
    image_url = None

    # 1. Título (#productTitle ou meta title)
    m_title = re.search(
        r'<span[^>]*id=["\']productTitle["\'][^>]*>(.*?)</span>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if m_title:
        title = html.unescape(re.sub(r"<[^>]+>", "", m_title.group(1))).strip()
    if not title:
        m_meta_t = re.search(
            r'<meta[^>]*name=["\']title["\'][^>]*content=["\']([^"\']+)["\']', html_text
        )
        if m_meta_t:
            raw_t = html.unescape(m_meta_t.group(1))
            title = re.sub(
                r"\s*:\s*Amazon\.com\.br.*$", "", raw_t, flags=re.IGNORECASE
            ).strip()

    # 2. Preço "Programe e Poupe"
    sns_block = re.search(
        r'id=["\'](?:snsAccordionRowMiddle|snsAccordionRow|sns_feature_div|apex_offerDisplay_sns_desktop)["\'](.*?)(?:id=["\']newAccordionRow|$)',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if sns_block:
        sns_chunk = sns_block.group(1)
        m_sns_p = (
            re.search(
                r'id=["\']sns-base-price["\'][^>]*>\s*R\$\s*([\d.,]+)',
                sns_chunk,
                re.IGNORECASE,
            )
            or re.search(
                r'data-csa-c-price-to-pay=["\']([\d.]+)["\']', sns_chunk, re.IGNORECASE
            )
            or re.search(
                r'<span[^>]*class=["\']a-offscreen["\'][^>]*>\s*R\$\s*([\d.,]+)\s*</span>',
                sns_chunk,
                re.IGNORECASE,
            )
        )
        if m_sns_p:
            sns_price = _parse_brl_float(m_sns_p.group(1))

    if sns_price is None and "programe e poupe" in html_text.lower():
        m_sns_alt = re.search(
            r'id=["\']sns-base-price["\'][^>]*>\s*R\$\s*([\d.,]+)',
            html_text,
            flags=re.IGNORECASE,
        )
        if m_sns_alt:
            sns_price = _parse_brl_float(m_sns_alt.group(1))

    # 3. Preço Compra Única
    core_block_match = re.search(
        r'id=["\'](?:newAccordionRow_0|corePriceDisplay_desktop_feature_div|corePrice_feature_div|apex_desktop|buybox)["\'](.*?)(?:<div[^>]*id=["\'](?:averageCustomerReviews|acBadge|climatePledge|zeitgeistBadge)|$)',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    price_search_area = core_block_match.group(1) if core_block_match else html_text

    m_csa_price = re.search(
        r'data-csa-c-price-to-pay=["\']([\d.]+)["\']',
        price_search_area,
        flags=re.IGNORECASE,
    )
    if m_csa_price:
        try:
            regular_price = float(m_csa_price.group(1))
        except Exception:
            pass

    if regular_price is None:
        m_pay = re.search(
            r'class=["\'][^"\']*(?:priceToPay|apexPriceToPay|apex-pricetopay-value)[^"\']*["\'].*?<span[^>]*class=["\']a-offscreen["\'][^>]*>\s*R\$\s*([\d.,]+)\s*</span>',
            price_search_area,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m_pay:
            regular_price = _parse_brl_float(m_pay.group(1))

    if regular_price is None:
        m_vis = re.search(
            r'name=["\']items\[0\.base\]\[customerVisiblePrice\]\[amount\]["\'][^>]*value=["\']([\d.]+)["\']',
            html_text,
            flags=re.IGNORECASE,
        )
        if m_vis:
            try:
                regular_price = float(m_vis.group(1))
            except Exception:
                pass

    if regular_price is None:
        m_wf = re.search(
            r'class=["\']a-price-whole["\'][^>]*>([\d.]+)(?:<span[^>]*>,</span>)?<.*?class=["\']a-price-fraction["\'][^>]*>(\d{2})<',
            price_search_area,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m_wf:
            regular_price = _parse_brl_float(f"{m_wf.group(1)},{m_wf.group(2)}")

    has_sns = sns_price is not None and (
        regular_price is None or sns_price < regular_price
    )
    current_price = sns_price if has_sns else regular_price

    # 4. Preço Antigo Riscado
    m_orig = re.search(
        r'class=["\'][^"\']*basisPrice[^"\']*["\'].*?<span[^>]*class=["\']a-offscreen["\'][^>]*>\s*R\$\s*([\d.,]+)\s*</span>',
        price_search_area,
        flags=re.DOTALL | re.IGNORECASE,
    ) or re.search(
        r'data-a-strike=["\']true["\'].*?<span[^>]*class=["\']a-offscreen["\'][^>]*>\s*R\$\s*([\d.,]+)\s*</span>',
        price_search_area,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if m_orig:
        original_price = _parse_brl_float(m_orig.group(1))

    # 5. Porcentagem de Desconto
    m_disc = re.search(
        r'class=["\'][^"\']*savingsPercentage[^"\']*["\'][^>]*>\s*-?(\d+)%\s*<',
        price_search_area,
        flags=re.IGNORECASE,
    )
    if m_disc:
        discount_pct = int(m_disc.group(1))
    elif original_price and current_price and original_price > current_price:
        discount_pct = round(((original_price - current_price) / original_price) * 100)

    # 6. Parcelamento
    m_inst = re.search(
        r"(Em at[eé]\s+\d+x\s+(?:de\s+)?R\$\s*[\d.,]+\s*(?:sem juros|com juros)?)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m_inst:
        installments_text = " ".join(html.unescape(m_inst.group(1)).split())

    # 7. Cupom de Página na Amazon
    m_cup = re.search(
        r"(?:Aplicar\s+cupom\s+de\s+[\d.,]+%|cupom\s+de\s+R\$\s*[\d.,]+|cupom\s+de\s+\d+%)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m_cup:
        coupon_badge = html.unescape(m_cup.group(0)).strip()

    # 8. Imagem Principal Isolada na Tag Oficial (#landingImage / #imgTagWrapperId)
    m_landing_tag = re.search(
        r'<img[^>]*\bid=["\'](?:landingImage|imgBlkFront|main-image)["\'][^>]*>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m_landing_tag:
        m_wrapper = re.search(
            r'id=["\'](?:imgTagWrapperId|main-image-container)["\'][^>]*>(.*?)</div',
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m_wrapper:
            m_landing_tag = re.search(
                r"<img[^>]+>", m_wrapper.group(1), flags=re.IGNORECASE | re.DOTALL
            )

    if m_landing_tag:
        img_tag_str = m_landing_tag.group(0)
        m_hires = re.search(
            r'data-old-hires=["\'](https://[^"\']+)["\']', img_tag_str, re.IGNORECASE
        )
        if m_hires and m_hires.group(1).strip():
            image_url = html.unescape(m_hires.group(1).strip())

        if not image_url:
            m_dyn = re.search(
                r'data-a-dynamic-image=["\']([^"\']+)["\']', img_tag_str, re.IGNORECASE
            )
            if m_dyn:
                try:
                    dyn_dict = json.loads(html.unescape(m_dyn.group(1)))
                    if isinstance(dyn_dict, dict) and dyn_dict:
                        image_url = max(
                            dyn_dict.keys(),
                            key=lambda k: (
                                (dyn_dict[k][0] * dyn_dict[k][1])
                                if isinstance(dyn_dict[k], list)
                                and len(dyn_dict[k]) >= 2
                                else 0
                            ),
                        )
                except Exception:
                    pass

        if not image_url:
            m_src = re.search(
                r'\bsrc=["\'](https://[^"\']+)["\']', img_tag_str, re.IGNORECASE
            )
            if m_src and "data:image" not in m_src.group(1):
                image_url = html.unescape(m_src.group(1).strip())

    if not image_url:
        m_hi_json = re.search(
            r'"hiRes"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html_text
        ) or re.search(
            r'"large"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html_text
        )
        if m_hi_json:
            image_url = m_hi_json.group(1)

    variant_info = extract_variant_and_units(title or "", current_price)

    return {
        "asin": asin,
        "title": title,
        "current_price": current_price,
        "regular_price": regular_price,
        "sns_price": sns_price,
        "has_sns": has_sns,
        "original_price": original_price,
        "discount_pct": discount_pct,
        "installments_text": installments_text,
        "coupon_badge": coupon_badge,
        "image_url": image_url,
        **variant_info,
    }


def _build_amazon_offer_sync(raw_url: str) -> dict:
    resolved_url = resolve_amazon_url(raw_url)
    long_tagged_url, asin = build_tagged_long_url(resolved_url)
    affiliate_url = generate_sitestripe_short_link(long_tagged_url)

    fetch_target = f"https://www.amazon.com.br/dp/{asin}" if asin else resolved_url
    info = {}
    try:
        if CFFI_AVAILABLE:
            resp = cffi_requests.get(
                fetch_target,
                headers=_build_amazon_headers(with_cookie=True),
                impersonate="chrome",
                timeout=15,
            )
        else:
            resp = requests.get(
                fetch_target,
                headers=_build_amazon_headers(with_cookie=True),
                timeout=15,
            )
        if (
            resp.status_code == 200
            and "api-services-support@amazon.com" not in resp.text
        ):
            info = scrape_amazon_product_html(resp.text, asin=asin)
    except Exception as e:
        logger.warning(f"Aviso ao ler HTML do produto na Amazon ({fetch_target}): {e}")

    return {
        "asin": asin,
        "product_url": fetch_target,
        "affiliate_url": affiliate_url,
        "is_short_link": is_short_amazon_link(affiliate_url),
        "title": info.get("title"),
        "current_price": info.get("current_price"),
        "regular_price": info.get("regular_price"),
        "sns_price": info.get("sns_price"),
        "has_sns": info.get("has_sns", False),
        "original_price": info.get("original_price"),
        "discount_pct": info.get("discount_pct"),
        "installments_text": info.get("installments_text"),
        "coupon_badge": info.get("coupon_badge"),
        "image_url": info.get("image_url"),
        "size": info.get("size"),
        "units": info.get("units"),
        "unit_price": info.get("unit_price"),
        "unit_price_str": info.get("unit_price_str"),
        "label_short": info.get("label_short"),
        "summary_line": info.get("summary_line"),
    }


async def get_amazon_offer(raw_url: str) -> dict:
    return await asyncio.to_thread(_build_amazon_offer_sync, raw_url)


async def get_multiple_amazon_offers(urls: list[str]) -> list[dict]:
    def _fetch_all():
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(urls)))) as ex:
            return list(ex.map(_build_amazon_offer_sync, urls))

    return await asyncio.to_thread(_fetch_all)


def _brl(val: float) -> str:
    return "R$ " + f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _extract_common_base_title(titles: list[str]) -> str:
    if not titles:
        return "Oferta Especial Amazon"
    first = titles[0]
    cleaned = re.sub(
        r"\b(?:Tamanho|Tam\.?)?\s*(?:RN|XXXG|XXG|XG|SXG|EG|P|M|G)\b.*$",
        "",
        first,
        flags=re.IGNORECASE,
    ).strip(" -–,/")
    cleaned = re.sub(
        r"\b\d{2,4}\s*(?:Unidades|Unid|Un|Fraldas|Tiras).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" -–,/")
    return cleaned if len(cleaned) >= 6 else first[:68]


def format_multi_variant_amazon_message(offers: list[dict]) -> str:
    valid_offers = [o for o in offers if o and o.get("affiliate_url")]
    if not valid_offers:
        return ""
    if len(valid_offers) == 1:
        return format_product_message(valid_offers[0])

    titles = [o.get("title") or "" for o in valid_offers if o.get("title")]
    base_title = _extract_common_base_title(titles)

    prices = [o["current_price"] for o in valid_offers if o.get("current_price")]
    any_sns = any(o.get("has_sns") for o in valid_offers)
    any_coupon = next(
        (o.get("coupon_badge") for o in valid_offers if o.get("coupon_badge")), None
    )

    lines = [f"🔥 <b>{html.escape(base_title)}</b>"]

    if prices:
        min_p, max_p = min(prices), max(prices)
        if abs(max_p - min_p) < 1.0:
            lines.append(f"💰 <b>{_brl(min_p)} cada</b>")
        else:
            lines.append(f"💰 <b>A partir de {_brl(min_p)}</b>")

    if any_sns:
        lines.append("🔄 <i>Selecione a opção “Programe e Poupe”</i>")
    if any_coupon:
        lines.append(f"🎟️ <i>Ative o {html.escape(any_coupon)} na página</i>")

    summary_lines = []
    for off in valid_offers:
        s_line = off.get("summary_line")
        if s_line:
            if (
                prices
                and (max(prices) - min(prices) >= 5.0)
                and off.get("current_price")
            ):
                s_line += f" — {_brl(off['current_price'])}"
            summary_lines.append(f"• {html.escape(s_line)}")

    if summary_lines:
        lines.append("\n" + "\n".join(summary_lines))

    lines.append("")
    for idx, off in enumerate(valid_offers, 1):
        lbl = (
            off.get("label_short") or (off.get("title") or f"Opção {idx}")[:45].strip()
        )
        lines.append(f"<b>{html.escape(lbl)}</b>\n👉 {off['affiliate_url']}")

    base_msg = "\n".join(lines).strip()

    return prepend_ai_hook_to_message(
        base_message=base_msg,
        title=base_title,
        current_price=min(prices) if prices else None,
        original_price=None,
        discount_pct=None,
        store="Amazon",
    )


def format_product_message(
    offer: dict,
    link: str | None = None,
    extra_coupon_links: list[str] | None = None,
) -> str:
    """
    Padrão Curto Unificado (5 a 6 linhas):
    🔥 Título Limpo
    💰 De ~R$ X~ por R$ Y (Z% OFF)
    📦 Detalhe extra (Custo unitário / Programe e Poupe / Parcelamento / Cupom)
    👉 Link direto
    """
    aff_link = link or offer.get("affiliate_url") or ""
    raw_title = offer.get("title")
    short_title = (
        clean_product_title(raw_title, max_len=68)
        if raw_title
        else "Oferta em Destaque na Amazon"
    )

    cur = offer.get("current_price")
    orig = offer.get("original_price")
    disc = offer.get("discount_pct")
    inst = offer.get("installments_text")
    coupon_badge = offer.get("coupon_badge")
    has_sns = offer.get("has_sns", False)
    unit_price_str = offer.get("unit_price_str")
    units = offer.get("units")

    disc_badge = f" <i>({int(disc)}% OFF)</i>" if (disc and int(disc) > 0) else ""

    lines = [f"🔥 <b>{html.escape(short_title)}</b>"]

    if cur is not None:
        if orig and orig > cur:
            lines.append(
                f"💰 De <s>{_brl(orig)}</s> por <b>{_brl(cur)}</b>{disc_badge}"
            )
        else:
            lines.append(f"💰 Por <b>{_brl(cur)}</b>{disc_badge}")

        if unit_price_str and units and cur < 300:
            lines.append(f"📦 <i>Sai a R$ {unit_price_str}/un ({units} un.)</i>")
    else:
        lines.append("💰 <i>Confira o preço promocional no link abaixo</i>")

    if has_sns:
        lines.append('🔄 <i>Selecione "Programe e Poupe" na página</i>')
    elif coupon_badge:
        lines.append(f'🎟️ <i>Marque "{html.escape(coupon_badge)}" na página</i>')
    elif inst:
        lines.append(f"💳 <i>{html.escape(inst)}</i>")

    if extra_coupon_links:
        for c_link in extra_coupon_links:
            lines.append(f"🎟️ <b>Cupom:</b> {c_link}")

    lines.append(f"\n🛒 <b>Compre na Amazon:</b>\n👉 {aff_link}")
    base_msg = "\n".join(lines)

    if "PREVIA_SIMULACAO" not in aff_link and raw_title:
        return prepend_ai_hook_to_message(
            base_message=base_msg,
            title=short_title,
            current_price=cur,
            original_price=orig,
            discount_pct=disc,
            store="Amazon",
        )

    return base_msg
