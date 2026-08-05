import os
import re
import json
import time
import hashlib
import logging
import requests
import urllib.parse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]
URL_PATTERN = r"https?://(?:[a-zA-Z0-9-]+\.)?shopee\.com\.br/[^\s]+"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- API SHOPEE AFFILIATE ----------
class ShopeeAffiliateAPI:
    GRAPHQL_URL = "https://open-api.affiliate.shopee.com.br/graphql"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    def _sign(self, payload: str, timestamp: str) -> str:
        factor = f"{self.app_id}{timestamp}{payload}{self.app_secret}"
        return hashlib.sha256(factor.encode("utf-8")).hexdigest()

    def _graphql_request(self, query: str) -> dict:
        payload = json.dumps({"query": query})
        timestamp = str(int(time.time()))
        signature = self._sign(payload, timestamp)

        headers = {
            "Authorization": f"SHA256 Credential={self.app_id}, Timestamp={timestamp}, Signature={signature}",
            "Content-Type": "application/json",
        }

        resp = requests.post(
            self.GRAPHQL_URL,
            data=payload.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            logger.error(f"Erro GraphQL: {data['errors']}")
            return {}
        return data.get("data", {})

    def get_product_offer(self, item_id: int) -> dict | None:
        query = f"""query {{
            productOfferV2(itemId: {item_id}) {{
                nodes {{
                    itemId
                    productName
                    priceMin
                    priceMax
                    priceDiscountRate
                    ratingStar
                    sales
                    shopName
                    offerLink
                }}
            }}
        }}"""
        data = self._graphql_request(query)
        nodes = data.get("productOfferV2", {}).get("nodes", [])
        return nodes[0] if nodes else None

    def generate_short_link(self, origin_url: str) -> str:
        query = f'mutation {{ generateShortLink(input: {{originUrl: "{origin_url}"}}) {{ shortLink }} }}'
        result = self._graphql_request(query)
        return result.get("generateShortLink", {}).get("shortLink", "")

# ---------- UTILITÁRIOS ----------
def extract_item_id(url: str) -> int | None:
    match_param = re.search(r"[?&](?:itemId|item_id)=(\d+)", url, re.IGNORECASE)
    if match_param:
        return int(match_param.group(1))

    match_path = re.search(r"i\.\d+\.(\d+)", url) or re.search(r"-i\.\d+\.(\d+)", url)
    if match_path:
        return int(match_path.group(1))

    match_product = re.search(r"/product/\d+/(\d+)", url)
    if match_product:
        return int(match_product.group(1))

    return None

def resolve_short_link(short_url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        session = requests.Session()
        resp = session.get(short_url, headers=headers, allow_redirects=True, timeout=12)
        return resp.url
    except Exception as e:
        logger.error(f"Erro ao expandir link: {e}")
        return short_url

def generate_persuasive_copy(offer: dict) -> str:
    discount = offer.get("priceDiscountRate")
    rating = offer.get("ratingStar")
    sales = offer.get("sales")
    shop_name = offer.get("shopName")

    lines = ["\n💡 *POR QUE VALE A PENA?*"]

    if discount and discount > 0:
        lines.append(f"🔥 *{discount}% OFF* aplicado no item!")
    if sales and sales > 10:
        lines.append(f"📦 Mais de *{sales:,} unidades vendidas*!")
    if rating and float(rating) >= 4.0:
        lines.append(f"⭐ Avaliação: *{rating}/5.0*")
    if shop_name:
        lines.append(f"🏪 Loja: *{shop_name}*")

    lines.append("\n⚠️ *Atenção:* O estoque e o preço promocional podem mudar a qualquer momento.")
    lines.append("👇 *Clique no link abaixo para garantir o seu:*")

    return "\n".join(lines)

def format_product_message(offer: dict, link: str) -> str:
    name = offer.get("productName", "Produto Incrível")
    price_min = offer.get("priceMin")
    price_max = offer.get("priceMax")

    msg = f"🔥 *{name}*\n\n"

    if price_min:
        p_min = float(price_min)
        p_max = float(price_max) if price_max else p_min
        if p_min != p_max and p_max > 0:
            msg += f"💰 *Preço:* R$ {p_min:,.2f} - R$ {p_max:,.2f}\n".replace(",", "X").replace(".", ",").replace("X", ".")
        else:
            msg += f"💰 *Preço:* R$ {p_min:,.2f}\n".replace(",", "X").replace(".", ",").replace("X", ".")

    msg += generate_persuasive_copy(offer)
    msg += f"\n👉 {link}"

    return msg

# ---------- HANDLERS ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text or update.message.caption or ""

    match = re.search(URL_PATTERN, raw_text)
    if not match:
        await update.message.reply_text("👋 Nenhum link da Shopee foi encontrado na mensagem enviada.")
        return

    # Salva o texto enviado temporariamente no contexto do usuário
    context.user_data["raw_text"] = raw_text
    context.user_data["raw_url"] = match.group(0).rstrip(".,;:!?)\n")

    # Botões de escolha do tipo de mensagem
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ CUPOM", callback_data="type_cupom")],
        [InlineKeyboardButton("🏷️ OFERTA", callback_data="type_oferta")],
        [InlineKeyboardButton("🔗 LINK", callback_data="type_link")]
    ])

    await update.message.reply_text(
        "⚙️ *Qual o tipo de mensagem que você enviou?*\n\n"
        "• *CUPOM:* Mantém o texto idêntico e converte o link do cupom.\n"
        "• *OFERTA:* Mantém o texto idêntico e converte o link da oferta.\n"
        "• *LINK:* Gera uma nova cópia completa do produto via API.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data
    raw_text = context.user_data.get("raw_text")
    raw_url = context.user_data.get("raw_url")

    if not raw_text or not raw_url:
        await query.edit_message_text("⚠️ Os dados da mensagem expiraram. Envie o texto novamente.")
        return

    await query.edit_message_text("⏳ Gerando seu link de afiliado...")

    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    
    if not app_id or not app_secret:
        await query.message.reply_text("❌ Credenciais da API da Shopee não configuradas.")
        return

    api = ShopeeAffiliateAPI(app_id, app_secret)
    full_url = resolve_short_link(raw_url)

    # Define se a mensagem deve ser processada com Markdown ou como texto puro
    parse_mode = None

    # 1. OPÇÃO CUPOM: Mantém mensagem idêntica trocando apenas o link
    if choice == "type_cupom":
        aff_link = api.generate_short_link(raw_url) or api.generate_short_link(full_url)
        if not aff_link:
            await query.message.reply_text("❌ Erro ao converter o link de cupom.")
            return
        formatted_msg = raw_text.replace(raw_url, aff_link)
        parse_mode = None  # Mantém o formato original enviado pelo usuário

    # 2. OPÇÃO OFERTA: Mantém mensagem idêntica trocando apenas o link
    elif choice == "type_oferta":
        aff_link = api.generate_short_link(full_url) or api.generate_short_link(raw_url)
        if not aff_link:
            await query.message.reply_text("❌ Erro ao converter o link de oferta.")
            return
        formatted_msg = raw_text.replace(raw_url, aff_link)
        parse_mode = None  # Mantém o formato original enviado pelo usuário

    # 3. OPÇÃO LINK: Busca dados via API e gera cópia persuasiva do produto
    elif choice == "type_link":
        item_id = extract_item_id(full_url) or extract_item_id(raw_url)
        offer = api.get_product_offer(item_id) if item_id else None

        if offer:
            aff_link = offer.get("offerLink") or api.generate_short_link(full_url)
            formatted_msg = format_product_message(offer, aff_link)
            parse_mode = "Markdown"  # Ativa o Markdown para renderizar os asteriscos em negrito
        else:
            # Fallback se não achar o produto via API
            aff_link = api.generate_short_link(full_url) or api.generate_short_link(raw_url)
            formatted_msg = raw_text.replace(raw_url, aff_link)
            parse_mode = None

    # Botão para envio direto ao WhatsApp
    whatsapp_share_url = f"https://api.whatsapp.com/send?text={urllib.parse.quote(formatted_msg)}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Compartilhar no WhatsApp", url=whatsapp_share_url)]
    ])

    await query.message.reply_text(
        formatted_msg,
        parse_mode=parse_mode,
        reply_markup=keyboard
    )

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()
    
    # Handlers para mensagens e para os botões do menu
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if hostname:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, webhook_url=f"https://{hostname}/{TOKEN}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()