import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { createAttempt, isHistoryRevision, mergeMessages, parseEventPage, requestLabel, requestMessages, sendAttempt, sendError, validateText } from '../src/chat.ts';
import { ApiError } from '../src/api.ts';
import type { Message } from '../src/types.ts';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
function message(id: string, seq: number, extra: Partial<Message> = {}): Message {
  return { id, seq, conversation_id: 'conversation', author_id: 123, author_kind: 'user', text: 'Привет', created_at: '2026-10-16T00:00:00Z', client_message_id: 'client', reply_to_id: null, ...extra };
}
test('uncertain retry reuses the exact UUID and body, with CSRF in a header only', async () => {
  const attempt = createAttempt(' Привет\nс новой строки ', 7, '2313564b-1c98-44d7-a0b1-79af7a55d161', '2026-10-16T00:00:00Z');
  const calls: RequestInit[] = [];
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/conversations/conversation/messages');
    calls.push(init!);
    if (calls.length === 1) throw new TypeError('connection lost after commit');
    return Response.json({ message: message('saved', 2), request: { id: 'job', state: 'queued' } }, { status: 202 });
  };
  await assert.rejects(sendAttempt('conversation', attempt, 'csrf-memory-only'), error => error instanceof ApiError && error.status === 0);
  const saved = await sendAttempt('conversation', attempt, 'csrf-memory-only');
  assert.equal(saved.message.id, 'saved');
  assert.equal(calls[0].body, calls[1].body);
  assert.deepEqual(JSON.parse(calls[1].body as string), { text: attempt.text, client_message_id: attempt.client_message_id, history_revision: 7 });
  assert.equal((calls[1].headers as Record<string, string>)['X-CSRF-Token'], 'csrf-memory-only');
  assert.equal(calls[1].credentials, 'include');
  assert.equal((calls[1].body as string).includes('csrf'), false);
});
test('edits create a new intent while whitespace and line breaks remain intact', () => {
  const first = createAttempt(' abc ', 0, 'one');
  const second = createAttempt(' abc\n ', 1, 'two');
  assert.notEqual(first.client_message_id, second.client_message_id);
  assert.equal(first.text, ' abc ');
  assert.equal(second.text, ' abc\n ');
  assert.equal(first.history_revision, 0);
  assert.equal(second.history_revision, 1);
});
test('an unknown or invalid history revision never reaches the POST transport', async () => {
  globalThis.fetch = async () => { assert.fail('unknown revision must not issue a POST'); };
  for (const value of [undefined, null, -1, 0.5, '1', Number.NaN, Infinity]) {
    assert.equal(isHistoryRevision(value), false);
    assert.throws(() => createAttempt('old text', value as number), /history_revision_required/);
    await assert.rejects(sendAttempt('conversation', { text: 'old text', client_message_id: 'client', created_at: 'now', history_revision: value as number }, 'csrf'), error => error instanceof ApiError && error.code === 'history_revision_required');
  }
  assert.equal(isHistoryRevision(0), true);
});
test('a retry after a server reset keeps its original revision instead of rebasing old text', async () => {
  const old = createAttempt('erased intent', 1, 'old-client');
  const bodies: string[] = [];
  let currentRevision = 1;
  globalThis.fetch = async (_url, init) => {
    bodies.push(init?.body as string);
    const value = JSON.parse(init?.body as string);
    if (currentRevision !== value.history_revision) return Response.json({ error: { code: 'history_reset' } }, { status: 409 });
    throw new TypeError('lost acknowledgement');
  };
  await assert.rejects(sendAttempt('conversation', old, 'csrf'), error => error instanceof ApiError && error.status === 0);
  currentRevision = 2;
  await assert.rejects(sendAttempt('conversation', old, 'csrf'), error => error instanceof ApiError && error.status === 409 && error.code === 'history_reset');
  assert.equal(bodies[0], bodies[1]);
  assert.equal(JSON.parse(bodies[1]).history_revision, 1);
});
test('character and UTF-8 bounds admit emoji without counting surrogate halves', () => {
  assert.equal(validateText('  \n '), 'Напишите сообщение.');
  assert.equal(validateText('a'.repeat(8000)), null);
  assert.equal(validateText('😀'.repeat(8000)), null);
  assert.ok(validateText('😀'.repeat(8001)));
  assert.ok(validateText('a'.repeat(8001)));
});
test('history prepend, SSE echo and server ack converge by stable ID and sequence', () => {
  const latest = message('new', 90, { source: 'miniapp', request_state: 'queued' });
  const merged = mergeMessages([latest], [message('old', 1, { source: 'telegram' }), message('new', 90, { source: 'miniapp', request_state: 'running' })]);
  assert.deepEqual(merged.map(item => item.id), ['old', 'new']);
  assert.equal(merged[0].source, 'telegram');
  assert.equal(merged[1].request_state, 'running');
  assert.equal(mergeMessages(merged, [latest]).length, 2);
});
test('request snapshots update the corresponding bubble without manufacturing a reply', () => {
  const rows = [message('input', 1, { request_id: 'job', request_state: 'queued' })];
  const updated = requestMessages(rows, [{ id: 'job', state: 'failed', message_id: 'input' }]);
  assert.equal(updated.length, 1);
  assert.equal(updated[0].request_state, 'failed');
  assert.equal(rows[0].request_state, 'queued');
  assert.equal(requestLabel('succeeded'), null);
  assert.equal(requestLabel('running'), 'Арти отвечает…');
  assert.ok(requestLabel('failed')?.includes('новый запрос'));
});
test('a later mirror receipt updates the existing message without a second bubble', () => {
  const original = message('answer', 9, { author_kind: 'arti', mirror_state: 'sending' });
  const updated = mergeMessages([original], [{ ...original, mirror_state: 'delivery_unknown' }]);
  assert.equal(updated.length, 1);
  assert.equal(updated[0].seq, 9);
  assert.equal(updated[0].mirror_state, 'delivery_unknown');
  assert.equal(mergeMessages(updated, [{ ...original, mirror_state: 'delivered' }])[0].mirror_state, 'delivered');
});
test('network and server uncertainty stays retryable; payload conflicts are not silently reissued', () => {
  assert.equal(sendError(new ApiError(0, 'network')).uncertain, true);
  assert.equal(sendError(new ApiError(503, 'unavailable')).uncertain, true);
  assert.equal(sendError(new ApiError(409, 'feature_unavailable')).uncertain, true);
  assert.equal(sendError(new ApiError(409, 'payload_conflict')).uncertain, false);
  assert.equal(sendError(new ApiError(413, 'too_large')).uncertain, false);
  assert.equal(sendError(new Error('private provider key')).text.includes('key'), false);
});
test('event envelopes validate cursors and put out-of-order rows in sequence', () => {
  const page = parseEventPage({ cursor: 9, history_revision: 2, requests: [], items: [{ seq: 9, kind: 'message.created', resource_id: 'b', message: null }, { seq: 3, kind: 'history.reset', resource_id: null, message: null }] });
  assert.deepEqual(page?.items.map(item => item.seq), [3, 9]);
  assert.equal(parseEventPage({ cursor: -1, history_revision: 0, requests: [], items: [] }), null);
  assert.equal(parseEventPage({ cursor: 1, history_revision: 0, requests: [], items: [{ seq: 2, kind: 'history.reset' }] }), null);
  assert.equal(parseEventPage({ cursor: '1', history_revision: 0, requests: [], items: [] }), null);
  assert.equal(parseEventPage({ cursor: 1, history_revision: 0, items: [] }), null);
});
