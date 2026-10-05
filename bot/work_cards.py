"""Opaque actions authorize nothing; receipt slots never retry unknown sends."""
import uuid
from hashlib import sha256
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from io import BytesIO
from materials.types import MaterialError,EvidenceRef
from artifacts.revisions import ArtifactRepository
from projects.repository import ProjectRepository

async def request_sources(service,actor,message):
    source=getattr(message,'_menu_request_source',None) or f'telegram:{message.chat_id}:{message.message_id}:user'
    asset=await service.ingest((message.text or message.caption or 'Explicit workflow request').encode(),'request.txt',actor,source,source)
    id,bundle=await service.extract(asset['id'],actor); b=bundle.blocks[0]
    from cognition.runtime import get_runtime
    runtime=get_runtime()
    if runtime:
        from cognition.types import AudienceScope,Origin
        await runtime.ingest(actor.scope.chat_id,actor.user_id,message.text or message.caption or 'Workflow request',str(getattr(message,'_menu_event_id',message.message_id)),actor.scope.mode,context=await runtime.context(actor.scope.chat_id,actor.scope.mode,actor.scope.topic_id),origin=Origin.USER,audience=AudienceScope('private' if actor.scope.chat_type=='private' else ('topic' if actor.scope.topic_id>0 else 'group'),actor.scope.chat_id,actor.scope.topic_id))
    return [EvidenceRef(asset['id'],1,id,b.block_id,b.locator)]

