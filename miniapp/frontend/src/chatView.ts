import type { Message } from './types';

/** Her typing state comes only from the durable request state in history and live events, never from a timer. */
export type ReplyPhase = 'queued' | 'running';
export function replyPhase(messages: Message[]): ReplyPhase | null {
  let phase: ReplyPhase | null = null;
  for (const message of messages) {
    if (message.author_kind !== 'user') continue;
    if (message.request_state === 'running') return 'running';
    if (message.request_state === 'queued') phase = 'queued';
  }
  return phase;
}
export const TYPING_TEXT: Record<ReplyPhase, string> = { queued: 'сейчас прочитает…', running: 'подбирает слово…' };

export function headerStatus(phase: ReplyPhase | null, revoked: boolean): string {
  if (revoked) return 'доступ закрыт';
  if (phase === 'running') return 'пишет…';
  if (phase === 'queued') return 'в очереди';
  return 'личный разговор';
}

function validDate(value: string): Date | null {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function startOfDay(date: Date): number { return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime(); }
function daysBetween(date: Date, now: Date): number { return Math.round((startOfDay(now) - startOfDay(date)) / 86400000); }

/** Local calendar day, used to place date separators between messages. */
export function dayKey(value: string): string | null {
  const date = validDate(value);
  return date ? `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}` : null;
}
export function dayLabel(value: string, now = new Date()): string {
  const date = validDate(value);
  if (!date) return '';
  const text = date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
  const days = daysBetween(date, now);
  return days === 0 ? `Сегодня, ${text}` : days === 1 ? `Вчера, ${text}` : text;
}
/** Chat-list time: clock for today, «Вчера», then a short date. */
export function rowTime(value: string, now = new Date()): string {
  const date = validDate(value);
  if (!date) return '';
  const days = daysBetween(date, now);
  if (days === 0) return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  if (days === 1) return 'Вчера';
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', ...(date.getFullYear() !== now.getFullYear() ? { year: '2-digit' } : {}) });
}
