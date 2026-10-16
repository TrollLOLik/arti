"""Build the static Arti screen gallery. No application logic or API calls."""
from pathlib import Path

ROOT = Path(__file__).parent

ICONS = {
 'chat':'<path d="M21 11a8 8 0 0 1-8 8H8l-5 3V11a9 9 0 0 1 18 0Z"/><path d="M8 10h8m-8 4h5"/>',
 'spark':'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
 'folder':'<path d="M3 5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M3 10h18"/>',
 'user':'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
 'search':'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
 'plus':'<path d="M12 4v16M4 12h16"/>',
 'arrow':'<path d="M4 12h16m-6-6 6 6-6 6"/>',
 'back':'<path d="m14 5-7 7 7 7"/>',
 'chevron':'<path d="m9 5 7 7-7 7"/>',
 'down':'<path d="m6 9 6 6 6-6"/>',
 'more':'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
 'edit':'<path d="m16 3 5 5-12 12-6 1 1-6ZM13 6l5 5"/>',
 'pin':'<path d="m8 3 8 0-1 6 4 4v2H5v-2l4-4ZM12 15v7"/>',
 'check':'<path d="m5 12 4 4L19 6"/>',
 'double':'<path d="m2 12 4 4L16 6m-5 9 2 2L23 7"/>',
 'attach':'<path d="m9 16 8-8a3 3 0 0 0-4-4L3 14a5 5 0 0 0 7 7L21 10m-4-2L7 18"/>',
 'mic':'<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8"/>',
 'send':'<path d="m3 3 19 9-19 9 4-9Zm4 9h15"/>',
 'image':'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
 'video':'<rect x="2" y="5" width="14" height="14" rx="3"/><path d="m16 10 6-4v12l-6-4Z"/>',
 'music':'<path d="M9 18V5l12-3v13M9 9l12-3"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="15" rx="3" ry="3"/>',
 'file':'<path d="M14 2H5v20h14V7ZM14 2v5h5M8 12h8m-8 4h6"/>',
 'table':'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12M3 15h18"/>',
 'clock':'<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
 'calendar':'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 2v6m10-6v6M3 11h18m-13 4h2m4 0h2m-8 3h2"/>',
 'bell':'<path d="M5 9a7 7 0 0 1 14 0v7l2 3H3l2-3ZM10 22h4"/>',
 'shield':'<path d="M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6Z"/><path d="m8 12 3 3 5-6"/>',
 'link':'<path d="m10 14 4-4m-5 8-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 6a4 4 0 0 0 6 0l5-5a4 4 0 0 0-6-6l-2 2"/>',
 'people':'<circle cx="9" cy="7" r="4"/><path d="M1 21v-2a8 8 0 0 1 16 0v2m0-18a4 4 0 0 1 0 8m3 10v-2a8 8 0 0 0-3-6"/>',
 'play':'<path d="m8 4 12 8-12 8Z"/>',
 'sun':'<circle cx="12" cy="12" r="4"/><path d="M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"/>',
 'moon':'<path d="M20.5 13A9 9 0 0 1 11 3.5 9 9 0 1 0 20.5 13Z"/>',
 'memory':'<path d="M9 3a4 4 0 0 0-6 4 5 5 0 0 0-1 8 4 4 0 0 0 7 5h3V6a3 3 0 0 0-3-3Zm6 0a4 4 0 0 1 6 4 5 5 0 0 1 1 8 4 4 0 0 1-7 5h-3M6 9a4 4 0 0 1 6 3m6-3a4 4 0 0 0-6 3"/>',
 'settings':'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
}
def icon(name, cls=''):
 return f'<svg class="ic {cls}" aria-hidden="true"><use href="#i-{name}"/></svg>'
def emblem(cls=''):
 return f'<span class="emblem {cls}" aria-hidden="true"><i></i></span>'
def avatar(text, cls='', arti=False):
 return f'<span class="av {cls}">{"<img src=assets/arti-portrait-v2.png alt=Арти>" if arti else text}</span>'
def btn(text, glyph='arrow', cls=''):
 return f'<span class="action {cls}">{text}{icon(glyph) if glyph else ""}</span>'
def ib(name, cls=''):
 return f'<span class="ib {cls}">{icon(name)}</span>'
