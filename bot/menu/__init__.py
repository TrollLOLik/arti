"""Native Telegram menu. Shared entry points fork into user-owned working panels."""
import logging
from datetime import datetime, timezone
from telegram import BotCommand, MenuButtonCommands
from telegram.ext import ApplicationHandlerStop
from telegram.error import TelegramError
from cognition.scope import CURRENT_SCOPE
from materials.types import MaterialError
from .store import MenuStore
from .panel import Panel, native
from .controller import Controller

logger = logging.getLogger(__name__)
MENU_WORDS = ('Меню', 'меню', '🧭 Меню')


async def install(bot):
    await bot.set_my_commands([BotCommand('menu','Открыть меню Арти'), BotCommand('start','Начать общение'),
                              BotCommand('cancel','Остановить запросы'), BotCommand('request','Статус последнего запроса')])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def pool_for_menu():
    from cognition.runtime import get_runtime
    from database import connection
    runtime = get_runtime()
    return getattr(runtime,'pool',None) if runtime else connection._pool


async def actor():
    from materials.runtime import actor_for_current
    return await actor_for_current()


async def menu_command(update, context):
    scope = CURRENT_SCOPE.get()
    if not scope or not update.effective_user or scope.sender_kind != 'user' or scope.topic_id < 0 and scope.group:
        return
    pool = pool_for_menu()
    if pool is None:
        await update.effective_message.reply_text('Меню станет доступно после запуска хранилища.')
        return
    store = MenuStore(pool)
    async with store.locked(scope.chat_id,scope.topic_id,update.effective_user.id):
        current = await actor()
        row = await store.get(scope.chat_id,scope.topic_id,current.user_id)
        existed = row is not None
        request_id='message:'+str(update.effective_message.message_id)
        if row and not await store.consume(row,request_id):
            return
        previous_message_id = row['message_id'] if row else None
        row = await store.open(scope.chat_id,scope.topic_id,current.user_id,current.scope.key)
        if not existed:
            await store.consume(row,request_id)
        # An explicit request must appear at the bottom of the conversation.
        # Buttons continue editing this new panel; redelivery stays deduplicated.
        await store.save(row,message_id=None,status='new',content_hash=None)
        panel = Panel(store,row,context.bot)
        controller = Controller(panel,update,context,current)
        controller.clear_legacy(force=True)
        try:
            await controller.show('home',allow_create=True)
        except TelegramError:
            logger.info('Menu opening outcome is unconfirmed; no automatic replacement')
        # Old tokens are already invalid in SQL. Remove their visible buttons
        # once; failure to clean an old message must not hide the new menu.
        if previous_message_id is not None:
            try:
                await native(context.bot,'edit_message_reply_markup')(
                    chat_id=scope.chat_id,message_id=previous_message_id,reply_markup=None)
            except TelegramError:
                logger.info('Previous menu buttons could not be removed; tokens revoked')


async def cancel_menu_input(update, context):
    # Commands invoked inside the panel already hold its lock and reset state.
    if getattr(context.bot,'_menu_panel',None) is not None:
        return
    scope = CURRENT_SCOPE.get()
    pool = pool_for_menu()
    if not scope or pool is None or not update.effective_user or scope.sender_kind!='user':
        return
    store = MenuStore(pool)
    async with store.locked(scope.chat_id,scope.topic_id,update.effective_user.id):
        row = await store.get(scope.chat_id,scope.topic_id,update.effective_user.id)
        if row and row['state'].get('pending') in ('legacy','form','confirm_form'):
            await store.save(row,state={},actions={},content_hash=None)


def public_action(row, action, scope, user_id):
    return (row['state'].get('shared') is True and scope.group and row['chat_id']==scope.chat_id
            and row['topic_id']==scope.topic_id and any(k in action for k in ('nav','form','command','confirm','close'))
            and not any(k in action for k in ('object','id','legacy','legacy_text','submit','value')))


async def menu_callback(update, context):
    query = update.callback_query
    scope = CURRENT_SCOPE.get()
    if not query or not scope or not query.message or scope.sender_kind!='user':
        return
    try:
        await query.answer()
        _, id, token = query.data.split(':')
        store = MenuStore(pool_for_menu())
        source = await store.by_id(id)
        if source is None:
            raise MaterialError('menu_expired')
        current = await actor()
        if source['message_id']!=query.message.message_id or source['chat_id']!=scope.chat_id or source['topic_id']!=scope.topic_id or source['scope_key']!=current.scope.key:
            raise MaterialError('menu_wrong_scope')
        value = source['actions'].get(token)
        if not value or source['status']!='active' or source['expires_at']<=datetime.now(timezone.utc):
            raise MaterialError('menu_stale_button')
        shared = public_action(source,value,scope,current.user_id)
        if source['user_id']!=current.user_id and not shared:
            await query.answer('Это чужая рабочая панель. Открой своё меню.',show_alert=True)
            return
        async with store.locked(scope.chat_id,scope.topic_id,current.user_id):
            fresh = await store.by_id(id)
            if fresh['revision']!=source['revision'] or fresh['actions'].get(token)!=value:
                raise MaterialError('menu_stale_button')
            row = await store.get(scope.chat_id,scope.topic_id,current.user_id)
            consumed=False
            if source['user_id']!=current.user_id:
                if row:
                    if not await store.consume(row,'callback:'+query.id):
                        return
                    consumed=True
                if row:
                    Controller(Panel(store,row,context.bot),update,context,current).clear_legacy()
                row = await store.open(scope.chat_id,scope.topic_id,current.user_id,current.scope.key)
            else:
                row = fresh
                store.action(row,token,user_id=current.user_id,chat_id=scope.chat_id,topic_id=scope.topic_id,
                             message_id=query.message.message_id,scope_key=current.scope.key)
            if not consumed and not await store.consume(row,'callback:'+query.id):
                return
            panel=Panel(store,row,context.bot)
            controller=Controller(panel,update,context,current)
            if row['message_id'] is None or row['status']=='gone':
                await store.save(row,message_id=None,status='new')
                from .views import SECTIONS
                await panel.render(*SECTIONS['home'],allow_create=True,state=dict(shared=True))
            # Invalidate the clicked generation before any awaited side effect.
            await store.save(row,actions={},content_hash=None)
            try:
                await controller.act(value)
            except (MaterialError,ValueError,TypeError,KeyError) as exc:
                await explain(controller,exc)
    except (MaterialError,ValueError):
        await query.answer('Кнопка устарела или относится к другой теме. Открой меню заново.',show_alert=True)
    except TelegramError:
        logger.info('Menu edit outcome is unconfirmed; no automatic replacement')


