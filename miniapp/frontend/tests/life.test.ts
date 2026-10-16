import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FRAME_LABEL, HOOP_RADIUS, MOOD_BASIS, duration, hoopLayout, ofStitches, pursuitState, safeUrl, stripEntries } from '../src/life.ts';
import type { ArtiLife, EmbroideryPattern, Pursuit } from '../src/types.ts';

function pursuit(kind: Pursuit['kind'], at: string | null, changes: Partial<Pursuit> = {}): Pursuit {
  return { kind, title: kind, status: 'active', progress: .5, intention: null, last_event: at ? { kind: 'step', summary: `слова: ${kind}`, feeling: 'спокойно', at, title: kind } : null, ...changes };
}
function life(changes: Partial<ArtiLife> = {}): ArtiLife {
  return { settings: { mentions: true, explainer_seen: false }, in_character: true, pursuits: [], embroidery: null, tastes: [],
    reading: { title: 'Заметка', url: 'https://example.org/n', opinion: 'Мнение.', read_on: '2026-10-16', at: '2026-10-16T08:00:00Z' }, mood: null, ...changes };
}

test('labels keep the in-character life apart from the real reading and the mood basis', () => {
  assert.equal(FRAME_LABEL.image, 'В образе');
  assert.equal(FRAME_LABEL.real, 'По-настоящему · ссылка ↗');
  assert.equal(MOOD_BASIS, 'Настроение · по вашему разговору');
});

test('the strip says her own words, freshest first, and the reading is framed as real', () => {
  const entries = stripEntries(life({ pursuits: [pursuit('embroidery', '2026-10-16T06:00:00Z'), pursuit('restoration', '2026-10-16T09:00:00Z'), pursuit('tea', null)] }));
  assert.deepEqual(entries.map(entry => [entry.key, entry.frame]), [['pursuit-restoration', 'image'], ['reading', 'real'], ['pursuit-embroidery', 'image']]);
  assert.equal(entries[1].text, 'Читала «Заметка». Мнение.');
  assert.ok(entries.every(entry => entry.hand));
});

test('mentions off leaves only the real reading; nothing in her words falls back to the state', () => {
  const hidden = stripEntries(life({ in_character: false, pursuits: [pursuit('embroidery', '2026-10-16T06:00:00Z')] }));
  assert.deepEqual(hidden.map(entry => entry.frame), ['real']);
  const quiet = stripEntries(life({ reading: null, pursuits: [pursuit('embroidery', null, { embroidery: { motif: 'М', stitched: 0, total: 10, replaced: null }, progress: 0 })] }));
  assert.deepEqual(quiet.map(entry => [entry.text, entry.hand]), [['embroidery · ещё не начата', false]]);
  assert.deepEqual(stripEntries(life({ reading: null, in_character: false })), []);
});

test('pursuit state, durations and declensions', () => {
  const stages = ['осмотр', 'очистка', 'пропуски', 'выравнивание', 'сверка'].map((name, index) => ({ name, goal: '', state: index < 2 ? 'done' as const : index === 2 ? 'current' as const : 'next' as const }));
  const record = { number: 7, medium: 'кассета', content: 'вальс', seconds: 84 };
  assert.equal(pursuitState(pursuit('restoration', null, { restoration: { record, stage: 3, step: 4, steps: 7, stages } })), 'этап 3 из 5');
  assert.equal(pursuitState(pursuit('restoration', null, { restoration: { record, stage: 1, step: 0, steps: 7, stages } })), 'ещё не начинала');
  assert.equal(pursuitState(pursuit('embroidery', null, { status: 'paused' })), 'отложено');
  assert.equal(pursuitState(pursuit('embroidery', null, { progress: .618, embroidery: { motif: 'М', stitched: 5, total: 8, replaced: null } })), '62%');
  assert.equal(duration(84), '1:24');
  assert.equal(duration(40), '0:40');
  assert.deepEqual([1, 21, 11, 760, 812].map(ofStitches), ['стежка', 'стежка', 'стежков', 'стежков', 'стежков']);
});

test('only http(s) links are opened', () => {
  assert.equal(safeUrl('https://example.org/a b'), 'https://example.org/a%20b');
  assert.equal(safeUrl('javascript:alert(1)'), null);
  assert.equal(safeUrl('data:text/html,x'), null);
  assert.equal(safeUrl('not a url'), null);
});

test('the hoop draws one cross per stitched mark in reading order, all inside the ring', () => {
  const rows = Array.from({ length: 40 }, (_, y) => Array.from({ length: 40 }, (_, x) => (Math.hypot(x - 19.5, y - 19.5) < 6 ? (x < 20 ? 'a' : 'b') : x === 0 && y === 0 ? 'c' : '.')).join(''));
  const total = rows.join('').replace(/\./g, '').length;
  const pattern: EmbroideryPattern = { number: 1, motif: 'Тест', size: 40, rows, palette: [{ thread: 'синий', color: '#123456' }, { thread: 'охра', color: '#654321' }, { thread: 'бордо', color: '#8c1d2e' }], stitched: 30, total };
  const layout = hoopLayout(pattern);
  assert.equal(layout.total, total);
  assert.equal(layout.stitched, 30);
  const crosses = layout.threads.reduce((sum, thread) => sum + (thread.d.match(/M/g)?.length ?? 0) / 2, 0);
  assert.equal(crosses, 30);
  assert.equal((layout.chart.match(/M/g)?.length ?? 0), total - 30);
  // The corner mark widens the reach, so the chart shrinks to keep it inside the hoop.
  const points = [...layout.threads.map(thread => thread.d).join('').matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g), ...layout.chart.matchAll(/M(-?[\d.]+) (-?[\d.]+)/g)];
  assert.ok(points.every(([, x, y]) => Math.hypot(Number(x) - 100, Number(y) - 100) <= HOOP_RADIUS + 1));
  assert.equal(hoopLayout({ ...pattern, stitched: 10_000 }).stitched, total);
  assert.equal(hoopLayout({ ...pattern, stitched: 0 }).threads.length, 0);
});