def nav(active):
 return '<div class="appnav">'+''.join(f'<span class="navitem {"on" if key==active else ""}">{icon(glyph)}<span>{label}</span></span>' for key,glyph,label in [('chats','chat','Чаты'),('create','spark','Создать'),('space','folder','Пространство'),('profile','user','Профиль')])+'</div>'
def heading(title, subtitle='', right='edit'):
 return f'<div class="screen-heading"><div><h2>{title}</h2>{f"<p>{subtitle}</p>" if subtitle else ""}</div>{ib(right) if right else ""}</div>'
def search(text):
 return f'<div class="search">{icon("search")}<span>{text}</span></div>'
def tabs(labels, active=0):
 return '<div class="tabs">'+''.join(f'<span class="{ "selected" if n==active else ""}">{label}</span>' for n,label in enumerate(labels))+'</div>'
def date_separator(label): return f'<div class="date-separator"><span>{label}</span></div>'
def bubble(text,time='',mine=False,author='',cls='',quote='',origin=''):
 return f'<div class="message {"mine" if mine else ""} {cls}">{f"<span class=author>{author}</span>" if author else ""}{f"<div class=quote>{quote}</div>" if quote else ""}<div class="message-text">{text}</div><span class="message-meta">{f"<span class=origin>{origin}</span>" if origin else ""}{time}{icon("double") if mine else ""}</span></div>'
def composer(text='Сообщение', active=False):
 return f'<div class="compose">{ib("attach")}<span class="compose-placeholder">{text}</span>{ib("send" if active else "mic", "send" if active else "")}</div>'
def chat_header(title,sub,av='А',cls='',arti=False):
 return f'<div class="chat-header">{icon("back")}{avatar(av,cls,arti)}<div class="chat-person"><h3>{title}</h3><p>{sub}</p></div>{icon("more")}</div>'
def chatrow(name,preview,time,av,cls='',count='',arti=False,pinned=False):
 return f'<div class="chatrow {"pinned" if pinned else ""}">{avatar(av,cls,arti)}<div class="chat-copy"><div class="chat-name">{name}{icon("pin") if pinned else ""}</div><p>{preview}</p></div><div class="chat-tail"><time>{time}</time>{f"<span class=unread>{count}</span>" if count else icon("double")}</div></div>'
def file_row(title,desc,glyph='file', cls=''):
 return f'<div class="file-row">{ib(glyph,cls)}<div><strong>{title}</strong><small>{desc}</small></div>{icon("chevron")}</div>'
def section(title,meta=''):
 return f'<div class="section-head"><h3>{title}</h3><span>{meta}</span></div>'
def task(title,meta,done=False,reminder=False):
 return f'<div class="task {"done" if done else ""}"><span class="checkbox {"checked" if done else ""}">{icon("check") if done else icon("bell") if reminder else ""}</span><div><strong>{title}</strong><small>{meta}</small></div>{icon("more")}</div>'

def chats():
 return heading('Чаты <span class="title-count">4</span>','',right='edit')+search('Поиск по чатам и сообщениям')+tabs(['Все','Личные','Группы'])+'<div class="chatlist">'+chatrow('Арти','Никакую. В этом и достоинство.','19:12','','av-arti',arti=True,pinned=True)+'<div class="conversation-continuation">'+icon('folder')+'<div><small>Продолжить проект</small><strong>Вечер без повода</strong></div>'+icon('chevron')+'</div>'+chatrow('Аня','Этот красный — очень в твоём духе.','18:36','А','av-rose','2')+chatrow('Идеи на выходные','<b>Арти:</b> Ранний подъём исключила.','17:58','И','av-group','4')+chatrow('Макс','Голосовое сообщение · 0:18','17:24','М','av-ochre')+'</div>'+nav('chats')

