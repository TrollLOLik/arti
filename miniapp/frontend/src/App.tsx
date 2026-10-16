import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ComponentType, CSSProperties, ReactNode } from 'react';
import {
  ArrowDown, ArrowLeft, ArrowRight, ChevronRight, CircleAlert, FileText,
  Folder, Image as ImageIcon, Info, Layers3, LoaderCircle, LockKeyhole, LogOut,
  Monitor, Music2, RefreshCw, Search, ShieldCheck, Table2, Users, Video, X, Check,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { ApiError, isAborted, request } from './api';
import { navigate, parseRoute } from './navigation';
import { CHROME_COLOR, systemTheme, updateTelegramChrome } from './theme';
import { requestLabel, validateText } from './chat';
import { dayKey, dayLabel, headerStatus, replyPhase, rowTime, TYPING_TEXT } from './chatView';
import type { ReplyPhase } from './chatView';
import { AttachIcon, NotebookIcon, SendIcon, StarIcon, TabChatsIcon, TabCreateIcon, TabProfileIcon, TabSpaceIcon, TelegramIcon, TickIcon } from './icons';
import type { IconProps } from './icons';
import { ArtiRoom, LifeSetting, LifeStrip, useArtiLife } from './ArtiLife';
import { NavRibbon } from './NavRibbon';
import { Portrait } from './Portrait';
import { useConversation, useConversationPreview } from './useConversation';
import type { Bootstrap, Conversation, Message, ResourcePage, Session, Tab, Theme } from './types';

const NAV: { id: Tab; name: string; icon: ComponentType<IconProps> }[] = [
  { id: 'chats', name: 'Чаты', icon: TabChatsIcon }, { id: 'create', name: 'Создать', icon: TabCreateIcon },
  { id: 'space', name: 'Пространство', icon: TabSpaceIcon }, { id: 'profile', name: 'Профиль', icon: TabProfileIcon },
];

function Emblem() { return <span className="emblem" aria-hidden="true"><StarIcon size={16} /></span>; }
function Wordmark() { return <span className="wordmark">арти<i>.</i></span>; }
function ArtiAvatar({ size = 'md' }: { size?: 'sm' | 'md' | 'lg' }) { return <span className={`avatar arti-avatar avatar-${size}`}><Portrait /></span>; }
function Spinner({ label = 'Загружаем…' }: { label?: string }) { return <div className="loading" role="status"><span className="breath" aria-hidden="true"><StarIcon size={17} /></span><span>{label}</span></div>; }
function ErrorState({ retry, compact = false }: { retry: () => void; compact?: boolean }) {
  return <div className={`error-state${compact ? ' compact' : ''}`} role="alert"><CircleAlert size={25} /><h3>Не получилось загрузить</h3><p>Проверьте соединение и попробуйте ещё раз.</p><button className="button secondary" onClick={retry}><RefreshCw size={17} />Повторить</button></div>;
}
function useLoad<T>(path: string | null, onExpired: () => void) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!path) { setLoading(false); setError(false); setData(null); return; }
    const controller = new AbortController();
    setLoading(true); setError(false); setData(null);
    request<T>(path, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setData(result); }).catch((cause: unknown) => {
      if (controller.signal.aborted || isAborted(cause)) return;
      if (cause instanceof ApiError && cause.status === 401) onExpired();
      else setError(true);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, revision, onExpired]);
  return { data, setData, loading, error, retry: () => setRevision(value => value + 1) };
}

function Dialog({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = `dialog-title`;
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} className="dialog" aria-labelledby={titleId} onCancel={close} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="dialog-inner"><header><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="Закрыть" onClick={close}><X size={21} /></button></header>{children}</div>
  </dialog>;
}

function useTheme(theme: Theme) {
  const [system, setSystem] = useState<'light' | 'dark'>(() => systemTheme(window.Telegram?.WebApp, window.matchMedia('(prefers-color-scheme: dark)').matches));
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const webapp = window.Telegram?.WebApp?.initData ? window.Telegram.WebApp : undefined;
    const update = () => setSystem(systemTheme(webapp, media.matches));
    media.addEventListener('change', update); webapp?.onEvent?.('themeChanged', update);
    return () => { media.removeEventListener('change', update); webapp?.offEvent?.('themeChanged', update); };
  }, []);
  useEffect(() => {
    const resolved = theme === 'system' ? system : theme;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    const color = CHROME_COLOR[resolved];
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', color);
    updateTelegramChrome(window.Telegram?.WebApp, color);
  }, [theme, system]);
}

