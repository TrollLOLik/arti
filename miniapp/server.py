"""Same-origin aiohttp API for Arti. No AI clients, secrets or blob paths in JSON."""
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web
from .auth import AuthError, validate_init_data
from . import handoffs, life
from cognition import life_settings
from .sessions import SessionStore, DemoSessions, check_csrf, csrf
from conversations.repository import Repository as ConversationRepository
from conversations.demo import DemoRepository
from conversations.types import ConversationError, private_context

log = logging.getLogger(__name__)
FRONTEND = Path(__file__).with_name('frontend') / 'dist'
ITEM_FIELDS = ('id', 'kind', 'title', 'due_at', 'timezone', 'state', 'notification_state', 'version')


@dataclass(frozen=True)
class Settings:
    demo: bool = False
    bot_token: str = ''
    bot_username: str = ''
    public_url: str = ''
    materials: bool = False
    voices: bool = False
    session_seconds: int = 1800

    def __post_init__(self):
        if self.bot_username and not re.fullmatch(r'[A-Za-z0-9_]{5,32}', self.bot_username):
            raise ValueError('Invalid Telegram bot username')
        if not self.demo and (not self.bot_token or not self.bot_username):
            raise ValueError('Live Mini App requires TELEGRAM_TOKEN and bot username')
        if not 60 <= self.session_seconds <= 3600:
            raise ValueError('Session TTL must be between 60 and 3600 seconds')
        if self.public_url:
            from .settings import public_url
            public_url(self.public_url)


STATE = web.AppKey('miniapp_state', dict)
USER = 'miniapp_user'


def response(value, *, status=200):
    return web.json_response(value, status=status, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=lambda d: d.isoformat() if isinstance(d, datetime) else str(d)))


def failure(code, message, status=400):
    return response({'error': {'code': code, 'message': message}}, status=status)


def public_item(row):
    return {key: row.get(key) for key in ITEM_FIELDS}


def request_key(body):
    value = body.get('idempotency_key')
    if not isinstance(value, str) or len(value) > 36:
        raise ValueError('idempotency_key')
    return str(uuid.UUID(value))


async def body_for(request, allowed):
    if request.content_type != 'application/json':
        raise ValueError('json_required')
    data = await request.json()
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('invalid_fields')
    return data


class RateLimit:
    def __init__(self):
        self.clients = OrderedDict()

    def accept(self, key, limit, window=60):
        now = time.monotonic()
        queue = self.clients.setdefault(key, deque())
        self.clients.move_to_end(key)
        while queue and queue[0] <= now - window:
            queue.popleft()
        while len(self.clients) > 10000:
            self.clients.popitem(last=False)
        if len(queue) >= limit:
            return False
        queue.append(now)
        return True


def cookie_name(settings):
    return 'arti_demo_session' if settings.demo else '__Host-arti_session'


def session_response(request, token, session):
    settings = request.app[STATE]['settings']
    return dict(user=session['user'], csrf_token=csrf(token), expires_at=session['expires_at'],
                mode='demo' if settings.demo else 'telegram')


async def dispatch(request, handler):
    state, unsafe = request.app[STATE], request.method not in ('GET', 'HEAD')
    settings = state['settings']
    if request.path.startswith('/api/'):
        if not state['rate'].accept(('ip', request.remote), 240):
            return failure('rate_limit', 'Слишком много запросов. Подожди минуту.', 429)
        if unsafe:
            expected = settings.public_url.rstrip('/') if settings.public_url else f'{request.scheme}://{request.host}'
            origin = request.headers.get('Origin')
            if origin != expected and (origin is not None or not settings.demo):
                return failure('wrong_origin', 'Открой приложение из Telegram.', 403)
        if request.path not in ('/api/auth/telegram', '/api/auth/demo'):
            token = request.cookies.get(cookie_name(settings), '')
            session = await state['sessions'].read(token)
            request[USER], request['session_token'], request['session'] = session['user'], token, session
            if settings.demo:
                request['demo'] = session['demo']
            if unsafe:
                try:
                    check_csrf(token, request.headers.get('X-CSRF-Token', ''))
                except AuthError:
                    return failure('invalid_csrf', 'Обнови приложение и попробуй снова.', 403)
                if not state['rate'].accept(('user', request[USER]['id']), 40):
                    return failure('rate_limit', 'Слишком много действий. Подожди минуту.', 429)
    return await handler(request)


