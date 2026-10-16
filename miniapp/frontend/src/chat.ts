import type { ChatRequest, EventPage, Message, RequestState, SendResult } from './types';
import { ApiError, request } from './api.ts';

/** Kept in memory only. An uncertain HTTP outcome must retry this exact intent. */
export interface SendAttempt { client_message_id: string; text: string; history_revision: number; created_at: string; reply_to_id?: string }
export function isHistoryRevision(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0; }
export function createAttempt(text: string, historyRevision: number, id = crypto.randomUUID(), now = new Date().toISOString()): SendAttempt {
  if (!isHistoryRevision(historyRevision)) throw new RangeError('history_revision_required');
  return { client_message_id: id, text, history_revision: historyRevision, created_at: now };
}
export function validateText(text: string): string | null {
  if (!text.trim()) return 'Напишите сообщение.';
  if (Array.from(text).length > 8000 || new TextEncoder().encode(text).length > 32000) return 'Сообщение слишком длинное. Максимум — 8 000 символов.';
  return null;
}
export function sendAttempt(conversationId: string, attempt: SendAttempt, csrf: string, signal?: AbortSignal): Promise<SendResult> {
  const { text, client_message_id, history_revision, reply_to_id } = attempt;
  if (!isHistoryRevision(history_revision)) return Promise.reject(new ApiError(409, 'history_revision_required'));
  return request<SendResult>(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, { method: 'POST', body: { text, client_message_id, history_revision, ...(reply_to_id ? { reply_to_id } : {}) }, csrf, signal });
}
export function mergeMessages(existing: Message[], incoming: Message[]): Message[] {
  const byId = new Map(existing.map(message => [message.id, message]));
  for (const message of incoming) byId.set(message.id, { ...byId.get(message.id), ...message });
  return [...byId.values()].sort((a, b) => a.seq - b.seq || a.id.localeCompare(b.id));
}
export function requestMessages(messages: Message[], requests: ChatRequest[]): Message[] {
  const byMessage = new Map(requests.filter(item => item.message_id).map(item => [item.message_id, item]));
  const byId = new Map(requests.map(item => [item.id, item]));
  return messages.map(message => {
    const job = byMessage.get(message.id) || (message.request_id ? byId.get(message.request_id) : undefined);
    return job ? { ...message, request_id: job.id, request_state: job.state } : message;
  });
}
export function requestLabel(state?: RequestState | null): string | null {
  switch (state) {
    case 'queued': return 'В очереди';
    case 'running': return 'Арти отвечает…';
    case 'failed': return 'Ответ не получился. Можно отправить новый запрос.';
    case 'cancelled': return 'Запрос отменён';
    case 'expired': case 'deadline': return 'Время ожидания истекло';
    case 'delivery_unknown': return 'Доставка ответа уточняется';
    default: return null;
  }
}
export function sendError(cause: unknown): { text: string; uncertain: boolean } {
  if (cause instanceof ApiError) {
    if (cause.status === 0 || cause.status >= 500 || cause.code === 'invalid_response') return { text: 'Связь прервалась. Сообщение могло дойти. Повтор проверит эту же отправку.', uncertain: true };
    if (cause.status === 429) return { text: 'Слишком много запросов. Подождите немного и повторите.', uncertain: true };
    if (cause.code === 'feature_unavailable') return { text: 'Отправка временно недоступна. Попробуйте позже.', uncertain: true };
    if (cause.status === 409) return { text: 'Не удалось подтвердить отправку. Обновите историю перед новым сообщением.', uncertain: false };
    if (cause.status === 400 || cause.status === 413 || cause.status === 422) return { text: 'Сообщение не принято. Проверьте его длину и попробуйте снова.', uncertain: false };
  }
  return { text: 'Не удалось отправить сообщение. Попробуйте ещё раз.', uncertain: true };
}
/** Validate only the transport envelope; message text always renders as React text. */
export function parseEventPage(value: unknown): EventPage | null {
  if (!value || typeof value !== 'object') return null;
  const page = value as EventPage;
  if (!Number.isSafeInteger(page.cursor) || page.cursor < 0 || !isHistoryRevision(page.history_revision) || !Array.isArray(page.items) || !Array.isArray(page.requests)) return null;
  if (page.items.some(item => !Number.isSafeInteger(item.seq) || item.seq <= 0 || item.seq > page.cursor || !['message.created', 'history.reset', 'membership.changed'].includes(item.kind))) return null;
  return { ...page, items: [...page.items].sort((a, b) => a.seq - b.seq) };
}