function useTelegram(conversation: boolean) {
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    if (!app?.initData) return;
    app.ready?.(); app.expand?.();
    const viewport = () => {
      const root = document.documentElement;
      if (app.viewportHeight && app.viewportHeight > 0) root.style.setProperty('--app-height', `${app.viewportHeight}px`);
      if (app.viewportStableHeight && app.viewportStableHeight > 0) root.style.setProperty('--stable-height', `${app.viewportStableHeight}px`);
      for (const side of ['top', 'bottom', 'left', 'right'] as const) {
        const value = Math.max(app.safeAreaInset?.[side] ?? 0, app.contentSafeAreaInset?.[side] ?? 0);
        root.style.setProperty(`--tg-inset-${side}`, `${value}px`);
      }
    };
    viewport();
    for (const event of ['viewportChanged', 'safeAreaChanged', 'contentSafeAreaChanged']) app.onEvent?.(event, viewport);
    return () => { for (const event of ['viewportChanged', 'safeAreaChanged', 'contentSafeAreaChanged']) app.offEvent?.(event, viewport); };
  }, []);
  useEffect(() => {
    const back = window.Telegram?.WebApp?.BackButton;
    if (!window.Telegram?.WebApp?.initData || !back) return;
    const onBack = () => navigate('chats');
    if (conversation) { back.show(); back.onClick(onBack); } else back.hide();
    return () => { back.offClick(onBack); back.hide(); };
  }, [conversation]);
}

type EntryState = 'loading' | 'demo' | 'telegram' | 'expired' | 'error' | 'signedOut' | 'ready';
export default function App() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const [theme, setTheme] = useState<Theme>(() => {
    try { const stored = localStorage.getItem('arti.theme'); return stored === 'light' || stored === 'dark' ? stored : 'system'; } catch { return 'system'; }
  });
  const [entry, setEntry] = useState<EntryState>('loading');
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [revision, setRevision] = useState(0);
  const [authBusy, setAuthBusy] = useState(false);
  useTheme(theme); useTelegram(route.conversation);
  useEffect(() => { const update = () => setRoute(parseRoute(window.location.hash)); window.addEventListener('hashchange', update); return () => window.removeEventListener('hashchange', update); }, []);
  const expired = useCallback(() => { setBootstrap(null); setEntry('expired'); }, []);
  useEffect(() => {
    const controller = new AbortController();
    async function start() {
      setEntry('loading');
      try {
        try { await request<Session>('/api/session', { signal: controller.signal }); }
        catch (error) {
          if (!(error instanceof ApiError) || error.status !== 401) throw error;
          const init = window.Telegram?.WebApp?.initData;
          if (init) await request<Session>('/api/auth/telegram', { method: 'POST', body: { init_data: init }, signal: controller.signal });
          else {
            const health = await request<{ mode: string }>('/healthz', { signal: controller.signal });
            if (!controller.signal.aborted) setEntry(health.mode === 'demo' ? 'demo' : 'telegram');
            return;
          }
        }
        const result = await request<Bootstrap>('/api/bootstrap', { signal: controller.signal });
        if (!controller.signal.aborted) { setBootstrap(result); setEntry('ready'); }
      } catch (error) {
        if (isAborted(error)) return;
        if (!controller.signal.aborted) setEntry(error instanceof ApiError && error.status === 401 ? 'expired' : 'error');
      }
    }
    void start(); return () => controller.abort();
  }, [revision]);
  async function startDemo() {
    setAuthBusy(true);
    try {
      await request<Session>('/api/auth/demo', { method: 'POST', body: {} });
      const result = await request<Bootstrap>('/api/bootstrap');
      setBootstrap(result); setEntry('ready');
    } catch { setEntry('error'); }
    finally { setAuthBusy(false); }
  }
  const changeTheme = (value: Theme) => { setTheme(value); try { localStorage.setItem('arti.theme', value); } catch { /* Theme remains usable with storage disabled. */ } };
  const signedOut = () => { setBootstrap(null); setEntry('signedOut'); navigate('chats'); };
  if (entry !== 'ready' || !bootstrap) return <EntryScreen state={entry} retry={() => setRevision(value => value + 1)} startDemo={startDemo} busy={authBusy} />;
  const current = NAV.find(item => item.id === route.tab)!;
  return <div className={`app-shell${route.conversation ? ' has-conversation' : ''}`}>
    <a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus(); }}>К содержимому</a>
    <aside className="sidebar">
      <a className="brand" href="#chats" aria-label="Арти — чаты"><Wordmark /></a>
      <p className="sidebar-caption">Ближе, чем кажется</p>
      <nav aria-label="Основная навигация" className="desktop-nav"><NavRibbon active={route.tab} axis="y" />{NAV.map(({ id, name, icon: Icon }) => <a key={id} data-tab={id} href={`#${id}`} aria-label={name} title={name} aria-current={route.tab === id ? 'page' : undefined}><Icon size={22} /><span>{name}</span></a>)}</nav>
      <div className="sidebar-bottom"><ArtiAvatar size="sm" /><div><strong>Ваша Арти</strong><span>{bootstrap.mode === 'demo' ? 'Локальный просмотр' : 'Личное пространство'}</span></div></div>
    </aside>
    <div className="app-main">
      {bootstrap.mode === 'demo' && <div className="preview-banner"><span className="preview-dot" /><span>Локальный просмотр</span><span className="preview-description">Данные Telegram не подключены</span></div>}
      <main key={route.tab} id="main-content" tabIndex={-1} className={`page page-${route.tab}`} aria-label={current.name}>
        {route.tab === 'chats' && <Chats bootstrap={bootstrap} conversationOpen={route.conversation} onExpired={expired} />}
        {route.tab === 'create' && <Create />}
        {route.tab === 'space' && <Space bootstrap={bootstrap} onExpired={expired} />}
        {route.tab === 'profile' && <Profile bootstrap={bootstrap} theme={theme} changeTheme={changeTheme} signedOut={signedOut} onExpired={expired} />}
      </main>
      <nav className="tabbar" aria-label="Основная навигация"><NavRibbon active={route.tab} axis="x" />{NAV.map(({ id, name, icon: Icon }) => <a key={id} data-tab={id} className="tab" href={`#${id}`} aria-label={name} aria-current={route.tab === id ? 'page' : undefined}><Icon size={24} /><span>{name}</span></a>)}</nav>
    </div>
  </div>;
}