async def explain(controller, exc):
    codes = {
        'menu_materials_disabled':'Сохранённая работа с файлами пока не включена администратором.',
        'menu_agents_disabled':'Исполнение агентских задач пока не включено администратором.',
        'menu_roleplay_private':'Ролевая сцена доступна в личном чате. Открой моё меню там.',
        'menu_review_required':'Сначала пролистай все части подтверждения.',
        'project_selection_required':'Сначала выбери или создай проект.',
        'menu_complex_binding':'У процедуры сложные входные данные. Простая форма для неё пока недоступна.',
        'menu_reference_reupload':'Образец для генерации не сохранился после перезапуска. Открой генерацию и прикрепи его заново — подменять запрос не буду.',
        'menu_interval_invalid':'Для интервала нужно целое число минут, от 5 до 44640.',
        'menu_schedule_invalid':'Время запуска нужно записать как 18:00.',
        'subscription_schedule_invalid':'Проверь время, часовой пояс и интервал запуска.',
    }
    text=codes.get(getattr(exc,'code',''),'Не получилось выполнить действие. Права, источник или версия могли измениться. Выбери запись заново.')
    await controller.render(text,controller.panel.navigation(),screen='notice',state={})


async def is_menu_input(update, pool=None):
    message = getattr(update,'message',None)
    scope = CURRENT_SCOPE.get()
    if not message or not scope or not update.effective_user or scope.sender_kind!='user':
        return False
    if message.text in MENU_WORDS:
        return True
    pool=pool or pool_for_menu()
    if pool is None:
        return False
    row=await MenuStore(pool).get(scope.chat_id,scope.topic_id,update.effective_user.id)
    if not row or row['status']!='active' or row['expires_at']<=datetime.now(timezone.utc):
        return False
    state=row['state']
    if state.get('pending') not in ('form','legacy') or datetime.fromisoformat(state['input_until'])<=datetime.now(timezone.utc):
        return False
    if state.get('pending')=='legacy' and not state.get('expects_text',True):
        return False
    if scope.group and getattr(getattr(message,'reply_to_message',None),'message_id',None)!=row['message_id']:
        return False
    return True


async def menu_input(update, context):
    if not await is_menu_input(update):
        scope=CURRENT_SCOPE.get()
        pool=pool_for_menu()
        if scope and pool and update.effective_user:
            store=MenuStore(pool)
            row=await store.get(scope.chat_id,scope.topic_id,update.effective_user.id)
            if row and row['state'].get('pending')=='legacy' and datetime.fromisoformat(row['state']['input_until'])<=datetime.now(timezone.utc):
                async with store.locked(scope.chat_id,scope.topic_id,update.effective_user.id):
                    current=await actor()
                    Controller(Panel(store,row,context.bot),update,context,current).clear_legacy()
                    await store.save(row,state={})
        return
    if update.message.text in MENU_WORDS:
        await menu_command(update,context)
        raise ApplicationHandlerStop
    scope=CURRENT_SCOPE.get()
    store=MenuStore(pool_for_menu())
    async with store.locked(scope.chat_id,scope.topic_id,update.effective_user.id):
        # Re-read after acquiring the lock; another callback may have cancelled input.
        if not await is_menu_input(update):
            raise ApplicationHandlerStop
        current=await actor()
        row=await store.get(scope.chat_id,scope.topic_id,current.user_id)
        if row['scope_key']!=current.scope.key:
            raise ApplicationHandlerStop
        if not await store.consume(row,'message:'+str(update.message.message_id)):
            raise ApplicationHandlerStop
        controller=Controller(Panel(store,row,context.bot),update,context,current)
        try:
            if row['state']['pending']=='form':
                await controller.answer(update.message.text or update.message.caption or '',update.message)
            else:
                await controller.legacy_input(update.message.text or update.message.caption or '',update.message)
        except (MaterialError,ValueError,TypeError,KeyError) as exc:
            await explain(controller,exc)
        except TelegramError:
            logger.info('Menu input edit unconfirmed; no duplicate send')
    raise ApplicationHandlerStop
