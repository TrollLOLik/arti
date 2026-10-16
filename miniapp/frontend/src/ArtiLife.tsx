import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent, PointerEvent } from 'react';
import { ArrowRight, X } from 'lucide-react';
import { ApiError, isAborted, request } from './api';
import { FRAME_LABEL, MOOD_BASIS, VERDICT, capitalize, duration, hoopLayout, ofStitches, pursuitState, safeUrl, shortDate, stripEntries } from './life';
import { Portrait } from './Portrait';
import type { ArtiLife, EmbroideryPattern, LifeEvent, LifeSettings, Pursuit, ReadingNote, Taste } from './types';

/* «Сейчас у Арти» and «Комната Арти» (H6, P2). Labels keep the frames apart:
   «В образе» — her declared in-character life; «По-настоящему» — real reading with a link;
   «Настроение · по вашему разговору» — her state in this person's own conversation. */

export function useArtiLife(enabled: boolean, onExpired: () => void) {
  const [life, setLife] = useState<ArtiLife | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    request<ArtiLife>('/api/arti/life', { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setLife(result); }).catch((cause: unknown) => {
      // Her life is a quiet extra: without it the chats work as before.
      if (!controller.signal.aborted && !isAborted(cause) && cause instanceof ApiError && cause.status === 401) onExpired();
    });
    return () => controller.abort();
  }, [enabled, onExpired]);
  return { life, setLife };
}

export async function saveLifeSettings(csrf: string, change: Partial<LifeSettings>): Promise<LifeSettings> {
  return (await request<{ settings: LifeSettings }>('/api/arti/life/settings', { method: 'PUT', body: change, csrf })).settings;
}

/** Inside Telegram a link opens in the external browser; elsewhere it is an ordinary new-tab link. */
function openExternal(url: string) {
  return (event: MouseEvent) => {
    const app = window.Telegram?.WebApp;
    if (app?.initData && app.openLink) { event.preventDefault(); app.openLink(url); }
  };
}