function EntryScreen({ state, retry, startDemo, busy }: { state: EntryState; retry: () => void; startDemo: () => void; busy: boolean }) {
  return <main className="entry-screen"><div className="entry-brand"><Wordmark /></div><section className="entry-panel">
    <div className="entry-art" role="img" aria-label="Арти"><Portrait crop="full" /></div>
    {state === 'loading' ? <Spinner label="Открываем ваше пространство…" /> : <div className="entry-copy">
      <span className="eyebrow">Разговоры · идеи · ваше пространство</span>
      <h1>{state === 'demo' ? 'Давайте осмотримся' : state === 'expired' ? 'Нужно войти снова' : state === 'error' ? 'Связь прервалась' : state === 'signedOut' ? 'Вы вышли из аккаунта' : 'Увидимся в Telegram'}</h1>
      <p>{state === 'demo' ? 'Это локальная версия интерфейса. Без доступа к вашему Telegram и без отправки сообщений.' : state === 'expired' ? 'Сессия завершилась. Попробуйте войти снова или заново откройте приложение из чата с Арти.' : state === 'error' ? 'Не удалось связаться с приложением. Проверьте соединение и попробуйте ещё раз.' : state === 'signedOut' ? 'Ваше пространство закрыто. Можно снова войти, когда захотите продолжить.' : 'Откройте приложение кнопкой в чате с Арти. Так мы узнаем вас и покажем ваше пространство.'}</p>
      <button className="button primary entry-action" disabled={busy} onClick={state === 'demo' ? startDemo : retry}>{busy ? <LoaderCircle className="spin" size={18} /> : state === 'demo' ? <ArrowRight size={18} /> : <RefreshCw size={18} />}{state === 'demo' ? 'Открыть локальный просмотр' : state === 'signedOut' || state === 'expired' ? 'Войти снова' : 'Повторить'}</button>
      {state === 'expired' && <p className="cookie-note">Если вход снова не сработает, откройте Mini App в мобильном Telegram. Браузер может блокировать cookies, необходимые для входа.</p>}
      <div className="entry-note"><LockKeyhole size={14} /><span>Вход и история принадлежат вашему аккаунту</span></div>
    </div>}
  </section><p className="entry-footer">Свой ритм. Свои разговоры.</p></main>;
}