@web.middleware
async def boundary(request, handler):
    try:
        result = await dispatch(request, handler)
    except AuthError:
        result = failure('unauthorized', 'Сессия истекла. Закрой приложение и открой его снова из Telegram.', 401)
    except handoffs.HandoffConflict:
        result = failure('handoff_conflict', 'Этот запрос уже открыт. Создай новый запрос.', 409)
    except ConversationError as exc:
        result = failure(exc.code, 'Беседа недоступна.' if exc.status == 404 else 'Не удалось выполнить действие.', exc.status)
    except web.HTTPException as exc:
        if not request.path.startswith('/api/'):
            raise
        result = failure('http_error', 'Запрос не поддерживается.', exc.status)
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError) as exc:
        code = str(exc)
        if code == 'stale_item':
            result = failure('stale_item', 'Дело уже изменилось. Обнови список и попробуй снова.', 409)
        else:
            result = failure('invalid_request', 'Проверь название, дату и заполненные поля.', 400)
    except Exception:
        # Do not log URLs, bodies or initData. A server error must never switch
        # to demo or reveal configuration, provider keys, files or SQL details.
        log.error('Mini App request failed (%s %s)', request.method, request.path.split('/')[1:3])
        result = failure('unavailable', 'Сервис временно недоступен. Попробуй ещё раз.', 503)
    if request.path.startswith('/api/'):
        result.headers['Cache-Control'] = 'no-store'
    result.headers.update({
        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    })
    return result


async def login(request):
    state, now = request.app[STATE], datetime.now(timezone.utc)
    settings = state['settings']
    is_demo = request.path == '/api/auth/demo'
    if is_demo:
        if not settings.demo or request.remote not in ('127.0.0.1', '::1'):
            return failure('not_found', 'Запрос не поддерживается.', 404)
        await body_for(request, set())
        user = dict(id=secrets.randbelow(2**52)+1, first_name='Гость', username='')
        expires = now+timedelta(seconds=settings.session_seconds)
    else:
        if settings.demo:
            return failure('wrong_mode', 'Этот сервер работает в режиме локального просмотра.', 409)
        body = await body_for(request, {'init_data'})
        raw = body.get('init_data', '')
        user = validate_init_data(raw, settings.bot_token)
        from urllib.parse import parse_qsl
        signed_at = int(dict(parse_qsl(raw))['auth_date'])
        expires = min(now+timedelta(seconds=settings.session_seconds), datetime.fromtimestamp(signed_at+3600, timezone.utc))
        if expires <= now:
            raise AuthError('expired_init_data')
    if not state['rate'].accept(('login', user['id']), 10):
        return failure('rate_limit', 'Слишком много попыток входа. Подожди минуту.', 429)
    await state['conversations'].ensure_user(user)
    previous = request.cookies.get(cookie_name(settings), '')
    token = await state['sessions'].create(user, expires)
    await state['sessions'].revoke(previous)
    session = await state['sessions'].read(token)
    result = response(session_response(request, token, session))
    result.set_cookie(cookie_name(settings), token, secure=not settings.demo, httponly=True,
                      samesite='Lax' if settings.demo else 'None', path='/', max_age=max(1, int((expires-now).total_seconds())))
    return result


async def session_info(request):
    return response(session_response(request, request['session_token'], request['session']))


async def logout(request):
    await request.app[STATE]['sessions'].revoke(request['session_token'])
    result = response(dict(ok=True))
    settings = request.app[STATE]['settings']
    # set_cookie supports these attributes throughout the root aiohttp 3.x range;
    # older del_cookie versions only accept domain/path.
    result.set_cookie(cookie_name(settings), '', path='/', secure=not settings.demo,
                      httponly=True, samesite='Lax' if settings.demo else 'None',
                      max_age=0, expires='Thu, 01 Jan 1970 00:00:00 GMT')
    return result


async def bootstrap(request):
    state = request.app[STATE]
    settings = state['settings']
    live = bool(state['service'] and state['service'].available and not settings.demo)
    tz = 'UTC' if settings.demo else await organizer(request).get_timezone(request[USER]['id'], request[USER]['id'])
    return response(dict(mode='demo' if settings.demo else 'telegram', user=request[USER], bot_username=settings.bot_username,
                         csrf_token=csrf(request['session_token']), timezone=tz or 'UTC',
                         capabilities=dict(chat_read=True, chat_send=live, human_chats=False, rooms=False, create=False,
                                           history_sync=live, live_events=live, memory=False, projects=settings.materials, library=settings.materials,
                                           organizer=True, voices=settings.voices, arti_life=not settings.demo)))


