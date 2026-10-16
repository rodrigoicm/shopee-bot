import asyncio
from datetime import datetime, time, timezone, timedelta
import logging
from dotenv import load_dotenv
from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn

load_dotenv()

from database import (
    get_config,
    update_config,
    get_next_pending_offer,
    mark_offer_as_sent,
    init_db,
)
from portal import app, refresh_offer_data

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from bot import (
    TOKEN,
    handle_gerar_command,
    handle_message,
    handle_callback,
    handle_whatsapp_dispatch,
    error_handler,
    on_startup,
    on_shutdown,
    send_whatsapp_offer,
    WHATSAPP_GROUP_PRINCIPAL,
    WHATSAPP_GROUP_PARCERIA,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

cuiaba_tz = timezone(timedelta(hours=-4))
init_db()

telegram_app: Application | None = None


async def background_dispatcher():
    logger.info("🤖 Motor de Disparos Automáticos iniciado (Fuso: Cuiabá -04:00).")

    while True:
        try:
            is_active = get_config("is_active")
            if is_active != "1":
                await asyncio.sleep(10)
                continue

            start_str = get_config("start_time") or "08:00"
            end_str = get_config("end_time") or "22:00"
            interval_min = int(get_config("interval_minutes") or "30")

            now_time = datetime.now(cuiaba_tz).time()
            start_time = time.fromisoformat(start_str)
            end_time = time.fromisoformat(end_str)

            if not (start_time <= now_time <= end_time):
                await asyncio.sleep(60)
                continue

            today_str = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")
            offer = get_next_pending_offer(today_str)

            if offer:
                offer_id, store, product_url, formatted_message, media_url, priority = (
                    offer
                )
                logger.info(
                    f"📤 [DISPARO AUTOMÁTICO] Processando oferta ID {offer_id} ({store})..."
                )

                # Se foi adicionado via Portal Manual (priority == 1) e for Shopee, atualiza preço na hora.
                # Se veio pronto do Bot ou da Geração Automática, mantém a mensagem formatada!
                if priority == 1 and store.lower() == "shopee":
                    fresh_msg, fresh_media = refresh_offer_data(product_url)
                else:
                    fresh_msg, fresh_media = formatted_message, media_url

                success_p = await send_whatsapp_offer(
                    WHATSAPP_GROUP_PRINCIPAL, fresh_msg, fresh_media
                )
                await asyncio.sleep(2)
                success_par = await send_whatsapp_offer(
                    WHATSAPP_GROUP_PARCERIA, fresh_msg, fresh_media
                )

                if success_p or success_par:
                    mark_offer_as_sent(offer_id)
                    logger.info(
                        f"✅ Oferta ID {offer_id} enviada e marcada como 'sent'!"
                    )
                else:
                    logger.error(f"❌ Falha ao enviar oferta ID {offer_id}.")

                wait_seconds = interval_min * 60
                next_run = (
                    datetime.now(cuiaba_tz) + timedelta(seconds=wait_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
                update_config("next_dispatch_at", next_run)

                logger.info(
                    f"⏳ Aguardando {interval_min} minutos para o próximo disparo (Próximo: {next_run})..."
                )
                await asyncio.sleep(wait_seconds)
            else:
                update_config("next_dispatch_at", "")
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Erro no loop do dispatcher: {e}")
            await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    global telegram_app

    dispatcher_task = asyncio.create_task(background_dispatcher())

    if TOKEN:
        telegram_app = Application.builder().token(TOKEN).build()

        telegram_app.add_handler(
            CommandHandler(["gerar", "auto"], handle_gerar_command)
        )
        telegram_app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message
            )
        )
        telegram_app.add_handler(
            CallbackQueryHandler(handle_callback, pattern="^type_")
        )
        telegram_app.add_handler(
            CallbackQueryHandler(handle_whatsapp_dispatch, pattern="^send_group_")
        )
        telegram_app.add_error_handler(error_handler)

        await telegram_app.initialize()
        await on_startup(telegram_app)
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("🤖 Bot do Telegram (Shopee + ML + /gerar) iniciado com sucesso!")

    yield

    dispatcher_task.cancel()
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await on_shutdown(telegram_app)
        await telegram_app.shutdown()


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