def arti_chat():
 return chat_header('Арти','Личный разговор',arti=True,cls='av-arti')+'<div class="sync-note">'+icon('link')+'Общая история с Telegram</div><div class="topic-chip">'+icon('folder')+'<strong>Вечер без повода</strong><span>Проект</span>'+icon('chevron')+'</div><div class="messages arti-messages">'+date_separator('Сегодня, 15 октября')+bubble('Как спасти совершенно скучный вечер?','19:08',mine=True,origin='из Telegram')+bubble('Нашла запись без помех. Не спрашивай, какую практическую задачу это решает.','19:10',cls='arti-answer')+bubble('И всё же?','19:11',mine=True)+bubble('<span class="arti-emphasis">Никакую. В этом и достоинство.</span>Ставим музыку и делаем один кадр. Свет я бы выбрала красный.','19:12',cls='arti-answer')+'</div><div class="conversation-options"><span class="chosen-option">'+icon('check')+'Твоя музыка + мой кадр'+icon('arrow')+'</span><span class="own-option">У меня свой вариант'+icon('edit')+'</span></div>'+composer('Я бы начала с…',True)

def human_chat():
 waveform='<span class="waveform">'+''.join(f'<i style="height:{v}px"></i>' for v in [10,17,24,12,29,34,20,12,25,31,17,24,12,30,18,10,23,15,28,20,12,16,8])+'</span>'
 return chat_header('Аня','в сети','А','av-rose')+'<div class="messages human-messages">'+date_separator('Сегодня')+bubble('Смотри, какой свет был утром. Надо сохранить это настроение.','12:28')+'<div class="photo-message"><img src="assets/paper-bloom.png" alt="Бумажные цветы в тёплых розовых тонах"><span>12:29</span></div>'+bubble('Да! Именно такая палитра нужна для нашего проекта.','12:32',mine=True,quote='<b>Аня</b>Надо сохранить это настроение.')+f'<div class="voice-message">{ib("play")}{waveform}<span>0:18<small>12:36</small></span></div></div>'+composer()

def group_chat():
 return chat_header('Идеи на выходные','4 человека · Арти','И','av-group')+'<div class="messages group-messages">'+date_separator('Сегодня')+bubble('Предлагаю прогулку, кофе и немного поснимать город.','17:52',author='Аня',cls='author-rose')+bubble('Я за! Только без раннего подъёма.','17:54',mine=True)+bubble('<span class="mention">@Арти</span>, собери план на субботу. Начинаем после 12.','17:56',author='Макс',cls='author-ochre')+bubble('Собрала:<br>12:30 — кофе<br>13:15 — прогулка с камерой<br>15:00 — обед и обмен снимками<br><br>Ранний подъём исключила. Возражение было убедительным.','17:58',author=emblem()+'Арти <span class="ai-label">ИИ</span>',cls='arti-answer')+'</div>'+composer('Сообщение в комнату',True)

def new_chat():
 people=''.join(f'<div class="select-person">{avatar(a,c)}<div><strong>{name}</strong><small>{note}</small></div><span class="select-check {"yes" if sel else ""}">{icon("check") if sel else ""}</span></div>' for name,a,c,note,sel in [('Аня','А','av-rose','Была недавно',True),('Макс','М','av-ochre','В сети',True),('Даша','Д','av-brown','Была 20 минут назад',True)])
 return '<div class="simple-back">'+icon('back')+'Чаты</div>'+heading('Новый разговор','Соберите своих',right='')+tabs(['Личный чат','Группа'],1)+'<div class="group-name"><span class="ib av-group">'+icon('people')+'</span><div><small>Название группы</small><strong>Идеи на выходные</strong></div></div>'+search('Найти по имени')+section('Участники','ВЫБРАНО 3')+people+'<div class="invite-inline">'+icon('link')+'Пригласить по ссылке'+icon('chevron')+'</div><div class="bottom-action">'+btn('Создать группу','arrow')+'<p>Арти можно добавить в комнате позже</p></div>'

def room_info():
 members=''.join(f'<div class="member-row">{avatar(a,c,arti=ai)}<strong>{name}</strong><small>{role}</small></div>' for name,a,c,role,ai in [('Вы','Е','av-rose','Владелец',False),('Аня','А','av-rose','Участник',False),('Макс','М','av-ochre','Участник',False),('Даша','Д','av-brown','Участник',False),('Арти','','av-arti','Помощник · ИИ',True)])
 return '<div class="simple-back">'+icon('back')+'О комнате'+icon('more')+'</div><div class="room-identity">'+avatar('И','av-group av-large')+'<h2>Идеи на выходные</h2><p>4 человека и Арти</p></div><div class="room-actions"><span>'+icon('people')+'Добавить</span><span>'+icon('link')+'Пригласить</span><span>'+icon('folder')+'12 файлов</span></div><div class="room-rule"><div>'+emblem()+'<strong>Арти отвечает на обращения</strong></div><span class="toggle on"></span></div><p class="room-hint">Напишите @Арти, когда нужна помощь.</p>'+section('Участники','5')+members+'<div class="private-inline">'+icon('shield')+'Личная память не доступна участникам этой комнаты.</div>'