async def conversation_list(request):
    rows = await request.app[STATE]['conversations'].list(request[USER]['id'])
    return response(dict(items=rows, next_cursor=None))


async def conversation_detail(request):
    row = await request.app[STATE]['conversations'].get(request[USER]['id'], request.match_info['id'])
    return response(dict(conversation=row))


async def conversation_messages(request):
    before = int(request.query['before']) if 'before' in request.query else None
    limit = int(request.query.get('limit', '50'))
    after = int(request.query['after']) if 'after' in request.query else None
    kwargs = dict(before=before,limit=limit)
    if after is not None:
        if not request.app[STATE]['service']:
            raise ValueError('invalid_cursor')
        kwargs['after'] = after
    rows = await request.app[STATE]['conversations'].messages(request[USER]['id'], request.match_info['id'], **kwargs)
    return response(rows)


async def send_message(request):
    await request.app[STATE]['conversations'].get(request[USER]['id'], request.match_info['id'])
    service = chat_service(request)
    body = await body_for(request, {'text','client_message_id','reply_to_id','history_revision'})
    if type(body.get('history_revision')) is not int or body['history_revision']<1:
        raise ConversationError('invalid_revision')
    result = await service.accept(request[USER]['id'], request.match_info['id'],body.get('text'),
        body.get('client_message_id'),reply_to_id=body.get('reply_to_id'),history_revision=body['history_revision'])
    return response(result,status=202)


def chat_service(request):
    state=request.app[STATE]
    service=state['service']
    if state['settings'].demo or not service or not service.available:
        raise ConversationError('feature_unavailable',409)
    return service


async def conversation_events(request):
    service=chat_service(request)
    result=await service.events(request[USER]['id'],request.match_info['id'],
        after=int(request.query.get('after','0')),limit=int(request.query.get('limit','100')))
    return response(result)


async def conversation_request(request):
    result=await chat_service(request).request_status(request[USER]['id'],request.match_info['id'],request.match_info['request_id'])
    return response(result)


async def conversation_stream(request):
    service=chat_service(request)
    state=request.app[STATE]
    actor=request[USER]['id']
    after=int(request.headers.get('Last-Event-ID',request.query.get('after','0')))
    batch=await service.events(actor,request.match_info['id'],after=after)
    streams=state['streams']
    if streams.get(actor,0)>=3:
        return failure('stream_limit','Закрой лишние окна приложения.',429)
    streams[actor]=streams.get(actor,0)+1
    stream=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-store',
        'X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})
    async def emit(kind,data,event_id=None):
        encoded=json.dumps(data,ensure_ascii=False,default=lambda value:value.isoformat() if isinstance(value,datetime) else str(value))
        frame=(f'id: {event_id}\n' if event_id is not None else '')+f'event: {kind}\ndata: {encoded}\n\n'
        async with asyncio.timeout(10):
            await stream.write(frame.encode('utf-8'))
    try:
        await stream.prepare(request)
        while not state['closing']:
            # Re-authorize every heartbeat; revoked sessions cannot keep a private stream.
            try:
                session=await state['sessions'].read(request['session_token'])
                if session['user']['id']!=actor:
                    raise AuthError('unauthorized')
                batch=await service.events(actor,request.match_info['id'],after=after)
            except AuthError:
                await emit('session.expired',{})
                break
            except ConversationError as exc:
                if exc.code=='history_rebuilding':
                    await asyncio.sleep(1)
                    continue
                await emit('revoked',{})
                break
            await emit('state',batch,batch['cursor'])
            after=batch['cursor']
            await asyncio.sleep(2)
    except (ConnectionError,asyncio.TimeoutError):
        pass
    finally:
        streams[actor]-=1
        if not streams[actor]:
            del streams[actor]
    return stream


def organizer(request):
    from organizer.repository import Repository
    return Repository(request.app[STATE]['pool'])


async def list_items(request):
    closed = request.query.get('include_closed', 'false') == 'true'
    if 'demo' in request:
        items = request['demo']['items']
        return response(dict(items=[public_item(row) for row in items if closed or row['state'] == 'active']))
    user = request[USER]['id']
    rows = await organizer(request).list(user, user, include_closed=closed, limit=100)
    return response(dict(items=[public_item(row) for row in rows]))