class WorkCards:
    def __init__(self,service): self.service=service; self.pool=service.repository.pool; self.artifacts=ArtifactRepository(service.repository)
    async def actions(self,row,actor):
        from telegram import InlineKeyboardMarkup,InlineKeyboardButton
        actions=[]
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM arti_work_actions WHERE expires_at<NOW()')
            for name,label in [('sources','Источники'),('format','Формат'),('json','Данные (JSON)'),('pdf','Для печати (PDF)'),('png','Картинки (PNG)'),('svg','Для редактора (SVG)'),('accept','Принять'),('reject','Отклонить')]:
                id=uuid.uuid4().hex
                await conn.execute('INSERT INTO arti_work_actions(id,realm,owner_id,scope_key,artifact_id,revision,action,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',id,actor.realm,actor.user_id,actor.scope.key,row['id'],row['revision'],name,datetime.now(timezone.utc)+timedelta(days=2))
                actions.append(InlineKeyboardButton(label,callback_data='work:'+id))
        return InlineKeyboardMarkup([actions[i:i+2] for i in range(0,len(actions),2)])
    async def resolve(self,id,actor):
        async with self.pool.acquire() as conn: action=await conn.fetchrow('SELECT * FROM arti_work_actions WHERE id=$1',id)
        if not action or action['realm']!=actor.realm or action['scope_key']!=actor.scope.key or action['expires_at']<=datetime.now(timezone.utc) or action['consumed_at']: raise MaterialError('work_action_expired')
        row=await self.artifacts.get(action['artifact_id'],actor)
        if row['revision']!=action['revision']: raise MaterialError('stale_artifact_revision')
        project=await ProjectRepository(self.service.repository).get(row['project_id'],actor)
        project.require('approve' if action['action'] in ('accept','reject') else 'edit' if action['action'].startswith('format_') else 'view')
        return dict(action),row
    async def task_actions(self,row,actor):
        from telegram import InlineKeyboardMarkup,InlineKeyboardButton
        buttons=[]
        choices=[('result','Результат')] if row['status']=='succeeded' else ([('result','Сохранённые результаты')] if row['status'] in ('partial','waiting','unknown','paused','failed') else [])+[('pause','Пауза'),('resume','Продолжить'),('cancel','Остановить')]
        if row['status']=='waiting':
            from agents.tools.core import build_registry
            from materials.derivatives import DerivativeRepository
            registry=build_registry(); plan=await DerivativeRepository(self.service.repository).load(row['plan_id'],actor,'task_plan')
            async with self.pool.acquire() as conn: completed=set(r['step_id'] for r in await conn.fetch("SELECT step_id FROM arti_task_calls WHERE task_id=$1 AND status='success'",row['id']))
            for step in plan['steps']:
                if step['id'] not in completed and set(step.get('depends',[]))<=completed and step['tool'] in registry.tools and registry.get(step['tool']).effect=='external': choices.insert(0,('preview_'+step['id'],'Проверить действие '+step['id'][:16]))
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM arti_work_actions WHERE expires_at<NOW()')
            for action,label in choices:
                id=uuid.uuid4().hex
                await conn.execute('INSERT INTO arti_work_actions(id,realm,owner_id,scope_key,task_id,revision,action,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',id,actor.realm,actor.user_id,actor.scope.key,row['id'],row['revision'],action,datetime.now(timezone.utc)+timedelta(days=2))
                buttons.append(InlineKeyboardButton(label,callback_data='work:'+id))
        return InlineKeyboardMarkup([buttons[i:i+2] for i in range(0,len(buttons),2)])
    def task_text(self,row):
        from agents.runtime import enabled
        labels=dict(queued='В очереди',running='Выполняется',waiting='Нужен ввод или разрешение',paused='Пауза',succeeded='Результат проверен',partial='Сохранён частичный результат',failed='Выполнение остановлено',cancelled='Отменено',unknown='Исход внешнего действия неизвестен')
        text=f"Задача {row['id']}; версия {row['revision']}.\n{labels.get(row['status'],row['status'])}. Вызовов инструментов: {row['used_calls']}."
        if row['status']=='waiting': text+='\nДля внешнего действия: /task preview '+row['id']+' STEP'
        if row['diagnostics']: text+='\nПричина: '+row['diagnostics'][:500]
        if not enabled() and row['status'] in ('queued','running'): text+='\nИсполнитель отключён администратором; задача сохранена.'
        return text
    async def show_task(self,row,actor,bot,key,*,force=False):
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        repo=TaskRepository(self.service.repository,build_registry())
        current=await repo.get(row['id'],actor)
        if current['revision']!=row['revision']: raise MaterialError('stale_task_revision')
        await repo.derivatives.load(current['plan_id'],actor,'task_plan')
        async with self.pool.acquire() as conn: existing=await conn.fetchrow('SELECT * FROM arti_processing_cards WHERE task_id=$1',row['id'])
        if not force and existing and existing['owner_id']==actor.user_id and existing['scope_key']==actor.scope.key:
            await self.update_task_card(row,actor,bot); return
        markup=await self.task_actions(row,actor)
        async def guard():
            fresh=await repo.get(row['id'],actor)
            if fresh['revision']!=row['revision']: raise MaterialError('stale_task_revision')
        text=self.task_text(row); kwargs=dict(chat_id=actor.scope.chat_id,text=text,reply_markup=markup)
        if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
        result=await self.send(actor,row['project_id'],row['id'],row['revision'],key,bot.send_message,kwargs,guard)
        if row['owner_id']==actor.user_id and getattr(bot,'_menu_panel',None) is None:
            async with self.pool.acquire() as conn:
                await conn.execute("INSERT INTO arti_processing_cards(task_id,realm,owner_id,scope_key,message_id,content_hash) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(task_id) DO UPDATE SET message_id=EXCLUDED.message_id,content_hash=EXCLUDED.content_hash,status='active',updated_at=NOW() WHERE arti_processing_cards.owner_id=EXCLUDED.owner_id AND arti_processing_cards.realm=EXCLUDED.realm AND arti_processing_cards.scope_key=EXCLUDED.scope_key",row['id'],actor.realm,actor.user_id,actor.scope.key,result.message_id,sha256(text.encode()).hexdigest())
        return result
    async def update_task_card(self,row,actor,bot):
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        repo=TaskRepository(self.service.repository,build_registry()); text=self.task_text(row); digest=sha256(text.encode()).hexdigest()
        async with self.pool.acquire() as conn:
            card=await conn.fetchrow("UPDATE arti_processing_cards SET updated_at=NOW(),content_hash=$2 WHERE task_id=$1 AND owner_id=$3 AND realm=$4 AND scope_key=$5 AND status='active' AND content_hash<>$2 AND updated_at<NOW()-INTERVAL '5 seconds' RETURNING *",row['id'],digest,actor.user_id,actor.realm,actor.scope.key)
        if not card or not card['message_id']: return
        async def guard():
            fresh=await repo.get(row['id'],actor)
            if fresh['revision']!=row['revision'] or fresh['status']!=row['status']: raise MaterialError('stale_task_revision')
            await repo.derivatives.load(fresh['plan_id'],actor,'task_plan')
            if actor.scope.chat_type!='private':
                member=await bot.get_chat_member(actor.scope.chat_id,actor.user_id)
                if member.status not in ('member','administrator','creator'): raise MaterialError('task_member_left')
        markup=await self.task_actions(row,actor)
        async def edit(**kwargs):
            # Telegram edits address an existing message; topic is checked by our scope,
            # and is not an editMessageText argument.
            kwargs.pop('message_thread_id',None)
            from bot.retry_bot import RetryBot
            method=getattr(super(RetryBot,bot),'edit_message_text') if isinstance(bot,RetryBot) else bot.edit_message_text
            return await method(**kwargs)
        try:
            await self.send(actor,row['project_id'],row['id'],row['revision'],'task-edit:'+row['id']+':'+digest,edit,dict(chat_id=actor.scope.chat_id,message_id=card['message_id'],text=text,reply_markup=markup),guard)
        except Exception as exc:
            from telegram.error import BadRequest
            status='gone' if isinstance(exc,BadRequest) else 'unknown'
            async with self.pool.acquire() as conn: await conn.execute('UPDATE arti_processing_cards SET status=$2 WHERE task_id=$1',row['id'],status)
    async def refresh_tasks(self,bot):
        from agents.tasks import task_actor
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("SELECT t.* FROM arti_tasks t JOIN arti_processing_cards c ON c.task_id=t.id WHERE c.status='active' AND c.updated_at<NOW()-INTERVAL '5 seconds' ORDER BY c.updated_at LIMIT 20")
        for row in rows:
            try: await self.update_task_card(dict(row),task_actor(row),bot)
            except MaterialError:
                async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_processing_cards SET status='revoked',message_id=NULL WHERE task_id=$1",row['id'])
    async def unattempted(self,key):
        # A completed or ambiguous send is never a reason to rerender or retry.
        async with self.pool.acquire() as conn:
            if await conn.fetchval('SELECT 1 FROM arti_work_delivery WHERE delivery_key=$1',key):
                raise MaterialError('work_delivery_already_attempted')
    async def send(self,actor,project_id,target_id,revision,key,method,kwargs,guard,*,dependencies=()):
        from materials.runtime import guard_current
        from agents.scope_guard import guard_scope
        await guard_scope(actor,self.pool)
        await guard(); await guard_current(actor.scope.chat_id)
        async with self.pool.acquire() as conn,conn.transaction():
            await self.service.repository._locks(conn,actor); (await ProjectRepository(self.service.repository)._get(conn,project_id,actor,lock=True)).require('view')
            row=await conn.fetchrow("INSERT INTO arti_work_delivery(delivery_key,realm,project_id,target_id,target_revision,status) VALUES($1,$2,$3,$4,$5,'sending') ON CONFLICT DO NOTHING RETURNING delivery_key",key,actor.realm,project_id,target_id,revision)
            if not row: raise MaterialError('work_delivery_already_attempted')
        group_token=None; group_cid=None; runtime=None; turn_token=None; support_before=None; turn=None
        try:
            await guard(); await guard_scope(actor,self.pool); await guard_current(actor.scope.chat_id)
            from bot.retry_bot import RetryBot
            from cognition.runtime import CURRENT_TURN
            from cognition.delivery import send_with_receipt
            native=getattr(super(RetryBot,method.__self__),method.__name__) if isinstance(getattr(method,'__self__',None),RetryBot) else method
            turn=CURRENT_TURN.get()
            from cognition.runtime import get_runtime,PreparedTurn
            from cognition.serialization import load_event
            runtime=get_runtime()
            if runtime and runtime.pool is self.pool:
                async with self.pool.acquire() as conn:
                    derivative=await conn.fetchval('SELECT head FROM arti_artifacts WHERE id=$1',target_id) or await conn.fetchval('SELECT plan_id FROM arti_tasks WHERE id=$1',target_id) or await conn.fetchval('SELECT head FROM arti_workflow_objects WHERE id=$1',target_id) or await conn.fetchval('SELECT id FROM material_derivatives WHERE id=$1',target_id)
                    from materials.derivatives import DerivativeRepository
                    refs=[]
                    for dependency in dict.fromkeys([derivative,*dependencies]):
                        if dependency: refs.extend(await DerivativeRepository(self.service.repository)._chain(conn,dependency,actor))
                    sources=await conn.fetch('''SELECT e.*,c.suppression_epoch,c.authority FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id JOIN material_assets a ON a.source_id=e.source_id AND a.owner_id=e.owner_id
                     WHERE a.id=ANY($1::text[]) AND c.persona_id=$2 AND c.chat_id=$3 AND c.topic_id=$4 AND c.mode=$5 AND c.scene_id=$6 AND e.origin='user' AND e.suppressed_at IS NULL ORDER BY e.id DESC''',list({r['asset_id'] for r in refs}),actor.scope.persona_id,actor.scope.chat_id,actor.scope.topic_id,actor.scope.mode,actor.scope.scene_id)
                if sources and not turn:
                    source=sources[0]; event=replace(load_event(source['payload']),event_id='work:'+key)
                    seeded=PreparedTurn(runtime,source['context_id'],source['id'],event,None,'',source['suppression_epoch'],source['authority'])
                    seeded.supporting_event_ids=[s['id'] for s in sources if s['context_id']==seeded.context_id]
                    if seeded.tracks_delivery: turn_token=CURRENT_TURN.set(seeded); turn=seeded
                elif sources and turn and turn.tracks_delivery:
                    # File outputs can depend on sources discovered after planning.
                    # Keep those sources in the receipt/erasure graph in native turns too.
                    support_before=getattr(turn,'supporting_event_ids',())
                    turn.supporting_event_ids=sorted(set(support_before)|{s['id'] for s in sources if s['context_id']==turn.context_id})
            if actor.scope.chat_type!='private' and not (turn and turn.tracks_delivery):
                from cognition.runtime import get_runtime
                from cognition.scope import TransportScope
                runtime=get_runtime()
                if runtime and runtime.pool is self.pool:
                    async with self.pool.acquire() as conn:
                        if not await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1',actor.scope.chat_id): raise MaterialError('responses_disabled')
                    group_cid=await runtime.groups.context_id(TransportScope(actor.scope.chat_id,actor.scope.topic_id,actor.scope.chat_type,actor.user_id),actor.scope.mode)
                    group_token=await runtime.groups.direct_lease(group_cid)
            async def guarded_transport(*args,**transport_kwargs):
                # Receipt preparation also awaits locks/leases. The source, ACL,
                # revision and status fence belongs immediately at the real I/O.
                await guard(); await guard_scope(actor,self.pool); await guard_current(actor.scope.chat_id)
                return await native(*args,**transport_kwargs)
            result=await send_with_receipt(guarded_transport,(),kwargs,method.__name__.removeprefix('send_')) if turn and turn.tracks_delivery else await guarded_transport(**kwargs)
            receipt=getattr(result,'message_id',None)
            if type(receipt) is not int: raise MaterialError('work_delivery_unknown')
        except BaseException:
            async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_work_delivery SET status='unknown',updated_at=NOW() WHERE delivery_key=$1",key)
            raise
        finally:
            if group_token: await runtime.groups.release(group_cid,group_token)
            if support_before is not None: turn.supporting_event_ids=support_before
            if turn_token is not None: CURRENT_TURN.reset(turn_token)
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_work_delivery SET status='delivered',receipt=$2,updated_at=NOW() WHERE delivery_key=$1 AND status='sending'",key,receipt)
        from bot.retry_bot import _maybe_record_sent
        _maybe_record_sent(result)
        return result
    async def result_panel(self,bot):
        # Call only after a confirmed result receipt, for photos and file/ZIP
        # delivery alike. Ordinary progress still uses its existing text card.
        panel=getattr(bot,'_menu_panel',None)
        if panel is not None:
            panel.output=True
            try: await bot.controller.capture('Готово. Результат отправлен отдельно.')
            except Exception:
                # Navigation failure must not retry an already delivered result.
                import logging
                logging.getLogger(__name__).debug('Result delivered; menu refresh unavailable')
    async def show(self,row,actor,bot,key,*,reply_to=None,extra_guard=None,dependencies=()):
        from artifacts.export import export_current
        from materials.validation import inspect_bytes
        await self.unattempted(key)
        project=await ProjectRepository(self.service.repository).get(row['project_id'],actor)
        async def guard():
            if extra_guard: await extra_guard()
            current=await self.artifacts.get(row['id'],actor)
            if current['revision']!=row['revision'] or current['head']!=row['head']: raise MaterialError('stale_artifact_revision')
            fresh_project=await ProjectRepository(self.service.repository).get(row['project_id'],actor)
            if fresh_project.access_generation!=project.access_generation: raise MaterialError('task_access_changed')
        await guard()
        # Render from the authorized current head, including its illustration graph.
        self.artifacts.service=self.service
        files,rendered=await export_current(self.artifacts,row['id'],actor,revision=row['revision'])
        pixels=files.get('page-1.png')
        if not isinstance(pixels,bytes) or inspect_bytes(pixels,'preview.png',max_bytes=10*1024**2)!='image/png':
            raise MaterialError('artifact_preview_invalid')
        if rendered['head']!=row['head']: raise MaterialError('stale_artifact_revision')
        await guard()
        markup=await self.actions(row,actor)
        photo=BytesIO(pixels); photo.name='preview.png'
        title=' '.join(row['spec']['title'].split())[:240]
        pages=sum(name.startswith('page-') and name.endswith('.png') for name in files)
        caption=f"{title}\nВерсия {row['revision']} · {len(row['spec']['elements'])} блоков"
        if pages>1: caption+=f" · Обзор 1/{pages}"
        caption+='\nСкачать или изменить результат можно кнопками ниже.'
        kwargs=dict(chat_id=actor.scope.chat_id,photo=photo,caption=caption,parse_mode=None,reply_markup=markup)
        if reply_to is not None: kwargs['reply_to_message_id']=reply_to
        if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
        result=await self.send(actor,row['project_id'],row['id'],row['revision'],key,bot.send_photo,kwargs,guard,dependencies=dependencies)
        await self.result_panel(bot)
        return result

