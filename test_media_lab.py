"""
Laboratório Isolado de Diagnóstico de Mídia e Miniaturas (WhatsApp / Evolution API).
Aceita:
  1) Link de PRODUTO (Shopee, Mercado Livre ou Amazon) -> extrai a imagem automaticamente!
  2) Link direto de IMAGEM (mlstatic.com, susercontent.com, media-amazon.com)
  3) ID da oferta no banco (--offer-id) ou busca pelo link/título no banco (--find)
"""

import argparse
import asyncio
import base64
from io import BytesIO
import json
import logging
import os
import sqlite3
import sys
import time
import requests
from dotenv import load_dotenv

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

try:
    from ml_social import build_ml_image_url
except ImportError:
    build_ml_image_url = lambda u: u

load_dotenv()

# Configuração de Log Duplo (Terminal + Arquivo media_lab.log)
log_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MediaLab")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    file_handler = logging.FileHandler("media_lab.log", encoding="utf-8")
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)


def resolve_product_url_to_image(url: str) -> tuple[str, str]:
    """
    Se o usuário passar um link de PRODUTO (Shopee, ML ou Amazon) em vez do link da foto,
    consulta primeiramente o database.db e, se não achar, extrai a foto usando os serviços do bot!
    """
    u_low = url.lower().strip()

    # 1. Se já for URL direta de servidor de imagem (CDN), retorna direto
    image_cdns = (
        "mlstatic.com",
        "susercontent.com",
        "shopee.com.br/file/",
        "media-amazon.com",
        "images-amazon.com",
    )
    if any(cdn in u_low for cdn in image_cdns) or u_low.endswith(
        (".jpg", ".jpeg", ".webp", ".png")
    ):
        return url, "URL Direta de Imagem"

    # 2. Procura se esse link de produto já está salvo na tabela queue do database.db
    try:
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id, store, media_url, formatted_message FROM queue WHERE product_url LIKE ? OR formatted_message LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{url.split('?')[0]}%", f"%{url}%"),
        ).fetchone()
        conn.close()
        if row and row[2]:
            logger.info(
                f"📦 Produto encontrado no database.db (Oferta #{row[0]} - {row[1]})! Usando media_url salva: {row[2]}"
            )
            return row[2], f"Oferta #{row[0]} ({row[1]}) do Banco"
    except Exception as e:
        logger.debug(f"Aviso ao consultar banco local: {e}")

    # 3. Se for link de produto da SHOPEE, busca a imagem na API da Shopee
    if "shopee" in u_low or "shope.ee" in u_low or "shp.ee" in u_low:
        logger.info(
            "🛍️ Detectado link de produto da SHOPEE! Extraindo imagem via API..."
        )
        from shopee_service import ShopeeAffiliateAPI

        app_id = os.environ.get("SHOPEE_APP_ID", "")
        app_secret = os.environ.get("SHOPEE_APP_SECRET", "")
        api = ShopeeAffiliateAPI(app_id, app_secret)
        clean_u = api.clean_shopee_url_for_affiliate(url)
        item_id = api.extract_item_id(clean_u)
        if not item_id:
            raise ValueError(f"Não foi possível extrair o itemId da URL Shopee: {url}")
        offer = api.get_product_offer(item_id)
        if not offer or not offer.get("imageUrl"):
            raise ValueError(
                f"API da Shopee não retornou imageUrl para o itemId {item_id}."
            )
        img_url = offer["imageUrl"]
        title = (offer.get("productName") or f"Shopee {item_id}")[:55]
        logger.info(f"✅ Imagem Shopee extraída: {img_url}")
        return img_url, f"Shopee: {title}"

    # 4. Se for link de produto do MERCADO LIVRE (mercadolivre.com.br ou meli.la)
    if "mercadolivre" in u_low or "meli.la" in u_low:
        logger.info(
            "🤝 Detectado link de produto do MERCADO LIVRE! Extraindo imagem..."
        )
        from ml_social import fetch_social_product

        info = fetch_social_product(url)
        img_url = info.get("thumbnail")
        if not img_url:
            raise ValueError(
                f"Não foi possível extrair a imagem do link do Mercado Livre: {url}"
            )
        title = (info.get("title") or "Produto ML")[:55]
        logger.info(f"✅ Imagem Mercado Livre extraída: {img_url}")
        return img_url, f"ML: {title}"

    # 5. Se for link de produto da AMAZON
    if "amazon" in u_low or "amzn.to" in u_low or "a.co" in u_low:
        logger.info("📦 Detectado link de produto da AMAZON! Extraindo imagem...")
        from amazon_service import get_amazon_offer

        offer = asyncio.run(get_amazon_offer(url))
        img_url = offer.get("image_url")
        if not img_url:
            raise ValueError(f"Não foi possível extrair a imagem da Amazon: {url}")
        title = (offer.get("title") or "Produto Amazon")[:55]
        logger.info(f"✅ Imagem Amazon extraída: {img_url}")
        return img_url, f"Amazon: {title}"

    return url, "URL Manual"


