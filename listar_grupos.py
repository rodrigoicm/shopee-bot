import os
import requests
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip(
    "/"
)
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "chip_01")


def encontrar_id_grupo():
    url = f"{EVOLUTION_API_URL}/chat/findChats/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    print(
        f"🔍 Buscando e listando todos os grupos da instância '{EVOLUTION_INSTANCE}'...\n"
    )

    try:
        response = requests.post(url, json={}, headers=headers, timeout=15)
        if response.status_code == 200:
            chats = response.json()
            encontrou = False
            for chat in chats:
                remote_jid = chat.get("remoteJid", "")
                # Grupos no WhatsApp terminam com @g.us
                if "@g.us" in remote_jid:
                    nome_grupo = chat.get("name", "Nome não disponível")
                    print(f"📌 Nome: {nome_grupo}")
                    print(f"🆔 ID do Grupo: {remote_jid}\n")
                    if "ofertas" in nome_grupo.lower():
                        encontrou = True

            if not encontrou:
                print(
                    "⚠️ O grupo com o nome 'Ofertas' não apareceu diretamente pelo nome nos metadados. Verifique na lista acima qual deles é o correto pelo ID."
                )
        else:
            print(f"❌ Erro ao buscar chats: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ Erro de conexão: {e}")


if __name__ == "__main__":
    encontrar_id_grupo()
