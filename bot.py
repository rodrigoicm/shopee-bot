import os
import re
import html
import json
import logging
import asyncio
import requests
import ml_offer
from ml_affiliate import create_affiliate_link_full
import ml_auto
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Playwright

from database import init_db, add_to_queue, get_config
from shopee_service import ShopeeAffiliateAPI
from whatsapp_service import (
    send_whatsapp_offer,
    WHATSAPP_GROUP_PRINCIPAL,
    WHATSAPP_GROUP_PARCERIA,
)

load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]
cuiaba_tz = timezone(timedelta(hours=-4))
init_db()

ML_DEFAULT_CUPON_LINK = "https://mercadolivre.com/sec/2YCiHoZ"

SHOPEE_URL_PATTERN = (
    r"https?://(?:[a-zA-Z0-9-]+\.)?(?:shopee\.com\.br|shope\.ee|br\.shp\.ee)/[^\s]+"
)
ML_URL_PATTERN = r"https?://(?:[a-zA-Z0-9-]+\.)?(?:mercadolivre\.com\.br|mercadolivre\.com|mercadolibre\.com\.br|mercadolibre\.com|ml\.br|meli\.la)/[^\s]+"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------- ESTADO GLOBAL DO BROWSER / FILA ASSÍNCRONA ----------
@dataclass
class MLState:
    playwright: Playwright | None = None
    browser: Browser | None = None
    cookies_dict: dict | None = None
    cookies_expiration: datetime | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker_task: asyncio.Task | None = None
    last_warning_date: date | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


ml_state = MLState()


def get_cookie_expiration(cookies_dict: dict) -> datetime | None:
    expirations = [
        c["expires"]
        for c in cookies_dict.get("cookies", [])
        if c.get("expires") and c["expires"] > 0
    ]
    if not expirations:
        return None
    return datetime.fromtimestamp(min(expirations))


def load_ml_cookies() -> None:
    cookies_json_str = os.environ.get("ML_COOKIES_JSON")
    if not cookies_json_str:
        logger.warning(
            "⚠️ ML_COOKIES_JSON não configurado — usando ML_AFFILIATE_COOKIE do .env."
        )
        return

    try:
        ml_state.cookies_dict = json.loads(cookies_json_str)
    except json.JSONDecodeError as e:
        logger.error(f"❌ ML_COOKIES_JSON inválido (não é um JSON válido): {e}")
        return

    ml_state.cookies_expiration = get_cookie_expiration(ml_state.cookies_dict)
    if ml_state.cookies_expiration:
        logger.info(
            f"🍪 Cookie do Mercado Livre carregado, válido até {ml_state.cookies_expiration:%d/%m/%Y %H:%M}"
        )
    else:
        logger.info(
            "🍪 Cookie do Mercado Livre carregado (sem data de expiração detectável)."
        )


async def maybe_warn_cookie_expiration(update: Update) -> None:
    if not ml_state.cookies_expiration:
        return

    today = date.today()
    if ml_state.last_warning_date == today:
        return

    days_left = (ml_state.cookies_expiration.date() - today).days
    if days_left > 3:
        return

    ml_state.last_warning_date = today

    if days_left >= 0:
        aviso = (
            f"⚠️ <b>Atenção:</b> o cookie de sessão do Mercado Livre vence em "
            f"<b>{ml_state.cookies_expiration:%d/%m/%Y}</b> (em {days_left} dia(s)). "
            f"Providencie a renovação em breve."
        )
    else:
        aviso = (
            f"🚨 <b>O cookie de sessão do Mercado Livre já venceu</b> "
            f"(vencimento em {ml_state.cookies_expiration:%d/%m/%Y}). "
            f"As conversões de link do Mercado Livre podem falhar até a renovação."
        )

    await update.message.reply_text(aviso, parse_mode="HTML")


# ---------- FUNÇÕES DE LIMPEZA E TRATAMENTO DE TEXTO ----------
def clean_corrupted_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\ufffd", "%")
    cleaned = re.sub(r"%+", "%", cleaned)
    return cleaned


