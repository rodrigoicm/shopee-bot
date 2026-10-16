import os
import re
import html
import json
import time
import hashlib
import logging
import asyncio
import sys
import requests
from typing import List
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

from database import init_db, add_to_queue
from shopee_service import ShopeeAffiliateAPI as ServiceShopeeAPI

load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]
cuiaba_tz = timezone(timedelta(hours=-4))
init_db()

# ---------- CONFIGURAÇÕES DA EVOLUTION API LOCAL E GRUPOS ----------
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip(
    "/"
)
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "chip_01")

WHATSAPP_GROUP_PRINCIPAL = os.environ.get(
    "WHATSAPP_GROUP_PRINCIPAL", "120363421300618666@g.us"
)
WHATSAPP_GROUP_PARCERIA = os.environ.get(
    "WHATSAPP_GROUP_PARCERIA", "556596440424-1585878265@g.us"
)

ML_DEFAULT_CUPON_LINK = "https://mercadolivre.com/sec/2YCiHoZ"

CONVITE_GRUPO = ""

SHOPEE_URL_PATTERN = (
    r"https?://(?:[a-zA-Z0-9-]+\.)?(?:shopee\.com\.br|shope\.ee|br\.shp\.ee)/[^\s]+"
)
ML_URL_PATTERN = r"https?://(?:[a-zA-Z0-9-]+\.)?(?:mercadolivre\.com\.br|mercadolivre\.com|mercadolibre\.com\.br|mercadolibre\.com|ml\.br|meli\.la)/[^\s]+"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------- ESTADO GLOBAL DO MERCADO LIVRE ----------
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
            "⚠️ ML_COOKIES_JSON não configurado — conversão de links do Mercado Livre ficará indisponível."
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