async def work_callback(update,context):
    from materials.runtime import enabled,actor_for_current,service_for_bot
    query=update.callback_query
    if not enabled(): await query.answer('Функция отключена.',show_alert=True); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); cards=WorkCards(service)
        async with cards.pool.acquire() as conn: action=await conn.fetchrow('SELECT * FROM arti_work_actions WHERE id=$1',query.data[5:])
        if action and action['task_id']:
            if action['realm']!=actor.realm or action['scope_key']!=actor.scope.key or action['expires_at']<=datetime.now(timezone.utc) or action['consumed_at']: raise MaterialError('work_action_expired')
            from agents.tasks import TaskRepository
            from agents.tools.core import build_registry
            tasks=TaskRepository(service.repository,build_registry()); row=await tasks.get(action['task_id'],actor)
            if row['revision']!=action['revision']: raise MaterialError('stale_task_revision')
            await query.answer()
            if action['action'].startswith('preview_'):
                from materials.types import canonical
                preview=await tasks.preview_effect(row['id'],actor,action['action'][8:]); file=BytesIO(canonical(preview).encode()); file.name='action-preview.json'
                async def preview_guard():
                    fresh=await tasks.preview_effect(row['id'],actor,preview['step_id'])
                    if fresh['revision']!=preview['revision'] or fresh['digest']!=preview['digest']: raise MaterialError('task_preview_changed')
                kwargs=dict(chat_id=actor.scope.chat_id,document=file,caption=f"Состав действия и аудитория. Разрешить: /task approve {row['id']} {row['revision']} {preview['step_id']} {preview['digest']}")
                if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
                await cards.send(actor,row['project_id'],row['id'],row['revision'],'task-preview:'+action['id']+':'+str(actor.user_id),context.bot.send_document,kwargs,preview_guard); return
            if action['action']=='result':
                from agents.runtime import deliver_task
                await deliver_task(service,tasks,row,context.bot,'task-action:'+action['id']+':'+str(actor.user_id),reader=actor); return
            row=await tasks.control(row['id'],actor,row['revision'],action['action'])
            async with cards.pool.acquire() as conn: await conn.execute('UPDATE arti_work_actions SET consumed_at=NOW() WHERE id=$1',action['id'])
            await cards.show_task(row,actor,context.bot,'task-control:'+action['id']+':'+str(actor.user_id)); return
        action,row=await cards.resolve(query.data[5:],actor)
        await query.answer()
        name=action['action']
        if name=='format':
            from telegram import InlineKeyboardMarkup,InlineKeyboardButton
            choices=[('comparison','Сравнение'),('timeline','Хронология'),('process','Процесс'),('roadmap','План'),('arguments','Аргументы'),('statistical','График'),('cards','Карточки'),('table','Таблица'),('teaching','Учебная схема')]; buttons=[]
            async with cards.pool.acquire() as conn:
                for fmt,label in choices:
                    id=uuid.uuid4().hex
                    await conn.execute('INSERT INTO arti_work_actions(id,realm,owner_id,scope_key,artifact_id,revision,action,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',id,actor.realm,actor.user_id,actor.scope.key,row['id'],row['revision'],'format_'+fmt,datetime.now(timezone.utc)+timedelta(days=2))
                    buttons.append(InlineKeyboardButton(label,callback_data='work:'+id))
            await query.message.reply_text('Выбери форму результата. Числа и свидетельства сохранятся.',reply_markup=InlineKeyboardMarkup([buttons[i:i+2] for i in range(0,len(buttons),2)])); return
        if name.startswith('format_'):
            from types import SimpleNamespace
            refs=await request_sources(service,actor,SimpleNamespace(chat_id=actor.scope.chat_id,message_id='callback:'+query.id,text='Выбор формата '+name[7:]+' для '+row['id'],caption=None))
            revised,_=await cards.artifacts.revise(row['id'],actor,row['revision'],[dict(op='format',value=name[7:])],sources=refs)
            async with cards.pool.acquire() as conn: await conn.execute('UPDATE arti_work_actions SET consumed_at=NOW() WHERE id=$1',action['id'])
            await cards.show(revised,actor,context.bot,'format:'+action['id']); return
        if name in ('accept','reject'):
            await cards.artifacts.decide(row['id'],actor,row['revision'],'accepted' if name=='accept' else 'rejected')
            async with cards.pool.acquire() as conn: await conn.execute('UPDATE arti_work_actions SET consumed_at=NOW() WHERE id=$1',action['id'])
            await query.message.reply_text('Версия принята.' if name=='accept' else 'Версия отклонена.'); return
        from artifacts.export import export_current
        import asyncio
        from materials.types import canonical
        cards.artifacts.service=service
        files={'sources.json':canonical(row['checks']).encode()} if name=='sources' else (await export_current(cards.artifacts,row['id'],actor,revision=row['revision']))[0]
        allowed={n:b for n,b in files.items() if (name=='sources') or (name=='json' and n=='spec.json') or (name=='pdf' and n=='report.pdf') or (name=='png' and n.endswith('.png')) or (name=='svg' and n.endswith('.svg'))}
        for n,b in allowed.items():
            stream=BytesIO(b); stream.name=n
            kwargs=dict(chat_id=actor.scope.chat_id,document=stream)
            if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
            async def guard(): await cards.resolve(action['id'],actor)
            await cards.send(actor,row['project_id'],row['id'],row['revision'],'action:'+action['id']+':'+str(actor.user_id)+':'+n,context.bot.send_document,kwargs,guard)
    except MaterialError:
        await query.answer('Источник, права или версия изменились. Открой результат заново.',show_alert=True)
