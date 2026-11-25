import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone, timedelta
import logging
import random

from dotenv import load_dotenv
from fastapi import FastAPI
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

cuiaba_tz = timezone(timedelta(hours=-4))
init_db()

telegram_app: Application | None = None


async def background_dispatcher():
    logger.info("🤖 Motor de Disparos Automáticos iniciado (Fuso: Cuiabá -04:00).")

    now_init = datetime.now(cuiaba_tz)
    saved_next = get_config("next_dispatch_at")
    last_idle_reason = ""

    need_one_minute_grace = True
    if saved_next:
        try:
            saved_dt = datetime.strptime(saved_next, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=cuiaba_tz
            )
            if saved_dt > now_init:
                need_one_minute_grace = False
                rem_sec = int((saved_dt - now_init).total_seconds())
                logger.info(
                    f"⏳ Retomando agendamento futuro existente: próximo ciclo às {saved_next} (faltam {rem_sec // 60}m {rem_sec % 60}s)."
                )
                last_idle_reason = f"waiting_{saved_next}"
        except Exception:
            pass

    if need_one_minute_grace:
        next_run_init = (now_init + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        update_config("next_dispatch_at", next_run_init)
        logger.info(
            f"⏳ Horário anterior vencido ou vazio. Primeiro ciclo agendado para daqui a 1 minuto ({next_run_init})."
        )
        last_idle_reason = f"waiting_{next_run_init}"

    while True:
        try:
            is_active = get_config("is_active")
            if is_active != "1":
                if last_idle_reason != "inactive":
                    logger.warning(
                        "⏸️ [DISPATCHER] Motor PAUSADO (is_active != '1'). Nenhuma busca ou disparo será feito."
                    )
                    last_idle_reason = "inactive"
                await asyncio.sleep(10)
                continue

            start_str = get_config("start_time") or "08:00"
            end_str = get_config("end_time") or "22:00"
            interval_min = int(get_config("interval_minutes") or "30")

            now_dt = datetime.now(cuiaba_tz)
            now_time = now_dt.time()
            start_time = time.fromisoformat(start_str)
            end_time = time.fromisoformat(end_str)

            # 1. Verifica se está dentro da janela de horário do dia
            if not (start_time <= now_time <= end_time):
                if last_idle_reason != "outside_window":
                    logger.info(
                        f"🌙 [DISPATCHER] Fora da janela operacional ({start_str}–{end_str}). Aguardando horário..."
                    )
                    last_idle_reason = "outside_window"
                await asyncio.sleep(30)
                continue

            # 2. Verifica se já chegou o horário do próximo ciclo (next_dispatch_at)
            next_dispatch_str = get_config("next_dispatch_at")
            if next_dispatch_str:
                try:
                    next_dt = datetime.strptime(
                        next_dispatch_str, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=cuiaba_tz)
                    diff_seconds = (next_dt - now_dt).total_seconds()
                    if diff_seconds > 0:
                        if last_idle_reason != f"waiting_{next_dispatch_str}":
                            logger.info(
                                f"⏳ [DISPATCHER] Aguardando próximo ciclo às {next_dispatch_str} (faltam {int(diff_seconds)}s)..."
                            )
                            last_idle_reason = f"waiting_{next_dispatch_str}"
                        await asyncio.sleep(min(5, diff_seconds))
                        continue
                except Exception:
                    pass

            # 3. Chegou a hora! Busca a próxima oferta pendente de hoje
            today_str = now_dt.strftime("%Y-%m-%d")
            offer = get_next_pending_offer(today_str)

            # OPÇÃO A: Se a fila de hoje estiver vazia e o auto-abastecimento estiver ativo, busca novas ofertas
            if not offer:
                auto_refill_on = (get_config("auto_refill_enabled") or "1") == "1"
                if not auto_refill_on:
                    if last_idle_reason != "refill_off":
                        logger.warning(
                            f"📭 [AUTO-ABASTECIMENTO] Fila de {today_str} vazia, mas auto_refill_enabled='0' (desativado)."
                        )
                        last_idle_reason = "refill_off"
                else:
                    last_idle_reason = ""
                    refill_count = int(get_config("auto_refill_count") or "15")
                    keywords_csv = get_config("auto_keywords") or ""
                    t_refill_start = asyncio.get_running_loop().time()
                    start_clock = datetime.now(cuiaba_tz).strftime("%H:%M:%S")
                    logger.info(
                        f"🔎 [AUTO-ABASTECIMENTO] INICIANDO busca às {start_clock} — Fila de {today_str} vazia! Meta: {refill_count} ofertas..."
                    )

                    import ml_auto

                    res_refill = await asyncio.to_thread(
                        ml_auto.generate_mixed_auto_offers_to_queue,
                        refill_count,
                        today_str,
                        keywords_csv,
                    )

                    elapsed_s = asyncio.get_running_loop().time() - t_refill_start
                    end_clock = datetime.now(cuiaba_tz).strftime("%H:%M:%S")
                    if res_refill["total_added"] > 0:
                        logger.info(
                            f"✅ [AUTO-ABASTECIMENTO] FINALIZADO às {end_clock} (⏱️ {elapsed_s:.1f}s) — "
                            f"{res_refill['total_added']} ofertas adicionadas "
                            f"({res_refill.get('shopee_added', 0)} Shopee + {res_refill.get('amazon_added', 0)} Amazon + {res_refill.get('ml_added', 0)} ML)!"
                        )
                        offer = get_next_pending_offer(today_str)
                    else:
                        cooldown_run = (
                            datetime.now(cuiaba_tz) + timedelta(minutes=10)
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        update_config("next_dispatch_at", cooldown_run)
                        last_idle_reason = f"waiting_{cooldown_run}"
                        logger.warning(
                            f"⚠️ [AUTO-ABASTECIMENTO] FINALIZADO às {end_clock} (⏱️ {elapsed_s:.1f}s) — "
                            f"Nenhuma oferta inédita gerada. Nova tentativa às {cooldown_run}."
                        )
                        continue

            # OPÇÃO B: Dispara a oferta pendente encontrada
            if offer:
                last_idle_reason = ""
                (
                    offer_id,
                    store,
                    product_url,
                    formatted_message,
                    media_url,
                    priority,
                    media_type,
                ) = offer

                is_video = (media_type == "video") or (
                    media_url and ".mp4" in media_url.lower()
                )
                logger.info(
                    f"📤 [DISPARO AUTOMÁTICO] Processando oferta ID {offer_id} ({store} | Mídia: {'VÍDEO' if is_video else 'IMAGEM'})..."
                )

                is_standard_template = "🛒 <b>Compre na" in (
                    formatted_message or ""
                ) or "💡 <b>POR QUE VALE A PENA?</b>" in (formatted_message or "")
                if priority == 1 and store.lower() == "shopee" and is_standard_template:
                    fresh_msg, fresh_image = await asyncio.to_thread(
                        refresh_offer_data, product_url
                    )
                    fresh_media = media_url if (is_video and media_url) else fresh_image
                else:
                    fresh_msg, fresh_media = formatted_message, media_url

                final_media_type = "video" if is_video else "image"

                success_p = await send_whatsapp_offer(
                    WHATSAPP_GROUP_PRINCIPAL,
                    fresh_msg,
                    fresh_media,
                    final_media_type,
                )
                await asyncio.sleep(random.uniform(6.0, 10.0))
                success_par = await send_whatsapp_offer(
                    WHATSAPP_GROUP_PARCERIA,
                    fresh_msg,
                    fresh_media,
                    final_media_type,
                )

                if success_p or success_par:
                    mark_offer_as_sent(offer_id)
                    logger.info(
                        f"✅ Oferta ID {offer_id} enviada e marcada como 'sent'!"
                    )
                else:
                    logger.error(f"❌ Falha ao enviar oferta ID {offer_id}.")

                next_run = (
                    datetime.now(cuiaba_tz) + timedelta(minutes=interval_min)
                ).strftime("%Y-%m-%d %H:%M:%S")
                update_config("next_dispatch_at", next_run)
                last_idle_reason = f"waiting_{next_run}"
                logger.info(
                    f"⏳ Aguardando {interval_min} minutos para o próximo disparo (Próximo: {next_run})..."
                )
            else:
                if next_dispatch_str:
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
    telegram_started = False

    if TOKEN:
        try:
            telegram_app = (
                Application.builder()
                .token(TOKEN)
                .connect_timeout(20.0)
                .read_timeout(20.0)
                .build()
            )

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
            telegram_started = True
            logger.info(
                "🤖 Bot do Telegram (Shopee + ML + /gerar) iniciado com sucesso!"
            )
        except Exception as e:
            logger.error(
                f"⚠️ Bot do Telegram não iniciou (timeout/rede), mas o Portal continuará ativo: {e}"
            )

    yield

    dispatcher_task.cancel()
    try:
        await dispatcher_task
    except asyncio.CancelledError:
        pass

    if telegram_app and telegram_started:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await on_shutdown(telegram_app)
            await telegram_app.shutdown()
        except Exception as e:
            logger.warning(f"Aviso ao encerrar Bot do Telegram: {e}")


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