def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def remove_affiliate_disclaimers(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"\*?\s*comprando (?:através|por meio) do link você (?:estará|está) ajudando[^\n]*",
        r"\*?\s*você me paga um café[^\n]*",
        r"\*?\s*ajude o canal[^\n]*",
        r"#publi\b[^\n]*",
        r"#ad\b[^\n]*",
        r"#publicidade\b[^\n]*",
        r"-?\s*anúncio\b[^\n]*",
        r"🫵🏼.*?(?:\n|$)",
        r"Convide Amigos.*?(?:\n|$)",
        r"https?://(?:chat\.whatsapp\.com|t\.me|grupos\.[^\s]+)[^\n]*",
    ]

    cleaned_text = text
    for pattern in patterns:
        cleaned_text = re.sub(pattern, "", cleaned_text, flags=re.IGNORECASE)

    cleaned_text = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned_text)
    return cleaned_text.strip()


# ---------- CONVERSÃO DE LINK DO MERCADO LIVRE (VIA ML_OFFER) ----------
async def convert_mercadolivre_link(product_url: str) -> dict | None:
    try:
        offer = await ml_offer.get_ml_offer(product_url)
        if not offer:
            if ml_offer.last_error:
                logger.error(f"❌ Erro no ml_offer: {ml_offer.last_error}")
            return None
        return offer
    except Exception as e:
        logger.error(f"❌ Exceção ao converter link do Mercado Livre no bot: {e}")
        return None


async def ml_worker() -> None:
    while True:
        job = await ml_state.queue.get()
        product_url: str = job["product_url"]
        future: asyncio.Future = job["future"]
        try:
            result = await convert_mercadolivre_link(product_url)
            if not future.cancelled():
                future.set_result(result)
        except Exception as e:
            if not future.cancelled():
                future.set_exception(e)
        finally:
            ml_state.queue.task_done()


async def enqueue_ml_conversion(product_url: str) -> dict | None:
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    await ml_state.queue.put({"product_url": product_url, "future": future})
    return await future


# ---------- COMANDO /gerar PARA LISTA AUTOMÁTICA ----------
async def handle_gerar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = int(get_config("auto_refill_count") or "15")
    if context.args and context.args[0].isdigit():
        count = max(1, min(50, int(context.args[0])))

    today_str = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")
    status_msg = await update.message.reply_text(
        f"🎲 Buscando e selecionando <b>{count} ofertas inéditas</b>...",
        parse_mode="HTML",
    )

    keywords_csv = get_config("auto_keywords") or ""
    res = await asyncio.to_thread(
        ml_auto.generate_mixed_auto_offers_to_queue, count, today_str, keywords_csv
    )

    await status_msg.edit_text(
        f"✅ <b>{res['total_added']} novas ofertas</b> adicionadas na Fila ({today_str})!\n"
        f"• 🟠 Shopee: <b>{res['shopee_added']}</b>\n"
        f"• 🟡 Mercado Livre: <b>{res['ml_added']}</b>",
        parse_mode="HTML",
    )


