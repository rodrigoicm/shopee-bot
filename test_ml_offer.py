import asyncio
import random
import re
from playwright.async_api import async_playwright, Browser, BrowserContext
import ml_auto


class MLAnonymousScraperTest:
    """
    Classe isolada para testar raspagem anônima (sem cookie de login) no Mercado Livre
    usando Playwright em modo invisível (headless=True).
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )

        # Descobre o User-Agent real do seu Chrome e remove apenas "Headless"
        temp_ctx = await self.browser.new_context()
        temp_page = await temp_ctx.new_page()
        real_ua = await temp_page.evaluate("() => navigator.userAgent")
        clean_ua = real_ua.replace("HeadlessChrome", "Chrome")
        await temp_ctx.close()
        print(f"🧭 User-Agent usado: {clean_ua}")

        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="pt-BR",
            timezone_id="America/Cuiaba",
            user_agent=clean_ua,
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def _get_nordic_ctx(
        self, page, url: str, referer: str | None = None
    ) -> dict | None:
        resp = await page.goto(
            url, referer=referer, wait_until="domcontentloaded", timeout=25000
        )
        status = resp.status if resp else None
        await asyncio.sleep(random.uniform(2.0, 3.2))

        await page.mouse.wheel(0, 500)
        await asyncio.sleep(0.5)

        title = await page.title()
        html_text = await page.content()
        print(f"   🔎 status={status} | title={title!r} | len={len(html_text)}")

        ctx = await page.evaluate(
            "() => (window._n && window._n.ctx && window._n.ctx.r) ? window._n.ctx.r : null"
        )
        if isinstance(ctx, dict) and ctx:
            return ctx

        return ml_auto._extract_nordic_ctx(html_text)

    async def warmup(self, page) -> None:
        print("1. Warm-up na Home (gerando cookies de dispositivo anônimos)...")
        resp = await page.goto(
            "https://www.mercadolivre.com.br/",
            wait_until="domcontentloaded",
            timeout=25000,
        )
        await asyncio.sleep(random.uniform(2.0, 3.0))
        cookies = await self.context.cookies()
        title = await page.title()
        print(
            f"   ✅ Home | status={resp.status if resp else None} | title={title!r} | Cookies={len(cookies)}"
        )

    async def scrape_best_sellers(self, page) -> list[dict]:
        url = "https://www.mercadolivre.com.br/mais-vendidos"
        print(f"\n2. Raspando Mais Vendidos: {url}")
        ctx = await self._get_nordic_ctx(
            page, url, referer="https://www.mercadolivre.com.br/"
        )
        if not ctx:
            print(f"   ❌ _n.ctx.r não encontrado | URL final: {page.url}")
            return []

        print(f"   📄 mainEntry: {ctx.get('mainEntry')} | URL final: {page.url}")
        carousels = []
        ml_auto._find_carousels_recursively(ctx, carousels)

        items = []
        for car in carousels:
            cat_clean = (
                re.sub(
                    r"^Mais vendidos em\s+",
                    "",
                    car.get("title") or "",
                    flags=re.IGNORECASE,
                ).strip()
                or "Geral"
            )
            for card in car.get("polycards") or []:
                parsed = ml_auto._parse_polycard(
                    card, source_type="best_sellers", category_name=f"ML: {cat_clean}"
                )
                if parsed:
                    items.append(parsed)

        print(
            f"   ✅ Carrosséis: {len(carousels)} | Produtos válidos extraídos: {len(items)}"
        )
        return items

    async def scrape_offers(
        self, page, url: str, label: str, referer: str
    ) -> list[dict]:
        print(f"\n3. Raspando {label}: {url}")
        ctx = await self._get_nordic_ctx(page, url, referer=referer)
        if not ctx:
            print(f"   ❌ _n.ctx.r não encontrado | URL final: {page.url}")
            return []

        print(f"   📄 mainEntry: {ctx.get('mainEntry')} | URL final: {page.url}")
        raw_cards = []
        ml_auto._find_offer_cards_recursively(ctx, raw_cards)

        items = []
        seen = set()
        for card in raw_cards:
            parsed = ml_auto._parse_polycard(
                card, source_type="offers", category_name=label
            )
            if parsed and parsed["item_key"] not in seen:
                seen.add(parsed["item_key"])
                items.append(parsed)

        print(
            f"   ✅ Cards brutos: {len(raw_cards)} | Produtos válidos extraídos: {len(items)}"
        )
        return items


async def main():
    async with MLAnonymousScraperTest(headless=True) as scraper:
        page = await scraper.context.new_page()

        await scraper.warmup(page)

        # 1. Teste de paginação em Ofertas (pág 1, 2 e 3)
        off_p1 = await scraper.scrape_offers(
            page,
            url="https://www.mercadolivre.com.br/ofertas",
            label="ML: Ofertas P1",
            referer="https://www.mercadolivre.com.br/",
        )
        off_p2 = await scraper.scrape_offers(
            page,
            url="https://www.mercadolivre.com.br/ofertas?page=2",
            label="ML: Ofertas P2",
            referer="https://www.mercadolivre.com.br/ofertas",
        )
        off_p3 = await scraper.scrape_offers(
            page,
            url="https://www.mercadolivre.com.br/ofertas?page=3",
            label="ML: Ofertas P3",
            referer="https://www.mercadolivre.com.br/ofertas?page=2",
        )

        # 2. Teste de filtro por categoria em Ofertas (Ex: MLB1574 = Casa, Móveis e Decoração)
        cat_p1 = await scraper.scrape_offers(
            page,
            url="https://www.mercadolivre.com.br/ofertas?category=MLB1574",
            label="ML: Ofertas Casa P1",
            referer="https://www.mercadolivre.com.br/ofertas",
        )
        cat_p2 = await scraper.scrape_offers(
            page,
            url="https://www.mercadolivre.com.br/ofertas?category=MLB1574&page=2",
            label="ML: Ofertas Casa P2",
            referer="https://www.mercadolivre.com.br/ofertas?category=MLB1574",
        )

        # 3. Teste de Mais Vendidos por categoria específica (MLB1574)
        print("\n4. Raspando Mais Vendidos por categoria (MLB1574)...")
        ctx_best_cat = await scraper._get_nordic_ctx(
            page,
            url="https://www.mercadolivre.com.br/mais-vendidos/MLB1574",
            referer="https://www.mercadolivre.com.br/mais-vendidos",
        )
        best_cat_cards = []
        if ctx_best_cat:
            ml_auto._find_offer_cards_recursively(ctx_best_cat, best_cat_cards)
        best_cat_items = [
            p
            for c in best_cat_cards
            if (
                p := ml_auto._parse_polycard(
                    c,
                    source_type="best_sellers",
                    category_name="ML: Casa (Mais Vendidos)",
                )
            )
        ]
        print(f"   ✅ Mais Vendidos MLB1574: {len(best_cat_items)} produtos extraídos")

        # Checa unicidade entre páginas
        k1 = {i["item_key"] for i in off_p1}
        k2 = {i["item_key"] for i in off_p2}
        k3 = {i["item_key"] for i in off_p3}
        kc1 = {i["item_key"] for i in cat_p1}
        kc2 = {i["item_key"] for i in cat_p2}

        print("\n" + "=" * 65)
        print(
            f"OFERTAS GERAL : P1={len(off_p1)} | P2={len(off_p2)} (novos: {len(k2 - k1)}) | P3={len(off_p3)} (novos: {len(k3 - k1 - k2)})"
        )
        print(
            f"OFERTAS CASA  : P1={len(cat_p1)} | P2={len(cat_p2)} (novos: {len(kc2 - kc1)})"
        )
        print(f"MAIS VEND. MLB: {len(best_cat_items)} itens")
        print("=" * 65)

        for lbl, lst in [
            ("Ofertas P2", off_p2),
            ("Ofertas Casa P1", cat_p1),
            ("Mais Vendidos Casa", best_cat_items),
        ]:
            if lst:
                s = lst[0]
                print(f"🔹 [{lbl}] {s['title'][:58]} | R$ {s['current_price']:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
