"""
Converte um export de cookies feito pela extensão Cookie-Editor (formato JSON
padrão dela) para o formato storage_state que o Playwright/bot.py espera.

Como usar:
1. Faça login normalmente no Mercado Livre com seu Chrome comum (sem nenhuma
   automação/debug port ativo — é isso que evita o captcha).
2. Instale a extensão "Cookie-Editor" na Chrome Web Store.
3. Com a aba do Mercado Livre aberta e logada, clique no ícone da extensão
   e use a opção "Export" > "Export as JSON" (ou "Export" simples — o
   formato padrão dela é uma lista de objetos com name/value/domain/...).
4. Cole o conteúdo exportado em um arquivo chamado cookie_editor_export.json
   nesta mesma pasta.
5. Rode: python convert_cookie_export.py
6. O script gera ml_cookies_export.json (formato Playwright) e imprime a
   linha pronta pra colar no .env.
"""
import json
from pathlib import Path

INPUT_FILE = Path("cookie_editor_export.json")
OUTPUT_FILE = Path("ml_cookies_export.json")

SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def convert(raw_cookies: list) -> dict:
    playwright_cookies = []
    for c in raw_cookies:
        expires = c.get("expirationDate")
        if c.get("session") or expires is None:
            expires_value = -1
        else:
            expires_value = expires

        playwright_cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": expires_value,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": SAMESITE_MAP.get(str(c.get("sameSite", "")).lower(), "Lax"),
        })

    return {"cookies": playwright_cookies, "origins": []}


def main():
    if not INPUT_FILE.exists():
        print(f"❌ Não encontrei {INPUT_FILE}. Exporte os cookies pelo Cookie-Editor "
              f"e salve o conteúdo nesse arquivo antes de rodar o script.")
        return

    raw = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]

    state = convert(raw)
    compact_json = json.dumps(state, separators=(",", ":"))

    OUTPUT_FILE.write_text(compact_json, encoding="utf-8")

    print(f"✅ Convertido {len(state['cookies'])} cookies.")
    print(f"✅ Salvo em {OUTPUT_FILE}")
    print("\nCole esta linha no seu .env (substituindo a existente, se houver):\n")
    print(f"ML_COOKIES_JSON='{compact_json}'")


if __name__ == "__main__":
    main()