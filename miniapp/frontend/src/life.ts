import type { ArtiLife, EmbroideryPattern, Pursuit, Verdict } from './types';

/** Honest frames: her in-character life is declared fiction, the reading is real. */
export type Frame = 'image' | 'real';
export const FRAME_LABEL: Record<Frame, string> = { image: 'В образе', real: 'По-настоящему · ссылка ↗' };
export const MOOD_BASIS = 'Настроение · по вашему разговору';

export interface StripEntry { key: string; frame: Frame; text: string; hand: boolean }

/** What the strip can say, freshest first: her own words about her pursuits and her real reading. */
export function stripEntries(life: ArtiLife): StripEntry[] {
  const dated: (StripEntry & { at: number })[] = [];
  if (life.in_character) for (const pursuit of life.pursuits) {
    if (pursuit.last_event) dated.push({ key: `pursuit-${pursuit.kind}`, frame: 'image', text: pursuit.last_event.summary, hand: true, at: Date.parse(pursuit.last_event.at) || 0 });
  }
  if (life.reading) dated.push({ key: 'reading', frame: 'real', text: `Читала «${life.reading.title}». ${life.reading.opinion}`, hand: true, at: Date.parse(life.reading.at) || 0 });
  dated.sort((a, b) => b.at - a.at);
  const entries = dated.map(({ key, frame, text, hand }) => ({ key, frame, text, hand }));
  if (!entries.length && life.in_character) {
    // Nothing in her words yet: the state itself, in the interface's own voice.
    for (const pursuit of life.pursuits) entries.push({ key: `pursuit-${pursuit.kind}`, frame: 'image', text: `${pursuit.title} · ${pursuitState(pursuit)}`, hand: false });
  }
  return entries;
}

export function percent(progress: number): string { return `${Math.round(Math.max(0, Math.min(1, progress)) * 100)}%`; }

export function pursuitState(pursuit: Pursuit): string {
  if (pursuit.status === 'paused') return 'отложено';
  if (pursuit.restoration) return pursuit.restoration.step ? `этап ${pursuit.restoration.stage} из ${pursuit.restoration.stages.length}` : 'ещё не начинала';
  if (pursuit.tea) return 'её выбор';
  return pursuit.embroidery?.stitched ? percent(pursuit.progress) : 'ещё не начата';
}

/** «из 760 стежков», «из 21 стежка». */
export function ofStitches(total: number): string { return total % 10 === 1 && total % 100 !== 11 ? 'стежка' : 'стежков'; }

export function duration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

export const VERDICT: Record<Verdict, { text: string; tone: 'yes' | 'no' | 'maybe' }> = {
  'да': { text: 'да ✓', tone: 'yes' }, 'нет': { text: 'нет ✕', tone: 'no' }, 'спорно': { text: 'спорно ~', tone: 'maybe' },
};

export function shortDate(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) : '';
}

/** Only real web links are ever opened. */
export function safeUrl(value: string): string | null {
  try { const url = new URL(value); return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null; } catch { return null; }
}

export function capitalize(value: string): string { return value ? value[0].toLocaleUpperCase('ru') + value.slice(1) : value; }

/** The hoop: a cross for every stitched mark, a pencil dot for the rest of the printed chart. */
export interface HoopLayout { cell: number; threads: { color: string; thread: string; d: string }[]; chart: string; stitched: number; total: number }
const MARKS = 'abcd';
export const HOOP_RADIUS = 84;
export function hoopLayout(pattern: EmbroideryPattern, radius = HOOP_RADIUS): HoopLayout {
  const cells: { x: number; y: number; mark: number }[] = [];
  pattern.rows.forEach((row, y) => { for (let x = 0; x < row.length; x++) { const mark = MARKS.indexOf(row[x]); if (mark >= 0 && mark < pattern.palette.length) cells.push({ x, y, mark }); } });
  const middle = (pattern.size - 1) / 2;
  const reach = cells.reduce((most, { x, y }) => Math.max(most, Math.hypot(x - middle, y - middle)), 0) + .75;
  const cell = Math.min(4.6, radius / Math.max(reach, 1));
  const at = (value: number) => (100 + (value - middle) * cell).toFixed(1);
  const arm = cell * .36;
  const paths = pattern.palette.map(() => [] as string[]);
  const chart: string[] = [];
  const done = Math.max(0, Math.min(pattern.stitched, cells.length));
  cells.forEach(({ x, y, mark }, index) => {
    const cx = Number(at(x)), cy = Number(at(y));
    if (index < done) {
      const [l, t, r, b] = [cx - arm, cy - arm, cx + arm, cy + arm].map(value => value.toFixed(1));
      paths[mark].push(`M${l} ${t}L${r} ${b}M${r} ${t}L${l} ${b}`);
    } else chart.push(`M${cx} ${cy}h0`);
  });
  return { cell, threads: pattern.palette.map((thread, index) => ({ ...thread, d: paths[index].join('') })).filter(thread => thread.d), chart: chart.join(''), stitched: done, total: cells.length };
}
