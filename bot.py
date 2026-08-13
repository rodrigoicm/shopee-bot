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
import urllib.parse
from typing import List  # adicione entre os imports
from dataclasses import dataclass, field
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Playwright

load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]

# Regexes para identificar links da Shopee e do Mercado Livre
SHOPEE_URL_PATTERN = (
    r"https?://(?:[a-zA-Z0-9-]+\.)?(?:shopee\.com\.br|shope\.ee)/[^\s]+"
)
ML_URL_PATTERN = r"https?://(?:[a-zA-Z0-9-]+\.)?(?:mercadolivre\.com\.br|mercadolivre\.com|mercadolibre\.com\.br|mercadolibre\.com|ml\.br|meli\.la)/[^\s]+"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------- ESTADO GLOBAL DO MERCADO LIVRE (browser reaproveitado + cookies em memória) ----------
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
    """Lê o storage_state do Playwright e retorna a data de expiração mais próxima
    entre os cookies com validade definida (ignora cookies de sessão sem 'expires')."""
    expirations = [
        c["expires"]
        for c in cookies_dict.get("cookies", [])
        if c.get("expires") and c["expires"] > 0
    ]
    if not expirations:
        return None
    return datetime.fromtimestamp(min(expirations))


def load_ml_cookies() -> None:
    """Carrega o storage_state do Mercado Livre a partir da variável de ambiente
    ML_COOKIES_JSON, mantendo tudo em memória (nunca grava em disco)."""
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
    """Envia um aviso, no máximo uma vez por dia, quando o cookie do ML está perto
    de vencer (ou já venceu). Disparado na primeira interação do dia com o bot."""
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
            f"⚠️ *Atenção:* o cookie de sessão do Mercado Livre vence em "
            f"*{ml_state.cookies_expiration:%d/%m/%Y}* (em {days_left} dia(s)). "
            f"Providencie a renovação em breve."
        )
    else:
        aviso = (
            f"🚨 *O cookie de sessão do Mercado Livre já venceu* "
            f"(vencimento em {ml_state.cookies_expiration:%d/%m/%Y}). "
            f"As conversões de link do Mercado Livre podem falhar até a renovação."
        )

    await update.message.reply_text(aviso, parse_mode="Markdown")


# ---------- FUNÇÕES DE LIMPEZA E TRATAMENTO DE TEXTO ----------
def clean_corrupted_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\ufffd", "%")
    cleaned = re.sub(r"%+", "%", cleaned)
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

    cleaned_text = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned_text)
    return cleaned_text.strip()


def resolve_short_link(short_url: str) -> str:
    """Extrai a URL bruta do produto caso caia em redirecionamentos via HTTP estático."""
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
    """Formata um valor numérico para o padrão de moeda brasileiro (ex: R$ 59,75)."""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_ml_price_line(
    current_price: float | None, original_price: float | None
) -> str:
    """Gera a linha de preço formatada para as mensagens do Mercado Livre."""
    if not current_price:
        return ""
    if original_price and original_price > current_price:
        return f"💰 *Preço:* de {format_price(original_price)} por *{format_price(current_price)}*\n\n"
    return f"💰 *Preço:* *{format_price(current_price)}*\n\n"