def create():
 return heading('Создать','От идеи — к результату',right='')+'<div class="atelier"><div class="atelier-copy"><span class="eyebrow">Мастерская Арти</span><h3>Запись № 7</h3><p>«Нашла пропавшую фразу.»</p><div class="record-sample">'+ib('play')+'<div><div class="record-progress"><i></i></div><span>0:18 <small>0:46</small></span></div></div></div><img src="assets/arti-portrait-v2.png" alt="Арти с серебристыми волосами и красным бантом"></div>'+section('Инструменты')+'<div class="creative-image">'+ib('image')+'<div><h3>Изображение</h3><p>Из описания — в кадр</p>'+icon('arrow')+'</div><img src="assets/paper-bloom.png" alt="Красная бумажная композиция"></div><div class="creative-duet">'+''.join(f'<div>{icon(g)}<h3>{t}</h3><p>{d}</p></div>' for g,t,d in [('video','Видео','Оживить идею'),('music','Музыка','Найти звучание')])+'</div><div class="secondary-tools">'+''.join(f'<div>{icon(g)}<strong>{t}</strong></div>' for g,t in [('file','Документ'),('table','Таблица'),('mic','Голос'),('folder','Материалы')])+'</div>'+nav('create')

def image_generation():
 return '<div class="simple-back">'+icon('back')+'Создать</div>'+heading('Изображение','Проект «Вечер без повода»',right='')+'<div class="prompt-field"><span>Описание</span><p>Бумажный цветок. Лаконичная форма, глубокий красный, свет сбоку. Никаких лишних деталей.</p></div><div class="format-row"><span class="format active">1:1</span><span class="format">4:5</span><span class="format">16:9</span><span class="style-label">Бумажная пластика '+icon('down')+'</span></div><figure class="image-result"><img src="assets/paper-bloom.png" alt="Пример результата: красные бумажные цветы"><figcaption><span>'+icon('image')+'Красный, без повода</span><span>1024 × 1024</span></figcaption></figure><div class="curator-line">'+avatar('','av-arti',True)+'<span>Я бы оставила красный.</span><small>Другой стиль '+icon('arrow')+'</small></div><p class="sample-label">Иллюстративный пример результата</p><div class="result-actions">'+btn('Сохранить в проект','folder')+btn('В чат','chat','secondary')+'</div>'+nav('create')

def project_card():
 return '<div class="project-card"><div class="row-between"><span class="source-badge">'+icon('chat')+'Из разговора с Арти</span>'+icon('more')+'</div><h3>Вечер без повода</h3><p>Музыка, свет и один хороший кадр</p><div class="project-cover"><img src="assets/paper-bloom.png" alt="Красная бумажная композиция"><div><span>Выбрали настроение</span><div class="project-palette"><i></i><i></i><i></i><i></i></div><small>2 из 5 задач готовы</small></div></div><div class="project-foot"><span class="progress-track"><i></i></span>'+icon('arrow')+'</div></div>'
def space():
 return heading('Пространство','Проекты, файлы и важные дела','plus')+tabs(['Проекты','Файлы','Дела'])+project_card()+section('Следующая задача','Все дела '+icon('arrow'))+'<div class="space-tasks">'+task('Сделать один кадр','Вечер без повода · без срока')+'</div>'+section('Недавние файлы','2 файла')+file_row('Красный, без повода.png','Изображение · из разговора','image','rose')+file_row('Идеи для вечера.docx','Документ · сегодня')+nav('space')

