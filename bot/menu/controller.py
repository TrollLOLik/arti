import json
import shlex
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from html import escape
from io import BytesIO
from types import SimpleNamespace
from telegram import Message
from materials.types import MaterialError, canonical
from . import bridge, forms, views

LABELS = dict(queued='В очереди', running='Работаю', waiting='Жду разрешения или ввода', paused='На паузе',
    succeeded='Готово, проверено', partial='Готово частично', failed='Остановлено с ошибкой',
    cancelled='Отменено', unknown='Исход действия неизвестен', active='Активно', archived='В архиве',
    offered='Предложено', accepted='Принято', declined='Отклонено', completed='Выполнено',proposed='Новая версия',rejected='Отклонено')
ROLES = dict(owner='Владелец', manager='Управляющий', editor='Редактор', contributor='Добавляет материалы',
    viewer='Читатель', approver='Принимает результаты')


class Controller:
    def __init__(self, panel, update, context, actor):
        self.panel, self.update, self.context, self.actor = panel, update, context, actor
        self.bot = bridge.BotSurface(self)
        self.last_text = ''
        self.service = None

    async def material_service(self):
        from materials.runtime import enabled, service_for_bot
        if not enabled():
            raise MaterialError('menu_materials_disabled')
        if self.service is None:
            self.service = await service_for_bot()
        return self.service

    async def sources(self, text):
        from bot.work_cards import request_sources
        service = await self.material_service()
        message = bridge.MessageSurface(self, text=text)
        return await request_sources(service, self.actor, message)

    async def render(self, text, buttons=(), *, screen=None, state=None, allow_create=False):
        from materials.runtime import actor_for_current
        current = await actor_for_current()
        if current.scope.key != self.actor.scope.key:
            self.actor = current
            await self.panel.store.save(self.panel.row,scope_key=current.scope.key)
        self.last_text = text
        return await self.panel.render(text, buttons, screen=screen, state=state, allow_create=allow_create)

    async def capture(self, text, markup=None, parse_mode=None):
        # Existing media/model flows become inline choices in the same panel.
        if parse_mode != 'HTML':
            text = escape(text)
        text = text.replace('prompt','описание').replace('промпт','описание').replace('/cancel','кнопку «Отменить ввод»')
        buttons = bridge.buttons_from_markup(markup)
        state = dict(self.panel.row['state'])
        if state.get('pending') == 'legacy':
            state['legacy_snapshot'] = self.snapshot_legacy()
            state['expects_text'] = self.legacy_waiting()
        # A technical handler's help is an error explanation, not a user manual.
        if '/artifact create' in text or '/decision propose' in text:
            text = 'Не получилось применить изменение. Возможно, версия или права уже изменились. Открой объект заново.'
        if state.get('pending'):
            buttons += [[('Отменить ввод',dict(nav=state.get('return_to','home'))), ('⌂ Меню',dict(nav='home'))]]
        elif state.get('view'):
            buttons += [[('‹ К результату',state['view']),('⌂ Меню',dict(nav='home'))]]
        else:
            buttons += self.panel.navigation(state.get('return_to','home'))
        return await self.render(text, buttons, screen='legacy', state=state)

    def snapshot_legacy(self):
        import config
        snapshot = dict(user_data={}, config={})
        for key in ('image_flow', 'video_flow', 'model_flow'):
            value = self.context.user_data.get(key)
            if value:
                if value.get('image_urls'):
                    snapshot['requires_reupload']=True
                # Do not persist inline image bytes or credentials from legacy state.
                snapshot['user_data'][key] = {k:v for k,v in value.items() if k not in ('image_urls', 'pings')}
        for key in ('waiting_for_image_prompt', 'waiting_for_video_prompt', 'waiting_for_model_search', 'music_flow_state'):
            value = getattr(config, key).get(self.actor.scope.chat_id, {}).get(self.actor.user_id)
            if value is not None:
                snapshot['config'][key] = value
        if self.context.user_data.get('pending_base64_for_gen') or self.context.user_data.get('pending_material_uses_for_gen'):
            snapshot['requires_reupload']=True
        return snapshot

    def restore_legacy(self):
        import config
        snapshot = self.panel.row['state'].get('legacy_snapshot', {})
        if snapshot.get('requires_reupload') and not any(self.context.user_data.get(k) for k in ('image_flow','video_flow','pending_base64_for_gen')):
            raise MaterialError('menu_reference_reupload')
        for key, value in snapshot.get('user_data', {}).items():
            if key not in self.context.user_data:
                self.context.user_data[key] = deepcopy(value)
        for key, value in snapshot.get('config', {}).items():
            getattr(config, key)[self.actor.scope.chat_id].setdefault(self.actor.user_id, deepcopy(value))

    def clear_legacy(self, *, force=False):
        if not force and self.panel.row['state'].get('pending') != 'legacy':
            return
        import config
        # Only clean temporary files belonging to this user's pending input.
        from pathlib import Path
        dub=config.dub_flow_state.get(self.actor.scope.chat_id,{}).get(self.actor.user_id) or {}
        if dub.get('input_file'):
            path=Path(dub['input_file']).resolve()
            root=Path(__file__).resolve().parents[2]/'temp'
            if path.is_relative_to(root) and path.is_file():
                path.unlink(missing_ok=True)
        vc=config.vclone_flow_state.get(self.actor.scope.chat_id,{}).get(self.actor.user_id) or {}
        if vc.get('reference_path') or vc.get('cleaned_path'):
            from bot.commands import cleanup_vclone_files
            cleanup_vclone_files(vc.get('reference_path'),vc.get('cleaned_path'))
        if config.vclone_save_flow_state.get(self.actor.scope.chat_id,{}).get(self.actor.user_id):
            from bot.commands import _vclone_cleanup_save_state
            _vclone_cleanup_save_state(self.actor.scope.chat_id,self.actor.user_id)
        for key in ('image_flow', 'video_flow', 'model_flow'):
            self.context.user_data.pop(key, None)
        for key in ('waiting_for_image_prompt', 'waiting_for_video_prompt', 'waiting_for_model_search',
                    'pending_image_inputs', 'pending_video_inputs', 'music_flow_state',
                    'dub_flow_state','vclone_flow_state','vclone_save_flow_state'):
            getattr(config, key).get(self.actor.scope.chat_id, {}).pop(self.actor.user_id, None)
        for key in ('pending_base64_for_gen','pending_material_uses_for_gen'):
            self.context.user_data.pop(key,None)
        if force:
            # A photo awaiting its caption must not consume the first message
            # after an explicit return to conversation. Its stored source remains.
            config.pending_photo_action.pop((self.actor.scope.chat_id,self.actor.user_id),None)

    def legacy_waiting(self):
        import config
        for key in ('image_flow','video_flow'):
            flow=self.context.user_data.get(key)
            if flow and flow.get('chat_id')==self.actor.scope.chat_id:
                return True
        for key in ('waiting_for_image_prompt','waiting_for_video_prompt','waiting_for_model_search',
                    'music_flow_state','dub_flow_state','vclone_flow_state','vclone_save_flow_state'):
            if getattr(config,key).get(self.actor.scope.chat_id,{}).get(self.actor.user_id):
                return True
        return False

    async def show(self, screen='home', page=0, *, allow_create=False):
        self.clear_legacy(force=screen in views.CONVERSATION_SCREENS)
        if screen in views.SECTIONS:
            text, buttons = deepcopy(views.SECTIONS[screen])
            # Shared entry screens may be used by another participant to open THEIR panel.
            state = dict(shared=True)
            from config import PRIVILEGED_USER_IDS, rp_mode_state
            private = self.actor.scope.chat_type == 'private'
            in_scene = bool(private and rp_mode_state.get(self.actor.scope.chat_id))
            if screen == 'settings':
                from utils.admin import is_admin
                admin = await is_admin(self.update.effective_user,self.actor.scope.chat_id,self.context)
                buttons = [line for line in buttons if all(
                    (not a.get('command') or admin) and (a.get('nav')!='chat_control' or admin)
                    and (a.get('nav')!='group' or not private) for _,a in line)]
                if self.actor.user_id in PRIVILEGED_USER_IDS:
                    buttons.insert(-1,[views.nav('🔍 Диагностика','diagnostics')])
                # Permission-dependent choices must not become another user's entry point.
                state = {}
            elif screen == 'diagnostics':
                if self.actor.user_id not in PRIVILEGED_USER_IDS:
                    return await self.show('settings')
                state = {}
            elif screen == 'roleplay':
                from materials.runtime import enabled
                if not enabled():
                    buttons = [line for line in buttons if not any(a.get('nav')=='learning' for _,a in line)]
                if not private:
                    buttons = [line for line in buttons if not any(a.get('command')=='/rp' for _,a in line)]
                    text += '\n\nСцены открываются в личном чате. В группе можно играть в «Камень, ножницы, бумага».'
                elif in_scene:
                    text = '🎭 <b>Сцена уже открыта</b>\n\nПродолжим с того места, где остановились? Просто напиши в чат. Память сцены хранится отдельно от обычного разговора.'
                    buttons = [line for line in buttons if not any(a.get('command')=='/rp' for _,a in line)]
                    buttons.insert(0,[('Закончить сцену',dict(confirm=dict(special='rp_off'),title='Закончить сцену и вернуться к обычному разговору?'))])
            elif screen == 'create' and in_scene:
                text = '✨ <b>Идея для творчества</b>\n\nСейчас мы в сцене: генерация картинки, видео и музыки в ней недоступна. Идею можем обсудить здесь, а к генерации вернуться после сцены.'
                buttons = [[views.nav('🎭 К текущей сцене','roleplay')], [views.nav('💬 Обсудим идею','talk')]]
            elif screen == 'memory':
                if in_scene:
                    text += '\n\n<i>Сейчас открыта память этой сцены.</i>'
                elif not private:
                    text += '\n\n<i>Только доступные тебе записи этой темы. Личная переписка остаётся в личном чате.</i>'
            elif screen == 'work':
                from materials.runtime import enabled
                from agents.runtime import enabled as agents_enabled
                if not enabled():
                    text = '🧰 <b>Помоги с делом</b>\n\nСохранённая работа с файлами и задачами пока не включена. Обсудить вопрос со мной можно прямо в чате.'
                    buttons = [[views.nav('💬 Обсудим вопрос','talk')], [views.nav('Что здесь можно делать','help_work')], [views.nav('🔌 Подключения','connections')]]
                elif not agents_enabled():
                    text += '\n\n<i>Новые многошаговые задачи и запуски по расписанию пока недоступны. Сохранённую работу можно посмотреть.</i>'
                    buttons = [[(label,a) for label,a in line if a.get('form') not in ('agent','infographic') and a.get('nav')!='routines'] for line in buttons]
                    buttons = [line for line in buttons if line]
            if screen in views.CONVERSATION_SCREENS:
                if not private:
                    text += '\n\n<i>В группе ответь на это сообщение или обратись ко мне по имени.</i>'
                # No pending state: the next real message takes the usual cognition route.
                buttons += [[('Продолжить в чате',dict(close=True))]]
            if screen != 'home':
                buttons += self.panel.navigation(views.PARENTS.get(screen, 'home'))
            else:
                buttons += [[('Вернуться в разговор', dict(close=True))]]
                if in_scene:
                    text += '\n\n<i>Сейчас мы в ролевой сцене.</i>'
            return await self.render(text, buttons, screen=screen, state=state,allow_create=allow_create)
        if screen == 'group':
            return await self.group_screen()
        if screen == 'projects':
            return await self.project_list(page)
        return await self.object_list(screen, page)

    async def project_list(self, page):
        from projects.repository import ProjectRepository
        service = await self.material_service()
        projects = await ProjectRepository(service.repository).list(self.actor)
        page = max(0, min(page, max(0, (len(projects)-1)//6)))
        buttons = [[(p.title[:40]+' · '+LABELS.get(p.status, p.status), dict(project=p.id))]
                   for p in projects[page*6:page*6+6]]
        buttons.insert(0, [views.form('＋ Новый проект', 'project_new')])
        buttons += self.pages('projects', page, len(projects)) + self.panel.navigation('work')
        return await self.render('🗂 <b>Проекты</b>\n\n'+('Выбери проект.' if projects else 'Пока пусто. Создадим первый?'), buttons,
                                screen='projects', state={})

    def pages(self, screen, page, count):
        row = []
        if page:
            row.append(('‹ Предыдущие', dict(nav=screen, page=page-1)))
        if (page+1)*6 < count:
            row.append(('Следующие ›', dict(nav=screen, page=page+1)))
        return [row] if row else []

    async def project(self, id):
        from projects.repository import ProjectRepository
        service = await self.material_service()
        repo = ProjectRepository(service.repository)
        p = await repo.get(id, self.actor)
        current = await repo.current(self.actor)
        buttons = []
        if p.status == 'active':
            buttons += [[('✓ Работать в этом проекте', dict(project_use=id, revision=p.revision))]]
        data = dict(id=id, revision=p.revision)
        from projects.types import ROLE_RIGHTS
        rights = ROLE_RIGHTS[p.role]
        if 'edit' in rights and p.status == 'active':
            buttons += [[views.form('Изменить цель', 'project_edit', **data)]]
        if 'attach' in rights and p.status=='active':
            buttons += [[views.form('Добавить файл', 'project_attach', **data)]]
        if 'manage' in rights:
            if p.status=='active':
                buttons += [[views.form('Права участника', 'project_member', **data)]]
            action = 'resume' if p.status == 'archived' else 'archive'
            buttons += [[('Вернуть из архива' if action=='resume' else 'В архив',
                dict(confirm=dict(project_control=action, **data), title='Изменить состояние проекта?'))]]
            buttons += [[('Удалить проект', dict(confirm=dict(project_control='delete', **data), title='Удалить проект и отозвать связанные результаты?'))]]
        if 'publish' in rights and p.status == 'active':
            buttons += [[views.form('Перенести в группу', 'project_publish', **data)]]
        if current and current.id == id:
            buttons += [[views.nav('Результаты проекта', 'artifacts'), views.nav('Задачи проекта', 'tasks')]]
        buttons += self.panel.navigation('projects')
        materials = await repo.materials_for(id, self.actor)
        text = '🗂 <b>'+escape(p.title)+'</b>\n\n'+escape(p.goal or 'Цель пока не указана.')
        text += '\n\n'+ROLES[p.role]+' · '+LABELS[p.status]+f' · файлов: {len(materials)}'
        if current and current.id == id:
            text += '\n✓ Текущий проект'
        return await self.render(text, buttons, screen='project', state=dict(view=dict(project=id)))

    async def object_list(self, kind, page=0, *, selection=None):
        from projects.repository import ProjectRepository
        from projects.workflows import WorkflowRepository
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        from artifacts.revisions import ArtifactRepository
        service = await self.material_service()
        current = await ProjectRepository(service.repository).current(self.actor)
        if not current:
            return await self.render('Сначала выбери или создай проект.', [[views.nav('Выбрать проект', 'projects')]]+self.panel.navigation(), state={})
        titles = dict(tasks='⏳ Задачи', artifacts='📊 Результаты', decision='🗳 Решения', assignment='📌 Поручения',
                      procedure='🧩 Процедуры', subscription='🕒 Подписки', learning='📚 Истории и квесты')
        table = 'arti_tasks' if kind=='tasks' else 'arti_artifacts' if kind=='artifacts' else 'arti_workflow_objects'
        async with service.repository.pool.acquire() as conn:
            query = ("SELECT a.id FROM arti_artifacts a JOIN arti_projects p ON p.id=a.project_id WHERE p.realm=$1 AND a.project_id=$2"
                     if kind=='artifacts' else f'SELECT id FROM {table} WHERE realm=$1 AND project_id=$2')
            params = [self.actor.realm, current.id]
            if kind not in ('tasks', 'artifacts'):
                query += " AND kind=$3 AND status<>'deleted'"
                params.append(kind)
            query += (' ORDER BY a.id DESC LIMIT 500' if kind=='artifacts' else ' ORDER BY created_at DESC LIMIT 500')
            candidates = await conn.fetch(query, *params)
        rows = []
        for candidate in candidates:
            try:
                if kind == 'tasks':
                    row = await TaskRepository(service.repository, build_registry()).get(candidate['id'], self.actor)
                    plan = await service.repository.pool.fetchval('SELECT payload FROM material_derivatives WHERE id=$1', row['plan_id'])
                    if not plan:
                        continue
                    from materials.derivatives import DerivativeRepository
                    value = await DerivativeRepository(service.repository).load(row['plan_id'], self.actor, 'task_plan')
                    row['title'] = value['goal']
                    if selection and (row['owner_id'] != self.actor.user_id or row['status'] != 'succeeded'):
                        continue
                elif kind == 'artifacts':
                    row = await ArtifactRepository(service.repository).get(candidate['id'], self.actor)
                    row['title'] = row['spec']['title']
                    row['status'] = 'accepted' if row['accepted']==row['head'] else 'proposed'
                else:
                    row = await WorkflowRepository(service.repository).get(candidate['id'], self.actor, kind)
                    b = row['body']
                    row['title'] = b.get('title') or b.get('text') or 'Подписка на процедуру'
                rows.append(row)
            except MaterialError:
                continue
        page = max(0, min(page, max(0, (len(rows)-1)//6)))
        buttons = []
        for row in rows[page*6:page*6+6]:
            action = dict(select_task=row['id'], selection=selection) if selection else dict(object=kind, id=row['id'])
            buttons.append([(str(row['title'])[:40]+' · '+LABELS.get(row['status'], row['status']), action)])
        if not selection:
            creation = dict(tasks='agent', artifacts='infographic', decision='decision_new', assignment='assignment_new', learning='scenario_new')
            if kind in creation:
                buttons.insert(0, [views.form('＋ Создать', creation[kind])])
        pagination=self.pages(kind,page,len(rows))
        if selection:
            for line in pagination:
                for _,action in line:
                    action['selection']=selection
        buttons += pagination + self.panel.navigation(views.PARENTS.get(kind, 'home'))
        return await self.render(titles[kind]+' · <b>'+escape(current.title)+'</b>\n\n'+
                                ('Выбери запись.' if rows else 'Пока нет доступных записей.'), buttons, screen=kind, state={})

    async def object(self, kind, id):
        service = await self.material_service()
        if kind == 'tasks':
            return await self.task(id)
        if kind == 'artifacts':
            from artifacts.revisions import ArtifactRepository
            from bot.work_cards import WorkCards
            row = await ArtifactRepository(service.repository).get(id, self.actor)
            markup = await WorkCards(service).actions(row, self.actor)
            buttons = bridge.buttons_from_markup(markup)
            p = await WorkCards(service).artifacts.projects.get(row['project_id'], self.actor)
            from projects.types import ROLE_RIGHTS
            if 'approve' not in ROLE_RIGHTS[p.role]:
                buttons = [line for line in buttons if not any(label in ('Принять', 'Отклонить') for label,_ in line)]
            if 'edit' in ROLE_RIGHTS[p.role]:
                buttons += [[('✏️ Правки', dict(artifact_edit=id, revision=row['revision']))],
                            [('↶ Предыдущие версии', dict(artifact_history=id, revision=row['revision']))]]
            text = '📊 <b>'+escape(row['spec']['title'])+'</b>\n\n'+f"Блоков: {len(row['spec']['elements'])}. Версия {row['revision']}."
            return await self.render(text, buttons+self.panel.navigation('artifacts'), screen='artifact', state=dict(view=dict(object='artifacts',id=id)))
        from projects.workflows import WorkflowRepository
        row = await WorkflowRepository(service.repository).get(id, self.actor, kind)
        from projects.repository import ProjectRepository
        from projects.types import ROLE_RIGHTS
        project=await ProjectRepository(service.repository).get(row['project_id'],self.actor)
        rights=ROLE_RIGHTS[project.role]
        b, version = row['body'], row['revision']
        data = dict(id=id, revision=version)
        text = self.workflow_text(row)
        buttons = []
        owner = row['owner_id'] == self.actor.user_id
        if kind == 'decision':
            buttons += [[views.form('Поддержать', 'decision_support', **data), views.form('Возразить', 'decision_object', **data)]]
            if 'approve' in rights:
                buttons += [[('Выбрать и подтвердить', dict(decision_choose=id, revision=version))],
                            [views.form('Отозвать выбор', 'decision_revoke', **data)]]
        elif kind == 'assignment':
            if b['recipient'] == self.actor.user_id:
                buttons += [[views.form('Принять', 'assignment_accept', **data), views.form('Отказаться', 'assignment_decline', **data)],
                            [views.form('Подтвердить выполнение', 'assignment_complete', **data)]]
        elif kind == 'procedure':
            if 'edit' in rights and row['status']=='active':
                buttons += [[views.form('Запустить', 'procedure_run', **data, input_schema=b['input_schema'])],
                            [views.form('Следить по расписанию', 'subscription_new', **data, input_schema=b['input_schema'])]]
            if owner and 'edit' in rights:
                buttons += [[('Заменить проверенным способом', dict(procedure_select=id, revision=version))]]
        elif kind == 'subscription':
            from agents.subscriptions import SubscriptionRepository
            from agents.tools.core import build_registry
            _, details = await SubscriptionRepository(service.repository, build_registry()).describe(id, self.actor)
            from zoneinfo import ZoneInfo
            next_at=details['next_at']
            if isinstance(next_at, str):
                next_at = datetime.fromisoformat(next_at)
            text += '\nСледующий запуск: '+(next_at.astimezone(ZoneInfo(b['schedule']['timezone'])).strftime('%d.%m.%Y %H:%M') if next_at else 'не запланирован')
            if details['paused_reason']:
                text += '\nПауза: '+escape(str(details['paused_reason']))
            buttons += [[views.command('Последний результат', '/subscription last '+id)]]
            if owner and 'edit' in rights:
                buttons += [[views.form('Изменить расписание и лимиты', 'subscription_change', **data)]]
        elif kind == 'learning':
            if b['owner']==self.actor.user_id and 'edit' in rights:
                for stage in b['stages']:
                    buttons += [[views.form(stage.get('label') or stage['prompt'][:40], 'scenario_answer', **data, stage=stage['id'],prompt=stage['prompt'])]]
            buttons += [[views.command('Показать наглядно', '/scenario visual '+id)]]
        if owner and 'edit' in rights:
            action = 'resume' if row['status']=='paused' else 'pause'
            cmd = 'scenario' if kind=='learning' else kind
            buttons += [[('Продолжить' if action=='resume' else 'Пауза', dict(confirm=dict(command=f'/{cmd} {action} {id} {version}'), title='Изменить состояние записи?'))],
                        [('Удалить', dict(confirm=dict(command=f'/{cmd} delete {id} {version}'), title='Удалить запись и отозвать связанные результаты?'))]]
        buttons += self.panel.navigation(kind)
        return await self.render(escape(text), buttons, screen=kind+'_detail', state=dict(view=dict(object=kind,id=id)))

    def workflow_text(self,row):
        b=row['body']; kind=row['kind']
        if kind=='decision':
            lines=['🗳 '+b['text']]
            if b.get('options'):
                lines+=['Варианты: '+', '.join(b['options'])]
            lines += ['Участник '+str(p['actor_id'])+': '+dict(support='поддерживает',object='возражает').get(p['position'],p['position'])+
                      (' — '+p['reason'] if p.get('reason') else '') for p in b.get('positions',[])]
            if b.get('confirmation'):
                lines+=['Выбор организатора: '+str(b['confirmation'].get('option') or 'решение подтверждено')]
            else:
                lines+=['Выбор ещё не подтверждён.']
        elif kind=='assignment':
            lines=['📌 '+b['text'],LABELS.get(b['status'],b['status']),'Исполнитель: '+str(b['recipient'])]
            if b.get('due'):
                lines+=['Срок: '+datetime.fromisoformat(b['due']).strftime('%d.%m.%Y %H:%M %z')]
        elif kind=='procedure':
            lines=['🧩 '+b['title'],'Сохранённый способ работы. Шагов: '+str(len(b['recipe']['steps'])),b['recipe']['goal'][:900]]
        elif kind=='subscription':
            s=b['schedule']
            when='Каждые '+str(s['seconds']//60)+' мин.' if s['kind']=='interval' else f"В {s['hour']:02}:{s['minute']:02}"
            if s.get('weekdays'):
                when+=' · '+', '.join(['пн','вт','ср','чт','пт','сб','вс'][d] for d in s['weekdays'])
            lines=['🕒 Подписка',when+' · '+s['timezone'],'Сообщаю о содержательных изменениях.','Лимит запусков: '+str(b.get('max_runs') or 'без ограничения')]
        else:
            complete=sum(bool(p['completed']) for p in b['progress'])
            lines=['📚 '+b['title'],f"Пройдено этапов: {complete}/{len(b['stages'])}"]
            if b['scenario']=='story':
                lines+=['Это художественная история.']
        return '\n\n'.join(lines)+'\n\n'+LABELS.get(row['status'],row['status'])

    async def task(self, id):
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        service = await self.material_service()
        repo = TaskRepository(service.repository, build_registry())
        row = await repo.get(id, self.actor)
        plan = await repo.derivatives.load(row['plan_id'], self.actor, 'task_plan')
        p = await repo.projects.get(row['project_id'], self.actor)
        from projects.types import ROLE_RIGHTS
        rights=ROLE_RIGHTS[p.role]
        text = '⏳ <b>'+escape(plan['goal'][:300])+'</b>\n\n'+LABELS[row['status']]+f"\nВызовов инструментов: {row['used_calls']}."
        if row['diagnostics']:
            text += '\nНужна проверка: '+escape(row['diagnostics'][:200])
        buttons = []
        if row['status'] in ('succeeded', 'partial', 'waiting', 'unknown', 'paused', 'failed'):
            buttons += [[views.command('Получить результат' if row['status']=='succeeded' else 'Сохранённая часть результата', '/task result '+id)]]
        if 'edit' in rights and (row['owner_id']==self.actor.user_id or p.role in ('owner', 'manager')):
            if row['status'] not in ('succeeded', 'cancelled'):
                for action, label in [('pause','Пауза'), ('resume','Продолжить'), ('cancel','Остановить')]:
                    if action == 'resume' and row['status'] not in ('paused', 'waiting', 'partial'):
                        continue
                    buttons += [[(label, dict(confirm=dict(task_control=action, id=id, revision=row['revision']), title=label+' задачу?'))]]
        if 'edit' in rights and row['owner_id']==self.actor.user_id and row['status']=='succeeded':
            buttons += [[views.form('Сохранить способ работы', 'procedure_save', id=id, revision=row['revision'])]]
        if 'edit' in rights and row['owner_id']==self.actor.user_id and row['status'] not in ('unknown','succeeded','cancelled'):
            buttons += [[views.form('Изменить задание','task_revise',id=id,revision=row['revision'])]]
        if 'approve' in rights and row['owner_id']==self.actor.user_id and row['status']=='waiting':
            for step in plan['steps']:
                if repo.registry.get(step['tool'], step['version']).effect == 'external':
                    buttons += [[('Проверить действие', dict(effect_preview=id, step=step['id']))]]
        buttons += [[('Обновить состояние', dict(object='tasks', id=id))]]+self.panel.navigation('tasks')
        return await self.render(text, buttons, screen='task', state=dict(view=dict(object='tasks',id=id)))

    async def group_screen(self):
        from cognition.runtime import get_runtime
        runtime = get_runtime()
        if self.actor.scope.chat_type == 'private' or not runtime:
            return await self.render('Настройки участия открываются в самой группе.', self.panel.navigation('settings'), state={})
        policy, _ = await runtime.groups.policies.get(self.actor.scope.chat_id, self.actor.scope.topic_id)
        buttons = [[views.command('Не обращаться ко мне первым', '/proactivity optout')],
                   [views.command('Можно обращаться ко мне', '/proactivity optin')]]
        from bot.group_commands import is_admin
        if await is_admin(self.update.effective_user, self.actor.scope.chat_id, self.context):
            buttons += [[views.command('Только обращения', '/proactivity topic mentions live'), views.command('Полезные инициативы', '/proactivity topic useful live')],
                        [views.command('Более общительная', '/proactivity topic social live')],
                        [views.command('Включить инициативы', '/proactivity topic on'), views.command('Выключить инициативы', '/proactivity topic off')],
                        [views.command('Пауза на час', '/quiet 60'), views.command('Снять паузу', '/quiet 0')],
                        [views.form('Тихие часы', 'group_hours'), views.form('Частота сообщений', 'group_limits')],
                        [views.form('Часовой пояс', 'group_timezone'), views.form('Частота оценки', 'group_assessments')],
                        [views.command('Реакции: включить', '/proactivity topic reactions on'), views.command('Реакции: выключить', '/proactivity topic reactions off')],
                        [views.command('Новые темы: включить', '/proactivity topic seeds on'), views.command('Новые темы: выключить', '/proactivity topic seeds off')],
                        [views.command('Вижу всю переписку', '/proactivity topic visibility full'), views.command('Видимость частичная', '/proactivity topic visibility partial')],
                        [views.command('Пробный режим без отправок', '/proactivity topic useful shadow')]]
        mode = dict(mentions='только обращения', useful='полезные инициативы', social='общительная')[policy.mode]
        text = '💬 <b>Участие в этой теме</b>\n\nСейчас: '+mode+'.\n'+('Инициативы выключены.' if policy.disabled else 'Инициативы разрешены правилами группы.')
        return await self.render(text, buttons+self.panel.navigation('settings'), screen='group', state={})

    async def begin(self, kind, data):
        self.clear_legacy()
        if kind not in ('archive','forget','group_timezone','group_limits','group_hours','group_assessments'):
            await self.material_service()
        if kind in ('agent','infographic','task_revise','procedure_run','subscription_new'):
            from agents.runtime import enabled
            if not enabled():
                raise MaterialError('menu_agents_disabled')
        origin = self.panel.row['screen']
        draft = dict(kind=kind, data=data, fields=forms.fields(kind, data), values={}, index=0,
                     return_to=origin if origin in views.SECTIONS else views.form_parent(kind))
        if data.get('file'):
            draft['values']['file'] = data['file']
            draft['fields'] = [f for f in draft['fields'] if f['key'] != 'file']
        if data.get('segment'):
            draft['values']['segment'] = data['segment']
            draft['fields'] = [f for f in draft['fields'] if f['key'] != 'segment']
        if kind=='scenario_answer' and data.get('prompt'):
            draft['fields'][0]['prompt']=data['prompt']+'\n\nТвой ответ:'
        return await self.form_screen(draft)

    async def form_screen(self, draft, error=None, *, confirm_page=0):
        state = dict(pending='form', draft=draft, return_to=draft.get('return_to',views.form_parent(draft['kind'])),
                     input_until=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat())
        if draft['index'] >= len(draft['fields']):
            state['pending'] = 'confirm_form'
            pages = forms.summary_pages(draft)
            page = min(max(0,confirm_page),len(pages)-1)
            state['confirm_page'] = page
            buttons = []
            if page:
                buttons.append([('‹ Предыдущая часть',dict(confirm_page=page-1))])
            if page+1<len(pages):
                buttons.append([('Следующая часть ›',dict(confirm_page=page+1))])
            else:
                buttons.append([('✓ Выполнить',dict(submit=True))])
            buttons.append([('Изменить',dict(restart_form=True))])
            text=pages[page]+(f'\n\nЧасть {page+1}/{len(pages)}' if len(pages)>1 else '')
            return await self.render(text, buttons+self.panel.navigation(state['return_to']), screen='form_confirm', state=state)
        f = draft['fields'][draft['index']]
        buttons = [[(label, dict(value=str(value))) for label, value in f['choices'][i:i+2]] for i in range(0,len(f['choices']),2)]
        if f['optional'] and not any(value=='' for _,value in f['choices']):
            buttons.append([('Пропустить', dict(value=''))])
        if draft['index']:
            buttons.append([('‹ Предыдущий шаг', dict(form_back=True))])
        buttons += [[('Отменить ввод',dict(nav=state['return_to'])), ('⌂ Меню',dict(nav='home'))]]
        text = f"<b>{escape(f['label'])}</b> · {draft['index']+1}/{len(draft['fields'])}\n\n"+escape(f['prompt'])
        if self.actor.scope.chat_type != 'private':
            text += '\n\n<i>Ответь на это сообщение меню.</i>'
        if error:
            text += '\n\n'+escape(error)
        return await self.render(text, buttons, screen='form', state=state)

    async def answer(self, text, message=None):
        draft = deepcopy(self.panel.row['state']['draft'])
        f = draft['fields'][draft['index']]
        if f['kind']=='stages' and message and getattr(message,'document',None):
            if message.document.file_size and message.document.file_size>50000:
                return await self.form_screen(draft,'Текстовый файл должен быть не больше 50 КБ.')
            file=await self.context.bot.get_file(message.document.file_id)
            raw=bytes(await file.download_as_bytearray())
            if len(raw)>50000:
                return await self.form_screen(draft,'Текстовый файл должен быть не больше 50 КБ.')
            try:
                text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                return await self.form_screen(draft,'Нужен текстовый файл в UTF-8.')
        try:
            value = forms.parse(f, text, message)
        except ValueError as exc:
            return await self.form_screen(draft, str(exc))
        draft['values'][f['key']] = value
        draft['index'] += 1
        return await self.form_screen(draft)

    async def execute_command(self, text, reply=None):
        self.clear_legacy()
        key = text.split()[0].lstrip('/')
        origin = self.panel.row['screen']
        return_to = origin if origin in views.SECTIONS else views.COMMAND_PARENTS.get(key,'home')
        pending = key in ('image', 'video', 'music', 'model', 'dub', 'vclone', 'voice_save', 'voice_delete', 'voices')
        await self.panel.store.save(self.panel.row, state=dict(pending='legacy', legacy_command=key,
            return_to=return_to,
            input_until=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()) if pending else
            dict(return_to=return_to))
        self.panel.output = False
        await bridge.command(self, text, reply)
        if key=='project' and text.split()[1:2] in (['new'],['edit'],['attach'],['member']):
            from projects.repository import ProjectRepository
            repo=ProjectRepository((await self.material_service()).repository)
            current=await repo.current(self.actor)
            if current:
                return await self.project(current.id)
        if key in ('decision','assignment'):
            service=await self.material_service()
            request=bridge.MessageSurface(self)._menu_event_id
            async with service.repository.pool.acquire() as conn:
                target=await conn.fetchval("SELECT target_id FROM arti_work_delivery WHERE delivery_key=$1 AND status='delivered'",
                    f'workflow-command:{self.actor.realm}:{request}')
            if target:
                return await self.object(key,target)
        if not self.panel.output:
            downloaded=(key=='task' and text.split()[1:2] in (['result'],['preview'])) or (key=='subscription' and text.split()[1:2]==['last'])
            await self.render('Сохранённый результат отправлен отдельно.' if downloaded else 'Запрос принят. Готовый результат придёт отдельно.', self.panel.navigation(), state={})

    async def legacy_input(self, text, message=None):
        self.restore_legacy()
        if not self.legacy_waiting():
            return await self.render('Этот ввод уже завершён или сброшен после перезапуска. Выбери действие заново.',self.panel.navigation('create'),state={})
        from bot.handlers import handle_all_messages
        update, context = bridge.surfaces(self, text=text)
        self.panel.output = False
        if message and not getattr(message, 'text', None):
            from bot import handlers
            if message.document:
                handler = handlers.handle_document
            elif message.voice:
                handler = handlers.handle_voice_message
            elif message.audio:
                handler = handlers.handle_audio_message
            elif message.video:
                handler = handlers.handle_video_upload_message
            elif message.photo:
                handler = handlers.handle_image_message
            else:
                return
            await handler(update, context)
        else:
            await handle_all_messages(update, context)
        if not self.panel.output:
            await self.render('Запрос принят. Готовый результат придёт отдельно.', self.panel.navigation(), state={})

    async def submit(self):
        state = self.panel.row['state']
        if state.get('pending') != 'confirm_form':
            raise MaterialError('menu_stale_form')
        draft = deepcopy(state['draft'])
        if state.get('confirm_page',0)!=len(forms.summary_pages(draft))-1:
            raise MaterialError('menu_review_required')
        kind, data, v = draft['kind'], draft['data'], draft['values']
        file = Message.de_json(v.get('file') or data.get('file'), self.context.bot) if v.get('file') or data.get('file') else None
        # Consume the form before mutation. Restart cannot blindly repeat an unknown effect.
        await self.panel.store.save(self.panel.row, state={})
        q = lambda *args: shlex.join([str(x) for x in args])
        id, revision = data.get('id'), data.get('revision')
        commands = {
            'project_new': lambda: q('/project','new',v['title'],v['goal']),
            'project_edit': lambda: q('/project','edit','version='+str(revision),v['goal']),
            'project_attach': lambda: q('/project','attach','version='+str(revision)),
            'project_member': lambda: q('/project','member','version='+str(revision),v['user'],v['role']),
            'dataset': lambda: q('/dataset', *(['sheet='+str(v['sheet'])] if v['sheet'] else [])),
            'calc': lambda: q('/calc',data['operation'],v['range'],*(['sheet='+str(v['sheet'])] if v['sheet'] else []),
                *(['reference='+v['reference']] if v.get('reference') else []), *(['unit='+v['unit']] if v.get('unit') else []),
                *(['locale='+v['locale']] if v.get('locale') else []),'missing='+v['missing']),
            'datafix': lambda: q('/datafix',v['cell'],v['value'],*(['sheet='+str(v['sheet'])] if v['sheet'] else [])),
            'transcript': lambda: '/transcript', 'storyboard': lambda: '/storyboard',
            'moment': lambda: q('/moment',v['seconds']),
            'listen': lambda: q('/listen',v['segment']),
            'transcript_fix': lambda: q('/transcript_fix',v['segment'],v['text'],'version='+str(data['transcript'])),
            'material_search': lambda: '/materials_find '+v['query'],
            'material_review': lambda: q('/material_review',v['position'],v['summary'],v['reason']),
            'archive': lambda: '/memory_archive '+v['query'], 'forget': lambda: '/forget '+v['query'],
            'decision_new': lambda: q('/decision','propose',v['text'],*v['options']),
            'assignment_new': lambda: q('/assignment','offer',v['user'],v['text'],*([v['due']] if v['due'] else [])),
            'scenario_answer': lambda: q('/scenario','answer',id,revision,data['stage'],v['answer']),
            'group_timezone': lambda: q('/proactivity','topic','tz',v['timezone']),
            'group_limits': lambda: q('/proactivity','topic','limits',v['daily'],v['spacing']*60),
            'group_hours': lambda: q('/proactivity','topic','hours',v['start'],v['end']),
            'group_assessments': lambda: q('/proactivity','topic','assessments',v['count']),
        }
        if kind.startswith('decision_') and kind!='decision_new':
            action = kind[9:]
            command = q('/decision',action,id,revision,*([data['option']] if action=='confirm' and data.get('option') else []),v.get('reason',''))
        elif kind.startswith('assignment_') and kind!='assignment_new':
            command = q('/assignment',kind[11:],id,revision,v.get('reason',''))
        else:
            command = commands[kind]() if kind in commands else None
        if kind.startswith('project_') and kind!='project_new':
            from projects.repository import ProjectRepository
            repo = ProjectRepository((await self.material_service()).repository)
            current = await repo.current(self.actor)
            if not current or current.id != id:
                # Current selection is not permission to edit some other project.
                await repo.select(id, self.actor)
            fresh = await repo.get(id, self.actor)
            if fresh.revision != revision:
                raise MaterialError('stale_project_revision')
        if command:
            await self.execute_command(command, file)
            if kind == 'transcript' and file:
                await self.audio_choices(file)
            return
        service = await self.material_service()
        if kind in ('agent','infographic'):
            from agents.runtime import enabled
            if not enabled():
                raise MaterialError('menu_agents_disabled')
            from bot.agent_requests import handle_agent_request
            message = bridge.MessageSurface(self)
            request = dict(user_message=('Сделай инфографику: ' if kind=='infographic' else 'Агент: ')+v['goal'],
                           message_id=message._menu_event_id, _telegram_scope=None, _material_uses=(), _project_context={})
            self.panel.output = False
            await handle_agent_request(request, self.bot)
            from hashlib import sha256
            from agents.tasks import TaskRepository
            from agents.tools.core import build_registry
            task_id=sha256(f'natural-task:{self.actor.realm}:{message._menu_event_id}'.encode()).hexdigest()[:32]
            try:
                await TaskRepository(service.repository,build_registry()).get(task_id,self.actor)
            except MaterialError:
                return
            return await self.task(task_id)
        if kind == 'scenario_new':
            from projects.learning import LearningRepository
            from projects.repository import ProjectRepository
            p = await ProjectRepository(service.repository).current(self.actor)
            if not p:
                raise MaterialError('project_selection_required')
            stages = [dict(id='stage_'+str(n), label='Этап '+str(n), prompt=text,
                           **(dict(fiction=True) if v['scenario']=='story' else {})) for n,text in enumerate(v['stages'],1)]
            row = await LearningRepository(service.repository).start(p.id,self.actor,v['title'],stages,
                await self.sources('Создать сценарий: '+canonical(v)),scenario=v['scenario'])
            return await self.object('learning',row['id'])
        if kind in ('procedure_save','procedure_revise'):
            from agents.procedures import ProcedureRepository
            from agents.tools.core import build_registry
            body = await self.procedure_body(data.get('task_id') or id,v['title'])
            repo = ProcedureRepository(service.repository,build_registry())
            refs = await self.sources('Подтверждение процедуры: '+v['title'])
            if kind=='procedure_save':
                row = await repo.save_success(id,self.actor,body,refs,confirmed=True,origin='user')
            else:
                proposal = await repo.propose_change(id,self.actor,revision,body,refs)
                return await self.render('🧩 <b>Изменение процедуры</b>\n\n'+escape(body['title'])+
                    '\nШагов: '+str(len(body['recipe']['steps']))+'\nНовый способ взят из выбранной успешной задачи. Текущий продолжит действовать до принятия.',
                    [[('Принять изменение',dict(confirm=dict(procedure_approve=id,revision=revision,proposal=proposal),title='Принять показанное изменение процедуры?'))]]+
                    self.panel.navigation('procedure'),state={})
            return await self.object('procedure',row['id'])
        if kind in ('procedure_run','subscription_new','subscription_change'):
            from agents.procedures import ProcedureRepository
            from agents.tools.core import build_registry
            registry = build_registry()
            bindings = forms.bindings(draft)
            if kind == 'procedure_run':
                from agents.tasks import TaskRepository
                plan,row = await ProcedureRepository(service.repository,registry).instantiate(id,self.actor,bindings)
                if row['revision'] != revision:
                    raise MaterialError('stale_workflow_revision')
                task = await TaskRepository(service.repository,registry).create(self.actor,row['project_id'],plan,
                    await self.sources('Запуск процедуры '+row['body']['title']))
                return await self.task(task['id'])
            from agents.subscriptions import SubscriptionRepository
            repo = SubscriptionRepository(service.repository,registry)
            schedule = dict(timezone=v['timezone'])
            if v['schedule']=='interval':
                if not v['time'].isdigit():
                    raise MaterialError('menu_interval_invalid')
                schedule.update(kind='interval',seconds=int(v['time'])*60,anchor=datetime.now(timezone.utc).isoformat())
            else:
                try:
                    hour,minute = map(int,v['time'].split(':'))
                except ValueError:
                    raise MaterialError('menu_schedule_invalid')
                schedule.update(kind='daily',hour=hour,minute=minute)
                if v['schedule']=='weekdays':
                    schedule['weekdays'] = [0,1,2,3,4]
            refs = await self.sources('Подтверждение расписания: '+canonical(v))
            options = dict(schedule=schedule,max_runs=v['runs'],max_calls=v['calls'],max_cost=v['cost'])
            if kind=='subscription_change':
                row = await repo.revise(id,self.actor,revision,options,refs)
            else:
                current = await ProcedureRepository(service.repository,registry).get(id,self.actor,'procedure')
                if current['revision'] != revision:
                    raise MaterialError('stale_workflow_revision')
                row = await repo.subscribe(self.actor,id,bindings,schedule,refs,confirmed=True,origin='user',
                    max_runs=v['runs'],max_calls=v['calls'],max_cost=v['cost'])
            return await self.object('subscription',row['id'])
        if kind == 'project_publish':
            from projects.publication import ProjectPublication
            from materials.types import AccessContext, MaterialScope
            destination = await self.context.bot.get_chat(v['chat'])
            member = await self.context.bot.get_chat_member(destination.id,self.actor.user_id)
            if destination.type not in ('group','supergroup') or member.status not in ('member','administrator','creator') or (getattr(destination,'is_forum',False) and v['topic']<=0):
                raise MaterialError('publication_destination_denied')
            actor = AccessContext(MaterialScope(self.actor.scope.persona_id,destination.id,v['topic'],destination.type,
                                  self.actor.scope.mode,self.actor.scope.scene_id),self.actor.user_id,self.actor.sender_ref)
            row = await ProjectPublication(service).preview(id,self.actor,revision,actor,bridge.MessageSurface(self)._menu_request_source)
            await self.render('📤 <b>Перенести проект</b>\n\nГруппа: '+escape(destination.title)+
                '\nТема: '+str(v['topic'])+'\nПроверенный состав переноса приложу отдельным файлом. Оригиналы требуют разрешения автора.',
                [[('Подтвердить перенос',dict(confirm=dict(command='/project publish '+row['id']),title='Перенести показанный состав в выбранную группу?'))]]+
                self.panel.navigation('projects'),state={})
            stream = BytesIO(canonical(row).encode()); stream.name='publication-preview.json'
            kwargs=dict(chat_id=self.actor.scope.chat_id,document=stream,caption='Состав переноса и аудитория')
            if self.actor.scope.topic_id>0:
                kwargs['message_thread_id']=self.actor.scope.topic_id
            await self.context.bot.send_document(**kwargs)
            return
        if kind in ('artifact_replace','artifact_title'):
            from artifacts.revisions import ArtifactRepository
            repo=ArtifactRepository(service.repository)
            old=await repo.get(id,self.actor)
            if kind=='artifact_title':
                operations=[dict(op='title',value=v['title'])]
            else:
                element=next(e for e in old['spec']['elements'] if e['id']==data['element'])
                operations=[dict(op='replace',id=element['id'],value=dict(element,text=v['text']))]
            row,_ = await repo.revise(id,self.actor,revision,operations,sources=await self.sources('Изменить результат: '+canonical(v)))
            return await self.object('artifacts',row['id'])
        if kind == 'task_revise':
            from agents.tasks import TaskRepository
            from agents.tools.core import build_registry
            repo=TaskRepository(service.repository,build_registry())
            row=await repo.get(id,self.actor)
            old=await repo.derivatives.load(row['plan_id'],self.actor,'task_plan')
            context=next((deepcopy(s['args']['context']) for s in old['steps'] if s['tool']=='workflow.plan'),
                         dict(project_id=row['project_id'],materials=[],numeric=[],allowed_input_derivatives=old.get('inputs',[])))
            async with repo.pool.acquire() as conn:
                refs=await repo.derivatives._chain(conn,row['plan_id'],self.actor)
            if not context['materials']:
                from dataclasses import asdict
                from materials.types import EvidenceRef
                for asset in sorted({ref['asset_id'] for ref in refs})[:8]:
                    extraction,bundle=await service.extract(asset,self.actor)
                    for b in bundle.blocks[:4]:
                        context['materials'].append(dict(text=b.text[:1800],source=asdict(EvidenceRef(asset,bundle.asset_version,extraction,b.block_id,b.locator)),quality=b.quality))
            plan=dict(goal=v['goal'],steps=[dict(id='plan',tool='workflow.plan',version='1',args=dict(goal=v['goal'],kind='task',context=context),depends=[])],
                      checks=[dict(step='plan',path=['id'],op='nonempty')],inputs=context.get('allowed_input_derivatives',[]))
            await repo.replan(id,self.actor,revision,plan,[*refs,*await self.sources('Изменить задание: '+v['goal'])],reason='new_evidence')
            return await self.task(id)
        raise MaterialError('menu_form_unknown')

    async def procedure_body(self, id, title):
        from agents.tasks import TaskRepository
        from agents.tools.core import build_registry
        service = await self.material_service()
        repo = TaskRepository(service.repository,build_registry())
        row = await repo.get(id,self.actor)
        if row['owner_id']!=self.actor.user_id or row['status']!='succeeded':
            raise MaterialError('procedure_success_required')
        plan = deepcopy(await repo.derivatives.load(row['plan_id'],self.actor,'task_plan'))
        for step in plan['steps']:
            step.pop('grant_id',None)
        outputs = await repo.outputs(row)
        return dict(title=title,recipe=plan,input_schema=dict(type='object',properties={},additionalProperties=False),
                    examples=[dict(bindings={},expected_tools=[s['tool'] for s in plan['steps']],outputs=outputs)])

    async def audio_choices(self, file):
        from materials.runtime import capture_document
        service = await self.material_service()
        audio = file.voice or file.audio or file.document
        descriptor = SimpleNamespace(file_id=audio.file_id,file_size=audio.file_size,file_name=getattr(audio,'file_name',None) or 'voice.ogg',mime_type=getattr(audio,'mime_type',None))
        source = await capture_document(self.context,descriptor,file,slot='audio' if file.voice or file.audio else 'document')
        id,timeline,_ = await service.transcript(source.material_uses[0].asset_id,self.actor)
        buttons=[]
        metadata=forms.parse(forms.FILE,'',file)
        for segment in timeline.segments[:12]:
            buttons.append([views.form('▶ '+segment.text[:25],'listen',file=metadata,segment=segment.id),
                views.form('✏️ Исправить','transcript_fix',file=metadata,segment=segment.id,transcript=id)])
        await self.render(self.last_text,buttons+self.panel.navigation('materials'),screen='audio',state={})

    async def act(self, action):
        if 'nav' in action:
            if action.get('selection'):
                return await self.object_list(action['nav'],action.get('page',0),selection=action['selection'])
            return await self.show(action['nav'],action.get('page',0))
        if 'confirm' in action:
            self.clear_legacy()
            return await self.render(escape(action['title']),[[('Да, выполнить',action['confirm']),('Нет',dict(nav='home'))]],screen='confirm',state={})
        if action.get('close'):
            self.clear_legacy(force=True)
            text = 'Я здесь. Продолжим в чате — просто напиши или пришли голосовое.'
            if self.actor.scope.chat_type != 'private':
                text += '\n\nВ группе ответь на это сообщение или обратись ко мне по имени.'
            await self.render(text,[[('🧭 Открыть меню',dict(nav='home'))]],screen='closed',state=dict(shared=True))
            return
        if 'form' in action:
            return await self.begin(action['form'],{k:v for k,v in action.items() if k!='form'})
        if 'value' in action:
            if self.panel.row['state'].get('pending')!='form':
                raise MaterialError('menu_stale_form')
            return await self.answer(action['value'])
        if 'confirm_page' in action:
            if self.panel.row['state'].get('pending')!='confirm_form':
                raise MaterialError('menu_stale_form')
            return await self.form_screen(deepcopy(self.panel.row['state']['draft']),confirm_page=action['confirm_page'])
        if action.get('form_back') or action.get('restart_form'):
            draft = deepcopy(self.panel.row['state']['draft'])
            draft['index']=0 if action.get('restart_form') else max(0,draft['index']-1)
            return await self.form_screen(draft)
        if action.get('submit'):
            return await self.submit()
        if 'project' in action:
            return await self.project(action['project'])
        if 'object' in action:
            return await self.object(action['object'],action['id'])
        if 'command' in action:
            return await self.execute_command(action['command'])
        if 'legacy' in action:
            self.restore_legacy()
            view=self.panel.row['state'].get('view')
            self.panel.output=False
            await bridge.callback(self,action['legacy'])
            if view and action['legacy'].startswith('work:'):
                # Exports keep the result's controls usable; acceptance refreshes it.
                service=await self.material_service()
                async with service.repository.pool.acquire() as conn:
                    name=await conn.fetchval('SELECT action FROM arti_work_actions WHERE id=$1',action['legacy'][5:])
                if name!='format':
                    return await self.act(view)
            if not self.panel.output:
                return await self.render('Готово. Результат отправлен отдельно.',self.panel.navigation(),state={})
            return
        if 'legacy_text' in action:
            return await self.legacy_input(action['legacy_text'])
        service = await self.material_service() if not action.get('special') else None
        if 'project_use' in action or 'project_control' in action:
            from projects.repository import ProjectRepository
            repo=ProjectRepository(service.repository)
            id=action.get('project_use') or action['id']
            p=await repo.get(id,self.actor)
            if p.revision!=action['revision']:
                raise MaterialError('stale_project_revision')
            if 'project_use' in action:
                await repo.select(id,self.actor)
            else:
                row=await repo.status(id,self.actor,action['revision'],dict(archive='archived',resume='active',delete='deleted')[action['project_control']])
                if row is None:
                    return await self.show('projects')
            return await self.project(id)
        if 'task_control' in action:
            from agents.tasks import TaskRepository
            from agents.tools.core import build_registry
            await TaskRepository(service.repository,build_registry()).control(action['id'],self.actor,action['revision'],action['task_control'])
            return await self.task(action['id'])
        if 'effect_preview' in action or 'effect_approve' in action:
            from agents.tasks import TaskRepository
            from agents.tools.core import build_registry
            repo=TaskRepository(service.repository,build_registry())
            if 'effect_approve' in action:
                await repo.authorize_effect(action['effect_approve'],self.actor,action['revision'],action['step'],action['digest'],bridge.MessageSurface(self)._menu_request_source)
                return await self.task(action['effect_approve'])
            preview=await repo.preview_effect(action['effect_preview'],self.actor,action['step'])
            text='🔎 <b>Проверить действие</b>\n\nИнструмент: '+escape(preview['tool'])+'\nАдресат: '+escape(preview['recipient'])+'\nАудитория: '+escape(preview['audience'])
            if action.get('digest') and (preview['digest']!=action['digest'] or preview['revision']!=action['revision']):
                raise MaterialError('task_preview_changed')
            parameters=json.dumps(preview['args'],ensure_ascii=False,indent=2)
            chunks=[parameters[i:i+500] for i in range(0,len(parameters),500)] or ['{}']
            page=min(max(0,action.get('page',0)),len(chunks)-1)
            text+='\n\nПараметры · '+str(page+1)+'/'+str(len(chunks))+':\n<code>'+escape(chunks[page])+'</code>\n\nЛимит затрат: '+escape(str(preview['cost_ceiling']))
            buttons=[]
            if page:
                buttons.append([('‹ Предыдущая часть',dict(effect_preview=preview['task_id'],step=preview['step_id'],page=page-1,revision=preview['revision'],digest=preview['digest']))])
            if page+1<len(chunks):
                buttons.append([('Следующая часть ›',dict(effect_preview=preview['task_id'],step=preview['step_id'],page=page+1,revision=preview['revision'],digest=preview['digest']))])
            else:
                buttons.append([('Разрешить это действие',dict(effect_approve=preview['task_id'],revision=preview['revision'],step=preview['step_id'],digest=preview['digest']))])
            buttons.append([('Не разрешать',dict(object='tasks',id=preview['task_id']))])
            return await self.render(text,buttons,screen='effect_preview',state={})
        if 'decision_choose' in action:
            from projects.workflows import WorkflowRepository
            row=await WorkflowRepository(service.repository).get(action['decision_choose'],self.actor,'decision')
            if row['revision']!=action['revision']:
                raise MaterialError('stale_workflow_revision')
            buttons=[[views.form(option,'decision_confirm',id=row['id'],revision=row['revision'],option=option)] for option in row['body'].get('options',[])]
            if not buttons:
                buttons=[[views.form('Подтвердить решение','decision_confirm',id=row['id'],revision=row['revision'])]]
            return await self.render('Какой вариант выбираешь?',buttons+self.panel.navigation('decision'),state={})
        if 'procedure_select' in action:
            return await self.object_list('tasks',selection=dict(kind='procedure_revise',id=action['procedure_select'],revision=action['revision']))
        if 'select_task' in action:
            selection=action['selection']
            return await self.begin(selection['kind'],dict(id=selection['id'],revision=selection['revision'],task_id=action['select_task']))
        if 'procedure_approve' in action:
            from agents.procedures import ProcedureRepository
            from agents.tools.core import build_registry
            row=await ProcedureRepository(service.repository,build_registry()).approve_change(action['procedure_approve'],self.actor,action['revision'],action['proposal'],await self.sources('Принять изменение процедуры'))
            return await self.object('procedure',row['id'])
        if 'artifact_history' in action or 'artifact_edit' in action:
            from artifacts.revisions import ArtifactRepository
            id=action.get('artifact_history') or action['artifact_edit']
            row=await ArtifactRepository(service.repository).get(id,self.actor)
            if row['revision']!=action['revision']:
                raise MaterialError('stale_artifact_revision')
            if 'artifact_edit' in action:
                buttons=[[views.form('Изменить: '+e['label'][:30],'artifact_replace',id=id,revision=row['revision'],element=e['id'])] for e in row['spec']['elements'][:20]]
                buttons.insert(0,[views.form('Изменить заголовок','artifact_title',id=id,revision=row['revision'])])
                buttons += [[('Удалить: '+e['label'][:30],dict(confirm=dict(artifact_remove=id,revision=row['revision'],element=e['id']),title='Удалить выбранный блок?'))] for e in row['spec']['elements'][:20]]
                return await self.render('Какой блок поправим? Числа исправляются в источнике.',buttons+self.panel.navigation('artifacts'),state={})
            async with service.repository.pool.acquire() as conn:
                versions=await conn.fetch('SELECT revision FROM arti_artifact_revisions WHERE artifact_id=$1 ORDER BY revision DESC LIMIT 20',id)
            buttons=[[('Версия '+str(v['revision']),dict(confirm=dict(command=f"/artifact rollback {id} {row['revision']} {v['revision']}"),title='Восстановить выбранную версию?'))] for v in versions if v['revision']!=row['revision']]
            return await self.render('Какую версию восстановить?',buttons+self.panel.navigation('artifacts'),state={})
        if 'artifact_remove' in action:
            from artifacts.revisions import ArtifactRepository
            row,_=await ArtifactRepository(service.repository).revise(action['artifact_remove'],self.actor,action['revision'],
                [dict(op='remove',id=action['element'])],sources=await self.sources('Удалить выбранный блок'))
            return await self.object('artifacts',row['id'])
        if action.get('special')=='rp_off':
            if self.actor.scope.chat_type!='private':
                raise MaterialError('menu_roleplay_private')
            import config
            config.rp_mode_state.pop(self.actor.scope.chat_id,None)
            from cognition.runtime import get_runtime
            runtime=get_runtime()
            if runtime:
                await runtime.new_scene(self.actor.scope.chat_id)
            return await self.show('home')
        raise MaterialError('menu_action_unknown')