# ---------- HANDLERS ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_warn_cookie_expiration(update)

    raw_text = update.message.text or update.message.caption or ""

    shopee_links = [
        l.rstrip(".,;:!?)\n") for l in re.findall(SHOPEE_URL_PATTERN, raw_text)
    ]
    ml_links = [l.rstrip(".,;:!?)\n") for l in re.findall(ML_URL_PATTERN, raw_text)]

    if not shopee_links and not ml_links:
        await update.message.reply_text(
            "👋 Nenhum link válido da Shopee ou do Mercado Livre foi encontrado na mensagem.\n"
            "💡 Dica: Use /gerar para adicionar ofertas automáticas na fila!"
        )
        return

    cleaned_text = remove_affiliate_disclaimers(clean_corrupted_text(raw_text))

    if shopee_links:
        context.user_data["platform"] = "shopee"
        context.user_data["shopee_links"] = shopee_links
        context.user_data["raw_url"] = shopee_links[-1]
        context.user_data["multiple_ml"] = False
    else:
        context.user_data["platform"] = "mercadolivre"
        context.user_data["ml_links"] = ml_links
        context.user_data["raw_url"] = ml_links[-1]
        context.user_data["multiple_ml"] = len(ml_links) > 1

    context.user_data["raw_text"] = cleaned_text

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎟️ CUPOM", callback_data="type_cupom")],
            [
                InlineKeyboardButton(
                    "🏷️ OFERTA (Manter Texto)", callback_data="type_oferta"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔗 LINK (Gerar Copy Automática)", callback_data="type_link"
                )
            ],
        ]
    )

    platform_name = (
        "Shopee" if context.user_data["platform"] == "shopee" else "Mercado Livre"
    )
    qtd_links = len(shopee_links) if shopee_links else len(ml_links)

    await update.message.reply_text(
        f"⚙️ <b>{qtd_links} Link(s) do {platform_name} identificado(s)!</b>\n\n"
        "Qual o tipo de mensagem que você deseja formatar?\n\n"
        "• <b>CUPOM:</b> Converte todos os links ou gera card especial de Central de Cupons.\n"
        "• <b>OFERTA:</b> Mantém seu texto original e substitui cada link pelo seu respectivo link de afiliado.\n"
        "• <b>LINK:</b> Gera copy completa via API (e inclui o link do cupom se houver mais de 1 link!).",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data
    platform = context.user_data.get("platform", "shopee")
    raw_text = context.user_data.get("raw_text", "")
    raw_url = context.user_data.get("raw_url", "")
    multiple_ml = context.user_data.get("multiple_ml", False)

    if not raw_text or not raw_url:
        await query.edit_message_text(
            "⚠️ Os dados da mensagem expiraram. Envie o link novamente."
        )
        return

    await query.edit_message_text("⏳ Convertendo link(s) para afiliado...")
    clean_base_text = remove_affiliate_disclaimers(raw_text)

    media_url = None
    aff_link = None
    formatted_msg = ""
    category_tag = "Manual"

    # ================= MERCADO LIVRE =================
    if platform == "mercadolivre":
        ml_links = context.user_data.get("ml_links", [raw_url])

        if choice == "type_cupom":
            category_tag = "Cupom ML"
            url_map = {}
            for orig_l in ml_links:
                try:
                    res_link = await asyncio.to_thread(
                        create_affiliate_link_full, orig_l
                    )
                    conv = res_link.get("short_url") or res_link.get("long_url")
                    url_map[orig_l] = conv if conv else ML_DEFAULT_CUPON_LINK
                except Exception:
                    url_map[orig_l] = ML_DEFAULT_CUPON_LINK

            aff_link = url_map.get(raw_url, ML_DEFAULT_CUPON_LINK)

            if clean_base_text.strip() == raw_url.strip():
                formatted_msg = ShopeeAffiliateAPI.format_coupon_page_message(
                    aff_link, "Mercado Livre"
                )
            else:
                rewritten = escape_html(clean_base_text)
                for orig_l, new_l in url_map.items():
                    rewritten = rewritten.replace(escape_html(orig_l), new_l)
                formatted_msg = rewritten

        elif multiple_ml or len(ml_links) > 1:
            url_map = {}
            main_ml_result = None
            coupon_ml_links = []

            for orig_l in ml_links:
                res = await enqueue_ml_conversion(orig_l)
                if res and res.get("affiliate_url"):
                    url_map[orig_l] = res["affiliate_url"]
                    if res.get("current_price") is not None:
                        main_ml_result = res
                        media_url = res.get("image_url") or media_url
                    else:
                        coupon_ml_links.append(res["affiliate_url"])
                else:
                    try:
                        res_link = await asyncio.to_thread(
                            create_affiliate_link_full, orig_l
                        )
                        conv = res_link.get("short_url") or orig_l
                        url_map[orig_l] = conv
                        coupon_ml_links.append(conv)
                    except Exception:
                        url_map[orig_l] = orig_l

            if choice == "type_link" and main_ml_result:
                formatted_msg = ml_offer.format_product_message(
                    main_ml_result, main_ml_result.get("affiliate_url")
                )
                if coupon_ml_links:
                    c_lines = "\n".join(
                        f"🎟️ <b>Resgate o cupom aqui:</b> {cl}"
                        for cl in coupon_ml_links
                    )
                    formatted_msg = f"{formatted_msg}\n\n{c_lines}"
            else:
                new_text = escape_html(clean_base_text)
                for orig_l, new_l in url_map.items():
                    new_text = new_text.replace(escape_html(orig_l), new_l)

                free_notice = "\n\n🚚 <b>Aproveite o frete grátis e benefícios do Mercado Livre!</b>"
                if "frete grátis" not in new_text.lower():
                    new_text += free_notice
                formatted_msg = new_text

        else:
            ml_result = await enqueue_ml_conversion(raw_url)
            if not ml_result:
                await query.message.reply_text(
                    "❌ Não foi possível gerar o link de afiliado do Mercado Livre. Verifique se a sessão de login está válida."
                )
                return

            aff_link = ml_result.get("affiliate_url")
            media_url = ml_result.get("image_url")

            if choice == "type_link":
                formatted_msg = ml_offer.format_product_message(ml_result, aff_link)
            else:
                free_shipping_notice = (
                    "🚚 <b>Aproveite o frete grátis e benefícios do Mercado Livre!</b>"
                )
                if clean_base_text.strip() == raw_url.strip():
                    formatted_msg = ml_offer.format_product_message(ml_result, aff_link)
                else:
                    converted_text = escape_html(clean_base_text).replace(
                        escape_html(raw_url), aff_link
                    )
                    formatted_msg = f"{converted_text}\n\n{free_shipping_notice}"

    # ================= SHOPEE (USANDO O SERVIÇO UNIFICADO shopee_service.py) =================
    else:
        app_id = os.environ.get("SHOPEE_APP_ID")
        app_secret = os.environ.get("SHOPEE_APP_SECRET")

        if not app_id or not app_secret:
            await query.message.reply_text(
                "❌ Credenciais da API da Shopee não configuradas."
            )
            return

        api = ShopeeAffiliateAPI(app_id, app_secret)
        shopee_links = context.user_data.get("shopee_links", [raw_url])

        url_map: dict[str, str] = {}
        offer = None
        product_link = raw_url
        product_aff_link = None
        coupon_aff_links: list[str] = []

        for link in shopee_links:
            full_url = await asyncio.to_thread(api.resolve_short_link, link)
            clean_dest_url = await asyncio.to_thread(
                api.clean_shopee_url_for_affiliate, full_url
            )
            item_id = api.extract_item_id(full_url) or api.extract_item_id(link)

            if item_id:
                temp_offer = await asyncio.to_thread(api.get_product_offer, item_id)
                if temp_offer:
                    if temp_offer.get("productName"):
                        temp_offer["productName"] = clean_corrupted_text(
                            temp_offer["productName"]
                        )
                    offer = temp_offer
                    product_link = clean_dest_url
                    conv_link = (
                        temp_offer.get("offerLink")
                        or await asyncio.to_thread(
                            api.generate_short_link, clean_dest_url
                        )
                        or link
                    )
                else:
                    product_link = clean_dest_url
                    conv_link = (
                        await asyncio.to_thread(api.generate_short_link, clean_dest_url)
                        or await asyncio.to_thread(api.generate_short_link, full_url)
                        or link
                    )
                url_map[link] = conv_link
                product_aff_link = conv_link
            else:
                conv_coupon = (
                    await asyncio.to_thread(api.generate_short_link, clean_dest_url)
                    or await asyncio.to_thread(api.generate_short_link, full_url)
                    or await asyncio.to_thread(api.generate_short_link, link)
                    or link
                )
                url_map[link] = conv_coupon
                coupon_aff_links.append(conv_coupon)

        media_url = offer.get("imageUrl") if offer else None
        is_only_coupon_page = (offer is None) and (len(coupon_aff_links) > 0)

        if choice == "type_cupom":
            category_tag = "Cupom Shopee"
            if clean_base_text.strip() in shopee_links:
                target_aff = (
                    coupon_aff_links[0]
                    if coupon_aff_links
                    else url_map.get(raw_url, raw_url)
                )
                formatted_msg = api.format_coupon_page_message(target_aff, "Shopee")
            else:
                converted_text = escape_html(clean_base_text)
                for orig_l, new_l in url_map.items():
                    converted_text = converted_text.replace(escape_html(orig_l), new_l)
                formatted_msg = converted_text

        elif choice == "type_oferta":
            category_tag = "Cupom Shopee" if is_only_coupon_page else "Manual Shopee"
            free_shipping_notice = "🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>"

            if clean_base_text.strip() in shopee_links:
                target_aff = url_map.get(raw_url, raw_url)
                if is_only_coupon_page:
                    formatted_msg = api.format_coupon_page_message(target_aff, "Shopee")
                else:
                    formatted_msg = (
                        "🏷 <b>OFERTA IMPERDÍVEL SHOPEE</b>\n\n"
                        f"{free_shipping_notice}\n\n"
                        f"Aproveite a promoção no link abaixo:\n👉 {target_aff}"
                    )
            else:
                converted_text = escape_html(clean_base_text)
                for orig_l, new_l in url_map.items():
                    converted_text = converted_text.replace(escape_html(orig_l), new_l)

                if (
                    "frete grátis" not in converted_text.lower()
                    and "frete gratis" not in converted_text.lower()
                ):
                    formatted_msg = f"{converted_text}\n\n{free_shipping_notice}"
                else:
                    formatted_msg = converted_text

        elif choice == "type_link":
            if offer and product_aff_link:
                category_tag = "Manual Shopee"
                formatted_msg = api.format_product_message(
                    offer,
                    product_aff_link,
                    store_name="Shopee",
                    extra_coupon_links=coupon_aff_links,
                )
            elif is_only_coupon_page:
                category_tag = "Cupom Shopee"
                formatted_msg = api.format_coupon_page_message(
                    coupon_aff_links[0], "Shopee"
                )
            else:
                target_aff = url_map.get(raw_url, raw_url)
                formatted_msg = (
                    "🔥 <b>PRODUTO EM DESTAQUE NA SHOPEE</b>\n\n"
                    "🏪 Loja: <b>Shopee</b>\n\n"
                    "🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>\n\n"
                    f"Confira as melhores condições de compra no link:\n👉 {target_aff}"
                )

    # --- MONTAGEM DOS BOTÕES E ENVIO ---
    formatted_msg = clean_corrupted_text(formatted_msg)

    context.user_data["last_formatted_msg"] = formatted_msg
    context.user_data["last_media_url"] = media_url
    context.user_data["last_store"] = (
        "Shopee" if platform == "shopee" else "Mercado Livre"
    )
    context.user_data["last_product_url"] = (
        product_link if platform == "shopee" else raw_url
    )
    context.user_data["last_category"] = category_tag

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📥 Adicionar à Fila do Portal",
                    callback_data="send_group_queue",
                )
            ],
            [
                InlineKeyboardButton(
                    "🚀 Enviar para Grupo Principal",
                    callback_data="send_group_principal",
                )
            ],
            [
                InlineKeyboardButton(
                    "🤝 Enviar para Grupo Parceria",
                    callback_data="send_group_parceria",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Enviar para Todos os Grupos", callback_data="send_group_all"
                )
            ],
        ]
    )

    if media_url:
        try:
            await query.message.reply_photo(
                photo=media_url,
                caption=formatted_msg,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return
        except Exception as e:
            logger.error(f"Erro ao enviar foto: {e}")

    await query.message.reply_text(
        formatted_msg, reply_markup=keyboard, parse_mode="HTML"
    )


# ---------- HANDLER DE DISPARO PARA OS GRUPOS OU FILA ----------
async def handle_whatsapp_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    message_text = context.user_data.get("last_formatted_msg")
    media_url = context.user_data.get("last_media_url")
    choice = query.data

    if not message_text:
        await query.message.reply_text("⚠️ Nenhuma mensagem pronta encontrada.")
        return

    await query.edit_message_reply_markup(reply_markup=None)

    if choice == "send_group_queue":
        store_name = context.user_data.get("last_store", "Shopee")
        prod_url = context.user_data.get("last_product_url", "")
        cat_tag = context.user_data.get("last_category", "Telegram Manual")
        today_str = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        add_to_queue(
            store=store_name,
            product_url=prod_url,
            message=message_text,
            media_url=media_url,
            priority=1,
            target_date=today_str,
            category=cat_tag,
        )
        await query.message.reply_text(
            f"📥 <b>Adicionado com sucesso à Fila do Portal</b> para hoje (<b>{today_str}</b>) na categoria <b>{cat_tag}</b>!",
            parse_mode="HTML",
        )
        return

    if choice == "send_group_principal":
        status_msg = await query.message.reply_text(
            "⏳ Enviando para o Grupo Principal..."
        )
        success = await send_whatsapp_offer(
            WHATSAPP_GROUP_PRINCIPAL,
            message_text,
            media_url,
        )

        if success:
            await status_msg.edit_text(
                "✅ Oferta disparada com sucesso para o Grupo Principal!"
            )
        else:
            await status_msg.edit_text(
                "❌ Falha ao disparar para o Grupo Principal. Verifique os logs."
            )

    elif choice == "send_group_parceria":
        status_msg = await query.message.reply_text(
            "⏳ Enviando para o Grupo Parceria..."
        )
        success = await send_whatsapp_offer(
            WHATSAPP_GROUP_PARCERIA,
            message_text,
            media_url,
        )

        if success:
            await status_msg.edit_text(
                "✅ Oferta disparada com sucesso para o Grupo Parceria!"
            )
        else:
            await status_msg.edit_text(
                "❌ Falha ao disparar para o Grupo Parceria. Verifique os logs."
            )

    elif choice == "send_group_all":
        status_msg = await query.message.reply_text(
            "⏳ Enviando para todos os grupos..."
        )

        success_principal = await send_whatsapp_offer(
            WHATSAPP_GROUP_PRINCIPAL,
            message_text,
            media_url,
        )
        success_parceria = await send_whatsapp_offer(
            WHATSAPP_GROUP_PARCERIA,
            message_text,
            media_url,
        )

        if success_principal and success_parceria:
            await status_msg.edit_text(
                "✅ Oferta disparada com sucesso para todos os grupos!"
            )
        else:
            await status_msg.edit_text(
                "⚠️ Disparo concluído, mas um dos grupos falhou. Verifique os logs."
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Exceção não tratada durante o processamento de um update:",
        exc_info=context.error,
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Ocorreu um erro inesperado ao processar sua solicitação. Tente novamente em instantes."
            )
        except Exception:
            pass


# ---------- CICLO DE VIDA DO BROWSER ----------
async def on_startup(app: Application) -> None:
    load_ml_cookies()
    headless = True
    ml_state.playwright = await async_playwright().start()
    ml_state.browser = await ml_state.playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ],
    )
    ml_state.worker_task = asyncio.create_task(ml_worker())
    logger.info(
        f"🚀 Browser Playwright iniciado (headless={headless}) e worker da fila em execução."
    )


async def on_shutdown(app: Application) -> None:
    if ml_state.worker_task:
        ml_state.worker_task.cancel()
        try:
            await ml_state.worker_task
        except asyncio.CancelledError:
            pass

    if ml_state.browser:
        try:
            await ml_state.browser.close()
        except Exception:
            pass

    if ml_state.playwright:
        try:
            await ml_state.playwright.stop()
        except Exception:
            pass

    logger.info("🛑 Browser Playwright encerrado.")


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["gerar", "auto"], handle_gerar_command))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
        )
    )
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^type_"))
    app.add_handler(
        CallbackQueryHandler(handle_whatsapp_dispatch, pattern="^send_group_")
    )
    app.add_error_handler(error_handler)
    app.run_polling()


if __name__ == "__main__":
    main()
