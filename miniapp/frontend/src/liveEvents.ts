import { ApiError, isAborted, request } from './api.ts';
import { parseEventPage } from './chat.ts';
import type { EventPage } from './types';

export type ConnectionState = 'connecting' | 'live' | 'polling' | 'offline' | 'paused';
interface Stream {
  addEventListener(type: string, listener: EventListener): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
}
interface Environment {
  stream: ((url: string) => Stream) | null;
  fetch: (url: string, signal: AbortSignal) => Promise<EventPage>;
  schedule: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  cancel: (timer: ReturnType<typeof setTimeout>) => void;
}
interface Callbacks {
  state: (page: EventPage) => void;
  expired: () => void;
  revoked: () => void;
  resync: () => void;
  connection: (value: ConnectionState) => void;
}
/** One active transport. Every failed SSE attempt falls back to bounded polling. */
export function conversationFeed(conversationId: string, callbacks: Callbacks, supplied?: Partial<Environment>) {
  const env: Environment = {
    stream: typeof EventSource === 'undefined' ? null : url => new EventSource(url, { withCredentials: true }),
    fetch: (url, signal) => request<EventPage>(url, { signal }),
    schedule: (callback, delay) => setTimeout(callback, delay), cancel: timer => clearTimeout(timer), ...supplied,
  };
  const base = `/api/conversations/${encodeURIComponent(conversationId)}/events`;
  let cursor = 0;
  let stopped = false;
  let paused = true;
  let source: Stream | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let watchdog: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;
  let failures = 0;
  let polls = 0;
  function clear() {
    source?.close(); source = null;
    if (timer !== null) env.cancel(timer);
    if (watchdog !== null) env.cancel(watchdog);
    timer = null; watchdog = null;
    controller?.abort(); controller = null;
  }
  function terminal(kind: 'expired' | 'revoked') {
    stopped = true; clear(); callbacks[kind]();
  }
  function accept(raw: unknown) {
    const page = parseEventPage(raw);
    if (!page) throw new Error('invalid_event_page');
    if (page.cursor < cursor) return;
    const previous = cursor;
    cursor = page.cursor;
    callbacks.state({ ...page, items: page.items.filter(item => item.seq > previous) });
  }
  function fallback() {
    if (stopped || paused) return;
    clear(); failures++; polls = 0; callbacks.connection('polling');
    timer = env.schedule(() => { void poll(); }, 0);
  }
  async function poll() {
    if (stopped || paused) return;
    timer = null;
    const active = new AbortController(); controller = active;
    let delay = 3500;
    try {
      const page = await env.fetch(`${base}?after=${cursor}&limit=100`, active.signal);
      if (active.signal.aborted || stopped || paused) return;
      accept(page); callbacks.connection('polling'); failures = Math.min(failures, 1);
      if (page.items.length >= 100) delay = 0;
      polls++;
    } catch (cause) {
      if (active.signal.aborted || isAborted(cause) || stopped || paused) return;
      if (cause instanceof ApiError && cause.status === 401) return terminal('expired');
      if (cause instanceof ApiError && (cause.status === 403 || cause.status === 404)) return terminal('revoked');
      if (cause instanceof ApiError && cause.status === 409) { cursor = 0; callbacks.resync(); }
      callbacks.connection('offline'); failures++;
      delay = Math.min(15000, 3500 * 2 ** Math.min(failures - 1, 2));
    } finally { if (controller === active) controller = null; }
    if (stopped || paused) return;
    timer = env.schedule(() => { if (polls >= 8 && env.stream) open(); else void poll(); }, delay);
  }
  function open() {
    if (stopped || paused) return;
    clear(); callbacks.connection('connecting');
    if (!env.stream) return fallback();
    try {
      const current = env.stream(`${base}/stream?after=${cursor}`); source = current;
      const alive = () => source === current && !stopped && !paused;
      const state: EventListener = event => {
        if (!alive()) return;
        if (watchdog !== null) env.cancel(watchdog);
        watchdog = null;
        try {
          accept(JSON.parse((event as MessageEvent).data));
          if (!alive()) return;
          failures = 0; callbacks.connection('live');
          // The service sends a state snapshot every two seconds. Detect half-open sockets.
          watchdog = env.schedule(() => { if (alive()) fallback(); }, 20000);
        }
        catch { fallback(); }
      };
      current.addEventListener('state', state);
      current.addEventListener('session.expired', () => { if (alive()) terminal('expired'); });
      current.addEventListener('revoked', () => { if (alive()) terminal('revoked'); });
      current.onerror = () => { if (alive()) fallback(); };
      // A hung HTTP handshake must not leave the UI permanently connecting.
      watchdog = env.schedule(() => { if (alive()) fallback(); }, 12000);
    } catch { fallback(); }
  }
  return {
    resume() { if (stopped || !paused) return; paused = false; open(); },
    pause() { if (stopped || paused) return; paused = true; clear(); callbacks.connection('paused'); },
    stop() { stopped = true; clear(); },
  };
}