function Chats({ bootstrap, conversationOpen, onExpired }: { bootstrap: Bootstrap; conversationOpen: boolean; onExpired: () => void }) {
  const list = useLoad<{ items: Conversation[] }>('/api/conversations', onExpired);
  const setConversations = list.setData;
  const preview = useCallback((message: Message | null) => {
    setConversations(previous => previous ? { items: previous.items.map(item => item.kind === 'arti_private' && !(item.last_message && message && item.last_message.seq > message.seq) ? { ...item, last_message: message } : item) } : previous);
  }, [setConversations]);
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const arti = list.data?.items.find(item => item.kind === 'arti_private');
  useConversationPreview(arti?.id, !conversationOpen && (bootstrap.capabilities.live_events ?? bootstrap.capabilities.history_sync), preview, onExpired);
  const visible = list.data?.items.filter(item => (filter === 'all' || filter === 'arti') && item.title.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')));
  const { life, setLife } = useArtiLife(!!bootstrap.capabilities.arti_life, onExpired);
  const [roomOpen, setRoomOpen] = useState(false);
  return <div className="chats-layout">
    <section className={`conversation-list${conversationOpen ? ' mobile-hidden' : ''}`} aria-label="Список чатов">
      <div className="page-heading"><span className="eyebrow">Всегда есть о чём</span><h1>Чаты</h1></div>
      <label className="search-field"><Search size={19} /><span className="sr-only">Найти чат</span><input type="search" placeholder="Найти разговор" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="chips chat-filters" role="group" aria-label="Типы чатов">{[{ id: 'all', name: 'Все' }, { id: 'arti', name: 'Арти' }, { id: 'people', name: 'Люди' }, { id: 'rooms', name: 'Комнаты' }].map(item => <button key={item.id} className="chip" aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>{item.name}</button>)}</div>
      {life && <LifeStrip life={life} open={() => setRoomOpen(true)} />}
      {life && roomOpen && <ArtiRoom life={life} csrf={bootstrap.csrf_token} close={() => setRoomOpen(false)} onExpired={onExpired}
        onSettings={settings => setLife(previous => previous ? { ...previous, settings } : previous)} />}
      {list.loading ? <Spinner label="Загружаем чаты…" /> : list.error ? <ErrorState retry={list.retry} compact /> : filter === 'people' || filter === 'rooms' ? <div className="list-empty"><span className="empty-icon"><Users size={26} /></span><h2>{filter === 'people' ? 'Ваши люди — здесь' : 'Место для своих'}</h2><p>{filter === 'people' ? 'Личные переписки появятся в этом разделе. Сейчас они ещё недоступны.' : 'Здесь будут общие разговоры с людьми и Арти. Создание комнат пока недоступно.'}</p><span className="status-pill">Скоро</span></div> : <>
        <div className="section-label"><span>Ваш разговор</span><button className="icon-button" aria-label="Обновить чаты" onClick={list.retry}><RefreshCw size={16} /></button></div>
        {visible?.length ? visible.map(item => <button className={`chat-row is-arti${conversationOpen ? ' selected' : ''}`} onClick={() => navigate('chats/arti')} key={item.id}><ArtiAvatar /><span className="row-main">
          <span className="row-top"><strong className="row-name">{item.title}</strong><span className="private-tag"><LockKeyhole size={11} />Личный</span>{item.last_message && <time className="row-time" dateTime={item.last_message.created_at}>{rowTime(item.last_message.created_at)}</time>}</span>
          <span className="row-bottom"><span className="row-preview">{item.last_message?.text || 'Основной разговор с Арти'}</span>{item.unread_count > 0 ? <span className="unread-count">{item.unread_count}</span> : <ChevronRight className="row-chevron" size={18} />}</span>
        </span></button>) : <div className="list-empty search-empty"><Search size={25} /><h2>{search ? 'Такого разговора нет' : 'Здесь пока пусто'}</h2><p>{search ? 'Попробуйте другое название.' : 'Обновите список, чтобы открыть основной разговор.'}</p></div>}
        <div className="history-note"><Info size={16} /><p>{bootstrap.capabilities.history_sync ? 'Здесь собрана история вашего разговора.' : 'История Telegram пока не подключена. Сообщения из бота появятся здесь позже.'}</p></div>
        <div className="social-note"><span className="social-icon"><Users size={21} /></span><div><strong>Разговоры с людьми</strong><p>Личные чаты и комнаты появятся здесь.</p></div></div>
      </>}
    </section>
    <section className={`conversation-pane${conversationOpen ? ' open' : ''}`} aria-label="Разговор с Арти">{conversationOpen ? list.loading ? <Spinner /> : list.error ? <ErrorState retry={list.retry} /> : arti ? <ConversationView key={arti.id} conversation={arti} bootstrap={bootstrap} onExpired={onExpired} onPreview={preview} /> : <div className="pane-welcome"><CircleAlert size={28} /><h2>Разговор не загрузился</h2><button className="button secondary" onClick={list.retry}>Повторить</button></div> : <div className="pane-welcome"><ArtiAvatar size="lg" /><span className="eyebrow">Один разговор. Ваш ритм.</span><h2>Мысль начинается<br />с пары слов</h2><p>Личный разговор с Арти — здесь.<br />Откройте его, чтобы посмотреть историю.</p><button className="button secondary" onClick={() => navigate('chats/arti')}>Открыть разговор<ArrowRight size={17} /></button><span className="welcome-privacy"><LockKeyhole size={13} />Только для вас</span></div>}</section>
  </div>;
}

function ConversationView({ conversation, bootstrap, onExpired, onPreview }: { conversation: Conversation; bootstrap: Bootstrap; onExpired: () => void; onPreview: (message: Message | null) => void }) {
  const chat = useConversation(conversation.id, bootstrap, onExpired, onPreview);
  const [draft, setDraft] = useState('');
  const [unseen, setUnseen] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const nearBottom = useRef(true);
  const ownSend = useRef(false);
  const previousLast = useRef<string | null>(null);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const canSend = bootstrap.capabilities.chat_send && !chat.revoked;
  const readyToSend = canSend && chat.historyRevision !== null && !chat.loading;
  const last = chat.messages[chat.messages.length - 1]?.id ?? null;
  const phase = chat.revoked ? null : replyPhase(chat.messages);
  useEffect(() => { setDraft(''); setUnseen(false); nearBottom.current = true; previousLast.current = null; }, [chat.resetToken]);
  useEffect(() => { setDraft(''); }, [chat.acceptedToken]);
  useLayoutEffect(() => {
    if (!textarea.current) return;
    textarea.current.style.height = '36px';
    textarea.current.style.height = `${Math.min(150, textarea.current.scrollHeight)}px`;
  }, [draft]);
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element) return;
    if (anchor.current && !chat.olderBusy) {
      element.scrollTop = anchor.current.top + element.scrollHeight - anchor.current.height;
      anchor.current = null;
    } else if (nearBottom.current || ownSend.current) {
      element.scrollTop = element.scrollHeight; ownSend.current = false; setUnseen(false);
    } else if (last !== previousLast.current && last) setUnseen(true);
    previousLast.current = last;
  }, [last, chat.messages, chat.attempt, chat.loading, chat.olderBusy, chat.sendError, chat.connection, draft, phase]);
  async function submit(retry = false) {
    if (!readyToSend || chat.sending || (!retry && (chat.attempt || !draft.trim()))) return;
    ownSend.current = true;
    if (await chat.send(retry ? undefined : draft)) { setDraft(''); textarea.current?.focus(); }
  }
  function loadOlder() {
    const element = scroll.current;
    if (element) anchor.current = { height: element.scrollHeight, top: element.scrollTop };
    void chat.older();
  }
  function jump() {
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    nearBottom.current = true; setUnseen(false);
  }
  const pendingVisible = chat.attempt && !chat.messages.some(message => message.client_message_id === chat.attempt?.client_message_id);
  const invalid = draft.trim() ? validateText(draft) : null;
  const connectionLabel = chat.connection === 'offline' ? 'Нет соединения · восстанавливаем' : chat.connection === 'polling' ? 'Обновляем через резервное соединение' : chat.connection === 'connecting' ? 'Подключаем обновления…' : null;
  let lastDay: string | null = null;
  const separator = (value: string) => {
    const key = dayKey(value);
    if (!key || key === lastDay) return null;
    lastDay = key;
    return <div className="date-sep" key={`day-${key}`}><span>{dayLabel(value)}</span></div>;
  };
  return <div className="chat-view"><header className="chat-head"><button className="icon-button back-button" onClick={() => navigate('chats')} aria-label="Назад к чатам"><ArrowLeft size={23} /></button><ArtiAvatar size="sm" /><div className="chat-id"><h1>Арти</h1><p className={`chat-status${phase === 'running' ? ' is-typing' : ''}`}>{headerStatus(phase, chat.revoked)}</p></div><span className="view-only"><LockKeyhole size={13} />{canSend ? 'Только для вас' : 'Только просмотр'}</span></header>
    {canSend && connectionLabel && <p className="connection-note" role="status">{connectionLabel}</p>}
    <div className="message-scroll" ref={scroll} onScroll={() => { const element = scroll.current; if (element) { nearBottom.current = element.scrollHeight - element.clientHeight - element.scrollTop < 90; if (nearBottom.current) setUnseen(false); } }}>
      {!chat.revoked && <div className="tg-sync"><svg className="sync-thread" viewBox="0 0 300 12" preserveAspectRatio="none" aria-hidden="true"><path d="M0 6c40-4 80 4 120 0s30 3 60 0 80-4 120 0" /></svg><span className="sync-label"><span className="tg-dot"><TelegramIcon size={12} /></span>{bootstrap.capabilities.history_sync ? 'Общая история с Telegram' : 'История Telegram пока не подключена'}</span></div>}
      {chat.revoked ? <div className="pane-welcome"><LockKeyhole size={29} /><h2>Разговор недоступен</h2><p>Доступ изменился. Вернитесь к списку чатов.</p><button className="button secondary" onClick={() => navigate('chats')}>К чатам</button></div> : chat.loading ? <Spinner label="Загружаем историю…" /> : chat.error ? <ErrorState retry={chat.refresh} /> : chat.messages.length || pendingVisible ? <div className="messages" role="log" aria-label="Сообщения разговора" aria-live="polite" aria-relevant="additions text">
        {chat.next !== null && <button className="button text-button earlier" onClick={loadOlder} disabled={chat.olderBusy}>{chat.olderBusy ? <LoaderCircle size={16} className="spin" /> : <ArrowDown size={16} />}{chat.olderError ? 'Повторить загрузку' : 'Более ранние сообщения'}</button>}
        {chat.messages.flatMap(message => [separator(message.created_at), <MessageBubble message={message} key={message.id} />])}
        {pendingVisible && chat.attempt && [separator(chat.attempt.created_at), <article className="msg me pending-message" key="pending"><div className="bubble"><span className="sr-only">Вы: </span><span className="bubble-text">{chat.attempt.text}</span><span className="meta"><time dateTime={chat.attempt.created_at}>{formatDate(chat.attempt.created_at, true)}</time></span></div><p className="request-status">{chat.sending ? 'Отправляем…' : 'Отправка не подтверждена'}</p></article>]}
        {phase && <TypingBubble phase={phase} />}
      </div> : <div className="conversation-empty"><span className="portrait-crop"><Portrait /></span><Emblem /><h2>{canSend ? <>О чём<br />поговорим?</> : <>Здесь будет<br />ваш разговор</>}</h2><p>И большие мысли, и пара слов между делом.<br />{canSend ? 'Напишите Арти — она рядом.' : 'Пока мы готовим отправку сообщений.'}</p><span className="privacy-line"><LockKeyhole size={13} />Личное остаётся в личном</span></div>}
    </div>
    {unseen && <button className="new-messages" onClick={jump}><ArrowDown size={16} />Новые сообщения</button>}
    <div className="composer-area">
      {chat.sendError && <div className="send-error" role="alert"><CircleAlert size={16} /><p>{chat.sendError}</p>{chat.retryable && <button className="retry-send" disabled={chat.sending} onClick={() => { void submit(true); }}><RefreshCw size={14} />Повторить</button>}</div>}
      <form className={`composer${canSend ? ' enabled' : ''}${draft.trim() ? ' has-text' : ''}`} aria-label={canSend ? 'Отправка сообщения Арти' : 'Отправка сообщений пока недоступна'} onSubmit={event => { event.preventDefault(); void submit(); }}><button type="button" disabled className="icon-button attach-button" aria-label="Вложения пока недоступны"><AttachIcon size={22} /></button><span className="composer-field"><textarea ref={textarea} disabled={!readyToSend || chat.sending || !!chat.attempt} aria-label={canSend ? 'Сообщение Арти' : 'Сообщение — отправка пока недоступна'} placeholder="Сообщение Арти" rows={1} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} aria-describedby={invalid ? 'composer-validation' : 'composer-note'} aria-invalid={invalid ? true : undefined} /></span><button type="submit" disabled={!readyToSend || chat.sending || !!chat.attempt || !draft.trim() || !!invalid} className="send-button" aria-label={canSend ? 'Отправить сообщение' : 'Отправка пока недоступна'}>{chat.sending ? <LoaderCircle size={20} className="spin" /> : <SendIcon size={21} />}</button></form>
      {invalid ? <p className="composer-validation" id="composer-validation" role="status">{invalid}</p> : <p className="composer-note" id="composer-note">{canSend ? !readyToSend ? 'Подключаем историю…' : chat.attempt && !chat.sending ? 'Повтор сохраняет ту же отправку' : 'Enter — отправить · Shift + Enter — новая строка' : 'Отправка пока недоступна'}</p>}
    </div>
  </div>;
}

