"""
Script auxiliar — usa um perfil de navegador dedicado e persistente (gerenciado
pelo próprio Playwright) para capturar o cookie de sessão do Mercado Livre.

Por que não conecta no Chrome do dia a dia:
Desde meados de 2025 o Chrome bloqueia debug remoto (--remote-debugging-port)
no perfil padrão por segurança, então conectar via CDP no seu navegador normal
não funciona mais de forma confiável.

Como funciona:
- Na PRIMEIRA execução, abre uma janela de Chromium vazia (perfil próprio,
  salvo em ./playwright_ml_profile). Você faz login manual uma única vez.
- Nas execuções SEGUINTES, o perfil já está logado — o script só reabre,
  confirma a sessão e extrai o cookie, sem pedir login de novo (a não ser
  que a sessão tenha expirado de verdade).

Uso:
    python capture_ml_cookies.py

Gera ml_cookies_export.json e imprime a linha pronta para colar no .env.

IMPORTANTE: a pasta playwright_ml_profile/ contém a sessão logada — não
deve ir pro Git (adicione no .gitignore, junto com .env e
ml_cookies_export.json).
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

PROFILE_DIR = Path("./playwright_ml_profile").resolve()


async def main():
    PROFILE_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            # Sem user_agent fixo: usa o valor real do Chromium instalado,
            # mesmo critério aplicado no bot.py para manter o fingerprint consistente.
            locale="pt-BR",
            timezone_id="America/Cuiaba",
            viewport={"width": 1366, "height": 768},
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.mercadolivre.com.br/afiliados/linkbuilder")

        print("\n👉 Se a página pedir login, faça login manualmente na janela aberta.")
        print("   Se já estiver logado (execuções seguintes), pode conferir e já apertar ENTER.")
        print("   Depois de ver o LinkBuilder carregado, volte aqui e aperte ENTER.\n")
        input()

        state = await context.storage_state()
        compact_json = json.dumps(state, separators=(",", ":"))

        with open("ml_cookies_export.json", "w", encoding="utf-8") as f:
            f.write(compact_json)

        print("\n✅ Salvo em ml_cookies_export.json")
        print("\nCole esta linha no seu .env (substituindo a existente, se houver):\n")
        print(f"ML_COOKIES_JSON='{compact_json}'")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())