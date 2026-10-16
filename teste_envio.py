import os
import requests
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip(
    "/"
)
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "chip_01")
WHATSAPP_GROUP_ID = os.environ.get("WHATSAPP_GROUP_ID", "120363403982627636@g.us")


def enviar_cupom_grupo():
    url_envio = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"

    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    mensagem = (
        "‼️🟡 NOVOS CUPONS MERCADO LIVRE\n"
        "Válidos nas listas abaixo\n\n"
        "Tecnologia\n"
        "🎟️ HOJEVAI 10% off\n"
        "lista: https://meli.la/29HfVm2\n\n"
        "Diversos\n"
        "🎟️ DESCONTOMELI 30% off\n"
        "lista: https://meli.la/2DBeaLV\n"
        "🎟️ OFFNOMELI 25% off\n"
        "lista: https://meli.la/2NSTVVo\n"
        "🎟️ SONOMELI 25% off\n"
        "lista: https://meli.la/1S6s7Mv\n"
        "🎟️ VEMPROMELI 25% off\n"
        "lista: https://meli.la/1yVprxc\n\n"
        "🚚 Aproveite o frete grátis e benefícios do Mercado Livre!"
    )

    # Payload corrigido com o campo "text" direto na raiz
    payload = {"number": WHATSAPP_GROUP_ID, "text": mensagem, "delay": 1200}

    print(f"🚀 Disparando cupons para o grupo: {WHATSAPP_GROUP_ID}...")

    try:
        response = requests.post(url_envio, json=payload, headers=headers, timeout=15)
        print(f"Status Code: {response.status_code}")
        print(f"Resposta da API: {response.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {e}")


if __name__ == "__main__":
    enviar_cupom_grupo()