function MessageBubble({ message }: { message: Message }) {
  // Queued and running replies are shown once, as her typing bubble at the end of the thread.
  const status = message.request_state === 'queued' || message.request_state === 'running' ? null : requestLabel(message.request_state);
  const failed = ['failed', 'cancelled', 'expired', 'deadline'].includes(message.request_state ?? '');
  if (message.author_kind === 'system') return <article className="sys-note"><span className="sr-only">Уведомление: </span><p>{message.text}</p><time dateTime={message.created_at}>{formatDate(message.created_at, true)}</time></article>;
  const mine = message.author_kind === 'user';
  const initiative = !mine && message.initiative === true;
  const reactions = message.reactions?.filter(reaction => reaction.by === 'user') ?? [];
  const stamp = message.reactions?.find(reaction => reaction.by === 'arti');
  return <article className={`msg ${mine ? 'me' : 'arti'}${initiative ? ' initiative' : ''}`}>
    <div className="bubble">
      <span className="sr-only">{mine ? 'Вы' : initiative ? 'Арти, написала первой' : 'Арти'}: </span>
      {initiative && <span className="init-tag" aria-hidden="true"><StarIcon size={11} />Написала первой</span>}
      <span className="bubble-text">{message.text}</span>
      <span className="meta">{mine && message.source === 'telegram' && <span className="origin"><TelegramIcon size={11} />из Telegram</span>}<time dateTime={message.created_at}>{formatDate(message.created_at, true)}</time>{mine && <TickIcon size={13} className="tick" />}</span>
      {reactions.length > 0 && <span className="reacts">{reactions.map((reaction, index) => <span className="react-chip" key={`${reaction.emoji}-${index}`}><span aria-hidden="true">{reaction.emoji}</span><span className="who" aria-hidden="true">вы</span><span className="sr-only">Ваша реакция {reaction.emoji}</span></span>)}</span>}
      {stamp && <span className="stamp" role="img" aria-label={`Арти отметила: ${stamp.emoji}`}>{stamp.emoji === '✦' ? <StarIcon size={16} /> : <span aria-hidden="true">{stamp.emoji}</span>}</span>}
    </div>
    {status && <p className={`request-status${failed ? ' failed' : ''}`} role="status">{status}</p>}
    {message.mirror_state === 'delivery_unknown' && <p className="mirror-note">Доставка копии в Telegram уточняется</p>}
  </article>;
}