# ---------- CONVERSÃO DE HTML PARA O WHATSAPP ----------
def convert_html_to_whatsapp(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


# ---------- FUNÇÕES DE ENVIO PARA O WHATSAPP ----------
async def send_whatsapp_offer(
    group_id: str,
    message: str,
    media_url: str | None = None,
) -> bool:
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        logger.error("❌ Credenciais da Evolution API não configuradas.")
        return False

    final_message = convert_html_to_whatsapp(message)

    try:
        loop = asyncio.get_running_loop()

        def _request():
            headers = {"apikey": EVOLUTION_API_KEY}

            if media_url:
                try:
                    media_resp = requests.get(media_url, timeout=20)
                    if media_resp.status_code == 200:
                        url_api = f"{EVOLUTION_API_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
                        files = {
                            "file": (
                                "oferta_qualidade.jpg",
                                media_resp.content,
                                "image/jpeg",
                            )
                        }
                        data = {
                            "number": group_id,
                            "mediatype": "image",
                            "caption": final_message,
                            "fileName": "oferta_qualidade.jpg",
                        }

                        res = requests.post(
                            url_api, data=data, files=files, headers=headers, timeout=40
                        )
                        if res.status_code in [200, 201]:
                            return res
                        else:
                            logger.warning(
                                f"⚠️ Falha no envio de mídia ({res.text}), tentando fallback para texto..."
                            )
                except Exception as e:
                    logger.warning(f"⚠️ Erro ao baixar mídia para envio: {e}")

            url_api = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
            payload = {"number": group_id, "text": final_message, "delay": 1200}
            headers["Content-Type"] = "application/json"
            return requests.post(url_api, json=payload, headers=headers, timeout=15)

        response = await loop.run_in_executor(None, _request)
        if response.status_code in [200, 201]:
            logger.info(f"✅ Oferta enviada com sucesso para o WhatsApp ({group_id})")
            return True
        else:
            logger.error(f"❌ Erro ao enviar para o WhatsApp: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Exceção ao enviar mensagem para o WhatsApp: {e}")
        return False


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


def resolve_short_link(short_url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        session = requests.Session()
        resp = session.get(short_url, headers=headers, allow_redirects=True, timeout=12)
        final_url = resp.url

        if "/social/" in final_url:
            decoded_html = html.unescape(resp.text)
            match = (
                re.search(
                    r'https?://(?:www\.)?mercadolivre\.com\.br/p/[^\s"\'<>]+',
                    decoded_html,
                )
                or re.search(
                    r'https?://produto\.mercadolivre\.com\.br/[^\s"\'<>]+', decoded_html
                )
                or re.search(
                    r'https?://[^\s"\'<>]*mercadolivre[^\s"\'<>]*MLB-[^\s"\'<>]+',
                    decoded_html,
                )
            )

            if match:
                extracted_url = match.group(0)
                logger.info(f"🎯 Link extraído do HTML: {extracted_url}")
                return extracted_url

        return final_url
    except Exception as e:
        logger.error(f"Erro ao expandir link via HTTP: {e}")
        return short_url


def format_price(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_ml_price_line(
    current_price: float | None, original_price: float | None
) -> str:
    if not current_price:
        return ""
    if original_price and original_price > current_price:
        return (
            f"💰 <b>Preço:</b> de {format_price(original_price)} "
            f"por <b>{format_price(current_price)}</b>\n\n"
        )
    return f"💰 <b>Preço:</b> <b>{format_price(current_price)}</b>\n\n"


async def resolve_product_url(page, url: str) -> str | None:
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2.5)

        target_href = await page.evaluate("""() => {
            const linkEl = document.querySelector('a.poly-component__link, a[class*="poly-component__link"]');
            if (linkEl && linkEl.href) {
                return linkEl.href;
            }

            const allLinks = Array.from(document.querySelectorAll('a'));
            const found = allLinks.find(el => el.innerText && (el.innerText.includes('Ir para produto') || el.innerText.includes('Ir para o produto')));
            if (found && found.href) {
                return found.href;
            }

            return null;
        }""")

        if target_href:
            resolved = target_href.split("?")[0].split("#")[0]
            return resolved

        return page.url.split("?")[0].split("#")[0]

    except Exception as e:
        logger.error(f"Erro ao resolver URL {url}: {e}")
        return None


# ---------- PLAYWRIGHT: MERCADO LIVRE AFILIADOS ----------
async def convert_mercadolivre_link(product_url: str) -> dict | None:
    if ml_state.browser is None or ml_state.cookies_dict is None:
        logger.error("❌ Browser ou cookies do Mercado Livre não inicializados.")
        return None

    async with ml_state.lock:
        ml_url_match = re.search(ML_URL_PATTERN, product_url)
        clean_url = (
            ml_url_match.group(0).rstrip(".,;:!?)\n ")
            if ml_url_match
            else product_url.strip()
        )

        context = None
        try:
            context = await ml_state.browser.new_context(
                storage_state=ml_state.cookies_dict,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = await context.new_page()

            product_url_final = await resolve_product_url(page, clean_url)
            if not product_url_final:
                return None

            product_info = await page.evaluate(r"""() => {
                let title = "";
                const h1 = document.querySelector('h1.ui-pdp-title, h1.poly-component__title, .ui-pdp-header__title-container h1, h1');
                if (h1 && h1.innerText) {
                    title = h1.innerText.trim();
                } else {
                    const ogTitle = document.querySelector('meta[property="og:title"]');
                    if (ogTitle && ogTitle.content) title = ogTitle.content.trim();
                }

                let imageUrl = null;
                const ogImage = document.querySelector('meta[property="og:image"]');
                if (ogImage && ogImage.content) {
                    imageUrl = ogImage.content;
                } else {
                    const img = document.querySelector('img.ui-pdp-image, img[data-zoom]');
                    if (img) imageUrl = img.src;
                }

                function parseAndesAmount(el) {
                    if (!el) return null;
                    const fractionEl = el.querySelector('.andes-money-amount__fraction');
                    if (!fractionEl) return null;
                    const centsEl = el.querySelector('.andes-money-amount__cents');
                    const fraction = fractionEl.innerText.replace(/\D/g, '');
                    const cents = centsEl ? centsEl.innerText.replace(/\D/g, '') : '00';
                    const val = parseFloat(fraction + '.' + cents);
                    return isNaN(val) ? null : val;
                }

                let originalPrice = null;
                let currentPrice = null;

                const origEl = document.querySelector(
                    '.ui-pdp-price__original-value .andes-money-amount, ' +
                    's.ui-pdp-price__part .andes-money-amount, ' +
                    '.andes-money-amount--previous, ' +
                    's .andes-money-amount, ' +
                    'del .andes-money-amount'
                );
                if (origEl) originalPrice = parseAndesAmount(origEl);

                const currEl = document.querySelector(
                    '.ui-pdp-price__second-line .andes-money-amount, ' +
                    '.ui-pdp-price__part--medium .andes-money-amount, ' +
                    '.ui-pdp-price__price .andes-money-amount, ' +
                    '.ui-pdp-price .andes-money-amount:not(.andes-money-amount--previous), ' +
                    '.andes-money-amount:not(.andes-money-amount--previous)'
                );
                if (currEl) currentPrice = parseAndesAmount(currEl);

                if (!currentPrice) {
                    const metaPrice = document.querySelector('meta[property="product:price:amount"]');
                    if (metaPrice && metaPrice.content) {
                        const p = parseFloat(metaPrice.content);
                        if (!isNaN(p)) currentPrice = p;
                    }
                }

                let rating = null;
                let reviewsCount = null;
                const ratingSelectors = [
                    '.ui-review-capability__rating__average',
                    '.ui-review-capability__rating__average--desktop',
                    '.ui-pdp-reviews__rating .ui-pdp-review__rating',
                    '.poly-reviews__rating .poly-reviews__rating__average',
                    '.andes-pagination__button .ui-pdp-review__rating',
                    '.ui-pdp-review__rating--value'
                ];
                for (const sel of ratingSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.innerText.trim().replace(',', '.');
                        const match = text.match(/^([\d.]+)/);
                        if (match) {
                            rating = parseFloat(match[1]);
                            break;
                        }
                    }
                }

                const reviewsSelectors = [
                    '.ui-review-capability__rating__label',
                    '.ui-pdp-reviews__amount',
                    '.poly-reviews__total',
                    '.ui-pdp-review__amount'
                ];
                for (const sel of reviewsSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const digits = el.innerText.trim().replace(/\D/g, '');
                        if (digits) {
                            reviewsCount = parseInt(digits, 10);
                            break;
                        }
                    }
                }

                let sales = null;
                const salesSelectors = [
                    '.ui-pdp-sold-stats__quantity',
                    '.ui-pdp-subtitle__sold',
                    '.poly-component__sold',
                    '.ui-pdp-sold-stats__quantity span'
                ];
                for (const sel of salesSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const digits = el.innerText.trim().replace(/\D/g, '');
                        if (digits) {
                            sales = parseInt(digits, 10);
                            break;
                        }
                    }
                }

                return {
                    title: title || null,
                    image_url: imageUrl || null,
                    current_price: currentPrice,
                    original_price: originalPrice,
                    rating: rating || null,
                    reviews_count: reviewsCount || null,
                    sales: sales || null
                };
            }""")

            await page.goto(
                "https://www.mercadolivre.com.br/afiliados/linkbuilder",
                timeout=25000,
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(2.0)

            if "login" in page.url or "navigation" in page.url:
                logger.error("❌ Sessão expirada! Redirecionado para login.")
                return None

            reset_btn = (
                page.locator("main button")
                .filter(has_text=re.compile(r"Criar outro|Gerar outro|Limpar", re.I))
                .first
            )
            if await reset_btn.is_visible(timeout=2000):
                await reset_btn.click()
                await asyncio.sleep(1.0)

            input_selector = "textarea[name*='link'], textarea[id*='link'], textarea.andes-form-control__field, input[placeholder*='http'], textarea[placeholder*='http']"
            input_el = page.locator(input_selector).first
            if not await input_el.is_visible(timeout=5000):
                input_selector = "main textarea, main input[type='text']"
                input_el = page.locator(input_selector).first
            await input_el.wait_for(state="visible", timeout=10000)

            await input_el.click()
            await asyncio.sleep(0.5)
            await page.evaluate(
                "(text) => navigator.clipboard.writeText(text)", product_url_final
            )
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            await page.keyboard.press(f"{modifier}+v")
            await asyncio.sleep(1.5)
            await page.dispatch_event(input_selector, "input")
            await page.dispatch_event(input_selector, "change")
            await page.keyboard.press("Tab")
            await asyncio.sleep(2.0)

            btn = (
                page.locator("main button")
                .filter(has_text=re.compile(r"^Gerar$", re.I))
                .first
            )
            try:
                await page.wait_for_selector(
                    "main button:not([disabled])", timeout=10000
                )
            except Exception:
                pass
            await btn.click()
            await asyncio.sleep(2.5)

            copy_btn = (
                page.locator("main button")
                .filter(has_text=re.compile(r"Copiar", re.I))
                .first
            )
            try:
                await copy_btn.wait_for(state="visible", timeout=10000)
            except Exception:
                await asyncio.sleep(2.0)

            affiliate_url = await page.evaluate(r"""() => {
                const inputs = Array.from(document.querySelectorAll('main input, main textarea, input, textarea'));
                for (const input of inputs) {
                    if (input.value && (input.value.includes('meli.la') || input.value.includes('mercadolivre.com.br/sec/'))) {
                        return input.value;
                    }
                }
                const bodyText = document.body.innerText;
                const match = bodyText.match(/https?:\/\/(?:meli\.la|www\.mercadolivre\.com\.br\/sec\/)[^\s"']+/);
                if (match) return match[0];
                return null;
            }""")

            if affiliate_url and "http" in affiliate_url:
                return {
                    "affiliate_url": affiliate_url.strip(),
                    "image_url": (
                        product_info.get("image_url") if product_info else None
                    ),
                    "title": product_info.get("title") if product_info else None,
                    "current_price": (
                        product_info.get("current_price") if product_info else None
                    ),
                    "original_price": (
                        product_info.get("original_price") if product_info else None
                    ),
                    "rating": product_info.get("rating") if product_info else None,
                    "reviews_count": (
                        product_info.get("reviews_count") if product_info else None
                    ),
                    "sales": product_info.get("sales") if product_info else None,
                }

            return None

        except Exception as e:
            logger.error(f"❌ Erro ao converter link do Mercado Livre: {e}")
            return None
        finally:
            if context is not None:
                await context.close()


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
        safe_origin_url = json.dumps(origin_url)
        query = f"mutation {{ generateShortLink(input: {{originUrl: {safe_origin_url}}}) {{ shortLink }} }}"
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

    match_product = re.search(r"/product/\d+/(\d+)", url) or re.search(
        r"/sp/product/(\d+)", url
    )
    if match_product:
        return int(match_product.group(1))

    match_end_id = re.search(r"/(\d+)(?:\?|$)", url)
    if match_end_id:
        return int(match_end_id.group(1))

    return None


def generate_persuasive_copy(offer: dict, store_name: str = "Shopee") -> str:
    discount = offer.get("priceDiscountRate")
    rating = offer.get("ratingStar")
    sales = offer.get("sales")

    lines = ["💡 <b>POR QUE VALE A PENA?</b>"]

    if discount and discount > 0:
        lines.append(f"🔥 <b>{discount}% OFF</b> aplicado no item!\n")
    if sales and sales > 10:
        lines.append(f"📦 Mais de <b>{sales:,} unidades vendidas</b>!")
    if rating and float(rating) >= 4.0:
        lines.append(f"⭐ Avaliação: <b>{rating}/5.0</b>")

    lines.append(f"\n🏪 Loja: <b>{escape_html(store_name)}</b>")
    lines.append(
        "\n\n🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>"
    )
    lines.append(
        "\n⚠️ <b>Atenção:</b> O estoque e o preço promocional podem mudar a qualquer momento."
    )
    lines.append("\n👇 <b>Clique no link abaixo para garantir o seu:</b>")

    return "\n".join(lines)


def format_product_message(offer: dict, link: str, store_name: str = "Shopee") -> str:
    name = escape_html(offer.get("productName", "Produto Incrível"))
    price_min = offer.get("priceMin")
    price_max = offer.get("priceMax")

    msg = f"🔥 <b>{name}</b>\n\n"

    if price_min:
        p_min = float(price_min)
        p_max = float(price_max) if price_max else p_min
        if p_min != p_max and p_max > 0:
            msg += (
                f"💰 <b>Preço:</b> R$ {p_min:,.2f} - R$ {p_max:,.2f}\n\n".replace(
                    ",", "X"
                )
                .replace(".", ",")
                .replace("X", ".")
            )
        else:
            msg += (
                f"💰 <b>Preço:</b> R$ {p_min:,.2f}\n\n".replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )

    msg += generate_persuasive_copy(offer, store_name=store_name)
    msg += f"\n👉 {link}"

    return msg


# ---------- COMANDO /gerar PARA LISTA AUTOMÁTICA SHOPEE ----------
async def handle_gerar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = os.environ.get("SHOPEE_APP_ID")
    app_secret = os.environ.get("SHOPEE_APP_SECRET")
    if not app_id or not app_secret:
        await update.message.reply_text(
            "❌ Credenciais da API da Shopee não configuradas."
        )
        return

    count = 15
    if context.args and context.args[0].isdigit():
        count = max(1, min(50, int(context.args[0])))

    today_str = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")
    status_msg = await update.message.reply_text(
        f"🎲 Buscando e embaralhando <b>{count} ofertas (TOP_PERFORMING)</b> na API da Shopee...",
        parse_mode="HTML",
    )

    loop = asyncio.get_running_loop()
    service_api = ServiceShopeeAPI(app_id, app_secret)
    added = await loop.run_in_executor(
        None, service_api.generate_auto_offers_to_queue, count, today_str
    )

    await status_msg.edit_text(
        f"✅ <b>{added} novas ofertas da Shopee</b> foram geradas de forma aleatória (sem repetir os últimos 2 dias) e inseridas direto na Fila do Portal para hoje (<b>{today_str}</b>)!",
        parse_mode="HTML",
    )


# ---------- HANDLERS ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_warn_cookie_expiration(update)

    raw_text = update.message.text or update.message.caption or ""

    shopee_links = re.findall(SHOPEE_URL_PATTERN, raw_text)
    ml_match = re.search(ML_URL_PATTERN, raw_text)

    if not shopee_links and not ml_match:
        await update.message.reply_text(
            "👋 Nenhum link válido da Shopee ou do Mercado Livre foi encontrado na mensagem.\n"
            "💡 Dica: Use /gerar para adicionar 15 ofertas automáticas da Shopee na fila!"
        )
        return

    cleaned_text = remove_affiliate_disclaimers(clean_corrupted_text(raw_text))

    if ml_match and not shopee_links:
        all_ml_links = re.findall(ML_URL_PATTERN, raw_text)
        all_ml_links = [re.sub(r"[.,;:!?)\n]+$", "", link) for link in all_ml_links]
        if len(all_ml_links) > 1:
            context.user_data["ml_links"] = all_ml_links
            context.user_data["multiple_ml"] = True
        else:
            context.user_data["multiple_ml"] = False
    else:
        context.user_data["multiple_ml"] = False

    if shopee_links:
        context.user_data["platform"] = "shopee"
        context.user_data["shopee_links"] = [
            l.rstrip(".,;:!?)\n") for l in shopee_links
        ]
        context.user_data["raw_url"] = context.user_data["shopee_links"][-1]
    else:
        context.user_data["platform"] = "mercadolivre"
        context.user_data["raw_url"] = ml_match.group(0).rstrip(".,;:!?)\n")

    context.user_data["raw_text"] = cleaned_text

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎟️ CUPOM", callback_data="type_cupom")],
            [InlineKeyboardButton("🏷️ OFERTA", callback_data="type_oferta")],
            [InlineKeyboardButton("🔗 LINK", callback_data="type_link")],
        ]
    )

    platform_name = (
        "Shopee" if context.user_data["platform"] == "shopee" else "Mercado Livre"
    )

    await update.message.reply_text(
        f"⚙️ <b>Link do {platform_name} identificado!</b>\n\n"
        "Qual o tipo de mensagem que você deseja formatar?\n\n"
        "• <b>CUPOM:</b> Mantém o texto idêntico e converte o link do cupom.\n"
        "• <b>OFERTA:</b> Mantém o texto idêntico e converte o link da oferta.\n"
        "• <b>LINK:</b> Gera uma nova cópia completa via API/Plataforma.",
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

    await query.edit_message_text("⏳ Gerando seu link de afiliado...")
    clean_base_text = remove_affiliate_disclaimers(raw_text)

    media_url = None
    aff_link = None
    formatted_msg = ""

    # ================= MERCADO LIVRE =================
    if platform == "mercadolivre":
        if choice == "type_cupom":
            aff_link = ML_DEFAULT_CUPON_LINK

            if clean_base_text.strip() == raw_url.strip():
                formatted_msg = (
                    "🎟️ <b>CUPOM DE DESCONTO</b>\n\n"
                    f"Resgate o seu cupom no link abaixo:\n👉 {aff_link}"
                )
            else:
                formatted_msg = escape_html(clean_base_text).replace(
                    escape_html(raw_url), aff_link
                )

        elif multiple_ml:
            ml_links = context.user_data.get("ml_links", [])
            if not ml_links:
                await query.message.reply_text(
                    "⚠️ Nenhum link encontrado para processar."
                )
                return

            aff_links = []
            for original_link in ml_links:
                result = await enqueue_ml_conversion(original_link)
                if result and result.get("affiliate_url"):
                    aff_links.append(result["affiliate_url"])
                else:
                    aff_links.append(original_link)

            new_text = escape_html(clean_base_text)
            for original, new in zip(ml_links, aff_links):
                new_text = new_text.replace(escape_html(original), new)

            free_notice = (
                "\n\n🚚 <b>Aproveite o frete grátis e benefícios do Mercado Livre!</b>"
            )
            if "frete grátis" not in new_text.lower():
                new_text += free_notice
            formatted_msg = clean_corrupted_text(new_text)

            context.user_data["last_formatted_msg"] = formatted_msg
            context.user_data["last_media_url"] = None
            context.user_data["last_store"] = "Mercado Livre"
            context.user_data["last_product_url"] = raw_url

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
                            "📢 Enviar para Todos os Grupos",
                            callback_data="send_group_all",
                        )
                    ],
                ]
            )
            await query.message.reply_text(
                formatted_msg, reply_markup=keyboard, parse_mode="HTML"
            )
            return

        else:
            ml_result = await enqueue_ml_conversion(raw_url)

            if not ml_result:
                await query.message.reply_text(
                    "❌ Não foi possível gerar o link de afiliado do Mercado Livre. Verifique se a sessão de login está válida."
                )
                return

            title = escape_html(ml_result.get("title", ""))
            current_price = ml_result.get("current_price")
            original_price = ml_result.get("original_price")
            aff_link = ml_result.get("affiliate_url")
            media_url = ml_result.get("image_url")
            rating = ml_result.get("rating")
            reviews_count = ml_result.get("reviews_count")
            sales = ml_result.get("sales")

            free_shipping_notice = (
                "🚚 <b>Aproveite o frete grátis e benefícios do Mercado Livre!</b>"
            )
            price_line = format_ml_price_line(current_price, original_price)

            trust_line = ""
            if rating is not None:
                trust_line += f"⭐ <b>{rating:.1f}</b>"
                if reviews_count is not None:
                    trust_line += f" ({reviews_count} avaliações)"
                trust_line += "\n"
            if sales is not None:
                trust_line += f"📦 <b>{sales:,}</b> unidades vendidas\n"
            if trust_line:
                trust_line += "\n"

            seller_line = "🏪 Loja: <b>Mercado Livre</b>\n\n"

            if choice == "type_link":
                header_title = (
                    f"🔥 <b>{title}</b>\n\n"
                    if title
                    else "🔥 <b>PRODUTO EM DESTAQUE NO MERCADO LIVRE</b>\n\n"
                )
                formatted_msg = (
                    f"{header_title}"
                    f"{price_line}"
                    f"{trust_line}"
                    f"{seller_line}"
                    f"{free_shipping_notice}\n\n"
                    f"👇 <b>Clique no link abaixo para garantir o seu:</b>\n👉 {aff_link}"
                )
            else:
                if clean_base_text.strip() == raw_url.strip():
                    header_title = (
                        f"🔥 <b>{title}</b>\n\n"
                        if title
                        else "🏷 <b>PROMOÇÃO IMPERDÍVEL MERCADO LIVRE</b>\n\n"
                    )
                    formatted_msg = (
                        f"{header_title}"
                        f"{price_line}"
                        f"{trust_line}"
                        f"{seller_line}"
                        f"{free_shipping_notice}\n\n"
                        f"👇 <b>Aproveite no link abaixo:</b>\n👉 {aff_link}"
                    )
                else:
                    converted_text = escape_html(clean_base_text).replace(
                        escape_html(raw_url), aff_link
                    )
                    formatted_msg = f"{converted_text}\n\n{free_shipping_notice}"

    # ================= SHOPEE =================
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

        offer = None
        product_link = raw_url

        for link in shopee_links:
            full_url = resolve_short_link(link)
            item_id = extract_item_id(full_url) or extract_item_id(link)
            if item_id:
                temp_offer = api.get_product_offer(item_id)
                if temp_offer and temp_offer.get("imageUrl"):
                    offer = temp_offer
                    product_link = link
                    break

        image_url = offer.get("imageUrl") if offer else None
        media_url = image_url

        if choice == "type_cupom":
            aff_link = api.generate_short_link(product_link) or api.generate_short_link(
                resolve_short_link(product_link)
            )
            if not aff_link:
                await query.message.reply_text("❌ Erro ao converter o link de cupom.")
                return

            if clean_base_text.strip() == raw_url.strip():
                formatted_msg = (
                    "🎟 <b>CUPOM DE DESCONTO SHOPEE</b>\n\n"
                    f"Resgate o seu cupom no link abaixo:\n👉 {aff_link}"
                )
            else:
                formatted_msg = escape_html(clean_base_text).replace(
                    escape_html(raw_url), aff_link
                )

        elif choice == "type_oferta":
            full_prod_url = resolve_short_link(product_link)
            aff_link = api.generate_short_link(
                full_prod_url
            ) or api.generate_short_link(product_link)
            if not aff_link:
                await query.message.reply_text("❌ Erro ao converter o link de oferta.")
                return

            free_shipping_notice = "🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>"

            if clean_base_text.strip() == raw_url.strip():
                formatted_msg = (
                    "🏷 <b>OFERTA IMPERDÍVEL SHOPEE</b>\n\n"
                    f"{free_shipping_notice}\n\n"
                    f"Aproveite a promoção no link abaixo:\n👉 {aff_link}"
                )
            else:
                converted_text = escape_html(clean_base_text)
                for slink in shopee_links:
                    converted_text = converted_text.replace(
                        escape_html(slink), aff_link
                    )
                formatted_msg = f"{converted_text}\n\n{free_shipping_notice}"

        elif choice == "type_link":
            if offer:
                aff_link = offer.get("offerLink") or api.generate_short_link(
                    resolve_short_link(product_link)
                )
                formatted_msg = format_product_message(offer, aff_link)
            else:
                full_prod_url = resolve_short_link(product_link)
                aff_link = api.generate_short_link(
                    full_prod_url
                ) or api.generate_short_link(product_link)
                formatted_msg = (
                    "🔥 <b>PRODUTO EM DESTAQUE NA SHOPEE</b>\n\n"
                    "🏪 Loja: <b>Shopee</b>\n\n"
                    "🚚 <b>Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!</b>\n\n"
                    f"Confira as melhores condições de compra no link:\n👉 {aff_link}"
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
        today_str = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

        add_to_queue(
            store=store_name,
            product_url=prod_url,
            message=message_text,
            media_url=media_url,
            priority=0,
            target_date=today_str,
        )
        await query.message.reply_text(
            f"📥 <b>Adicionado com sucesso à Fila do Portal</b> para hoje (<b>{today_str}</b>)!",
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
    headless = False
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
        f"🚀 Browser do Mercado Livre iniciado (headless={headless}) e worker da fila em execução."
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

    logger.info("🛑 Browser do Mercado Livre encerrado.")


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
