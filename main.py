"""
Точка входа в приложение - Telegram бот Арти
"""
import logging
import sys
import asyncio
import signal
import telegram
from typing import Optional

asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
sys.stdout.reconfigure(encoding='utf-8')

from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, MessageReactionHandler, filters
)

# Инициализация логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Импорты модулей бота
from bot.handlers import (
    handle_all_messages, handle_image_message,
    handle_voice_message, handle_document, error_handler,
    handle_video_upload_message,
    handle_location_message,
    handle_video_note,
    handle_audio_message,
    photo_action_callback, document_action_callback,
    video_url_action_callback, handle_message_reaction
)
from bot.commands import (
    clear_context, arti_commands, start, stop,
    handle_image_command, handle_video_command, handle_music_command,
    handle_rps_command, rps_callback,
    handle_model_command, model_callback,
    handle_cancel_command, handle_rp_command,
    handle_dub_command,
    handle_vclone_command, vclone_clean_callback,
    handle_voices_command, handle_voice_save_command, handle_voice_delete_command,
    vclone_save_callback, saved_voice_callback,
    handle_my_profile_command, handle_forget_command, forget_callback,
    handle_charge_command, profile_callback,
)
from bot.queue import (
    generation_worker, dubbing_worker, vclone_worker, 
    vclone_fsm_timeout_watchdog, proactive_scheduler_worker
)
from bot.retry_bot import RetryBot
from config import TELEGRAM_TOKEN


# Глобальные переменные для работы бота
application: Optional[telegram.ext.Application] = None


def setup_signal_handlers():
    """Настройка обработчиков сигналов для graceful shutdown"""
    if sys.platform != "win32":
        def signal_handler(signum, frame):
            if application:
                # В PTB v20+ сигнал прерывания лучше отдавать самому приложению
                # Но мы можем вызвать остановку вручную если нужно
                pass
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)