function TypingBubble({ phase }: { phase: ReplyPhase }) {
  return <article className="msg arti typing"><div className="bubble"><span className="sr-only">Арти: </span><span className="breath" aria-hidden="true"><StarIcon size={16} /></span><span className="typing-text" key={phase}>{TYPING_TEXT[phase]}</span></div></article>;
}

const TOOLS: { id: string; title: string; subtitle: string; description: string; icon: LucideIcon; tone: string }[] = [
  { id: 'image', title: 'Изображение', subtitle: 'Найти форму для идеи', description: 'Здесь можно будет описать изображение, выбрать формат и получить результат прямо в приложении.', icon: ImageIcon, tone: 'rose' },
  { id: 'video', title: 'Видео', subtitle: 'Привести историю в движение', description: 'Короткие видео по описанию или изображению. Задание, состояние выполнения и результат появятся в этом разделе.', icon: Video, tone: 'ochre' },
  { id: 'music', title: 'Музыка', subtitle: 'Поймать своё звучание', description: 'Музыка из настроения, текста или идеи. Прослушивание и готовые записи будут доступны здесь.', icon: Music2, tone: 'graphite' },
  { id: 'document', title: 'Документ', subtitle: 'Собрать мысли в целое', description: 'Документы, тексты и работа с материалами вместе с Арти. Создание ещё не подключено.', icon: FileText, tone: 'neutral' },
  { id: 'table', title: 'Таблица', subtitle: 'Навести ясность в деталях', description: 'Таблицы и структурированные данные по вашему запросу. Готовые файлы можно будет сохранить в пространстве.', icon: Table2, tone: 'neutral' },
  { id: 'analysis', title: 'Разобрать материал', subtitle: 'Заметить то, что важно', description: 'Здесь можно будет передать файл Арти для анализа, обсуждения или продолжения работы.', icon: Layers3, tone: 'neutral' },
];
function Create() {
  const [selected, setSelected] = useState<(typeof TOOLS)[number] | null>(null);
  return <div className="standard-page create-page"><div className="page-heading"><span className="eyebrow">У каждой идеи свой путь</span><h1>Создать</h1></div>
    <div className="creative-hero"><span className="hero-kicker"><StarIcon size={11} />Место для нового</span><h2>А что, если<br />попробовать?</h2><p>Картинка, музыка, целая история.<br />Выберите, с чего начнётся ваша идея.</p><span className="hero-mark" aria-hidden="true"><StarIcon size={150} /></span></div>
    <div className="section-heading"><h2>Инструменты</h2><span>Готовим к запуску</span></div><div className="tool-list">{TOOLS.map(({ id, title, subtitle, icon: Icon, tone }, index) => <button className={`tool-row tool-${tone}`} key={id} onClick={() => setSelected(TOOLS[index])}><span className="tool-icon"><Icon size={23} /></span><span className="tool-copy"><strong>{title}</strong><span>{subtitle}</span></span><ChevronRight size={18} /></button>)}</div>
    <div className="quiet-note"><Info size={16} /><p>Создание пока недоступно. Здесь будут ваши задания и готовые работы.</p></div>
    {selected && <Dialog title={selected.title} close={() => setSelected(null)}><span className={`dialog-tool tool-${selected.tone}`}><selected.icon size={30} /></span><p className="dialog-description">{selected.description}</p><div className="availability"><Info size={17} /><span>Создание пока недоступно</span></div><button className="button secondary full-width" onClick={() => setSelected(null)}>Понятно</button></Dialog>}
  </div>;
}

