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
    vclone_fsm_timeout_watchdog,
    run_supervised
)
from bot.retry_bot import RetryBot
from bot.intake import bounded_intake
from config import TELEGRAM_TOKEN


# Глобальные переменные для работы бота
application: Optional[telegram.ext.Application] = None
_instance_lock = None


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
    # Keep one event loop across retries: module-owned queues/SDK clients bind
    # to it and cannot safely be reused in a new loop after a network failure.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while restart_count < max_restarts:
        try:
            logger.info(f"Запуск бота (попытка {restart_count + 1})...")
            
            # post_init callback — инициализация БД и воркера
            async def post_init(app):
                # Инициализация базы данных
                try:
                    from database.connection import init_db
                    await init_db()
                    from database import connection
                    from utils.instance_lock import PollerLease
                    app.bot_data['poller_lease'] = await PollerLease(connection._pool, TELEGRAM_TOKEN).acquire()
                    from cognition.runtime import start_runtime
                    runtime=await start_runtime(connection._pool)
                    runtime.bot_id=app.bot.id
                    runtime.bot_username=app.bot.username
                    from bot.menu import install
                    try:
                        await install(app.bot)
                    except telegram.error.TelegramError:
                        logger.warning('Не удалось настроить кнопку Telegram; /menu остаётся доступна.')
                    logger.info("База данных инициализирована")
                except Exception as e:
                    logger.error(f"Ошибка при инициализации БД: {e}", exc_info=True)
                    raise

                def spawn_worker(coro):
                    task = asyncio.create_task(coro)
                    app.bot_data.setdefault('owned_workers',[]).append(task)
                    return task

                async def watch_instance():
                    while True:
                        await asyncio.sleep(1)
                        stop = _instance_lock is not None and _instance_lock.stop_requested()
                        try:
                            healthy = await asyncio.wait_for(app.bot_data['poller_lease'].healthy(), 3)
                        except Exception:
                            healthy = False
                        if stop or not healthy:
                            logger.info('Остановка Арти: %s', 'команда перезапуска/остановки' if stop else 'потеря блокировки poller')
                            app.stop_running()
                            return
                spawn_worker(watch_instance())

                # L-03: воркеры под супервизором — упавший автоматически перезапустится.
                # REL-01: раздельные воркеры по типам медиа (image/video/music).
                from bot.request_runtime import worker as request_worker
                for slot in range(10):
                    spawn_worker(run_supervised(request_worker, f"request_text_{slot}", app.bot, ['text']))
                for kind in ('image', 'video', 'music', 'dubbing', 'vclone'):
                    spawn_worker(run_supervised(request_worker, f"request_{kind}", app.bot, [kind]))
                logger.info("Медиа-воркеры (image/video/music) запущены (supervised).")
                logger.info("Воркер дубляжа видео запущен (supervised).")
                logger.info("Воркер vclone запущен (supervised).")
                spawn_worker(run_supervised(vclone_fsm_timeout_watchdog, "vclone_fsm_watchdog", app.bot))
                logger.info("Watchdog vclone FSM запущен (supervised).")
                from bot.media_jobs import maintenance_worker as media_maintenance
                spawn_worker(run_supervised(media_maintenance, 'media_spool_cleanup'))
                from organizer.runtime import worker as organizer_worker
                spawn_worker(run_supervised(organizer_worker,'native_organizer',app.bot))
                from cognition.runtime import get_runtime
                from cognition.intentions import intention_scheduler
                spawn_worker(run_supervised(intention_scheduler,'cognitive_intentions',get_runtime(),app.bot))
                from cognition.proactivity import group_scheduler
                spawn_worker(run_supervised(group_scheduler,'group_proactivity',get_runtime(),app.bot))
                from materials.runtime import maintenance_worker
                spawn_worker(run_supervised(maintenance_worker,'materials_maintenance'))
                from agents.runtime import agent_worker,processing_card_worker
                spawn_worker(run_supervised(agent_worker,'agent_tasks',app.bot))
                spawn_worker(run_supervised(processing_card_worker,'agent_cards',app.bot))
                logger.info("Проактивный воркер шедулера запущен (supervised).")
                logger.info("Транспорт готов; active-контексты сохраняют квитанции и не повторяют неоднозначные отправки.")

            # Создаём приложение с кастомными таймаутами и RetryBot
            async def post_stop(app):
                tasks = app.bot_data.pop('owned_workers',[])
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                from bot.queue import drain_background_tasks
                await drain_background_tasks()
                from cognition.runtime import stop_runtime
                await stop_runtime()

            async def post_shutdown(app):
                from database.connection import close_db
                lease = app.bot_data.pop('poller_lease', None)
                try:
                    # post_init can fail before PTB invokes post_stop.
                    await post_stop(app)
                finally:
                    try:
                        if lease:
                            await lease.close()
                    finally:
                        await close_db()
            # Таймауты подняты для нестабильной сети (особенно при VPN/прокси).
            request_config = HTTPXRequest(
                connect_timeout=30,
                read_timeout=30,
                write_timeout=30,
                pool_timeout=10,
            )
            my_bot = RetryBot(token=TELEGRAM_TOKEN, request=request_config)
            from cognition.telegram_scope import CognitiveUpdateProcessor

            application = (
                ApplicationBuilder()
                .bot(my_bot)
                .concurrent_updates(CognitiveUpdateProcessor(32))
                .post_init(post_init)
                .post_stop(post_stop)
                .post_shutdown(post_shutdown)
                .build()
            )

            # Регистрируем хендлеры команд
            from bot.organizer_commands import register as register_organizer
            register_organizer(application)
            from bot.menu import menu_command,menu_callback,menu_input
            application.add_handler(CommandHandler(['menu','arti_commands'],menu_command))
            application.add_handler(CallbackQueryHandler(menu_callback,pattern='^menu:'))
            application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,menu_input),group=-20)
            application.add_handler(CommandHandler("clear_context", clear_context))
            from bot.group_commands import proactivity_command,quiet_command
            application.add_handler(CommandHandler('proactivity',proactivity_command))
            application.add_handler(CommandHandler('quiet',quiet_command))
            from bot.table_commands import dataset_command,calc_command,datafix_command
            application.add_handler(CommandHandler('dataset',dataset_command))
            application.add_handler(CommandHandler('calc',calc_command))
            application.add_handler(CommandHandler('datafix',datafix_command))
            from bot.audio_commands import transcript_command,transcript_fix_command,listen_command
            application.add_handler(CommandHandler('transcript',transcript_command))
            from bot.video_commands import moment_command,storyboard_command
            application.add_handler(CommandHandler('moment',moment_command))
            application.add_handler(CommandHandler('storyboard',storyboard_command))
            from bot.material_search import material_search_command,material_review_command
            application.add_handler(CommandHandler('materials_find',material_search_command))
            application.add_handler(CommandHandler('material_review',material_review_command))
            from bot.project_commands import project_command
            application.add_handler(CommandHandler('project',project_command))
            from bot.artifact_commands import artifact_command,task_command
            from bot.workflow_commands import workflow_command
            from bot.work_cards import work_callback
            application.add_handler(CommandHandler('artifact',artifact_command))
            application.add_handler(CommandHandler('task',task_command))
            application.add_handler(CommandHandler(['decision','assignment','procedure','subscription','scenario'],workflow_command))
            application.add_handler(CallbackQueryHandler(work_callback,pattern='^work:'))
            application.add_handler(CommandHandler('transcript_fix',transcript_fix_command))
            application.add_handler(CommandHandler('listen',listen_command))
            from bot.commands import handle_memory_archive_command
            application.add_handler(CommandHandler("memory_archive",handle_memory_archive_command))
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
            from bot.media_jobs import save_voice_callback as durable_voice_save, retry_callback as durable_media_retry
            application.add_handler(CallbackQueryHandler(durable_voice_save, pattern="^media_voice_save:[0-9a-f]{32}$"))
            application.add_handler(CallbackQueryHandler(durable_media_retry, pattern="^media_retry:[0-9a-f]{32}$"))
            application.add_handler(CallbackQueryHandler(vclone_clean_callback, pattern="^vclone_clean:"))
            application.add_handler(CallbackQueryHandler(vclone_save_callback, pattern="^vsave:"))
            application.add_handler(CallbackQueryHandler(saved_voice_callback, pattern="^(vsel|vdel):"))
            application.add_handler(CallbackQueryHandler(forget_callback, pattern="^forget_(fact|source|set|asset):"))
            application.add_handler(CallbackQueryHandler(profile_callback, pattern="^prof_"))

            # Обработчики сообщений
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bounded_intake(handle_all_messages)))
            application.add_handler(MessageHandler(filters.PHOTO, bounded_intake(handle_image_message)), group=2)
            application.add_handler(MessageHandler(filters.VOICE, bounded_intake(handle_voice_message)), group=3)
            application.add_handler(MessageHandler(filters.Document.ALL, bounded_intake(handle_document)), group=4)
            application.add_handler(MessageHandler(filters.VIDEO, bounded_intake(handle_video_upload_message)), group=5)
            application.add_handler(MessageHandler(filters.VIDEO_NOTE, bounded_intake(handle_video_note)), group=5)
            application.add_handler(MessageHandler(filters.AUDIO, bounded_intake(handle_audio_message)), group=5)
            application.add_handler(MessageHandler(filters.LOCATION, bounded_intake(handle_location_message)), group=6)
            from bot.request_runtime import request_status
            application.add_handler(CommandHandler('request', request_status))
            application.add_handler(MessageReactionHandler(handle_message_reaction))

            # Обработчик ошибок
            application.add_error_handler(error_handler)
            from cognition.scope import wrap_callback
            for handlers in application.handlers.values():
                for handler in handlers:
                    handler.callback=wrap_callback(handler.callback)

            logger.info("Бот запускается в режиме polling...")
            application.run_polling(
                drop_pending_updates=False,
                close_loop=False,
                allowed_updates=telegram.Update.ALL_TYPES
            )
            
            logger.info("Бот штатно остановлен")
            break
            
        except KeyboardInterrupt:
            logger.info("Получен сигнал прерывания (Ctrl+C)")
            break
        except Exception as e:
            from utils.instance_lock import AlreadyRunning
            if isinstance(e, AlreadyRunning):
                logger.error('Другой экземпляр Арти уже использует этот токен и базу. Второй poller не запущен.')
                break
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
    from cognition.logging import install_private_log_filter
    install_private_log_filter()
    # Fail-fast: без токена бот всё равно не сможет работать — лучше упасть сразу
    # с понятной ошибкой, чем стартовать и циклически перезапускаться.
    if not (TELEGRAM_TOKEN or "").strip():
        logger.critical(
            "TELEGRAM_TOKEN не задан. Укажите его в .env (см. .env.example). Запуск прерван."
        )
        sys.exit(1)
    import argparse
    import time
    from utils.instance_lock import InstanceLock, AlreadyRunning
    parser = argparse.ArgumentParser(description='Арти: один экземпляр, управляемый перезапуск')
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--status', action='store_true', help='показать состояние запущенной Арти')
    operation.add_argument('--stop', action='store_true', help='штатно остановить запущенную Арти')
    operation.add_argument('--restart', action='store_true', help='дождаться остановки и запустить Арти заново')
    args = parser.parse_args()
    global _instance_lock
    _instance_lock = InstanceLock(TELEGRAM_TOKEN)
    if args.status:
        info = _instance_lock.status()
        print('Арти запущена, PID '+str(info.get('pid')) if info else 'Арти не запущена.')
        return
    if args.stop or args.restart:
        if _instance_lock.request_stop():
            until = time.monotonic()+45
            while _instance_lock.status() is not None and time.monotonic()<until:
                time.sleep(.25)
            if _instance_lock.status() is not None:
                print('Арти ещё завершает работу. Повтори команду после завершения; второй процесс не запущен.')
                return
        if args.stop:
            print('Арти остановлена.')
            return
    try:
        _instance_lock.acquire()
    except AlreadyRunning as exc:
        print('Арти уже запущена (PID '+str(exc.info.get('pid', 'на другом хосте'))+'). Для перезапуска: python main.py --restart')
        return
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
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
            loop.run_until_complete(close_db())
            loop.close()
            logger.info("Соединение с БД окончательно закрыто")
        except Exception:
            pass
        logger.info("Бот завершил работу")
        _instance_lock.close()


if __name__ == "__main__":
    main()
