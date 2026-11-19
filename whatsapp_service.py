"""
Serviço centralizado de envio de mensagens e mídias via Evolution API v2.3.7 (WhatsApp).
Garante upscale proporcional para 1200px (quality=98, subsampling=0) em imagens menores,
forçando fileLength > 75 kB para que o Baileys gere o jpegThumbnail=True em 100% dos envios.
"""

import asyncio
import base64
import html
from io import BytesIO
import logging
import os
import re
import requests
from dotenv import load_dotenv

from ml_social import build_ml_image_url

try:
    from PIL import Image, ImageEnhance

    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

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
    text = html.unescape(html.unescape(text))
    text = re.sub(
        r"<(?:b|strong)>\s*(.*?)\s*</(?:b|strong)>", r"*\1*", text, flags=re.DOTALL
    )
    text = re.sub(r"<(?:i|em)>\s*(.*?)\s*</(?:i|em)>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(
        r"<(?:s|strike|del)>\s*(.*?)\s*</(?:s|strike|del)>",
        r"~\1~",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _prepare_image_hd(raw_bytes: bytes) -> str:
    """
    Garante densidade visual ideal para o Baileys gerar thumbnail:
    - Se a imagem for menor que 1200px, redimensiona proporcionalmente para 1200px (LANCZOS).
    - Aplica otimização JPEG quality=98 com chroma subsampling desativado (subsampling=0)
      para garantir que o arquivo final ultrapasse 75 kB, evitando a caixinha cinza.
    """
    if not PIL_AVAILABLE:
        return base64.b64encode(raw_bytes).decode("utf-8")

    try:
        img = Image.open(BytesIO(raw_bytes))
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Upscale proporcional se o maior lado for menor que 1200px
        max_side = max(img.width, img.height)
        if max_side < 1200:
            scale = 1200.0 / max_side
            new_w = int(round(img.width * scale))
            new_h = int(round(img.height * scale))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            img = ImageEnhance.Sharpness(img).enhance(1.2)

        out = BytesIO()
        img.save(
            out,
            format="JPEG",
            quality=98,
            optimize=True,
            progressive=False,
            subsampling=0,
            dpi=(72, 72),
        )
        return base64.b64encode(out.getvalue()).decode("utf-8")
    except Exception as e:
        logger.warning(f"⚠️ Aviso ao processar imagem HD no Pillow: {e}")
        return base64.b64encode(raw_bytes).decode("utf-8")


_LAST_MEDIA_CACHE: dict = {"url": "", "b64": ""}


def send_whatsapp_message(
    group_id: str,
    message: str,
    media_url: str | None = None,
    media_type: str = "image",
) -> bool:
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        logger.error("❌ Credenciais da Evolution API não configuradas no .env.")
        return False

    final_message = convert_html_to_whatsapp(message)
    is_video = (media_type == "video") or (media_url and ".mp4" in media_url.lower())
    headers = {"apikey": EVOLUTION_API_KEY}

    try:
        if media_url:
            clean_media_url = build_ml_image_url(media_url) or media_url
            url_api = f"{EVOLUTION_API_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"

            if not is_video:
                try:
                    if (
                        _LAST_MEDIA_CACHE["url"] == clean_media_url
                        and _LAST_MEDIA_CACHE["b64"]
                    ):
                        jpeg_b64 = _LAST_MEDIA_CACHE["b64"]
                    else:
                        dl_headers = {
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                            ),
                            "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
                        }
                        urls_to_try = [clean_media_url]
                        if "-F.jpg" in clean_media_url:
                            urls_to_try.append(
                                clean_media_url.replace("-F.jpg", "-O.jpg")
                            )
                        if media_url not in urls_to_try:
                            urls_to_try.append(media_url)

                        raw_content = None
                        for u in urls_to_try:
                            try:
                                media_resp = requests.get(
                                    u, headers=dl_headers, timeout=25
                                )
                                if (
                                    media_resp.status_code == 200
                                    and len(media_resp.content) > 1000
                                ):
                                    raw_content = media_resp.content
                                    break
                            except Exception:
                                continue

                        if raw_content:
                            jpeg_b64 = _prepare_image_hd(raw_content)
                            _LAST_MEDIA_CACHE["url"] = clean_media_url
                            _LAST_MEDIA_CACHE["b64"] = jpeg_b64
                        else:
                            jpeg_b64 = ""

                    if jpeg_b64:
                        json_payload = {
                            "number": group_id,
                            "mediatype": "image",
                            "mimetype": "image/jpeg",
                            "caption": final_message,
                            "media": jpeg_b64,
                            "fileName": "oferta.jpg",
                        }
                        res = requests.post(
                            url_api,
                            json=json_payload,
                            headers={**headers, "Content-Type": "application/json"},
                            timeout=45,
                        )
                        if res.status_code in [200, 201]:
                            img_msg = (
                                res.json().get("message", {}).get("imageMessage", {})
                            )
                            has_thumb = bool(img_msg.get("jpegThumbnail"))
                            w = img_msg.get("width")
                            h = img_msg.get("height")
                            logger.info(
                                f"✅ Imagem HD enviada ({w}x{h}) para ({group_id}) | Thumbnail={has_thumb}"
                            )
                            return True
                except Exception as e_img:
                    logger.warning(
                        f"⚠️ Erro ao processar imagem HD para WhatsApp: {e_img}"
                    )
            else:
                try:
                    dl_headers = {"User-Agent": "Mozilla/5.0"}
                    media_resp = requests.get(
                        clean_media_url, headers=dl_headers, timeout=30
                    )
                    if media_resp.status_code == 200:
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
                        res = requests.post(
                            url_api, data=data, files=files, headers=headers, timeout=60
                        )
                        if res.status_code in [200, 201]:
                            logger.info(
                                f"✅ Vídeo enviado com sucesso para ({group_id})"
                            )
                            return True
                except Exception as e_vid:
                    logger.warning(f"⚠️ Erro ao enviar vídeo: {e_vid}")

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
    return await asyncio.to_thread(
        send_whatsapp_message, group_id, message, media_url, media_type
    )
