import os
import getpass
import asyncio
from playwright.async_api import async_playwright

async def inspect_ml_actions():
    username = getpass.getuser()
    chrome_user_data = f"C:\\Users\\{username}\\AppData\\Local\\Google\\Chrome\\User Data"
    cookies_path = "cookies_ml.json"
    
    print(f"📁 Usando o perfil do Chrome real em: {chrome_user_data}")
    print("\n⚠️  IMPORTANTE: Feche TODAS as janelas do Chrome antes de pressionar Enter!\n")
    input("👉 Feche o Chrome e pressione [ENTER] para continuar...")

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=chrome_user_data,
                channel="chrome",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized"
                ],
                viewport=None
            )

            page = context.pages[0] if context.pages else await context.new_page()

            print("\n========================================================")
            print("1. Abrindo a página do LinkBuilder...")
            print("2. Verifique se sua conta está ativa no navegador.")
            print("========================================================\n")

            await page.goto("https://www.mercadolivre.com.br/afiliados/linkbuilder", wait_until="domcontentloaded")

            input("\n👉 Pressione [ENTER] no terminal para SALVAR OS COOKIES... ")

            # Salva o arquivo de cookies diretamente
            await context.storage_state(path=cookies_path)
            
            if os.path.exists(cookies_path):
                print(f"\n✅ SUCESSO! Arquivo '{cookies_path}' criado/atualizado com sucesso!")
            else:
                print(f"\n❌ Atenção: O arquivo '{cookies_path}' não foi localizado no diretório.")

        except Exception as e:
            print(f"\n❌ Erro durante a execução: {e}")

if __name__ == "__main__":
    asyncio.run(inspect_ml_actions())