function Space({ bootstrap, onExpired }: { bootstrap: Bootstrap; onExpired: () => void }) {
  const [section, setSection] = useState<'projects' | 'library' | 'organizer'>('projects');
  const supported = bootstrap.capabilities[section];
  const data = useLoad<ResourcePage>(supported ? `/api/${section}` : null, onExpired);
  const icon = section === 'projects' ? Folder : section === 'library' ? FileText : Check;
  const Icon = icon;
  const unavailable = !supported || data.data?.supported === false;
  return <div className="standard-page space-page"><div className="page-heading"><span className="eyebrow">Важное не теряется</span><h1>Пространство</h1></div>
    <p className="page-intro">Место для того, к чему хочется вернуться.</p>
    <div className="tabs-line space-tabs" role="group" aria-label="Содержимое пространства">{[{ id: 'projects', name: 'Проекты' }, { id: 'library', name: 'Файлы' }, { id: 'organizer', name: 'Дела' }].map(item => <button key={item.id} aria-pressed={section === item.id} onClick={() => setSection(item.id as typeof section)}>{item.name}</button>)}</div>
    {data.loading && supported ? <Spinner /> : data.error ? <ErrorState retry={data.retry} /> : unavailable ? <div className="space-empty"><span className="empty-icon"><Icon size={30} /></span><span className="eyebrow">Всё в своём месте</span><h2>{section === 'projects' ? 'Здесь вырастут ваши идеи' : 'Ваши материалы — рядом'}</h2><p>{section === 'projects' ? 'Проекты пока не подключены. Здесь будут разговоры, материалы и следующие шаги по каждой идее.' : 'Библиотека пока не подключена. Здесь появятся доступные вашему аккаунту файлы.'}</p><span className="status-pill">Пока недоступно</span></div> : data.data?.items.length ? <div className="resource-list">{data.data.items.map(item => <article className="resource-row" key={item.id}><span className="resource-icon"><Icon size={21} /></span><div><h2>{item.title || item.filename || 'Без названия'}</h2>{item.goal && <p>{item.goal}</p>}<span>{section === 'organizer' ? `${item.state === 'done' ? 'Завершено' : item.kind === 'reminder' ? 'Напоминание' : item.kind === 'event' ? 'Событие' : 'Задача'}${item.due_at ? ` · ${formatDate(item.due_at)}` : ''}` : section === 'library' ? item.filename || 'Файл' : 'Проект'}</span></div></article>)}<p className="read-only-note">Сейчас доступен только просмотр.</p></div> : <div className="space-empty"><span className="empty-icon"><Icon size={30} /></span><span className="eyebrow">Всё начинается с малого</span><h2>{section === 'projects' ? 'Пока чистый лист' : section === 'library' ? 'Здесь будут ваши файлы' : 'Можно выдохнуть'}</h2><p>{section === 'projects' ? 'Доступных проектов пока нет. Здесь появятся идеи, которые вы захотите развивать с Арти.' : section === 'library' ? 'Доступных файлов пока нет. Материалы из вашего пространства будут собраны здесь.' : 'Активных дел пока нет. Задачи и напоминания из вашего личного пространства появятся здесь.'}</p><span className="status-pill">Создание пока недоступно</span></div>}
    <div className="quiet-note"><LockKeyhole size={16} /><p>Показываем только материалы вашего аккаунта. Доступ к данным проверяется при каждом запросе.</p></div>
  </div>;
}