def run_with_restart():
    """Запуск бота с автоматическим перезапуском при ошибках"""
    global application
    max_restarts = 10
    restart_count = 0
    restart_delay = 5
    
    while restart_count < max_restarts:
        try:
            logger.info(f"Запуск бота (попытка {restart_count + 1})...")
            
            # post_init callback — инициализация БД и воркера
            async def post_init(app):
                # Инициализация базы данных
                try:
                    from database.connection import init_db
                    await init_db()
                    logger.info("База данных инициализирована")
                except Exception as e:
                    logger.error(f"Ошибка при инициализации БД: {e}", exc_info=True)
                    logger.warning("Продолжаем работу без БД")

                app.create_task(generation_worker())
                logger.info("Глобальный воркер генерации запущен.")
                app.create_task(dubbing_worker())
                logger.info("Воркер дубляжа видео запущен.")
                app.create_task(vclone_worker())
                logger.info("Воркер vclone запущен.")
                app.create_task(vclone_fsm_timeout_watchdog(app.bot))
                logger.info("Watchdog vclone FSM запущен.")
                app.create_task(proactive_scheduler_worker(app.bot))
                logger.info("Проактивный воркер шедулера запущен.")
                logger.info("Глобальные ретраи для отправки сообщений (3 попытки) активированы.")

            # Создаём приложение с кастомными таймаутами и RetryBot
            # Таймауты подняты для нестабильной сети (особенно при VPN/прокси).
            request_config = HTTPXRequest(
                connect_timeout=30,
                read_timeout=30,
                write_timeout=30,
                pool_timeout=10,
            )
            my_bot = RetryBot(token=TELEGRAM_TOKEN, request=request_config)

            application = (
                ApplicationBuilder()
                .bot(my_bot)
                .post_init(post_init)
                .build()
            )

            # Регистрируем хендлеры команд
            application.add_handler(CommandHandler("clear_context", clear_context))
            application.add_handler(CommandHandler("arti_commands", arti_commands))
            application.add_handler(CommandHandler("cancel", handle_cancel_command))
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("stop", stop))
            application.add_handler(CommandHandler("image", handle_image_command))
            application.add_handler(CommandHandler("video", handle_video_command))
            application.add_handler(CommandHandler("music", handle_music_command))
            application.add_handler(CommandHandler("rps", handle_rps_command))
            application.add_handler(CommandHandler("rp", handle_rp_command))
            application.add_handler(CommandHandler("model", handle_model_command))
            application.add_handler(CommandHandler("models", handle_model_command))
            application.add_handler(CommandHandler("dub", handle_dub_command))
            application.add_handler(CommandHandler("vclone", handle_vclone_command))
            application.add_handler(CommandHandler("steal", handle_vclone_command))  # alias
            application.add_handler(CommandHandler("voices", handle_voices_command))
            application.add_handler(CommandHandler("voice_save", handle_voice_save_command))
            application.add_handler(CommandHandler("voice_delete", handle_voice_delete_command))
            application.add_handler(CommandHandler("my_profile", handle_my_profile_command))
            application.add_handler(CommandHandler("forget", handle_forget_command))
            application.add_handler(CommandHandler("charge", handle_charge_command))
            application.add_handler(CommandHandler("mood", handle_charge_command))

            # Callback-запросы
            application.add_handler(CallbackQueryHandler(rps_callback, pattern="^rps_"))
            application.add_handler(CallbackQueryHandler(model_callback, pattern="^model_"))
            application.add_handler(CallbackQueryHandler(photo_action_callback, pattern="^photo_act:"))
            application.add_handler(CallbackQueryHandler(document_action_callback, pattern="^doc_act:"))
            application.add_handler(CallbackQueryHandler(video_url_action_callback, pattern="^vurl:"))
            application.add_handler(CallbackQueryHandler(vclone_clean_callback, pattern="^vclone_clean:"))
            application.add_handler(CallbackQueryHandler(vclone_save_callback, pattern="^vsave:"))
            application.add_handler(CallbackQueryHandler(saved_voice_callback, pattern="^(vsel|vdel):"))
            application.add_handler(CallbackQueryHandler(forget_callback, pattern="^forget_fact:"))
            application.add_handler(CallbackQueryHandler(profile_callback, pattern="^prof_"))

            # Обработчики сообщений
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))
            application.add_handler(MessageHandler(filters.PHOTO, handle_image_message), group=2)
            application.add_handler(MessageHandler(filters.VOICE, handle_voice_message), group=3)
            application.add_handler(MessageHandler(filters.Document.ALL, handle_document), group=4)
            application.add_handler(MessageHandler(filters.VIDEO, handle_video_upload_message), group=5)
            application.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note), group=5)
            application.add_handler(MessageHandler(filters.AUDIO, handle_audio_message), group=5)
            application.add_handler(MessageHandler(filters.LOCATION, handle_location_message), group=6)
            application.add_handler(MessageReactionHandler(handle_message_reaction))

            # Обработчик ошибок
            application.add_error_handler(error_handler)

            logger.info("Бот запускается в режиме polling...")
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=telegram.Update.ALL_TYPES
            )
            
            logger.info("Бот штатно остановлен")
            break
            
        except KeyboardInterrupt:
            logger.info("Получен сигнал прерывания (Ctrl+C)")
            break
        except Exception as e:
            restart_count += 1
            logger.error(f"Критическая ошибка при работе бота (попытка {restart_count}/{max_restarts}): {e}", exc_info=True)
            
            if restart_count >= max_restarts:
                logger.critical(f"Достигнуто максимальное количество перезапусков ({max_restarts}). Завершение работы.")
                break
            
            logger.info(f"Перезапуск через {restart_delay} секунд...")
            import time
            time.sleep(restart_delay)
            restart_delay = min(restart_delay * 1.5, 60)
        finally:
            if application:
                try:
                    # В PTB v20 run_polling сам вызывает shutdown, 
                    # но если мы упали до запуска polling - вызываем вручную
                    pass
                except Exception as e:
                    logger.error(f"Ошибка при завершении приложения: {e}")


def main():
    """Главная функция для запуска бота"""
    # Fail-fast: без токена бот всё равно не сможет работать — лучше упасть сразу
    # с понятной ошибкой, чем стартовать и циклически перезапускаться.
    if not (TELEGRAM_TOKEN or "").strip():
        logger.critical(
            "TELEGRAM_TOKEN не задан. Укажите его в .env (см. .env.example). Запуск прерван."
        )
        sys.exit(1)
    try:
        setup_signal_handlers()
        run_with_restart()
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания")
    except Exception as e:
        logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
    finally:
        # Пытаемся закрыть БД в конце пути
        try:
            import asyncio
            from database.connection import close_db
            loop = asyncio.new_event_loop()
            loop.run_until_complete(close_db())
            loop.close()
            logger.info("Соединение с БД окончательно закрыто")
        except:
            pass
        logger.info("Бот завершил работу")


if __name__ == "__main__":
    main()
