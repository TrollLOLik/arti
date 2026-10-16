/**
 * UI protocol fixture ONLY: no real identity, PostgreSQL, Telegram, or AI provider.
 * Explicit opt-in and loopback bind prevent its use as an application backend.
 * Run: $env:ARTI_FRONTEND_PROTOCOL_FIXTURE='1'; npm.cmd run test:ui:serve
 */
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

if (process.env.ARTI_FRONTEND_PROTOCOL_FIXTURE !== '1') throw new Error('This is a test fixture. Set ARTI_FRONTEND_PROTOCOL_FIXTURE=1 explicitly.');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../dist');
const conversationId = '04f745a5-039b-4df0-b843-63db8b345709';
const csrf = 'local-protocol-fixture-no-secrets';
const user = { id: 99000001, first_name: 'Тестовый аккаунт', username: 'local_fixture' };
const capabilities = { chat_read: true, chat_send: true, live_events: true, history_sync: true, human_chats: false, rooms: false, create: false, projects: false, library: false, organizer: false, memory: false, voices: false, arti_life: true };
// Synthetic «Комната Арти» data in the API's shape; the reading note is a placeholder link, not a real source.
let lifeSettings = { mentions: true, explainer_seen: false };
let lifeMood = { label: 'ясное, с интересом', emotion: 'interest', valence: 0.3, arousal: 0.2 };
function fixturePattern() {
  const half = 20, quarter = Array.from({ length: half }, () => Array(half).fill(0));
  const shapes = [[19, 19, 6, 'disc', 1], [19, 19, 10, 'ring', 2], [11, 11, 4, 'diamond', 3], [19, 6, 3, 'cross', 1], [6, 19, 3, 'diamond', 2], [7, 7, 2, 'disc', 1], [14, 3, 2, 'ring', 3]];
  for (const [cx, cy, r, shape, color] of shapes) for (let y = 0; y < half; y++) for (let x = 0; x < half; x++) {
    const dx = Math.abs(x - cx), dy = Math.abs(y - cy), d = dx * dx + dy * dy;
    const inside = shape === 'disc' ? d <= r * r : shape === 'ring' ? (r - 1) ** 2 <= d && d <= r * r : shape === 'diamond' ? dx + dy <= r : (dx === 0 && dy <= r) || (dy === 0 && dx <= r);
    if (inside) quarter[y][x] = color;
  }
  const rows = Array.from({ length: 40 }, (_, y) => Array.from({ length: 40 }, (_, x) => { const value = quarter[Math.min(y, 39 - y)][Math.min(x, 39 - x)]; return value ? 'abcd'[value - 1] : '.'; }).join(''));
  const total = rows.join('').replace(/\./g, '').length;
  return { number: 2, motif: 'Мотылёк', size: 40, rows, palette: [{ thread: 'бордо', color: '#8c1d2e' }, { thread: 'охра', color: '#c18a2e' }, { thread: 'синий', color: '#3b5b92' }], stitched: Math.round(total * 0.62), total };
}
const pattern = fixturePattern();
function lifeView() {
  const ago = hours => new Date(Date.now() - hours * 3600000).toISOString();
  const stages = [['осмотр', 'прослушать запись целиком и отметить дефекты'], ['очистка', 'убрать щелчки и шип'], ['пропуски', 'восстановить выпавшие места'], ['выравнивание', 'выровнять громкость и тембр'], ['сверка', 'дослушать целиком и сверить с черновиком']]
    .map(([name, goal], index) => ({ name, goal, state: index < 2 ? 'done' : index === 2 ? 'current' : 'next' }));
  const reading = { title: 'Тестовая заметка: как чай возили клиперами через полмира', url: 'https://example.org/fixture-tea-clippers', opinion: 'Скорость ради вкуса — это я понимаю.', read_on: ago(30).slice(0, 10), at: ago(30) };
  const room = !lifeSettings.mentions ? { pursuits: [], embroidery: null, tastes: [] } : {
    pursuits: [
      { kind: 'embroidery', title: 'Вышивка «Мотылёк»', status: 'active', progress: pattern.stitched / pattern.total, intention: 'вышивать дальше', last_event: { kind: 'step', summary: `Вышила ещё 52 стежка. Охра легла ровно, и мотылёк наконец стал похож на себя.`, feeling: 'тихая радость', at: ago(2), title: 'Вышивка «Мотылёк»' },
        embroidery: { motif: 'Мотылёк', stitched: pattern.stitched, total: pattern.total, replaced: { motif: 'Клевер', stitched: 214, total: 812, reason: 'цвета спорят друг с другом' } } },
      { kind: 'restoration', title: 'Запись № 7', status: 'active', progress: 0.43, intention: 'восстановить выпавшие места', last_event: { kind: 'stage', summary: 'Убрала последние щелчки, шип стал тише. Теперь слышно, где запись обрывается.', feeling: 'сосредоточенно', at: ago(5), title: 'Запись № 7' },
        restoration: { record: { number: 7, medium: 'кассета', content: 'вальс на фортепиано', seconds: 84 }, stage: 3, step: 3, steps: 7, stages } },
      { kind: 'tea', title: 'Вкусы в чае', status: 'active', progress: 3 / 24, intention: 'попробовать то, о чём прочитает о чае', last_event: { kind: 'verdict_change', summary: 'Попробовала чернослив ещё раз: нет, это не моё.', feeling: 'уверенно', at: ago(28), title: 'Вкусы в чае' }, tea: { tasted: 3, known: 24 } },
    ],
    embroidery: pattern,
    tastes: [
      { item: 'чернослив', category: 'добавка', verdict: 'нет', reason: 'Попробовала чернослив ещё раз: нет, это не моё.', basis: 'Тестовая заметка: сухофрукты в чайных смесях', source: 'https://example.org/fixture-prunes', previous: 'спорно', at: ago(28) },
      { item: 'бергамот', category: 'аромат', verdict: 'да', reason: 'Заварила бергамот впервые: да, это мой вкус.', basis: 'Тестовая заметка: бергамот и эрл грей', source: 'https://example.org/fixture-bergamot', previous: null, at: ago(60) },
      { item: 'мята', category: 'добавка', verdict: 'нет', reason: 'Мята в чае заглушает всё остальное, нет.', basis: 'Тестовая заметка: мятные чаи', source: 'https://example.org/fixture-mint', previous: null, at: ago(90) },
    ],
  };
  return { settings: lifeSettings, in_character: lifeSettings.mentions, ...room, reading, mood: lifeMood };
}
let messages = [], events = [], requests = [], attempts = new Map(), revision = 1, sequence = 0, eventSequence = 0;
let mode = 'normal', postDelay = 0, expired = false, revoked = false;
const receipts = [];
const streams = new Set();
function add(text, author = 'user', source = 'telegram', client = null) {
  const item = { id: randomUUID(), conversation_id: conversationId, seq: ++sequence, author_id: author === 'user' ? user.id : null, author_kind: author, text, source, created_at: new Date(Date.now() - (60 - sequence) * 60000).toISOString(), client_message_id: client, reply_to_id: null };
  messages.push(item); events.push({ seq: ++eventSequence, kind: 'message.created', resource_id: item.id, message: item });
  return item;
}
for (let i = 1; i <= 65; i++) add(i === 65 ? 'Последняя строка общей истории Telegram. Это протокольная тестовая запись.' : `Тестовая история ${i}. Проверяем порядок и сохранение места при загрузке предыдущих сообщений.`, i % 2 ? 'user' : 'arti');
function reset() {
  revision++; messages = []; requests = []; attempts = new Map();
  // Deleted event resources are no longer exposed by replay.
  events = events.map(event => ({ ...event, message: null }));
  events.push({ seq: ++eventSequence, kind: 'history.reset', resource_id: null, message: null });
}
function json(response, status, body) { response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(body)); }
function state(after) {
  const items = events.filter(event => event.seq > after).slice(0, 100);
  return { items, cursor: items.at(-1)?.seq ?? Math.min(after, eventSequence), requests, history_revision: revision };
}
async function body(request) {
  let text = '';
  for await (const chunk of request) { text += chunk; if (text.length > 64000) throw new Error('body too large'); }
  return text ? JSON.parse(text) : {};
}
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1:8779');
    const name = url.pathname;
    if (name === '/__fixture' && request.method === 'POST') {
      const value = await body(request);
      if (value.reset) reset();
      if (value.expired !== undefined) expired = value.expired;
      if (value.revoked !== undefined) revoked = value.revoked;
      if (value.mode) mode = value.mode;
      if (value.postDelay !== undefined) postDelay = value.postDelay;
      if (value.telegramText) add(value.telegramText);
      if (value.artiText) add(value.artiText, 'arti');
      if (value.requestState) requests.forEach(job => { job.state = value.requestState; });
      if (value.lifeReset) lifeSettings = { mentions: true, explainer_seen: false };
      if (value.lifeMood !== undefined) lifeMood = value.lifeMood;
      return json(response, 200, { ok: true });
    }
    if (name === '/__fixture') return json(response, 200, { mode, revision, expired, revoked, receipts, attempts: attempts.size, messages: messages.length, eventSequence, streams: streams.size });
    if (name.startsWith('/api/') && expired) return json(response, 401, { error: { code: 'session_expired' } });
    if (name === '/api/session') return json(response, 200, { user, csrf_token: csrf, mode: 'telegram', expires_at: '2099-01-01T00:00:00Z' });
    if (name === '/api/bootstrap') return json(response, 200, { user, csrf_token: csrf, mode: 'telegram', capabilities, timezone: 'Asia/Yekaterinburg', bot_username: 'local_fixture' });
    if (name === '/api/auth/logout') { expired = true; return json(response, 200, { ok: true }); }
    if (name === '/api/arti/life' && request.method === 'GET') return json(response, 200, lifeView());
    if (name === '/api/arti/life/settings' && request.method === 'GET') return json(response, 200, { settings: lifeSettings });
    if (name === '/api/arti/life/settings' && request.method === 'PUT') {
      if (request.headers['x-csrf-token'] !== csrf) return json(response, 403, { error: { code: 'invalid_csrf' } });
      const value = await body(request);
      const keys = Object.keys(value);
      if (!keys.length || keys.some(key => !['mentions', 'explainer_seen', 'reset'].includes(key))) return json(response, 400, { error: { code: 'invalid_request' } });
      if (value.reset === true && keys.length === 1) lifeSettings = { mentions: true, explainer_seen: false };
      else if (keys.every(key => key !== 'reset' && typeof value[key] === 'boolean')) lifeSettings = { ...lifeSettings, ...value };
      else return json(response, 400, { error: { code: 'invalid_request' } });
      return json(response, 200, { settings: lifeSettings });
    }
    if (name === '/api/conversations') return json(response, 200, { items: [{ id: conversationId, kind: 'arti_private', title: 'Арти', role: 'owner', created_at: '2026-10-16T00:00:00Z', updated_at: new Date().toISOString(), last_message: messages.at(-1) ?? null, message_count: messages.length, unread_count: 0 }] });
    if (name.startsWith(`/api/conversations/${conversationId}/`) && revoked) return json(response, 404, { error: { code: 'not_found' } });
    if (name === `/api/conversations/${conversationId}/messages` && request.method === 'GET') {
      const limit = Math.min(100, Number(url.searchParams.get('limit') || 50));
      const before = Number(url.searchParams.get('before')) || Infinity;
      const items = messages.filter(message => message.seq < before).slice(-limit);
      return json(response, 200, { items, next_before: items.length && messages.some(message => message.seq < items[0].seq) ? items[0].seq : null, history_revision: revision });
    }
    if (name === `/api/conversations/${conversationId}/messages` && request.method === 'POST') {
      const value = await body(request); const expectedRevision = revision;
      receipts.push({ client_message_id: value.client_message_id, text: value.text, history_revision: value.history_revision });
      if (request.headers['x-csrf-token'] !== csrf) return json(response, 403, { error: { code: 'csrf' } });
      if (!Number.isSafeInteger(value.history_revision) || value.history_revision < 0) return json(response, 400, { error: { code: 'history_revision_required' } });
      if (postDelay) await new Promise(resolve => setTimeout(resolve, postDelay));
      if (expectedRevision !== revision || value.history_revision !== revision) return json(response, 409, { error: { code: 'history_reset' } });
      if (mode === 'reject') return json(response, 413, { error: { code: 'message_too_large' } });
      let saved = attempts.get(value.client_message_id);
      if (saved && (saved.message.text !== value.text || saved.history_revision !== value.history_revision)) return json(response, 409, { error: { code: 'payload_conflict' } });
      if (!saved) {
        const message = add(value.text, 'user', 'miniapp', value.client_message_id);
        const job = { id: randomUUID(), message_id: message.id, state: 'queued' };
        message.request_id = job.id; message.request_state = job.state;
        requests.push(job); saved = { message, request: job, history_revision: value.history_revision }; attempts.set(value.client_message_id, saved);
        if (mode === 'invalid-ack-offline') { mode = 'offline'; response.writeHead(202, { 'Content-Type': 'application/json' }); response.end('{'); return; }
        if (mode === 'lost-ack' || mode === 'lost-ack-offline') { mode = mode === 'lost-ack' ? 'normal' : 'offline'; response.destroy(); return; }
        if (mode === 'normal') {
          setTimeout(() => { if (expectedRevision === revision) job.state = 'running'; }, 700);
          setTimeout(() => { if (expectedRevision === revision) { job.state = 'succeeded'; add('Тестовая запись ответа для проверки интерфейса. Реальная Арти здесь не подключена.\n\nПроверка безопасного текста: <script>window.fixtureXss=true</script>\nСтроки и переносы сохраняются.', 'arti', 'miniapp'); } }, 2200);
        }
      }
      return json(response, 202, saved);
    }
    if (name === `/api/conversations/${conversationId}/events`) {
      if (mode === 'offline') return json(response, 503, { error: { code: 'unavailable' } });
      const after = Number(url.searchParams.get('after') || 0);
      if (after > eventSequence) return json(response, 409, { error: { code: 'invalid_cursor' } });
      return json(response, 200, state(after));
    }
    if (name === `/api/conversations/${conversationId}/events/stream`) {
      if (['polling', 'offline', 'lost-ack', 'lost-ack-offline'].includes(mode)) return json(response, 503, { error: { code: 'test_stream_unavailable' } });
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
      streams.add(response);
      let after = Number(url.searchParams.get('after') || 0);
      const send = () => {
        if (expired || revoked) { response.write(`event: ${expired ? 'session.expired' : 'revoked'}\ndata: {}\n\n`); response.end(); return; }
        if (['polling', 'offline', 'lost-ack', 'lost-ack-offline'].includes(mode)) { response.destroy(); return; }
        const value = state(after); after = value.cursor;
        response.write(`id: ${after}\nevent: state\ndata: ${JSON.stringify(value)}\n\n`);
      };
      send(); const timer = setInterval(send, 500);
      request.on('close', () => { clearInterval(timer); streams.delete(response); });
      return;
    }
    if (name.startsWith('/api/')) return json(response, 404, { error: { code: 'not_found' } });
    const relative = name.startsWith('/static/app/') ? name.slice('/static/app/'.length) : 'index.html';
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) return json(response, 404, {});
    const content = await readFile(file);
    const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.woff': 'font/woff', '.woff2': 'font/woff2' }[path.extname(file)] || 'application/octet-stream';
    response.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-store' }); response.end(content);
  } catch { if (!response.headersSent) json(response, 500, { error: { code: 'fixture_error' } }); else response.end(); }
});
server.listen(8779, '127.0.0.1', () => process.stdout.write('TEST ONLY frontend protocol fixture: http://127.0.0.1:8779/\n'));
process.on('SIGINT', () => { for (const stream of streams) stream.end(); server.close(() => process.exit(0)); });
