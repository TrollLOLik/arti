import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError } from '../src/api.ts';
import { conversationFeed } from '../src/liveEvents.ts';
import type { ConnectionState } from '../src/liveEvents.ts';
import type { EventPage } from '../src/types.ts';

class FakeStream {
  listeners = new Map<string, EventListener>();
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  addEventListener(name: string, listener: EventListener) { this.listeners.set(name, listener); }
  close() { this.closed = true; }
  emit(name: string, value?: unknown) { this.listeners.get(name)?.({ data: JSON.stringify(value) } as MessageEvent); }
  fail() { this.onerror?.(new Event('error')); }
}
const page = (cursor = 0, revision = 0): EventPage => ({ cursor, history_revision: revision, items: [], requests: [] });
function fixture(streaming = true) {
  const streams: { url: string; source: FakeStream }[] = [];
  const states: EventPage[] = [];
  const connections: ConnectionState[] = [];
  const timers = new Map<number, { delay: number; run: () => void }>();
  const urls: string[] = [];
  const signals: AbortSignal[] = [];
  const responses: (EventPage | Error | Promise<EventPage>)[] = [];
  let timerId = 0, expired = 0, revoked = 0, resynced = 0;
  const feed = conversationFeed('private-conversation', { state: value => states.push(value), connection: value => connections.push(value), expired: () => expired++, revoked: () => revoked++, resync: () => resynced++ }, {
    stream: streaming ? url => { const source = new FakeStream(); streams.push({ url, source }); return source; } : null,
    fetch: async (url, signal) => { urls.push(url); signals.push(signal); const value = responses.shift() ?? page(); if (value instanceof Error) throw value; return value; },
    schedule: (run, delay) => { const id = ++timerId; timers.set(id, { delay, run }); return id as unknown as ReturnType<typeof setTimeout>; },
    cancel: id => { timers.delete(id as unknown as number); },
  });
  return { feed, streams, states, connections, timers, urls, signals, responses,
    counts: () => ({ expired, revoked, resynced }),
    async next() {
      const first = [...timers].sort((a, b) => a[1].delay - b[1].delay)[0];
      assert.ok(first, 'scheduled timer exists'); timers.delete(first[0]); first[1].run();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    },
  };
}
test('starts after zero with cookie-based SSE and ignores duplicate event rows', () => {
  const f = fixture(); f.feed.resume(); f.feed.resume();
  assert.equal(f.streams.length, 1);
  assert.equal(f.streams[0].url, '/api/conversations/private-conversation/events/stream?after=0');
  const batch = { ...page(2), items: [{ seq: 2, kind: 'message.created', resource_id: 'm', message: null }] };
  f.streams[0].source.emit('state', batch); f.streams[0].source.emit('state', batch);
  assert.equal(f.states[0].items.length, 1);
  assert.equal(f.states[1].items.length, 0);
  assert.equal(f.connections.at(-1), 'live');
  f.feed.stop(); assert.equal(f.timers.size, 0);
});
test('SSE failure closes its socket before polling and resumes from the last cursor', async () => {
  const f = fixture(); f.feed.resume(); f.streams[0].source.emit('state', page(17));
  f.streams[0].source.fail();
  assert.equal(f.streams[0].source.closed, true);
  assert.equal(f.timers.size, 1);
  f.responses.push(page(18)); await f.next();
  assert.equal(f.urls[0], '/api/conversations/private-conversation/events?after=17&limit=100');
  assert.equal(f.states.at(-1)?.cursor, 18);
  assert.equal([...f.timers.values()][0].delay, 3500);
  f.feed.stop();
});
test('future cursor conflict clears cursor and asks for full private-history resync', async () => {
  const f = fixture(); f.feed.resume(); f.streams[0].source.emit('state', page(100)); f.streams[0].source.fail();
  f.responses.push(new ApiError(409, 'invalid_cursor')); await f.next();
  assert.equal(f.counts().resynced, 1);
  f.responses.push(page()); await f.next();
  assert.ok(f.urls[1].includes('after=0&'));
  f.feed.stop();
});
test('revision-only state batches still reach the consumer after reset', () => {
  const f = fixture(); f.feed.resume();
  f.streams[0].source.emit('state', page(10, 1)); f.streams[0].source.emit('state', page(10, 2));
  assert.deepEqual(f.states.map(item => item.history_revision), [1, 2]);
  f.feed.stop();
});
test('session expiry and revoked events terminate timers and sockets', () => {
  for (const event of ['session.expired', 'revoked']) {
    const f = fixture(); f.feed.resume(); f.streams[0].source.emit(event); f.streams[0].source.fail(); f.feed.resume();
    assert.equal(f.streams[0].source.closed, true); assert.equal(f.timers.size, 0); assert.equal(f.streams.length, 1);
    assert.equal(event === 'revoked' ? f.counts().revoked : f.counts().expired, 1);
  }
});
test('poll authorization failure terminates without leaking private responses', async () => {
  for (const status of [401, 403, 404]) {
    const f = fixture(false); f.responses.push(new ApiError(status, 'unauthorized')); f.feed.resume(); await f.next();
    assert.equal(f.states.length, 0); assert.equal(f.timers.size, 0);
    assert.equal(status === 401 ? f.counts().expired : f.counts().revoked, 1);
  }
});
test('hidden/left conversation aborts in-flight polling and ignores stale callbacks', async () => {
  const f = fixture(false);
  let finish: (value: EventPage) => void = () => {};
  f.responses.push(new Promise(resolve => { finish = resolve; }));
  f.feed.resume(); await f.next(); f.feed.pause();
  assert.equal(f.signals[0].aborted, true); assert.equal(f.timers.size, 0);
  finish(page(100)); for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(f.states.length, 0);
  f.feed.resume(); await f.next(); assert.equal(f.urls.length, 2);
  f.feed.stop(); assert.equal(f.timers.size, 0);
});
test('network retry backs off and a hung stream falls back instead of staying stuck', async () => {
  const f = fixture(); f.feed.resume(); await f.next();
  assert.equal(f.streams[0].source.closed, true);
  f.responses.push(new ApiError(0, 'network')); await f.next();
  assert.equal(f.connections.at(-1), 'offline');
  assert.equal([...f.timers.values()][0].delay, 7000);
  f.responses.push(new ApiError(0, 'network')); await f.next();
  assert.equal([...f.timers.values()][0].delay, 14000);
  f.responses.push(new ApiError(0, 'network')); await f.next();
  assert.ok([...f.timers.values()][0].delay <= 15000);
  f.feed.stop();
});
test('a healthy stream with missing periodic snapshots triggers recovery', async () => {
  const f = fixture(); f.feed.resume(); f.streams[0].source.emit('state', page());
  assert.equal([...f.timers.values()][0].delay, 20000);
  await f.next(); assert.equal(f.streams[0].source.closed, true);
  assert.equal(f.connections.at(-1), 'polling'); f.feed.stop();
});
