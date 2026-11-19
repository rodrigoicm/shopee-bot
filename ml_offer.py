"""
Monta a oferta do Mercado Livre sem navegador e sem API oficial,
usando apenas a conversão interna (createLink) e a leitura do HTML (_n.ctx.r).
Formatado no Padrão Curto Escaneável (5 linhas).
"""

import asyncio
import html
import logging
import requests

from ml_affiliate import (
    AffiliateSessionExpired,
    clean_url,
    create_affiliate_link_full,
    ensure_product_url,
    resolve_product_url,
)
from ml_social import SocialPageBlocked, fetch_social_product
from ai_copy_service import clean_product_title, prepend_ai_hook_to_message

logger = logging.getLogger(__name__)

last_error: str | None = None


def _build_offer_sync(raw_url: str, debug: bool = False) -> dict:
    _, product_url, info = resolve_product_url(raw_url, debug=debug)
    cleaned = clean_url(product_url)
    ensure_product_url(cleaned)

    link = create_affiliate_link_full(cleaned, debug=debug)
    affiliate_url = link["short_url"]

    if not info or info.get("price") is None:
        target_to_read = affiliate_url or link.get("long_url") or cleaned
        try:
            info = fetch_social_product(target_to_read, debug=debug)
        except (SocialPageBlocked, ValueError, requests.RequestException) as e:
            logger.warning(f"Não consegui ler os dados via {target_to_read}: {e}")
            if link.get("long_url") and link["long_url"] != target_to_read:
                try:
                    info = fetch_social_product(link["long_url"], debug=debug)
                except Exception:
                    pass

    info = info or {}
    price = info.get("price")
    pix = info.get("pix_price")

    return {
        "affiliate_url": affiliate_url,
        "image_url": info.get("thumbnail"),
        "title": info.get("title"),
        "current_price": pix if pix is not None else price,
        "original_price": info.get("original_price"),
        "rating": None,
        "reviews_count": None,
        "sales": None,
        "regular_price": price,
        "pix_price": pix,
        "price_note": "no Pix" if pix is not None else None,
        "discount_label": info.get("discount_label"),
        "installments_text": info.get("installments_text"),
        "installments_no_interest": info.get("installments_no_interest"),
        "seller": info.get("seller"),
        "item_id": info.get("itemId"),
        "product_url": cleaned,
    }


async def get_ml_offer(product_url: str, debug: bool = False) -> dict | None:
    global last_error
    last_error = None
    try:
        return await asyncio.to_thread(_build_offer_sync, product_url, debug)
    except AffiliateSessionExpired as e:
        last_error = str(e)
        logger.error(f"❌ {e}")
    except Exception as e:
        last_error = str(e)
        logger.error(f"❌ Erro ao montar a oferta do Mercado Livre: {e}")
    return None


def _brl(value) -> str:
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_product_message(offer: dict, link: str | None = None) -> str:
    """
    Padrão Curto Unificado (5 a 6 linhas):
    🔥 Título Limpo
    💰 De ~R$ X~ por R$ Y no Pix (Z% OFF)
    💳 Parcelamento (se houver)
    👉 Link direto
    """
    aff_link = link or offer.get("affiliate_url") or ""
    raw_title = offer.get("title")
    short_title = (
        clean_product_title(raw_title, max_len=68)
        if raw_title
        else "Oferta no Mercado Livre"
    )

    cur = offer.get("current_price")
    orig = offer.get("original_price")
    pix = offer.get("pix_price")
    reg = offer.get("regular_price")
    discount_label = (offer.get("discount_label") or "").strip()
    inst_text = offer.get("installments_text")

    # Badge de desconto compacto na mesma linha do preço
    disc_badge = ""
    if discount_label:
        lbl = html.escape(discount_label.replace("!", "").strip())
        disc_badge = f" <i>({lbl})</i>"
    elif orig and cur and orig > cur:
        pct = round(((orig - cur) / orig) * 100)
        if pct > 0:
            disc_badge = f" <i>({pct}% OFF)</i>"

    lines = [f"🔥 <b>{html.escape(short_title)}</b>"]

    note = f" {offer['price_note']}" if offer.get("price_note") else ""
    if cur is not None:
        if orig and orig > cur:
            lines.append(
                f"💰 De <s>{_brl(orig)}</s> por <b>{_brl(cur)}{note}</b>{disc_badge}"
            )
        else:
            lines.append(f"💰 Por <b>{_brl(cur)}{note}</b>{disc_badge}")

        if inst_text:
            lines.append(f"💳 <i>{html.escape(str(inst_text))}</i>")
        elif pix is not None and reg is not None and reg != pix:
            lines.append(f"💳 <i>ou {_brl(reg)} em outros meios</i>")
    else:
        lines.append("💰 <i>Confira o preço promocional no link abaixo</i>")

    lines.append(f"\n🛒 <b>Compre no Mercado Livre:</b>\n👉 {aff_link}")
    base_msg = "\n".join(lines)

    if "PREVIA_SIMULACAO" not in aff_link and raw_title:
        return prepend_ai_hook_to_message(
            base_message=base_msg,
            title=short_title,
            current_price=cur,
            original_price=orig,
            discount_pct=discount_label,
            store="Mercado Livre",
        )
    return base_msg


def preview_message(offer: dict) -> str:
    return format_product_message(offer)