def validate_create(body):
    key = request_key(body)
    title = body.get('title')
    kind = body.get('kind')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500 or kind not in ('todo', 'event', 'reminder'):
        raise ValueError('invalid_item')
    due, tz = None, None
    if kind != 'todo':
        tz = body.get('timezone')
        if not isinstance(tz, str) or len(tz) > 100:
            raise ValueError('invalid_timezone')
        ZoneInfo(tz)
        raw = body.get('due_at')
        if not isinstance(raw, str):
            raise ValueError('invalid_date')
        due = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        if due.tzinfo is None or not now < due <= now+timedelta(days=365):
            raise ValueError('schedule_out_of_range')
    elif body.get('due_at') is not None:
        raise ValueError('todo_has_no_deadline')
    return key, title.strip(), kind, due, tz


async def create_item(request):
    body = await body_for(request, {'kind', 'title', 'due_at', 'timezone', 'idempotency_key'})
    key, title, kind, due, tz = validate_create(body)
    if 'demo' in request:
        demo = request['demo']
        digest = json.dumps(body, sort_keys=True)
        previous = demo['actions'].get(key)
        if previous:
            if previous[0] != digest:
                return failure('idempotency_conflict', 'Создай новый запрос.', 409)
            return response(dict(item=public_item(previous[1])))
        if len(demo['items']) >= 100:
            return failure('item_limit', 'В демо можно сохранить до 100 дел.', 409)
        row = dict(id=uuid.uuid4().hex[:12], kind=kind, title=title, due_at=due, timezone=tz, state='active', notification_state='none' if kind == 'todo' else 'pending', version=1)
        demo['items'].append(row)
        demo['actions'][key] = (digest, dict(row))
    else:
        user = request[USER]['id']
        repo = organizer(request)
        row = await repo.create(user, user, kind, title, 'miniapp:'+key, due_at=due, timezone_name=tz)
        if (row['title'], row['kind'], row.get('due_at'), row.get('timezone')) != (title, kind, due, tz):
            return failure('idempotency_conflict', 'Создай новый запрос.', 409)
    return response(dict(item=public_item(row)), status=201)


async def change_item(request):
    body = await body_for(request, {'expected_version', 'state', 'idempotency_key'})
    key = request_key(body)
    version, status = body.get('expected_version'), body.get('state')
    if type(version) is not int or version < 1 or status not in ('active', 'done', 'cancelled'):
        raise ValueError('invalid_state')
    if 'demo' in request:
        demo = request['demo']
        row = next((row for row in demo['items'] if row['id'] == request.match_info['id']), None)
        if row is None:
            return failure('not_found', 'Дело не найдено.', 404)
        digest = json.dumps([request.match_info['id'], body], sort_keys=True)
        previous = demo['actions'].get(key)
        if previous:
            if previous[0] != digest:
                return failure('idempotency_conflict', 'Создай новый запрос.', 409)
            return response(dict(item=public_item(previous[1])))
        if row['version'] != version:
            raise ValueError('stale_item')
        if row['kind'] != 'todo' and status != 'cancelled':
            raise ValueError('task_operation_only')
        row.update(state=status, version=version+1)
        if status == 'cancelled':
            row['notification_state'] = 'cancelled'
        demo['actions'][key] = (digest, dict(row))
    else:
        user = request[USER]['id']
        row = await organizer(request).change(user, user, request.match_info['id'], status, source_key='miniapp:'+key, expected_version=version)
        if row is None:
            return failure('not_found', 'Дело не найдено.', 404)
    return response(dict(item=public_item(row)))


def material_actor(request):
    from materials.types import AccessContext, MaterialScope
    user = request[USER]['id']
    ref = private_context(user)
    return AccessContext(MaterialScope(ref.persona_id, ref.telegram_chat_id, ref.topic_id, 'private', ref.mode, ref.scene_id), user, 'user:'+str(user))


async def projects(request):
    settings = request.app[STATE]['settings']
    if settings.demo:
        return response(dict(items=[], supported=False))
    if not settings.materials:
        return response(dict(items=[], supported=False))
    from materials.repository import MaterialRepository
    from projects.repository import ProjectRepository
    rows = await ProjectRepository(MaterialRepository(request.app[STATE]['pool'])).list(material_actor(request))
    return response(dict(items=[dict(id=p.id, title=p.title, goal=p.goal, status=p.status, revision=p.revision) for p in rows]))


