"""
Monta a oferta do Mercado Livre sem navegador e sem API oficial,
usando apenas a conversão interna (createLink) e a leitura do HTML (_n.ctx.r).

Retorna:
    affiliate_url, image_url, title, current_price, original_price,
    regular_price, pix_price, discount_label, installments_text,
    installments_no_interest, seller, item_id, product_url
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

logger = logging.getLogger(__name__)

# Último erro (texto) para o bot poder avisar, ex.: sessão do afiliado expirada
last_error: str | None = None


def _build_offer_sync(raw_url: str, debug: bool = False) -> dict:
    # 1. Expande o link. Se for link de outro afiliado (/social/), já traz 'info' completo!
    _, product_url, info = resolve_product_url(raw_url, debug=debug)

    # 2. Limpa a URL do produto e cria o seu link de afiliado (short_url + long_url)
    cleaned = clean_url(product_url)
    ensure_product_url(cleaned)

    link = create_affiliate_link_full(cleaned, debug=debug)
    affiliate_url = link["short_url"]

    # 3. Se o link enviado era um link direto de produto (info ainda é None),
    #    buscamos os dados na página do link gerado (short_url abre a página /social/ com o card!)
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
        # --- mesmas chaves esperadas pelo bot.py ---
        "affiliate_url": affiliate_url,
        "image_url": info.get("thumbnail"),
        "title": info.get("title"),
        "current_price": pix if pix is not None else price,
        "original_price": info.get("original_price"),
        "rating": None,
        "reviews_count": None,
        "sales": None,
        # --- detalhes de preço (Pix, Normal e Parcelamento) ---
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
    """Versão assíncrona (roda o código bloqueante em outra thread)."""
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
    """Função única e centralizada para formatar a mensagem do Mercado Livre (Telegram, Portal e WhatsApp)."""
    aff_link = link or offer.get("affiliate_url") or ""
    raw_title = offer.get("title")
    title = html.escape(str(raw_title)) if raw_title else None

    header_title = (
        f"🔥 <b>{title}</b>\n\n"
        if title
        else "🔥 <b>PRODUTO EM DESTAQUE NO MERCADO LIVRE</b>\n\n"
    )

    cur = offer.get("current_price")
    orig = offer.get("original_price")
    pix = offer.get("pix_price")
    reg = offer.get("regular_price")
    discount_label = (offer.get("discount_label") or "").strip()
    inst_text = offer.get("installments_text")

    # --- 1. Destaque do Desconto (% OFF no Pix / Normal) ---
    is_pix_deal = bool(pix or (discount_label and "pix" in discount_label.lower()))
    discount_line = ""

    if discount_label:
        label_clean = html.escape(discount_label.replace("!", "").strip())
        if "pix" in label_clean.lower():
            discount_line = f"🔥 <b>{label_clean}</b>\n\n"
        elif is_pix_deal:
            discount_line = f"🔥 <b>{label_clean} no Pix</b>\n\n"
        else:
            discount_line = f"🔥 <b>{label_clean}</b>\n\n"
    elif orig and cur and orig > cur:
        percent_off = round(((orig - cur) / orig) * 100)
        if percent_off > 0:
            if is_pix_deal:
                discount_line = f"🔥 <b>{percent_off}% OFF no Pix</b>\n\n"
            else:
                discount_line = f"🔥 <b>{percent_off}% OFF aplicado no item!</b>\n\n"

    # --- 2. Preço e Parcelamento ---
    price_line = ""
    note = f" {offer['price_note']}" if offer.get("price_note") else ""

    if cur is not None:
        if orig and orig > cur:
            price_line += (
                f"💰 <b>Preço:</b> de {_brl(orig)} por <b>{_brl(cur)}</b>{note}\n"
            )
        else:
            price_line += f"💰 <b>Preço:</b> <b>{_brl(cur)}</b>{note}\n"

        if pix is not None and reg is not None and reg != pix:
            price_line += f"💳 ou <b>{_brl(reg)}</b> em outros meios\n"

        if inst_text:
            price_line += f"💳 Parcelamento: <b>{html.escape(str(inst_text))}</b>\n"

        price_line += "\n"

    # --- 3. Loja ---
    seller_line = ""
    if offer.get("seller"):
        seller_raw = offer["seller"].removeprefix("Por ").strip()

        # Se já tiver "Oficial" ou "Loja", tratamos para ficar elegante
        if "oficial" in seller_raw.lower():
            seller_clean = html.escape(seller_raw)
            seller_line = f"🏪 Loja: <b>{seller_clean} no Mercado Livre</b>\n\n"
        else:
            seller_clean = html.escape(seller_raw)
            seller_line = f"🏪 Loja: <b>{seller_clean} no Mercado Livre</b>\n\n"
    else:
        seller_line = "🏪 Loja: <b>Mercado Livre</b>\n\n"

    # --- 4. Frete e Link de Ação ---
    free_shipping = "🚚 <b>Aproveite frete grátis (conforme regras do app) e benefícios do Mercado Livre!</b>\n\n"
    cta_line = f"👇 <b>Clique no link abaixo para garantir o seu:</b>\n👉 {aff_link}"

    return f"{header_title}{discount_line}{price_line}{seller_line}{free_shipping}{cta_line}"


def preview_message(offer: dict) -> str:
    """Alias que reaproveita a função única format_product_message."""
    return format_product_message(offer)
