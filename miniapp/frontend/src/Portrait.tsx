import { useId } from 'react';
import { STAR_PATH } from './icons';

export const PORTRAIT = `${import.meta.env.BASE_URL}assets/arti-portrait-v4.webp`;
/** Crops of the 1145×1374 portrait; pupils sit at 486,319 and 702,366. */
const CROPS = {
  avatar: { box: '372 168 440 440', glow: 34, spark: 18 },
  eyes: { box: '330 262 560 150', glow: 22, spark: 13 },
  room: { box: '140 196 880 368', glow: 24, spark: 14 },
  full: { box: '0 0 1145 1374', glow: 30, spark: 16 },
} as const;
const PUPILS = [[486, 319, 0], [702, 366, .8]] as const;

/** Her pupils light up only in the evening («лампа») theme; the stylesheet decides. */
export function Portrait({ crop = 'avatar' }: { crop?: keyof typeof CROPS }) {
  const glow = `pupil-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const { box, glow: radius, spark } = CROPS[crop];
  return <svg className="portrait" viewBox={box} preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
    <defs><radialGradient id={glow}><stop offset="0" stopColor="#fff" /><stop offset=".22" stopColor="#ffe3ec" stopOpacity=".95" /><stop offset=".5" stopColor="#ffb3c8" stopOpacity=".55" /><stop offset="1" stopColor="#ff8fb0" stopOpacity="0" /></radialGradient></defs>
    <image href={PORTRAIT} x="0" y="0" width="1145" height="1374" />
    <g className="pupils">{PUPILS.map(([x, y, delay]) => <g className="eye-glow" key={x} style={{ animationDelay: `${delay}s` }}>
      <circle cx={x} cy={y} r={radius} fill={`url(#${glow})`} />
      <path d={STAR_PATH} transform={`translate(${x} ${y}) scale(${(spark / 24).toFixed(3)}) translate(-12 -12)`} fill="#fff" />
    </g>)}</g>
  </svg>;
}