class WhatsAppMediaLab:
    def __init__(self, target_number: str):
        self.api_url = os.environ.get(
            "EVOLUTION_API_URL", "http://localhost:8080"
        ).rstrip("/")
        self.api_key = os.environ.get("EVOLUTION_API_KEY", "")
        self.instance = os.environ.get("EVOLUTION_INSTANCE", "chip_01")
        self.target_number = self._normalize_target(target_number)

        logger.info("=" * 75)
        logger.info("🧪 INICIANDO LABORATÓRIO DE MÍDIA WHATSAPP (EVOLUTION API)")
        logger.info(f"📡 API URL      : {self.api_url}")
        logger.info(f"📱 Instância    : {self.instance}")
        logger.info(f"🎯 Destino Teste: {self.target_number}")
        logger.info(f"🖼️ Pillow Ativo : {PIL_AVAILABLE}")
        logger.info("=" * 75)

    @staticmethod
    def _normalize_target(raw_target: str) -> str:
        t = raw_target.strip()
        if "@" in t:
            return t
        return "".join(c for c in t if c.isdigit())

    @staticmethod
    def detect_magic_bytes(data: bytes) -> str:
        if not data or len(data) < 12:
            return "DESCONHECIDO/VAZIO"
        if data.startswith(b"\xff\xd8\xff"):
            return "JPEG (Magic: FF D8 FF)"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "PNG (Magic: 89 50 4E 47)"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "WEBP (Magic: RIFF....WEBP)"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "GIF"
        return f"NAO-IMAGEM (Hex inicial: {data[:8].hex().upper()})"

    def inspect_and_download(self, raw_url: str) -> dict:
        normalized_url = build_ml_image_url(raw_url) or raw_url
        logger.info(f"🔗 URL da Imagem Original   : {raw_url}")
        if normalized_url != raw_url:
            logger.info(f"✨ URL da Imagem Normalizada: {normalized_url}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
        }

        t0 = time.perf_counter()
        resp = requests.get(normalized_url, headers=headers, timeout=25)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        raw_bytes = resp.content
        http_ct = resp.headers.get("Content-Type", "N/A")
        magic_fmt = self.detect_magic_bytes(raw_bytes)
        size_kb = round(len(raw_bytes) / 1024, 2)

        info = {
            "original_url": raw_url,
            "final_url": resp.url,
            "http_status": resp.status_code,
            "http_content_type": http_ct,
            "download_ms": elapsed_ms,
            "raw_bytes": raw_bytes,
            "size_kb": size_kb,
            "magic_format": magic_fmt,
            "pil_format": None,
            "pil_mode": None,
            "width": None,
            "height": None,
            "is_progressive": False,
        }

        if PIL_AVAILABLE and raw_bytes:
            try:
                img = Image.open(BytesIO(raw_bytes))
                info["pil_format"] = img.format
                info["pil_mode"] = img.mode
                info["width"], info["height"] = img.size
                info["is_progressive"] = bool(img.info.get("progressive", False))
            except Exception as e:
                logger.error(f"❌ Pillow falhou ao abrir os bytes baixados: {e}")

        logger.info(
            f"📥 Download HTTP {resp.status_code} em {elapsed_ms}ms | Tamanho: {size_kb} kB"
        )
        logger.info(
            f"🔍 Header Content-Type: '{http_ct}' | Assinatura Binária: {magic_fmt}"
        )
        logger.info(
            f"📐 Pillow Info: format={info['pil_format']}, mode={info['pil_mode']}, "
            f"dim={info['width']}x{info['height']}, progressive={info['is_progressive']}"
        )
        return info

    def convert_to_jpeg(
        self, raw_bytes: bytes, square_800: bool = False, quality: int = 92
    ) -> dict:
        if not PIL_AVAILABLE:
            raise RuntimeError("Pillow (PIL) não está instalado!")

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

        if square_800:
            img.thumbnail((800, 800), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (800, 800), (255, 255, 255))
            offset_x = (800 - img.width) // 2
            offset_y = (800 - img.height) // 2
            canvas.paste(img, (offset_x, offset_y))
            final_img = canvas
        else:
            final_img = img

        out = BytesIO()
        final_img.save(
            out,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=False,
            subsampling=0,
            dpi=(72, 72),
        )
        jpeg_bytes = out.getvalue()
        b64_str = base64.b64encode(jpeg_bytes).decode("utf-8")
        size_kb = round(len(jpeg_bytes) / 1024, 2)

        logger.info(
            f"🔄 Convertido p/ JPEG ({'Canvas 800x800' if square_800 else f'Original {final_img.width}x{final_img.height}'}) "
            f"| Novo tamanho: {size_kb} kB | Magic: {self.detect_magic_bytes(jpeg_bytes)}"
        )

        return {
            "bytes": jpeg_bytes,
            "b64": b64_str,
            "width": final_img.width,
            "height": final_img.height,
            "size_kb": size_kb,
        }

    def _log_evolution_response(self, mode_label: str, resp: requests.Response):
        logger.info(f"📤 [{mode_label}] Status HTTP Evolution: {resp.status_code}")
        try:
            data = resp.json()
            msg_obj = data.get("message") or {}
            img_msg = msg_obj.get("imageMessage") or {}
            raw_thumb = img_msg.get("jpegThumbnail")
            has_thumb = bool(raw_thumb)
            thumb_len = len(str(raw_thumb)) if raw_thumb else 0
            w = img_msg.get("width")
            h = img_msg.get("height")
            f_len = img_msg.get("fileLength")
            logger.info(
                f"✅ [{mode_label}] Retorno Baileys -> jpegThumbnail GERADO? {has_thumb} (len={thumb_len}) "
                f"| width={w}, height={h}, fileLength={f_len}"
            )
            logger.debug(
                f"📄 [{mode_label}] JSON Resposta: {json.dumps(data, ensure_ascii=False)[:600]}"
            )
        except Exception:
            logger.warning(f"⚠️ [{mode_label}] Resposta bruta: {resp.text[:400]}")

    def send_mode_1_direct_url(self, media_url: str, caption: str):
        url_api = f"{self.api_url}/message/sendMedia/{self.instance}"
        payload = {
            "number": self.target_number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "caption": caption,
            "media": media_url,
            "fileName": "teste_modo1_url.jpg",
        }
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}
        resp = requests.post(url_api, json=payload, headers=headers, timeout=45)
        self._log_evolution_response("MODO 1: URL Direta no JSON", resp)

    def send_mode_2_b64_original_conv(self, raw_bytes: bytes, caption: str):
        conv = self.convert_to_jpeg(raw_bytes, square_800=False)
        url_api = f"{self.api_url}/message/sendMedia/{self.instance}"
        payload = {
            "number": self.target_number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "caption": caption,
            "media": conv["b64"],
            "fileName": "teste_modo2_b64_orig.jpg",
        }
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}
        resp = requests.post(url_api, json=payload, headers=headers, timeout=45)
        self._log_evolution_response("MODO 2: Base64 JPEG (Dimensão Original)", resp)

    def send_mode_3_b64_square_800(self, raw_bytes: bytes, caption: str):
        conv = self.convert_to_jpeg(raw_bytes, square_800=True)
        url_api = f"{self.api_url}/message/sendMedia/{self.instance}"
        payload = {
            "number": self.target_number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "caption": caption,
            "media": conv["b64"],
            "fileName": "teste_modo3_b64_800x800.jpg",
        }
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}
        resp = requests.post(url_api, json=payload, headers=headers, timeout=45)
        self._log_evolution_response("MODO 3: Base64 JPEG (Quadrado 800x800)", resp)

    def send_mode_4_multipart_file(self, raw_bytes: bytes, caption: str):
        conv = self.convert_to_jpeg(raw_bytes, square_800=True)
        url_api = f"{self.api_url}/message/sendMedia/{self.instance}"
        files = {"file": ("teste_modo4_multipart.jpg", conv["bytes"], "image/jpeg")}
        data = {
            "number": self.target_number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "caption": caption,
            "fileName": "teste_modo4_multipart.jpg",
        }
        headers = {"apikey": self.api_key}
        resp = requests.post(
            url_api, data=data, files=files, headers=headers, timeout=45
        )
        self._log_evolution_response("MODO 4: Multipart File (JPEG 800x800)", resp)

    def run_all_modes_for_url(
        self, raw_input_url: str, title_label: str = "Teste de Mídia"
    ):
        logger.info("-" * 75)
        image_url, resolved_label = resolve_product_url_to_image(raw_input_url)
        final_label = resolved_label if title_label == "URL Manual" else title_label
        logger.info(f"🚀 INICIANDO BATERIA DE TESTES PARA: {final_label}")

        info = self.inspect_and_download(image_url)
        raw_bytes = info["raw_bytes"]
        if not raw_bytes or info["pil_format"] is None:
            logger.error(
                "❌ Os bytes baixados NÃO são uma imagem válida! Verifique a URL acima."
            )
            return

        base_summary = (
            f"📋 *{final_label}*\n"
            f"• Origem: `{info['magic_format']}`\n"
            f"• Content-Type: `{info['http_content_type']}`\n"
            f"• Dimensão Orig.: `{info['width']}x{info['height']}` ({info['size_kb']} kB)"
        )

        self.send_mode_1_direct_url(
            info["final_url"],
            f"🧪 *[MODO 1: URL Direta]*\n{base_summary}",
        )
        time.sleep(5)

        self.send_mode_2_b64_original_conv(
            raw_bytes,
            f"🧪 *[MODO 2: Base64 JPEG Dimensão Original]*\n{base_summary}",
        )
        time.sleep(5)

        self.send_mode_3_b64_square_800(
            raw_bytes,
            f"🧪 *[MODO 3: Base64 JPEG Quadrado 800x800]*\n{base_summary}",
        )
        time.sleep(5)

        self.send_mode_4_multipart_file(
            raw_bytes,
            f"🧪 *[MODO 4: Multipart Binário 800x800]*\n{base_summary}",
        )
        logger.info(
            "🏁 Bateria concluída! Verifique no WhatsApp quais modos geraram miniatura e confira o media_lab.log."
        )