def project():
 return '<div class="simple-back">'+icon('back')+'Пространство'+icon('more')+'</div><div class="project-title"><span class="source-badge">Личный проект</span><h2>Вечер без повода</h2><p>Собрать из музыки, света и одного кадра вечер, который хочется сохранить.</p></div><div class="linked-chat">'+avatar('','av-arti',True)+'<div><strong>Обсуждение проекта</strong><small>15 октября · из разговора с Арти</small></div>'+icon('chevron')+'</div><div class="continuity-path"><span>'+icon('chat')+'Разговор</span>'+icon('chevron')+'<span>'+icon('folder')+'Проект</span>'+icon('chevron')+'<span>'+icon('image')+'Результат</span></div>'+section('Задачи','2 из 5')+'<div class="project-tasks">'+task('Выбрать музыку','Запись № 7 · из разговора',True)+task('Собрать палитру','Красный, графит, светлый фон',True)+task('Сделать один кадр','Следующая задача')+'</div>'+section('Материалы','2 файла')+'<div class="project-file-grid"><div><img src="assets/paper-bloom.png" alt="Бумажные цветы"><span><strong>Красный,<br>без повода</strong><small>Изображение</small></span></div><div>'+icon('file')+'<span><strong>Идеи вечера</strong><small>Документ</small></span></div></div>'+nav('space')

def organizer():
 return '<div class="simple-back">'+icon('back')+'Пространство</div>'+heading('Дела','','plus')+'<div class="day-card"><div class="month-title"><strong>Октябрь 2026</strong><small>Сегодня</small></div><div class="week-strip">'+''.join(f'<span class="{"current" if d==15 else ""}"><small>{day}</small><b>{d}</b></span>' for day,d in [('пн',12),('вт',13),('ср',14),('чт',15),('пт',16),('сб',17)])+'</div></div>'+tabs(['Предстоит','Готово'])+'<div class="organizer-list">'+task('Сделать один кадр','Вечер без повода · без срока')+task('Встреча с Аней','Сегодня, 20:00 · за 30 минут',reminder=True)+task('Отправить Даше наброски','Завтра, 10:00 · напоминание',reminder=True)+task('Забрать фотографии','Завтра, 18:00 · напоминание',reminder=True)+'</div><div class="timezone-note">'+icon('clock')+'Екатеринбург · UTC+5</div>'+nav('space')

def profile():
 return heading('Профиль','',right='')+'<div class="profile-person">'+avatar('Е','av-neutral av-large')+'<div><h3>Елена</h3><p>Личное пространство</p></div>'+icon('chevron')+'</div><div class="memory-card"><div class="memory-heading">'+icon('memory')+'<h3>Память и договорённости</h3></div><span class="memory-kind">Договорились</span><h4>Сначала спросить</h4><p>Выбирать за тебя — только если попросишь.</p><div class="memory-source">'+icon('chat')+'<div><strong>Из разговора с Арти</strong><span>15 октября, 19:14 · с твоих слов</span></div>'+icon('chevron')+'</div><div class="memory-actions"><span>'+icon('edit')+'Исправить</span><span>Убрать</span></div></div>'+section('Настройки')+'<div class="setting-row"><span>'+icon('sun')+'Оформление</span><small>Светлое / Тёмное '+icon('chevron')+'</small></div><div class="setting-row"><span>'+icon('bell')+'Напоминания</span><span class="toggle on"></span></div><div class="setting-row"><span>'+icon('shield')+'Приватность</span><small>Память видна только тебе '+icon('chevron')+'</small></div>'+nav('profile')

SCREENS=[
 ('Чаты','Закреплённая Арти, люди, группы и быстрый переход к проекту.',chats,'chats'),
 ('Разговор с Арти','Общая история с Telegram. Предложение Арти и свой вариант.',arti_chat,'chat arti-conversation'),
 ('Личный разговор','Ответы, снимки и голосовые. Пространство для двоих.',human_chat,'chat'),
 ('Группа с Арти','Помощник включается по @обращению и видит контекст комнаты.',group_chat,'chat'),
 ('Новый разговор','Личный чат или своя группа. Можно пригласить по ссылке.',new_chat,'new'),
 ('О комнате','Участники, приглашения и понятные границы памяти Арти.',room_info,'info'),
 ('Создать','Личный музыкальный проект Арти и доступные инструменты.',create,'create'),
 ('Изображение','Описание, формат и результат с понятным сохранением.',image_generation,'image'),
 ('Пространство','Проекты, задачи и файлы, к которым легко вернуться.',space,'space'),
 ('Проект','Связанный разговор, прогресс, задачи и результаты.',project,'project'),
 ('Дела и напоминания','Понятное время и короткий список без перегруженного календаря.',organizer,'organizer'),
 ('Профиль и память','Договорённости с источником, датой и управлением записью.',profile,'profile'),
]

