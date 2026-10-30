"""
Serviço centralizado de envio de mensagens e mídias via Evolution API (WhatsApp).
Utilizado pelo bot do Telegram, pelo Portal Web e pelo despachante automático.
"""

import asyncio
import logging
import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

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


def convert_html_to_whatsapp(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def send_whatsapp_message(
    group_id: str,
    message: str,
    media_url: str | None = None,
    media_type: str = "image",
) -> bool:
    """
    Função síncrona de envio para a Evolution API.
    Usada diretamente pelo Portal Web (portal.py) e internamente pelo send_whatsapp_offer.
    """
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        logger.error("❌ Credenciais da Evolution API não configuradas no .env.")
        return False

    final_message = convert_html_to_whatsapp(message)
    is_video = (media_type == "video") or (media_url and ".mp4" in media_url.lower())
    headers = {"apikey": EVOLUTION_API_KEY}

    try:
        if media_url:
            try:
                dl_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                media_resp = requests.get(media_url, headers=dl_headers, timeout=30)
                if media_resp.status_code == 200:
                    url_api = (
                        f"{EVOLUTION_API_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
                    )
                    if is_video:
                        files = {
                            "file": (
                                "oferta_video.mp4",
                                media_resp.content,
                                "video/mp4",
                            )
                        }
                        data = {
                            "number": group_id,
                            "mediatype": "video",
                            "caption": final_message,
                            "fileName": "oferta_video.mp4",
                        }
                    else:
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
                        url_api, data=data, files=files, headers=headers, timeout=60
                    )
                    if res.status_code in [200, 201]:
                        logger.info(
                            f"✅ Mídia ({'VÍDEO' if is_video else 'IMAGEM'}) enviada com sucesso para o WhatsApp ({group_id})"
                        )
                        return True
                    else:
                        logger.warning(
                            f"⚠️ Falha no envio de mídia ({res.text}), tentando fallback para texto..."
                        )
            except Exception as e:
                logger.warning(f"⚠️ Erro ao baixar mídia para envio: {e}")

        url_api = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        payload = {"number": group_id, "text": final_message, "delay": 1200}
        headers["Content-Type"] = "application/json"
        response = requests.post(url_api, json=payload, headers=headers, timeout=15)

        if response.status_code in [200, 201]:
            logger.info(f"✅ Texto enviado com sucesso para o WhatsApp ({group_id})")
            return True
        else:
            logger.error(f"❌ Erro ao enviar texto para o WhatsApp: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Exceção ao enviar mensagem para o WhatsApp: {e}")
        return False


async def send_whatsapp_offer(
    group_id: str,
    message: str,
    media_url: str | None = None,
    media_type: str = "image",
) -> bool:
    """
    Versão assíncrona (não bloqueia o loop de eventos).
    Usada pelo Bot do Telegram (bot.py) e pelo Disparador Automático (app_runner.py).
    """
    return await asyncio.to_thread(
        send_whatsapp_message, group_id, message, media_url, media_type
    )