def get_offer_from_db(
    offer_id: int | None = None, search_term: str | None = None
) -> tuple[str, str]:
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    if offer_id:
        row = cur.execute(
            "SELECT id, store, media_url, formatted_message FROM queue WHERE id = ?",
            (offer_id,),
        ).fetchone()
    elif search_term:
        row = cur.execute(
            "SELECT id, store, media_url, formatted_message FROM queue WHERE formatted_message LIKE ? OR product_url LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{search_term}%", f"%{search_term}%"),
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT id, store, media_url, formatted_message FROM queue WHERE media_url IS NOT NULL AND media_url != '' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        raise ValueError("Nenhuma oferta correspondente encontrada no database.db.")
    return row[2], f"Oferta #{row[0]} ({row[1]})"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Laboratório de Diagnóstico de Imagens e Miniaturas WhatsApp"
    )
    parser.add_argument(
        "--to",
        required=True,
        help="Número de destino com DDI+DDD (ex: 5565984590135) ou ID de grupo (@g.us)",
    )
    parser.add_argument(
        "--url",
        help="Link de PRODUTO (Shopee/ML/Amazon) OU URL direta da imagem",
    )
    parser.add_argument(
        "--offer-id",
        type=int,
        help="ID da oferta na tabela queue do database.db",
    )
    parser.add_argument(
        "--find",
        help="Busca uma oferta no database.db pelo nome/link (ex: --find 'Sutiãs' ou --find '2zRkaxf')",
    )
    args = parser.parse_args()

    lab = WhatsAppMediaLab(target_number=args.to)

    if args.url:
        target_url = args.url
        label = "URL Manual"
    else:
        target_url, label = get_offer_from_db(args.offer_id, args.find)

    lab.run_all_modes_for_url(target_url, title_label=label)