async def library(request):
    state = request.app[STATE]
    if state['settings'].demo:
        return response(dict(supported=False, items=[]))
    if not state['settings'].materials:
        return response(dict(items=[], supported=False))
    from materials.repository import MaterialRepository
    from materials.types import MaterialError
    actor = material_actor(request)
    repo = MaterialRepository(state['pool'])
    async with state['pool'].acquire() as conn:
        ids = await conn.fetch('''SELECT id FROM material_assets WHERE realm=$1 AND scope_key=$2 AND owner_id=$3
            AND erased_at IS NULL AND expires_at>NOW() ORDER BY created_at DESC,id LIMIT 100''', actor.realm, actor.scope.key, actor.user_id)
    items = []
    for ref in ids:
        try:
            row, version = await repo.read(ref['id'], actor)
        except MaterialError:
            continue
        mime = version['mime']
        kind = 'image' if mime.startswith('image/') else 'audio' if mime.startswith('audio/') else 'video' if mime.startswith('video/') else 'document'
        items.append(dict(id=row['id'], filename=row['filename'], title=row['filename'], kind=kind, created_at=row['created_at'], size_bytes=version['byte_size']))
    return response(dict(items=items, supported=True))


async def download(request):
    state = request.app[STATE]
    if state['settings'].demo:
        return failure('not_found', 'Файл не найден.', 404)
    else:
        if not state['settings'].materials:
            return failure('not_found', 'Файл не найден.', 404)
        from materials.repository import MaterialRepository
        from materials.storage import LocalBlobStore
        from materials.types import MaterialError
        repo = MaterialRepository(state['pool'])
        actor = material_actor(request)
        try:
            row, version = await repo.read(request.match_info['id'], actor)
            store = LocalBlobStore(os.getenv('ARTI_MATERIALS_DIR', 'data/materials'))
            data = await asyncio.to_thread(store.read, version['blob_id'], version['sha256'], repo.quotas.max_file_bytes)
            # Recheck ownership and revocation after filesystem I/O.
            current, _ = await repo.read(row['id'], actor, version['version'])
            if current['generation'] != row['generation']:
                raise MaterialError('stale_material')
            filename = row['filename']
        except MaterialError:
            return failure('not_found', 'Файл недоступен или срок хранения истёк.', 404)
    return web.Response(body=data, content_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename=arti-file; filename*=UTF-8''"+quote(filename, safe=''),
        'Cache-Control': 'no-store',
    })


async def handoff(request):
    body = await body_for(request, {'action', 'prompt', 'idempotency_key'})
    key = request_key(body)
    action, prompt = body.get('action'), body.get('prompt', '')
    if action not in handoffs.ACTIONS or not isinstance(prompt, str) or len(prompt) > 3500:
        raise ValueError('invalid_handoff')
    if action == 'talk' and not prompt.strip():
        # An empty conversation entry opens the normal native menu.
        prompt = ''
    state = request.app[STATE]
    settings = state['settings']
    if action == 'voices' and not settings.voices:
        return failure('feature_disabled', 'Голосовые инструменты сейчас не включены. Голосовое сообщение можно прислать прямо в чат.', 409)
    if action in ('projects', 'materials') and not settings.demo and not settings.materials:
        return failure('feature_disabled', 'Работа с проектами и материалами пока не включена.', 409)
    if settings.demo:
        return response(dict(url=None, demo=True))
    token = await handoffs.prepare(state['pool'], request[USER]['id'], action, prompt.strip(), key)
    return response(dict(url=f'https://t.me/{settings.bot_username}?start=ma_{token}', demo=False))


def demo_life(request):
    return request['demo'].setdefault('life', dict(life_settings.DEFAULTS))


async def arti_life(request):
    if 'demo' in request:
        return response(life.empty_view(dict(demo_life(request))))
    return response(await life.view(request.app[STATE]['pool'], request[USER]['id'], datetime.now(timezone.utc)))


async def life_settings_get(request):
    if 'demo' in request:
        return response(dict(settings=dict(demo_life(request))))
    async with request.app[STATE]['pool'].acquire() as conn:
        return response(dict(settings=await life_settings.read(conn, request[USER]['id'])))


async def life_settings_put(request):
    change = life.settings_change(await body_for(request, {'mentions', 'explainer_seen', 'reset'}))
    if 'demo' in request:
        current = demo_life(request)
        current.update(life_settings.DEFAULTS if change is None else change)
        return response(dict(settings=dict(current)))
    user = request[USER]['id']
    async with request.app[STATE]['pool'].acquire() as conn:
        if change is None:
            result = await life_settings.reset(conn, user)
        else:
            result = await life_settings.update(conn, user, datetime.now(timezone.utc), **change)
    return response(dict(settings=result))