async def resolve_product_url(page, url: str) -> str | None:
    """Navega para a URL e retorna a URL final do produto (sem parâmetros)."""
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

        if "/social/" in page.url or "social" in url or "meli.la" in url:
            target_href = await page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const href = a.href;
                    if (href.includes('produto.mercadolivre.com.br/MLB') || href.includes('/p/MLB')) {
                        return href;
                    }
                }
                return null;
            }""")

            if target_href:
                resolved = target_href.split("?")[0].split("#")[0]
                await page.goto(
                    target_href, timeout=30000, wait_until="domcontentloaded"
                )
                await asyncio.sleep(2.0)
                return resolved.split("?")[0].split("#")[0]

            go_to_btn = page.locator(
                "a[href*='produto.mercadolivre'], a[href*='/p/MLB'], a:has-text('Ir para o produto'), a:has-text('Ver produto')"
            ).first
            if await go_to_btn.is_visible(timeout=5000):
                await go_to_btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2.0)
                return page.url.split("?")[0].split("#")[0]

        return page.url.split("?")[0].split("#")[0]
    except Exception as e:
        logger.error(f"Erro ao resolver URL {url}: {e}")
        return None


# ---------- PLAYWRIGHT: MERCADO LIVRE AFILIADOS ----------
async def convert_mercadolivre_link(product_url: str) -> dict | None:
    """Converte um link do Mercado Livre em link de afiliado e extrai dados do produto.
    Reaproveita o browser global (ml_state.browser) e os cookies em memória."""
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

            # Resolver URL do produto
            product_url_final = await resolve_product_url(page, clean_url)
            if not product_url_final:
                logger.error("❌ Não foi possível resolver a URL do produto.")
                return None

            # A página já está na URL final do produto, podemos extrair dados
            # (O código de extração continua o mesmo, mas agora a página já está no produto)
            product_info = await page.evaluate("""() => {
                // Título
                let title = "";
                const h1 = document.querySelector('h1.ui-pdp-title, h1.poly-component__title, .ui-pdp-header__title-container h1, h1');
                if (h1 && h1.innerText) {
                    title = h1.innerText.trim();
                } else {
                    const ogTitle = document.querySelector('meta[property="og:title"]');
                    if (ogTitle && ogTitle.content) title = ogTitle.content.trim();
                }

                // Imagem
                let imageUrl = null;
                const ogImage = document.querySelector('meta[property="og:image"]');
                if (ogImage && ogImage.content) {
                    imageUrl = ogImage.content;
                } else {
                    const img = document.querySelector('img.ui-pdp-image, img[data-zoom]');
                    if (img) imageUrl = img.src;
                }

                // Parser de preços
                function parseAndesAmount(el) {
                    if (!el) return null;
                    const fractionEl = el.querySelector('.andes-money-amount__fraction');
                    if (!fractionEl) return null;
                    const centsEl = el.querySelector('.andes-money-amount__cents');
                    const fraction = fractionEl.innerText.replace(/\\D/g, '');
                    const cents = centsEl ? centsEl.innerText.replace(/\\D/g, '') : '00';
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

                // AVALIAÇÃO E VENDAS (com novos seletores)
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
                        const digits = el.innerText.trim().replace(/\\D/g, '');
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
                        const digits = el.innerText.trim().replace(/\\D/g, '');
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

            logger.info(f"🎯 URL Final do Produto: {product_url_final}")
            logger.info(
                f"📦 Título: {product_info.get('title') if product_info else 'N/A'}"
            )

            # AGORA GERAR O LINK DE AFILIADO (mesmo código de antes, mas usando product_url_final)
            logger.info("🌐 Navegando para o LinkBuilder do Mercado Livre...")
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

            logger.info("✍️ Preenchendo a URL no campo do LinkBuilder (Ctrl+V)...")
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
                logger.warning(
                    "⚠️ Botão não ativou via wait_for_selector. Tentando forçar..."
                )
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

            affiliate_url = await page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('main input, main textarea, input, textarea'));
                for (const input of inputs) {
                    if (input.value && (input.value.includes('meli.la') || input.value.includes('mercadolivre.com.br/sec/'))) {
                        return input.value;
                    }
                }
                const bodyText = document.body.innerText;
                const match = bodyText.match(/https?:\\/\\/(?:meli\\.la|www\\.mercadolivre\\.com\\.br\\/sec\\/)[^\\s"']+/);
                if (match) return match[0];
                return null;
            }""")

            if affiliate_url and "http" in affiliate_url:
                logger.info(f"✅ Link de Afiliado Gerado: {affiliate_url}")
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

            logger.error("⚠️ Não foi possível extrair a URL gerada.")
            return None

        except Exception as e:
            logger.error(
                f"❌ Erro ao converter link do Mercado Livre via Playwright: {e}"
            )
            return None
        finally:
            if context is not None:
                await context.close()


async def ml_worker() -> None:
    """Processa as conversões de link do Mercado Livre uma de cada vez,
    evitando disputa pelo mesmo browser/cookies e sobrecarga de memória."""
    while True:
        job = await ml_state.queue.get()
        product_url: str = job["product_url"]
        future: asyncio.Future = job["future"]
        try:
            result = await convert_mercadolivre_link(product_url)
            if not future.cancelled():
                future.set_result(result)
        except Exception as e:
            logger.error(f"❌ Erro no worker do Mercado Livre: {e}")
            if not future.cancelled():
                future.set_exception(e)
        finally:
            ml_state.queue.task_done()


async def convert_multiple_mercadolivre_links(urls: List[str]) -> List[str] | None:
    """Recebe uma lista de URLs do Mercado Livre e retorna uma lista de links de afiliado na mesma ordem."""
    if ml_state.browser is None or ml_state.cookies_dict is None:
        logger.error("❌ Browser ou cookies do Mercado Livre não inicializados.")
        return None

    async with ml_state.lock:
        context = None
        try:
            context = await ml_state.browser.new_context(
                storage_state=ml_state.cookies_dict,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = await context.new_page()

            # Resolver cada URL para a URL final do produto
            product_urls = []
            for url in urls:
                clean_url = (
                    re.search(ML_URL_PATTERN, url).group(0).rstrip(".,;:!?)\n ")
                    if re.search(ML_URL_PATTERN, url)
                    else url.strip()
                )
                product_final = await resolve_product_url(page, clean_url)
                if not product_final:
                    logger.warning(f"⚠️ Não foi possível resolver a URL: {clean_url}")
                    product_final = clean_url
                product_urls.append(product_final)

            # Navegar para o LinkBuilder
            logger.info("🌐 Navegando para o LinkBuilder do Mercado Livre...")
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

            # Colar todos os URLs finais, um por linha
            text_to_paste = "\n".join(product_urls)
            logger.info(f"✍️ Colando {len(product_urls)} URLs no LinkBuilder...")
            await input_el.click()
            await asyncio.sleep(0.5)
            await page.evaluate(
                "(text) => navigator.clipboard.writeText(text)", text_to_paste
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
                logger.warning(
                    "⚠️ Botão não ativou via wait_for_selector. Tentando forçar..."
                )
            await btn.click()
            await asyncio.sleep(3.0)  # tempo extra para múltiplos

            # Capturar todos os links gerados
            logger.info("⏳ Capturando links de afiliado gerados...")
            affiliate_links = await page.evaluate("""() => {
                const links = [];
                const inputs = Array.from(document.querySelectorAll('main input, main textarea, input, textarea'));
                for (const input of inputs) {
                    if (input.value && (input.value.includes('meli.la') || input.value.includes('mercadolivre.com.br/sec/'))) {
                        links.push(input.value.trim());
                    }
                }
                if (links.length === 0) {
                    const bodyText = document.body.innerText;
                    const matches = bodyText.match(/https?:\\/\\/(?:meli\\.la|www\\.mercadolivre\\.com\\.br\\/sec\\/)[^\\s"']+/g);
                    if (matches) {
                        for (const m of matches) {
                            links.push(m.trim());
                        }
                    }
                }
                return links;
            }""")

            if affiliate_links and len(affiliate_links) > 0:
                # Se o número de links gerados não bater, tentamos usar os que temos ou fallback
                if len(affiliate_links) != len(urls):
                    logger.warning(
                        f"⚠️ Número de links gerados ({len(affiliate_links)}) difere do esperado ({len(urls)}). Tentando reextrair..."
                    )
                    await asyncio.sleep(2.0)
                    affiliate_links = await page.evaluate("""() => {
                        const links = [];
                        const inputs = Array.from(document.querySelectorAll('main input, main textarea, input, textarea'));
                        for (const input of inputs) {
                            if (input.value && (input.value.includes('meli.la') || input.value.includes('mercadolivre.com.br/sec/'))) {
                                links.push(input.value.trim());
                            }
                        }
                        return links;
                    }""")
                # Se ainda assim não bater, retornamos os que temos (ou None)
                if len(affiliate_links) == len(urls):
                    return affiliate_links
                elif len(affiliate_links) > 0:
                    # Retorna o que temos, mesmo que quantidade diferente (pode ser que alguns não geraram)
                    return affiliate_links
                else:
                    return None

            logger.error("⚠️ Nenhum link de afiliado foi gerado.")
            return None

        except Exception as e:
            logger.error(f"❌ Erro no processamento múltiplo: {e}")
            return None
        finally:
            if context is not None:
                await context.close()


async def enqueue_ml_conversion(product_url: str) -> dict | None:
    """Coloca uma conversão de link do ML na fila e aguarda o resultado."""
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

    lines = ["💡 *POR QUE VALE A PENA?*"]

    if discount and discount > 0:
        lines.append(f"🔥 *{discount}% OFF* aplicado no item!\n")
    if sales and sales > 10:
        lines.append(f"📦 Mais de *{sales:,} unidades vendidas*!")
    if rating and float(rating) >= 4.0:
        lines.append(f"⭐ Avaliação: *{rating}/5.0*")

    lines.append(f"\n🏪 Loja: *{store_name}*")

    lines.append(
        "\n\n🚚 *Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!*"
    )
    lines.append(
        "\n⚠️ *Atenção:* O estoque e o preço promocional podem mudar a qualquer momento."
    )
    lines.append("\n👇 *Clique no link abaixo para garantir o seu:*")

    return "\n".join(lines)


def format_product_message(offer: dict, link: str, store_name: str = "Shopee") -> str:
    name = offer.get("productName", "Produto Incrível")
    price_min = offer.get("priceMin")
    price_max = offer.get("priceMax")

    msg = f"🔥 *{name}*\n\n"

    if price_min:
        p_min = float(price_min)
        p_max = float(price_max) if price_max else p_min
        if p_min != p_max and p_max > 0:
            msg += (
                f"💰 *Preço:* R$ {p_min:,.2f} - R$ {p_max:,.2f}\n\n".replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )
        else:
            msg += (
                f"💰 *Preço:* R$ {p_min:,.2f}\n\n".replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )

    msg += generate_persuasive_copy(offer, store_name=store_name)
    msg += f"\n👉 {link}"

    return msg


# ---------- HANDLERS ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await maybe_warn_cookie_expiration(update)

    raw_text = update.message.text or update.message.caption or ""

    shopee_match = re.search(SHOPEE_URL_PATTERN, raw_text)
    ml_match = re.search(ML_URL_PATTERN, raw_text)

    # Se não houver nenhum link, avisa e sai
    if not shopee_match and not ml_match:
        await update.message.reply_text(
            "👋 Nenhum link válido da Shopee ou do Mercado Livre foi encontrado na mensagem."
        )
        return

    cleaned_text = clean_corrupted_text(raw_text)

    # Detecta múltiplos links do Mercado Livre (apenas se houver match e não for Shopee)
    if ml_match and not shopee_match:
        # Encontra todos os links do ML na mensagem
        all_ml_links = re.findall(ML_URL_PATTERN, raw_text)
        # Remove pontuação final de cada link (mantendo a URL limpa)
        all_ml_links = [re.sub(r"[.,;:!?)\n]+$", "", link) for link in all_ml_links]
        if len(all_ml_links) > 1:
            context.user_data["ml_links"] = all_ml_links
            context.user_data["multiple_ml"] = True
        else:
            context.user_data["multiple_ml"] = False
    else:
        context.user_data["multiple_ml"] = False

    # Define plataforma e URL principal (apenas para fluxo único)
    if shopee_match:
        context.user_data["platform"] = "shopee"
        context.user_data["raw_url"] = shopee_match.group(0).rstrip(".,;:!?)\n")
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
        f"⚙️ *Link do {platform_name} identificado!*\n\n"
        "Qual o tipo de mensagem que você deseja formatar?\n\n"
        "• *CUPOM:* Mantém o texto idêntico e converte o link do cupom.\n"
        "• *OFERTA:* Mantém o texto idêntico e converte o link da oferta.\n"
        "• *LINK:* Gera uma nova cópia completa via API/Plataforma.",
        reply_markup=keyboard,
        parse_mode="Markdown",
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

    image_url = None
    aff_link = None
    formatted_msg = ""

    # ================= MERCADO LIVRE =================
    if platform == "mercadolivre":
        # --- PROCESSAMENTO MÚLTIPLO (link por link) ---
        if multiple_ml:
            ml_links = context.user_data.get("ml_links", [])
            if not ml_links:
                await query.message.reply_text(
                    "⚠️ Nenhum link encontrado para processar."
                )
                return

            # Processa cada link individualmente e coleta os resultados
            aff_links = []  # lista para guardar os links de afiliado na mesma ordem
            for original_link in ml_links:
                # Chama a conversão individual (usa a fila e o lock)
                result = await enqueue_ml_conversion(original_link)
                if result and result.get("affiliate_url"):
                    aff_links.append(result["affiliate_url"])
                else:
                    # Se falhar, mantém o link original (fallback)
                    logger.warning(f"⚠️ Falha ao converter link: {original_link}")
                    aff_links.append(original_link)  # fallback

            # Agora substitui cada link original pelo novo link (na ordem)
            new_text = clean_base_text
            for original, new in zip(ml_links, aff_links):
                new_text = new_text.replace(original, new)

            # Adiciona aviso de frete grátis se não existir
            free_notice = (
                "\n\n🚚 *Aproveite o frete grátis e benefícios do Mercado Livre!*"
            )
            if "frete grátis" not in new_text.lower():
                new_text += free_notice

            formatted_msg = clean_corrupted_text(new_text)

            # Envia a mensagem sem imagem
            encoded_text = urllib.parse.quote(formatted_msg, safe="")
            encoded_text = encoded_text.replace("%25", "%2525")
            whatsapp_url = f"https://wa.me/?text={encoded_text}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📲 Compartilhar no WhatsApp", url=whatsapp_url
                        )
                    ]
                ]
            )
            await query.message.reply_text(
                formatted_msg, reply_markup=keyboard, parse_mode="Markdown"
            )
            return  # finaliza aqui

        # --- PROCESSAMENTO ÚNICO (fluxo existente) ---
        ml_result = await enqueue_ml_conversion(raw_url)
        if not ml_result:
            await query.message.reply_text(
                "❌ Não foi possível gerar o link de afiliado do Mercado Livre. Verifique se a sessão de login está válida."
            )
            return

        title = ml_result.get("title")
        current_price = ml_result.get("current_price")
        original_price = ml_result.get("original_price")
        aff_link = ml_result.get("affiliate_url")
        image_url = ml_result.get("image_url")
        rating = ml_result.get("rating")
        reviews_count = ml_result.get("reviews_count")
        sales = ml_result.get("sales")

        free_shipping_notice = (
            "🚚 *Aproveite o frete grátis e benefícios do Mercado Livre!*"
        )
        price_line = format_ml_price_line(current_price, original_price)

        # Monta a linha de confiança (apenas se houver dados)
        trust_line = ""
        if rating is not None:
            trust_line += f"⭐ *{rating:.1f}*"
            if reviews_count is not None:
                trust_line += f" ({reviews_count} avaliações)"
            trust_line += "\n"
        if sales is not None:
            trust_line += f"📦 *{sales:,}* unidades vendidas\n"
        if trust_line:
            trust_line += "\n"

        seller_line = "🏪 Loja: *Mercado Livre*\n\n"

        # Formatação conforme escolha do usuário
        if choice == "type_link":
            header_title = (
                f"🔥 *{title}*\n\n"
                if title
                else "🔥 *PRODUTO EM DESTAQUE NO MERCADO LIVRE*\n\n"
            )
            formatted_msg = (
                f"{header_title}"
                f"{price_line}"
                f"{trust_line}"
                f"{seller_line}"
                f"{free_shipping_notice}\n\n"
                f"👇 *Clique no link abaixo para garantir o seu:*\n👉 {aff_link}"
            )
        else:  # type_cupom ou type_oferta
            if clean_base_text.strip() == raw_url.strip():
                header_title = (
                    f"🔥 *{title}*\n\n"
                    if title
                    else "🏷️ *PROMOÇÃO IMPERDÍVEL MERCADO LIVRE*\n\n"
                )
                formatted_msg = (
                    f"{header_title}"
                    f"{price_line}"
                    f"{trust_line}"
                    f"{seller_line}"
                    f"{free_shipping_notice}\n\n"
                    f"👇 *Aproveite no link abaixo:*\n👉 {aff_link}"
                )
            else:
                converted_text = clean_base_text.replace(raw_url, aff_link)
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
        full_url = resolve_short_link(raw_url)

        item_id = extract_item_id(full_url) or extract_item_id(raw_url)
        offer = api.get_product_offer(item_id) if item_id else None
        image_url = offer.get("imageUrl") if offer else None

        if choice == "type_cupom":
            aff_link = api.generate_short_link(raw_url) or api.generate_short_link(
                full_url
            )
            if not aff_link:
                await query.message.reply_text("❌ Erro ao converter o link de cupom.")
                return

            if clean_base_text.strip() == raw_url.strip():
                formatted_msg = f"🎟️ CUPOM DE DESCONTO SHOPEE\n\nResgate o seu cupom no link abaixo:\n👉 {aff_link}"
            else:
                formatted_msg = clean_base_text.replace(raw_url, aff_link)

        elif choice == "type_oferta":
            aff_link = api.generate_short_link(full_url) or api.generate_short_link(
                raw_url
            )
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
                aff_link = api.generate_short_link(full_url) or api.generate_short_link(
                    raw_url
                )
                formatted_msg = (
                    f"🔥 PRODUTO EM DESTAQUE NA SHOPEE\n\n"
                    f"🏪 Loja: *Shopee*\n\n"
                    f"🚚 *Aproveite os cupons de FRETE GRÁTIS aplicados na finalização do pedido!*\n\n"
                    f"Confira as melhores condições de compra no link:\n👉 {aff_link}"
                )

    # --- MONTAGEM DA URL DO WHATSAPP E ENVIO (comum a ambos) ---
    formatted_msg = clean_corrupted_text(formatted_msg)
    encoded_text = urllib.parse.quote(formatted_msg, safe="")
    encoded_text = encoded_text.replace("%25", "%2525")
    whatsapp_share_url = f"https://wa.me/?text={encoded_text}"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📲 Compartilhar no WhatsApp", url=whatsapp_share_url)]]
    )

    if image_url:
        try:
            await query.message.reply_photo(
                photo=image_url,
                caption=formatted_msg,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.error(f"Erro ao enviar foto: {e}")

    await query.message.reply_text(
        formatted_msg, reply_markup=keyboard, parse_mode="Markdown"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler global — captura qualquer exceção não tratada nos handlers acima."""
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


# ---------- CICLO DE VIDA DO BROWSER (startup/shutdown) ----------
async def on_startup(app: Application) -> None:
    load_ml_cookies()

    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

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
    if ml_state.browser:
        await ml_state.browser.close()
    if ml_state.playwright:
        await ml_state.playwright.stop()
    logger.info("🛑 Browser do Mercado Livre encerrado.")


# ---------- MAIN ----------
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
        )
    )
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if hostname:
        port = int(os.environ.get("PORT", 10000))
        logger.info(f"🚀 Iniciando Webhook na porta: {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"https://{hostname}/{TOKEN}",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
