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

# Regex abrangente para URLs da Shopee (inclui shopee.com.br, s.shopee.com.br, shope.ee, etc.)
URL_PATTERN = r"https?://(?:[a-zA-Z0-9-]+\.)?(?:shopee\.com\.br|shope\.ee)/[^\s]+"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- FUNÇÕES DE LIMPEZA E TRATAMENTO DE TEXTO ----------
def clean_corrupted_text(text: str) -> str:
    if not text:
        return ""
    # Remove apenas o caractere unicode de substituição (\ufffd) que costuma vir no lugar do %
    cleaned = text.replace("\ufffd", "%")
    # Ajusta possíveis duplicidades de porcentagem
    cleaned = re.sub(r'%+', '%', cleaned)
    return cleaned

def remove_affiliate_disclaimers(text: str) -> str:
    """Remove frases sobre apoio ao canal, café, transparência e hashtags de publi."""
    if not text:
        return ""

    patterns = [
        r"\*?\s*comprando (?:através|por meio) do link você (?:estará|está) ajudando[^\n]*",
        r"\*?\s*você me paga um café[^\n]*",
        r"\*?\s*ajude o canal[^\n]*",
        r"#publi\b[^\n]*",
        r"#ad\b[^\n]*",
        r"#publicidade\b[^\n]*",
    ]

    cleaned_text = text
    for pattern in patterns:
        cleaned_text = re.sub(pattern, "", cleaned_text, flags=re.IGNORECASE)

    # Limpa linhas vazias duplicadas deixadas pela remoção
    cleaned_text = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned_text)
    return cleaned_text.strip()

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
                    imageUrl
                }}
            }}
        }}"""
        data = self._graphql_request(query)
        nodes = data.get("productOfferV2", {}).get("nodes", [])
        if nodes:
            offer = nodes[0]
            if "productName" in offer and offer["productName"]:
                offer["productName"] = clean_corrupted_text(offer["productName"])
            return offer
        return None

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

    match_product = re.search(r"/product/\d+/(\d+)", url) or re.search(r"/sp/product/(\d+)", url)
    if match_product:
        return int(match_product.group(1))
        
    match_end_id = re.search(r"/(\d+)(?:\?|$)", url)
    if match_end_id:
        return int(match_end_id.group(1))

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

    lines = ["💡 *POR QUE VALE A PENA?*"]

    if discount and discount > 0:
        lines.append(f"🔥 *{discount}% OFF* aplicado no item!\n")
    if sales and sales > 10:
        lines.append(f"📦 Mais de *{sales:,} unidades vendidas*!")
    if rating and float(rating) >= 4.0:
        lines.append(f"⭐ Avaliação: *{rating}/5.0*")
    
    # Campo de loja fixo para Shopee
    lines.append("\n🏪 Loja: *Shopee*")

    lines.append("\n\n🚚 *Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!*")
    lines.append("\n⚠️ *Atenção:* O estoque e o preço promocional podem mudar a qualquer momento.")
    lines.append("\n👇 *Clique no link abaixo para garantir o seu:*")

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
            msg += f"💰 *Preço:* R$ {p_min:,.2f} - R$ {p_max:,.2f}\n\n".replace(",", "X").replace(".", ",").replace("X", ".")
        else:
            msg += f"💰 *Preço:* R$ {p_min:,.2f}\n\n".replace(",", "X").replace(".", ",").replace("X", ".")

    msg += generate_persuasive_copy(offer)
    msg += f"\n👉 {link}"

    return msg

# ---------- HANDLERS ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text or update.message.caption or ""

    # Procura pela URL antes de aplicar modificações no texto
    match = re.search(URL_PATTERN, raw_text)
    if not match:
        await update.message.reply_text("👋 Nenhum link da Shopee foi encontrado na mensagem enviada.")
        return

    # Limpa possíveis caracteres corrompidos do texto
    cleaned_text = clean_corrupted_text(raw_text)

    context.user_data["raw_text"] = cleaned_text
    context.user_data["raw_url"] = match.group(0).rstrip(".,;:!?)\n")

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
    raw_text = context.user_data.get("raw_text", "")
    raw_url = context.user_data.get("raw_url", "")

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

    item_id = extract_item_id(full_url) or extract_item_id(raw_url)
    offer = api.get_product_offer(item_id) if item_id else None
    image_url = offer.get("imageUrl") if offer else None

    # Aplica a remoção dos avisos de afiliado/publi/café no texto original
    clean_base_text = remove_affiliate_disclaimers(raw_text)

    if choice == "type_cupom":
        aff_link = api.generate_short_link(raw_url) or api.generate_short_link(full_url)
        if not aff_link:
            await query.message.reply_text("❌ Erro ao converter o link de cupom.")
            return
        
        if clean_base_text.strip() == raw_url.strip():
            formatted_msg = f"🎟️ CUPOM DE DESCONTO SHOPEE\n\nResgate o seu cupom no link abaixo:\n👉 {aff_link}"
        else:
            formatted_msg = clean_base_text.replace(raw_url, aff_link)

    elif choice == "type_oferta":
        aff_link = api.generate_short_link(full_url) or api.generate_short_link(raw_url)
        if not aff_link:
            await query.message.reply_text("❌ Erro ao converter o link de oferta.")
            return

        free_shipping_notice = "🚚 *Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!*"

        if clean_base_text.strip() == raw_url.strip():
            formatted_msg = f"🏷️ OFERTA IMPERDÍVEL SHOPEE\n\n{free_shipping_notice}\n\nAproveite a promoção no link abaixo:\n👉 {aff_link}"
        else:
            converted_text = clean_base_text.replace(raw_url, aff_link)
            formatted_msg = f"{converted_text}\n\n{free_shipping_notice}"

    elif choice == "type_link":
        if offer:
            aff_link = offer.get("offerLink") or api.generate_short_link(full_url)
            formatted_msg = format_product_message(offer, aff_link)
        else:
            aff_link = api.generate_short_link(full_url) or api.generate_short_link(raw_url)
            formatted_msg = (
                f"🔥 PRODUTO EM DESTAQUE NA SHOPEE\n\n"
                f"🏪 Loja: *Shopee*\n\n"
                f"🚚 *Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!*\n\n"
                f"Confira as melhores condições de compra no link:\n👉 {aff_link}"
            )

    # Sanitização do texto final
    formatted_msg = clean_corrupted_text(formatted_msg)

    # --- MONTAGEM SEGURA DA URL DO WHATSAPP ---
    # O asterisco (*) é mantido no texto para formatar o negrito na mensagem final do WhatsApp
    encoded_text = urllib.parse.quote(formatted_msg, safe="")
    encoded_text = encoded_text.replace("%25", "%2525")

    whatsapp_share_url = f"https://wa.me/?text={encoded_text}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Compartilhar no WhatsApp", url=whatsapp_share_url)]
    ])

    if image_url:
        try:
            await query.message.reply_photo(
                photo=image_url,
                caption=formatted_msg,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Erro ao enviar foto: {e}")

    await query.message.reply_text(
        formatted_msg,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if hostname:
        port = int(os.environ.get("PORT", 10000))
        logger.info(f"🚀 Iniciando Webhook na porta: {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"https://{hostname}/{TOKEN}"
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()