def phone(n,title,caption,body,key,dark=False):
 return f'''<figure class="shot" id="screen-{n:02}"><figcaption><div><span class="shot-number">{n:02}</span><h3>{title}</h3>{'<span class="dark-tag">DARK</span>' if dark else ''}</div><p>{caption}</p></figcaption><div class="device"><div class="screen {'dark' if dark else 'light'} {key}"><div class="statusbar"><b>9:41</b><span class="island"></span><span class="status-icons"><i class="signal"></i><svg viewBox="0 0 18 14" aria-hidden="true"><path d="M1 4a12 12 0 0 1 16 0M4 7a8 8 0 0 1 10 0m-7 3a3 3 0 0 1 4 0"/></svg><i class="battery"></i></span></div><div class="telegram-header"><span>{icon('back')}Закрыть</span><strong>Арти{icon('down')}</strong>{icon('more')}</div><div class="app-content">{body}</div><span class="home-indicator"></span></div></div></figure>'''

sprite='<svg class="sprite" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'+''.join(f'<symbol id="i-{k}" viewBox="0 0 24 24">{v}</symbol>' for k,v in ICONS.items())+'</svg>'
header='''<header class="gallery-header"><div class="gallery-brand">'''+emblem()+'''<div><h1>арти<span>.</span></h1><p>Общение. Идеи. Всё рядом.</p></div></div><div class="header-meta"><span>TELEGRAM MINI APP</span><b>Концепция 03 <span class="version-tag">MODERN</span></b><a href="archive/v2/index.html">Версия 02 ↗</a></div></header><div class="gallery-guide"><div class="design-principles"><span><i></i>Разговоры</span><span>Создание</span><span>Личное пространство</span></div><nav class="route-map" aria-label="Разделы концепции"><a href="#communication">Чаты</a><a href="#creation">Создать</a><a href="#space">Пространство</a><a href="#screen-12">Профиль</a></nav></div><p class="gallery-disclaimer">16 статичных экранов с примерными данными. Для согласования дизайна и сценариев.</p>'''

parts=[f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Арти — современная связь. Концепция 03 Telegram Mini App: 16 статичных экранов."><title>Арти — современная связь / концепция 03</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"><style>{(ROOT / "gallery.css").read_text(encoding="utf-8")}</style></head><body>{sprite}<main class="gallery-page">{header}']
for start,end,id_,label,note in [(0,6,'communication','Общение','С Арти, один на один и в своих группах.'),(6,9,'creation','Идеи и инструменты','Создавать новое и сохранять результат.'),(9,12,'space','Проекты и личное','Всё важное с понятным контекстом.')]:
 parts.append(f'<section class="chapter" id="{id_}"><div class="chapter-heading"><h2>{label}</h2><p>{note}</p><span>{start+1:02}—{end:02}</span></div><div class="gallery-grid">')
 for i in range(start,end):
  title,caption,render,key=SCREENS[i]; parts.append(phone(i+1,title,caption,render(),key))
 parts.append('</div></section>')
parts.append('<section class="chapter dark-chapter" id="dark"><div class="chapter-heading"><h2>Тёмная тема</h2><p>Те же сценарии. Мягче для глаз вечером.</p><span>13—16</span></div><div class="gallery-grid">')
for n,i in enumerate([0,1,3,11],13):
 title,caption,render,key=SCREENS[i];parts.append(phone(n,title,caption,render(),key,True))
parts.append('</div></section><footer class="gallery-footer"><span>'+emblem()+'арти.</span><p>Концепция 03 · октябрь 2026</p><a href="archive/v2/index.html">Версия 02 ↗</a><a href="archive/v1/index.html">Версия 01 ↗</a><a href="#">Наверх ↑</a></footer></main></body></html>')
(ROOT / 'index.html').write_text(''.join(parts),encoding='utf-8')
print(f'Built {ROOT / "index.html"} — 16 static screens')
