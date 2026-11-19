"""
Teste de Densidade para Imagens Leves da Shopee (< 65 kB) -> Evolution API / Baileys.
Testa na prática o aumento de resolução e micro-entropia invisível para forçar
fileLength > 75 kB na Evolution API e destravar jpegThumbnail=True.
"""

import argparse
import base64
from io import BytesIO
import logging
import os
import random
import sqlite3
import sys
import time
import requests
from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageFilter

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("shopee_image_lab.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ShopeeDensityLab")

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080").rstrip(
    "/"
)
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "chip_01")


def get_shopee_offer_image(
    search_term: str | None = None, offer_id: int | None = None
) -> tuple[int, str]:
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    if offer_id:
        row = cur.execute(
            "SELECT id, media_url FROM queue WHERE id = ?", (offer_id,)
        ).fetchone()
    elif search_term:
        row = cur.execute(
            """
            SELECT id, media_url FROM queue
            WHERE REPLACE(formatted_message, char(10), ' ') LIKE ?
               OR product_url LIKE ?
            ORDER BY id DESC LIMIT 1
            """,
            (f"%{search_term}%", f"%{search_term}%"),
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT id, media_url FROM queue WHERE store LIKE '%Shopee%' AND media_url IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()

    if not row or not row[1]:
        raise ValueError(f"Nenhuma oferta encontrada para: {search_term or offer_id}")
    return row[0], row[1]


def log_baileys_result(label: str, resp: requests.Response) -> None:
    logger.info(f"📤 [{label}] HTTP Status: {resp.status_code}")
    try:
        data = resp.json()
        img_msg = (data.get("message") or {}).get("imageMessage") or {}
        thumb = img_msg.get("jpegThumbnail")
        has_thumb = bool(thumb)
        thumb_len = len(str(thumb)) if thumb else 0
        w = img_msg.get("width")
        h = img_msg.get("height")
        f_len = img_msg.get("fileLength")
        logger.info(
            f"   ↳ Baileys: jpegThumbnail={has_thumb} (len={thumb_len}) | width={w} | height={h} | fileLength={f_len}"
        )
    except Exception:
        logger.error(f"   ❌ Resposta bruta: {resp.text[:300]}")


def _to_rgb(raw_bytes: bytes) -> Image.Image:
    img = Image.open(BytesIO(raw_bytes))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def build_variant_1_upscale_1200(raw_bytes: bytes) -> bytes:
    """Variante 1: Redimensiona proporcionalmente para lado mínimo de 1200px + Nitidez."""
    img = _to_rgb(raw_bytes)
    scale = 1200.0 / max(img.width, img.height)
    new_size = (int(round(img.width * scale)), int(round(img.height * scale)))
    img = img.resize(new_size, Image.Resampling.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    out = BytesIO()
    img.save(out, format="JPEG", quality=98, subsampling=0)
    return out.getvalue()


def build_variant_2_upscale_1600(raw_bytes: bytes) -> bytes:
    """Variante 2: Redimensiona proporcionalmente para 1600px (Alta Resolução)."""
    img = _to_rgb(raw_bytes)
    scale = 1600.0 / max(img.width, img.height)
    new_size = (int(round(img.width * scale)), int(round(img.height * scale)))
    img = img.resize(new_size, Image.Resampling.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=98, subsampling=0)
    return out.getvalue()


def build_variant_3_anti_compression_1200(raw_bytes: bytes) -> bytes:
    """
    Variante 3: 1200px + Micro-textura imperceptível (impede o compressor da Evolution
    de achatar fundos lisos para < 65 kB, forçando fileLength > 85 kB).
    """
    img = _to_rgb(raw_bytes)
    scale = 1200.0 / max(img.width, img.height)
    new_size = (int(round(img.width * scale)), int(round(img.height * scale)))
    img = img.resize(new_size, Image.Resampling.LANCZOS)

    # Cria micro-variação RGB imperceptível a olho nu (alpha=0.03 = 3%)
    noise = Image.effect_noise(img.size, 12.0).convert("RGB")
    img = Image.blend(img, noise, alpha=0.025)

    out = BytesIO()
    img.save(out, format="JPEG", quality=96, subsampling=0)
    return out.getvalue()


def run_density_tests(target_number: str, media_url: str, label: str) -> None:
    clean_to = (
        target_number
        if "@" in target_number
        else "".join(c for c in target_number if c.isdigit())
    )
    url_api = f"{EVOLUTION_API_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"

    logger.info("=" * 70)
    logger.info(f"🧪 LABORATÓRIO ANTI-46KB (DESTRAVANDO THUMBNAIL SHOPEE) | {label}")
    logger.info(f"🔗 URL: {media_url}")

    headers_dl = {"User-Agent": "Mozilla/5.0"}
    r_dl = requests.get(media_url, headers=headers_dl, timeout=25)
    raw_bytes = r_dl.content

    orig_img = Image.open(BytesIO(raw_bytes))
    logger.info(
        f"📥 Original da Shopee: {round(len(raw_bytes)/1024, 2)} kB | Dimensão Original: {orig_img.width}x{orig_img.height} | Formato: {orig_img.format}"
    )

    variants = [
        (
            "OPÇÃO 1: Upscale 1200px + Sharpness",
            build_variant_1_upscale_1200(raw_bytes),
        ),
        ("OPÇÃO 2: Upscale 1600px HD", build_variant_2_upscale_1600(raw_bytes)),
        (
            "OPÇÃO 3: 1200px + Anti-Compressão (>85kB)",
            build_variant_3_anti_compression_1200(raw_bytes),
        ),
    ]

    for v_name, v_bytes in variants:
        b64_str = base64.b64encode(v_bytes).decode("utf-8")
        payload = {
            "number": clean_to,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "caption": f"🧪 *{v_name}* (Enviado: {round(len(v_bytes)/1024, 1)} kB)",
            "media": b64_str,
            "fileName": "oferta.jpg",
        }
        resp = requests.post(
            url_api,
            json=payload,
            headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
            timeout=45,
        )
        log_baileys_result(v_name, resp)
        time.sleep(4)

    logger.info("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--find", help="Ex: 9V35b9Jvl1")
    parser.add_argument("--id", type=int)
    parser.add_argument("--url")
    args = parser.parse_args()

    if args.url:
        run_density_tests(args.to, args.url, "URL Manual")
    else:
        oid, m_url = get_shopee_offer_image(args.find, args.id)
        run_density_tests(args.to, m_url, f"Oferta #{oid}")