async def index(request):
    if not (FRONTEND / 'index.html').is_file():
        return web.Response(text='Клиент Mini App ещё не собран. Выполни npm ci и npm run build в miniapp/frontend.', status=503)
    return web.FileResponse(FRONTEND / 'index.html', headers={'Cache-Control': 'no-cache'})


async def health(request):
    return response(dict(status='ok', mode='demo' if request.app[STATE]['settings'].demo else 'telegram'))


def create_app(settings, *, pool=None, sessions=None, conversations=None, service=None):
    if not settings.demo and pool is None:
        raise ValueError('Live Mini App requires the bot database pool')
    app = web.Application(middlewares=[boundary], client_max_size=40*1024, handler_args={'handler_cancellation':True})
    app[STATE] = dict(settings=settings, pool=pool, rate=RateLimit(),service=service,streams={},closing=False,
                      sessions=sessions if sessions is not None else (DemoSessions() if settings.demo else SessionStore(pool)),
                      conversations=conversations if conversations is not None else (DemoRepository() if settings.demo else service if service is not None else ConversationRepository(pool)))
    async def stop_streams(application):
        application[STATE]['closing']=True
    app.on_shutdown.append(stop_streams)
    app.add_routes([
        web.get('/', index), web.get('/api/bootstrap', bootstrap),
        web.post('/api/auth/telegram', login), web.post('/api/auth/demo', login),
        web.get('/api/session', session_info), web.post('/api/auth/logout', logout),
        web.get('/api/conversations', conversation_list), web.get('/api/conversations/{id}', conversation_detail),
        web.get('/api/conversations/{id}/messages', conversation_messages), web.post('/api/conversations/{id}/messages', send_message),
        web.get('/api/conversations/{id}/events',conversation_events),
        web.get('/api/conversations/{id}/events/stream',conversation_stream),
        web.get('/api/conversations/{id}/requests/{request_id}',conversation_request),
        web.get('/api/organizer', list_items), web.post('/api/organizer', create_item),
        web.patch('/api/organizer/{id}', change_item),
        web.get('/api/projects', projects), web.get('/api/library', library),
        web.get('/api/library/{id}/download', download), web.post('/api/handoff', handoff),
        web.get('/api/arti/life', arti_life), web.get('/api/arti/life/settings', life_settings_get),
        web.put('/api/arti/life/settings', life_settings_put),
        web.get('/healthz', health),
    ])
    # Never serve source files, old prototype pages or the material blob directory.
    # Directory exists after build; missing build remains an explicit 503 above.
    if FRONTEND.is_dir():
        app.router.add_static('/static/app/', FRONTEND, show_index=False, follow_symlinks=False)
    return app


async def start_for_bot(bot, pool):
    """Opt-in listener in the existing event loop, with explicit shutdown owner."""
    if os.getenv('ARTI_MINIAPP_ENABLED', '0').lower() not in ('1', 'true', 'yes'):
        return None
    from .settings import public_url
    url = public_url(os.getenv('ARTI_MINIAPP_URL', ''))
    settings = Settings(bot_token=bot.token, bot_username=bot.username, public_url=url,
                        materials=os.getenv('ARTI_MATERIALS_ENABLED', '0').lower() in ('1', 'true', 'yes'),
                        voices=os.getenv('TTS_ENABLED', 'false').lower() == 'true')
    from conversations.schema import ensure_schema
    await ensure_schema(pool)
    await handoffs.initialize(pool)
    from cognition.runtime import get_runtime
    from conversations.service import ArtiConversationService
    service=ArtiConversationService(pool,get_runtime())
    app=create_app(settings,pool=pool,service=service)
    async def mirror_lifecycle(app):
        from conversations.mirror import MirrorWorker
        from telegram.ext import ExtBot
        # Bypass RetryBot's transport retries: a timeout can follow a successful send.
        async def send(**kwargs):
            return await ExtBot.send_message(bot,**kwargs)
        task=asyncio.create_task(MirrorWorker(pool,send).run(),name='miniapp-telegram-mirror')
        yield
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    app.cleanup_ctx.append(mirror_lifecycle)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, os.getenv('ARTI_MINIAPP_HOST', '127.0.0.1'), int(os.getenv('ARTI_MINIAPP_PORT', '8765'))).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
