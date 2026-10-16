import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dayKey, dayLabel, headerStatus, replyPhase, rowTime } from '../src/chatView.ts';
import type { Message, RequestState } from '../src/types.ts';

function message(seq: number, author: Message['author_kind'], state: RequestState | null = null): Message {
  return { id: `m${seq}`, conversation_id: 'c', seq, author_id: author === 'user' ? 1 : null, author_kind: author, text: 'текст', created_at: '2026-10-16T10:00:00Z', client_message_id: null, reply_to_id: null, request_state: state };
}

test('typing comes only from real request states of the user messages', () => {
  assert.equal(replyPhase([]), null);
  assert.equal(replyPhase([message(1, 'user', 'completed'), message(2, 'arti')]), null);
  assert.equal(replyPhase([message(1, 'user', 'queued')]), 'queued');
  assert.equal(replyPhase([message(1, 'user', 'queued'), message(2, 'user', 'running')]), 'running');
  assert.equal(replyPhase([message(1, 'user', 'failed'), message(2, 'arti', 'running')]), null);
});

test('header status follows the reply phase and revoked access', () => {
  assert.equal(headerStatus(null, false), 'личный разговор');
  assert.equal(headerStatus('queued', false), 'в очереди');
  assert.equal(headerStatus('running', false), 'пишет…');
  assert.equal(headerStatus('running', true), 'доступ закрыт');
});

test('date separators use the local calendar day', () => {
  const now = new Date(2026, 9, 16, 12, 0);
  assert.equal(dayKey(new Date(2026, 9, 16, 0, 5).toISOString()), '2026-10-16');
  assert.equal(dayKey('not a date'), null);
  assert.equal(dayLabel(new Date(2026, 9, 16, 9, 0).toISOString(), now), 'Сегодня, 16 октября');
  assert.equal(dayLabel(new Date(2026, 9, 15, 23, 50).toISOString(), now), 'Вчера, 15 октября');
  assert.equal(dayLabel(new Date(2026, 9, 2, 8, 0).toISOString(), now), '2 октября');
  assert.match(dayLabel(new Date(2025, 11, 31, 8, 0).toISOString(), now), /^31 декабря 2025/);
  assert.equal(dayLabel('', now), '');
});

test('chat list time is a clock today, then yesterday, then a short date', () => {
  const now = new Date(2026, 9, 16, 12, 0);
  assert.equal(rowTime(new Date(2026, 9, 16, 9, 5).toISOString(), now), '09:05');
  assert.equal(rowTime(new Date(2026, 9, 15, 9, 5).toISOString(), now), 'Вчера');
  assert.match(rowTime(new Date(2026, 9, 2, 9, 5).toISOString(), now), /^2 окт/);
  assert.equal(rowTime('nope', now), '');
});