const ROTATE_MS = 7000;
export function LifeStrip({ life, open }: { life: ArtiLife; open: () => void }) {
  const entries = useMemo(() => stripEntries(life), [life]);
  const [index, setIndex] = useState(0);
  const [turning, setTurning] = useState(false);
  const held = useRef(false);
  useEffect(() => {
    if (entries.length < 2 || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timers: number[] = [];
    const interval = window.setInterval(() => {
      if (held.current || document.hidden) return;
      // She blinks; the line changes while her eyes are closed.
      setTurning(true);
      timers.push(window.setTimeout(() => setIndex(value => value + 1), 420), window.setTimeout(() => setTurning(false), 760));
    }, ROTATE_MS);
    return () => { window.clearInterval(interval); timers.forEach(window.clearTimeout); };
  }, [entries.length]);
  const entry = entries.length ? entries[index % entries.length] : null;
  if (!entry && !life.mood) return null;
  const hold = (value: boolean) => () => { held.current = value; };
  return <button type="button" className={`presence${turning ? ' blink' : ''}`} onClick={open} aria-haspopup="dialog" onMouseEnter={hold(true)} onMouseLeave={hold(false)} onFocus={hold(true)} onBlur={hold(false)}>
    <span className="presence-eyes" aria-hidden="true"><Portrait crop="eyes" /><span className="lid lid-t" /><span className="lid lid-b" /></span>
    <span className="presence-tag"><i className="live" aria-hidden="true" />Сейчас у Арти</span>
    <span className="presence-body">
      {entry && <><span className={`presence-line${entry.hand ? ' hand' : ''}${turning ? ' swap' : ''}`}>{entry.text}</span><span className="frame-tag" data-frame={entry.frame}>{FRAME_LABEL[entry.frame]}</span></>}
      <span className="presence-foot">
        {life.mood ? <span className="mood"><i className="orb" aria-hidden="true" /><span className="mood-copy"><span className="mood-value">{life.mood.label}</span><span className="mood-k">{MOOD_BASIS}</span></span></span> : <span />}
        <span className="presence-go">Комната<ArrowRight size={15} aria-hidden="true" /></span>
      </span>
    </span>
  </button>;
}

export function ArtiRoom({ life, csrf, close, onSettings, onExpired }: { life: ArtiLife; csrf: string; close: () => void; onSettings: (settings: LifeSettings) => void; onExpired: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const how = useRef<HTMLButtonElement>(null);
  const ids = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  // The rules are explained once, on the first visit; then «Как это устроено» opens them.
  const [explainer, setExplainer] = useState(!life.settings.explainer_seen);
  const [drag, setDrag] = useState<{ start: number; offset: number } | null>(null);
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  async function understood() {
    setExplainer(false);
    how.current?.focus();
    if (life.settings.explainer_seen) return;
    try { onSettings(await saveLifeSettings(csrf, { explainer_seen: true })); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 401) onExpired(); }
  }
  const grab = {
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => { event.currentTarget.setPointerCapture?.(event.pointerId); setDrag({ start: event.clientY, offset: 0 }); },
    onPointerMove: (event: PointerEvent<HTMLDivElement>) => { if (drag) setDrag({ ...drag, offset: Math.max(0, event.clientY - drag.start) }); },
    onPointerUp: () => { if (drag && drag.offset > 110) close(); setDrag(null); },
    onPointerCancel: () => setDrag(null),
  };
  const kind = (name: Pursuit['kind']) => (life.in_character ? life.pursuits.find(pursuit => pursuit.kind === name) : undefined);
  const restoration = kind('restoration'), embroidery = kind('embroidery'), tea = kind('tea');
  return <dialog ref={ref} className={`room${drag ? ' is-dragging' : ''}`} aria-labelledby={`${ids}-title`} style={drag ? { '--drag': `${drag.offset}px` } as CSSProperties : undefined}
    onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="room-inner">
      <div className="sheet-grab" aria-hidden="true" {...grab}><span /></div>
      <header className="sheet-head">
        <div>
          <p className="sheet-kicker">{life.settings.mentions ? 'Жизнь в образе · ведётся последовательно' : 'Сейчас у Арти'}</p>
          <h2 id={`${ids}-title`} className="room-title">Комната Арти</h2>
          <button ref={how} type="button" className="how-btn" aria-expanded={explainer} aria-controls={`${ids}-how`} onClick={() => setExplainer(value => !value)}>Как это устроено</button>
        </div>
        <button type="button" className="icon-button" aria-label="Закрыть комнату" onClick={close}><X size={22} /></button>
      </header>
      <div className="sheet-body">
        <section className="life-explainer" id={`${ids}-how`} aria-labelledby={`${ids}-how-title`} hidden={!explainer}>
          <h3 id={`${ids}-how-title`}>Как живёт Арти</h3>
          <p>Арти — ИИ. Чай, вышивка и реставрация записи — жизнь её персонажа. Физически их нет, но ведутся они последовательно: у каждого занятия есть история, и Арти не придумывает сверх неё.</p>
          <p>Настроение — настоящее состояние её модели в разговоре с вами.</p>
          <p>Прочитанное — реальные материалы со ссылками.</p>
          <button type="button" className="button secondary small" onClick={() => { void understood(); }}>Понятно</button>
        </section>
        {!life.settings.mentions && <p className="life-off-note">Жизнь в образе выключена в профиле — здесь только прочитанное и настроение.</p>}
        <div className="room-portrait"><Portrait crop="room" />{life.mood && <p className="room-mood"><i className="orb" aria-hidden="true" /><span><span className="room-mood-k">{MOOD_BASIS}</span><span className="room-mood-v">{life.mood.label}</span></span></p>}</div>
        {restoration?.restoration && <RestorationCard pursuit={restoration} />}
        {(embroidery?.embroidery || tea) && <div className="pursuit-pair">
          {embroidery?.embroidery && <EmbroideryCard pursuit={embroidery} pattern={life.embroidery} />}
          {tea && <TasteCard pursuit={tea} tastes={life.tastes} />}
        </div>}
        <ReadingCard note={life.reading} />
      </div>
    </div>
  </dialog>;
}

function Kicker({ real = false, children }: { real?: boolean; children?: string }) {
  return <p className="p-kicker"><span className={`frame-word${real ? ' frame-real' : ''}`}>{real ? 'По-настоящему' : 'В образе'}</span>{children ? ` · ${children}` : ''}</p>;
}

function LastEvent({ event }: { event: LifeEvent }) {
  return <div className="p-event"><p className="hand p-note">{event.summary}</p><p className="p-feeling"><span className="hand">— {event.feeling}</span><time dateTime={event.at}>{shortDate(event.at)}</time></p></div>;
}

function RestorationCard({ pursuit }: { pursuit: Pursuit }) {
  const { record, stages } = pursuit.restoration!;
  return <article className="pursuit p-rec">
    <Kicker>{pursuitState(pursuit)}</Kicker>
    <h3 className="p-title">{pursuit.title}</h3>
    <p className="p-meta">{record.medium} · {record.content} · {duration(record.seconds)}</p>
    <ol className="stages" aria-label="Этапы реставрации">
      {stages.map(stage => <li key={stage.name} className={`stage is-${stage.state}`} aria-current={stage.state === 'current' ? 'step' : undefined}>
        <span className="stage-mark" aria-hidden="true" />
        <span className="stage-name">{stage.name}<span className="sr-only">{stage.state === 'done' ? ' — пройден' : stage.state === 'current' ? ' — текущий этап' : ''}</span></span>
        {stage.state === 'current' && pursuit.status === 'active' && <span className="stage-goal">{stage.goal}</span>}
      </li>)}
    </ol>
    {pursuit.last_event && <LastEvent event={pursuit.last_event} />}
  </article>;
}

function EmbroideryCard({ pursuit, pattern }: { pursuit: Pursuit; pattern: EmbroideryPattern | null }) {
  const work = pursuit.embroidery!;
  return <article className="pursuit p-emb">
    {pattern && <Hoop pattern={pattern} />}
    <Kicker>{pursuitState(pursuit)}</Kicker>
    <h3 className="p-title">Вышивка</h3>
    <p className="p-meta">«{work.motif}» · {work.stitched} из {work.total} {ofStitches(work.total)}</p>
    {work.replaced && <p className="p-aside">Прежний узор «{work.replaced.motif}» отложен на {work.replaced.stitched} из {work.replaced.total}{work.replaced.reason ? `: ${work.replaced.reason}` : ''}.</p>}
    {pursuit.intention && <p className="p-next">Дальше: {pursuit.intention}</p>}
    {pursuit.last_event && <LastEvent event={pursuit.last_event} />}
  </article>;
}

function TasteCard({ pursuit, tastes }: { pursuit: Pursuit; tastes: Taste[] }) {
  return <article className="pursuit p-taste">
    <Kicker>её выбор</Kicker>
    <h3 className="p-title">Вкусы в чае</h3>
    {pursuit.tea && <p className="p-meta">распробовано {pursuit.tea.tasted} из {pursuit.tea.known}</p>}
    {tastes.length ? <ul className="tastes">{tastes.map(taste => {
      const url = safeUrl(taste.source);
      return <li key={taste.item}><details>
        <summary><span className="taste-item">{capitalize(taste.item)}</span><span className={`hand mark ${VERDICT[taste.verdict].tone}`}>{VERDICT[taste.verdict].text}</span></summary>
        <div className="taste-why">
          <p className="hand">{taste.reason}</p>
          {taste.previous && <p className="taste-was">было: {VERDICT[taste.previous].text}</p>}
          {url && <p className="taste-basis"><span className="frame-word frame-real">По-настоящему</span> читала: <a href={url} target="_blank" rel="noopener noreferrer" onClick={openExternal(url)}>«{taste.basis}» ↗</a></p>}
        </div>
      </details></li>;
    })}</ul> : <p className="p-muted">Вердиктов пока нет.</p>}
  </article>;
}

function ReadingCard({ note }: { note: ReadingNote | null }) {
  const url = note ? safeUrl(note.url) : null;
  return <article className="pursuit p-read">
    <Kicker real>ссылка ↗</Kicker>
    <h3 className="p-title">Читала</h3>
    {note ? <>
      {url ? <a className="read-link" href={url} target="_blank" rel="noopener noreferrer" onClick={openExternal(url)}><span className="read-title">«{note.title}»</span><span className="read-meta">{new URL(url).hostname} · {shortDate(note.at)}</span></a> : <p className="read-link">«{note.title}»</p>}
      <p className="hand p-note">{note.opinion}</p>
    </> : <p className="p-muted">Свежих заметок о прочитанном пока нет.</p>}
  </article>;
}

/** Her hoop, drawn from the server's chart: crosses for the stitched marks, the rest still printed in pencil. */
export function Hoop({ pattern }: { pattern: EmbroideryPattern }) {
  const id = `hoop-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const layout = useMemo(() => hoopLayout(pattern), [pattern]);
  const stroke = (layout.cell * .3).toFixed(2);
  return <svg className="hoop" viewBox="-8 -18 216 226" role="img" aria-label={`Вышивка «${pattern.motif}»: ${layout.stitched} из ${layout.total} ${ofStitches(layout.total)}`}>
    <defs>
      <clipPath id={`${id}-clip`}><circle cx="100" cy="100" r="87" /></clipPath>
      <radialGradient id={`${id}-shade`} cx="50%" cy="45%" r="55%"><stop offset=".7" stopColor="#000" stopOpacity="0" /><stop offset="1" stopColor="#000" stopOpacity=".16" /></radialGradient>
      <linearGradient id={`${id}-wood`} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#e2c9a2" /><stop offset=".45" stopColor="#c19d70" /><stop offset=".7" stopColor="#d9bc92" /><stop offset="1" stopColor="#9c7650" /></linearGradient>
      <pattern id={`${id}-weave`} width="2.4" height="2.4" patternUnits="userSpaceOnUse"><path className="hoop-weave" d="M0 .6h2.4M.6 0v2.4" /></pattern>
    </defs>
    <circle className="hoop-back" cx="100" cy="100" r="88" />
    <g clipPath={`url(#${id}-clip)`}>
      <circle className="hoop-fabric" cx="100" cy="100" r="90" />
      <rect x="0" y="0" width="200" height="200" fill={`url(#${id}-weave)`} />
      <path className="hoop-chart" d={layout.chart} strokeWidth={(layout.cell * .34).toFixed(2)} />
      {layout.threads.map(thread => <g key={thread.thread}>
        <path className="st-shadow" d={thread.d} strokeWidth={stroke} />
        <path className="st-thread" d={thread.d} stroke={thread.color} strokeWidth={stroke} />
      </g>)}
      <circle cx="100" cy="100" r="88" fill={`url(#${id}-shade)`} />
    </g>
    <circle className="ring-in" cx="100" cy="100" r="88.5" />
    <circle className="ring-out" cx="100" cy="100" r="94" stroke={`url(#${id}-wood)`} />
    <circle className="ring-hi" cx="100" cy="100" r="97.6" />
    <g className="clamp"><rect x="89" y="-9" width="22" height="9" rx="2" /><rect x="94" y="-16" width="12" height="8" rx="2" /><path d="M96.5 -12h7" /></g>
  </svg>;
}

const LIFE_HINTS = {
  on: 'Чай, вышивка и запись — жизнь её персонажа. Если выключить, Арти не будет рассказывать вам о них ни здесь, ни в Telegram, а в Комнате останутся чтение и настроение.',
  off: 'Арти не рассказывает вам о жизни в образе ни здесь, ни в Telegram. В Комнате — только прочитанное по-настоящему и настроение.',
};
export function LifeSetting({ csrf, onExpired }: { csrf: string; onExpired: () => void }) {
  const [settings, setSettings] = useState<LifeSettings | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    request<{ settings: LifeSettings }>('/api/arti/life/settings', { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setSettings(result.settings); }).catch((cause: unknown) => {
      if (controller.signal.aborted || isAborted(cause)) return;
      if (cause instanceof ApiError && cause.status === 401) onExpired(); else setFailed(true);
    });
    return () => controller.abort();
  }, [onExpired]);
  async function choose(mentions: boolean) {
    if (!settings || busy || settings.mentions === mentions) return;
    const previous = settings;
    setSettings({ ...settings, mentions }); setBusy(true); setSaveError(false);
    try { setSettings(await saveLifeSettings(csrf, { mentions })); }
    catch (cause) { setSettings(previous); if (cause instanceof ApiError && cause.status === 401) onExpired(); else setSaveError(true); }
    finally { setBusy(false); }
  }
  return <section className="settings-section life-setting">
    <div className="section-heading"><h2>Жизнь Арти в образе</h2><span>Для вашего аккаунта</span></div>
    {settings ? <>
      <div className="segmented" role="group" aria-label="Жизнь Арти в образе" aria-busy={busy || undefined} style={{ '--n': 2, '--i': settings.mentions ? 0 : 1 } as CSSProperties}>
        <button type="button" aria-pressed={settings.mentions} onClick={() => { void choose(true); }}>Показывать</button>
        <button type="button" aria-pressed={!settings.mentions} onClick={() => { void choose(false); }}>Не упоминать</button>
        <i className="seg-thumb" aria-hidden="true" />
      </div>
      <p className="set-explain">{LIFE_HINTS[settings.mentions ? 'on' : 'off']}</p>
      {saveError && <p className="inline-error settings-error" role="alert">Не удалось сохранить. Проверьте соединение и попробуйте снова.</p>}
    </> : <p className="set-explain">{failed ? 'Настройка сейчас недоступна. Попробуйте открыть профиль позже.' : 'Загружаем настройку…'}</p>}
  </section>;
}