const THEMES: { id: Theme; title: string; note: string }[] = [
  { id: 'light', title: 'День', note: 'Бумага и чернила.' },
  { id: 'dark', title: 'Вечер', note: 'Включена лампа, глаза Арти светятся.' },
  { id: 'system', title: 'Как в Telegram', note: 'Следует теме Telegram или устройства.' },
];
function Profile({ bootstrap, theme, changeTheme, signedOut, onExpired }: { bootstrap: Bootstrap; theme: Theme; changeTheme: (theme: Theme) => void; signedOut: () => void; onExpired: () => void }) {
  const [logoutOpen, setLogoutOpen] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutError, setLogoutError] = useState(false);
  async function logout() {
    setLogoutBusy(true); setLogoutError(false);
    try { await request('/api/auth/logout', { method: 'POST', body: {}, csrf: bootstrap.csrf_token }); signedOut(); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 401) onExpired(); else setLogoutError(true); }
    finally { setLogoutBusy(false); }
  }
  const selectedTheme = Math.max(0, THEMES.findIndex(item => item.id === theme));
  return <div className="standard-page profile-page"><div className="page-heading"><span className="eyebrow">Как вам удобно</span><h1>Профиль</h1></div>
    <section className="profile-person"><span className="avatar user-avatar">{bootstrap.user.first_name.slice(0, 1).toLocaleUpperCase('ru')}</span><div><h2>{bootstrap.user.first_name}</h2><p>{bootstrap.user.username ? `@${bootstrap.user.username}` : bootstrap.mode === 'demo' ? 'Локальный просмотр' : 'Аккаунт Telegram'}</p></div><span className="verified-mark"><ShieldCheck size={21} /><span className="sr-only">{bootstrap.mode === 'demo' ? 'Локальная сессия' : 'Вход через Telegram'}</span></span></section>
    <section className="settings-section"><div className="section-heading"><h2>Оформление</h2><span>На этом устройстве</span></div><div className="segmented theme-options" role="group" aria-label="Тема приложения" style={{ '--n': THEMES.length, '--i': selectedTheme } as CSSProperties}>{THEMES.map(({ id, title }) => <button key={id} aria-pressed={theme === id} onClick={() => changeTheme(id)}>{title}</button>)}<i className="seg-thumb" aria-hidden="true" /></div><p className="set-explain">{THEMES[selectedTheme].note}</p></section>
    {bootstrap.capabilities.arti_life && <LifeSetting csrf={bootstrap.csrf_token} onExpired={onExpired} />}
    <section className="memory-preview"><span className="nb-cover" aria-hidden="true"><NotebookIcon size={24} /></span><div><h2>То, что Арти помнит</h2><p>Личная память с источниками.<br />Чтобы важное оставалось точным.</p><div className="memory-unavailable"><Info size={16} /><span>Просмотр и управление пока недоступны</span></div></div></section>
    <section className="settings-section"><div className="section-heading"><h2>Личное пространство</h2></div><div className="set-card"><div className="setting-info"><LockKeyhole size={19} /><div><h3>Ваш разговор — ваш</h3><p>Личный чат доступен вашему аккаунту. Общие комнаты не получают доступ к личной переписке.</p></div></div><div className="setting-info"><Monitor size={19} /><div><h3>Часовой пояс</h3><p>{bootstrap.timezone || 'UTC'}</p></div></div><div className="setting-info"><Info size={19} /><div><h3>Уведомления</h3><p>Настройки уведомлений пока недоступны.</p></div></div></div></section>
    <button className="logout-button" onClick={() => { setLogoutError(false); setLogoutOpen(true); }}><LogOut size={19} /><span>Выйти из аккаунта</span><ChevronRight size={16} /></button>
    <p className="profile-footer"><Wordmark /> Свой ритм. Свои разговоры.</p>
    {logoutOpen && <Dialog title="Выйти из аккаунта?" close={() => { if (!logoutBusy) setLogoutOpen(false); }}><p className="dialog-description">Приложение закроет доступ к вашему пространству на этом устройстве. История разговора сохранится.</p>{logoutError && <p className="inline-error" role="alert">Не удалось выйти. Проверьте соединение и попробуйте снова.</p>}<button className="button primary full-width" disabled={logoutBusy} onClick={logout}>{logoutBusy && <LoaderCircle className="spin" size={17} />}Выйти</button><button className="button secondary full-width" disabled={logoutBusy} onClick={() => setLogoutOpen(false)}>Остаться</button></Dialog>}
  </div>;
}

function formatDate(value: string, timeOnly = false): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  return date.toLocaleString('ru-RU', timeOnly ? { hour: '2-digit', minute: '2-digit' } : { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
