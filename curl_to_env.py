"""
Lê um cURL (formato bash) salvo em curl.txt e atualiza o .env com:
  ML_AFFILIATE_COOKIE, ML_AFFILIATE_CSRF, ML_AFFILIATE_TAG

Uso:
  1. No navegador: F12 > Network > Fetch/XHR > gere um link no linkbuilder
  2. Botão direito na requisição 'createLink' > Copy > Copy as cURL (bash)
  3. Cole tudo em um arquivo chamado curl.txt (na pasta do projeto)
  4. python curl_to_env.py
"""

import os
import re
import sys

CURL_FILE = sys.argv[1] if len(sys.argv) > 1 else "curl.txt"
ENV_FILE = ".env"

if not os.path.exists(CURL_FILE):
    sys.exit(f"❌ Arquivo {CURL_FILE} não encontrado. Cole o cURL nele e rode de novo.")

with open(CURL_FILE, "r", encoding="utf-8") as f:
    text = f.read()

# Junta linhas quebradas com '\' (formato bash)
text = text.replace("\\\n", " ")

cookie = re.search(r"(?:-b|--cookie)\s+'([^']*)'", text)
csrf = re.search(r"-H\s+'x-csrf-token:\s*([^']+)'", text, re.I)
tag = re.search(r'"tag"\s*:\s*"([^"]+)"', text)

values = {}
if cookie:
    values["ML_AFFILIATE_COOKIE"] = f"'{cookie.group(1).strip()}'"
if csrf:
    values["ML_AFFILIATE_CSRF"] = csrf.group(1).strip()
if tag:
    values["ML_AFFILIATE_TAG"] = tag.group(1).strip()

missing = [
    k
    for k in ("ML_AFFILIATE_COOKIE", "ML_AFFILIATE_CSRF", "ML_AFFILIATE_TAG")
    if k not in values
]
if missing:
    print(f"⚠️ Não encontrei no cURL: {', '.join(missing)}")
    print("   Confira se copiou como 'cURL (bash)' e se é a requisição createLink.")

if not values:
    sys.exit("❌ Nada para atualizar.")

# Atualiza (ou acrescenta) as chaves no .env, preservando o resto
lines = []
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

for key, val in values.items():
    new_line = f"{key}={val}"
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)

with open(ENV_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

for key, val in values.items():
    print(f"✅ {key} atualizado ({len(val)} caracteres)")

# O cURL contém a sessão: apaga o arquivo depois de usar
os.remove(CURL_FILE)
print(f"🗑️ {CURL_FILE} removido (continha dados da sessão).")
