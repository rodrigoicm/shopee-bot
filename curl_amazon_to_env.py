"""
Lê o cURL do SiteStripe da Amazon salvo em curl_amazon.txt e atualiza o .env com:
  AMAZON_PARTNER_TAG, AMAZON_STORE_ID, AMAZON_MARKETPLACE_ID, AMAZON_COOKIE, AMAZON_USER_AGENT
"""

import os
import re
import sys

CURL_FILE = sys.argv[1] if len(sys.argv) > 1 else "curl_amazon.txt"
ENV_FILE = ".env"

if not os.path.exists(CURL_FILE):
    sys.exit(
        f"❌ Ficheiro {CURL_FILE} não encontrado. Cole o cURL da Amazon nele e execute novamente."
    )

with open(CURL_FILE, "r", encoding="utf-8") as f:
    text = f.read().replace("\\\n", " ")

cookie_match = re.search(r"(?:-b|--cookie)\s+'([^']+)'", text)
store_match = re.search(r"[?&]storeId=([a-zA-Z0-9_-]+)", text)
tag_match = (
    re.search(r'"tag"\s*:\s*"([a-zA-Z0-9_-]+-20)"', text)
    or re.search(r"tag%3D([a-zA-Z0-9_-]+-20)", text)
    or store_match
)
mkt_match = re.search(r"marketplaceId[=\":\s]+(\d+)", text)
ua_match = re.search(r"(?:-H|--header)\s+'[Uu]ser-[Aa]gent:\s*([^']+)'", text)

tag_val = tag_match.group(1).strip() if tag_match else "alinemoraeshe-20"
store_val = store_match.group(1).strip() if store_match else tag_val

values = {
    "AMAZON_PARTNER_TAG": tag_val,
    "AMAZON_STORE_ID": store_val,
    "AMAZON_MARKETPLACE_ID": mkt_match.group(1).strip() if mkt_match else "526970",
}
if cookie_match:
    values["AMAZON_COOKIE"] = f"'{cookie_match.group(1).strip()}'"
if ua_match:
    values["AMAZON_USER_AGENT"] = f"'{ua_match.group(1).strip()}'"

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

for k, v in values.items():
    print(f"✅ {k} configurado no .env ({len(v)} chars)")

os.remove(CURL_FILE)
print(f"🗑️ {CURL_FILE} removido por segurança.")
