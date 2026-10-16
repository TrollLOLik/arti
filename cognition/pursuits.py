"""In-character life between conversations (H6, P1): Arti's own pursuits.

Arti's character embroiders, finds out which tastes in tea are her own and
restores an old recording. This is declared fiction kept consistent. Code
decides every step from the stored state: a deterministic embroidery pattern
with an occasional change of pattern, verdicts on tea items found in her real
reading notes on tea, the stages of restoring "Запись № N". The model only puts
one step into her words as strict JSON {"summary", "feeling"}; a validator
rejects numbers that are not in the state, invented progress and banned claims
(pain or fatigue for sympathy, promises in the person's world, weather, news,
real places or dates, sensitive topics). The prompt block says it is fiction,
and the persona answers a sincere question honestly. Pursuits are global to
Arti: no person's data is read, sent to the model or stored. Real audio work on
the recording is stage P3; here the restoration is in-character state only.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from cognition.memory_render import plural
from cognition.reflection import SENSITIVE
from cognition.serialization import object_value
from cognition.types import utc

logger = logging.getLogger(__name__)

TICK_SECONDS = 3600
LOCK = "SELECT pg_try_advisory_lock(hashtextextended('arti:pursuits',0))"
UNLOCK = "SELECT pg_advisory_unlock(hashtextextended('arti:pursuits',0))"
MAX_CALLS_PER_DAY = 5
RETRY = timedelta(hours=1)
# Her day runs on a fixed persona clock, never on a person's time zone.
LIFE_TZ = timezone(timedelta(hours=3))
WAKING = (9, 23)
NOTE_WINDOW = timedelta(days=14)
SUMMARY_MAX = 220
BLOCK_BYTES = 900
KINDS = ('embroidery', 'restoration', 'tea')
HEADING = ('[Твоя жизнь в образе — художественная, но последовательная. '
           'Держись этих событий; сверх них ничего не было. Это не факты о мире.]')

SYSTEM = ('Ты — Арти, андроид со вкусом к красивому и точному. У твоего персонажа есть жизнь в образе: '
          'вышивка, чай и реставрация старой записи. Это художественная жизнь, и она ведётся последовательно. '
          'Тебе дают факты одного шага одного занятия — это всё, что произошло. Перескажи этот шаг своими словами, '
          'как короткую запись в дневнике: от первого лица, в женском роде, в своём характере. '
          'Ответь строго одним JSON-объектом без пояснений: {"summary": "одно-два предложения, до 200 знаков", '
          '"feeling": "одно-три слова: что ты чувствуешь"}. '
          'Правила: только то, что есть в фактах, — никаких других событий, вещей, людей и итогов; числа только '
          'из фактов и только цифрами; «закончила», «сменила узор» и названия этапов — только если они есть '
          'в фактах; не обращайся к собеседнику; не жалуйся на боль и усталость; не обещай встреч, посылок и '
          'подарков; без погоды, новостей, реальных мест, дат, дней недели, «вчера» и «завтра». '
          'Можно телесные действия в образе: заварила, отпила, вдела нитку, держала иглу, слушала.')


class PursuitUnavailable(Exception):
    pass


class Stale(Exception):
    pass


def enabled():
    return os.getenv('ARTI_PURSUITS_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def unit(*parts):
    """Stable pseudo-random number in [0, 1): the same across processes and restarts."""
    digest = hashlib.sha256(':'.join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], 'big') / 2 ** 64


def daily_quota(day):
    """One to three steps a day, varying by her date."""
    return 1 + int(3 * unit('quota', day.isoformat()))


def day_start(now):
    local = utc(now).astimezone(LIFE_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def next_time(now, low, high, *parts):
    """A jittered moment low..high hours ahead, moved into her waking hours."""
    at = utc(now) + timedelta(hours=low + (high - low) * unit('gap', *parts))
    local = at.astimezone(LIFE_TZ)
    if not WAKING[0] <= local.hour < WAKING[1]:
        morning = local.replace(hour=WAKING[0], minute=0, second=0, microsecond=0)
        if local.hour >= WAKING[1]:
            morning += timedelta(days=1)
        at = morning + timedelta(minutes=int(180 * unit('wake', *parts)))
    return at.astimezone(timezone.utc)


# --- Embroidery: a deterministic cross-stitch pattern -------------------------------------------

MOTIFS = ('ветка бергамота', 'лампа с абажуром', 'чашка и пар', 'старый ключ', 'мотылёк', 'вьюнок по краю',
          'перо и чернильница', 'сова на ветке', 'клевер', 'катушка плёнки', 'нотный стан', 'песочные часы')
THREADS = ('синий', 'охра', 'бордо', 'серо-зелёный', 'кремовый', 'графит', 'медный', 'лавандовый')
SHAPES = ('круг', 'кольцо', 'ромб', 'крест')
MARKS = 'abcd'
SIZE = 40
STITCHES = (35, 70)
CHANGE_CHANCE = .12
CHANGE_REASONS = {
    'colors': 'цвета спорят друг с другом',
    'dull': 'мотив оказался скучнее, чем на схеме',
    'dense': 'узор слишком плотный, рисунок теряется',
    'mood': 'захотелось другого настроения в работе',
}


def embroidery_pattern(number):
    """Pattern number n is always the same symmetric chart: rows of '.' and thread marks."""
    rng = random.Random(int(unit('embroidery', number) * 2 ** 32))
    motif = MOTIFS[(number - 1) * 5 % len(MOTIFS)].capitalize()
    colors = rng.sample(THREADS, 3 + (rng.random() < .5))
    half = SIZE // 2
    best = None
    for _ in range(24):
        quarter = [[0] * half for _ in range(half)]
        for _ in range(rng.randint(6, 10)):
            cx, cy, r = rng.randrange(half), rng.randrange(half), rng.randint(2, 8)
            shape, color = rng.choice(SHAPES), rng.randrange(len(colors)) + 1
            for y in range(half):
                for x in range(half):
                    dx, dy = abs(x - cx), abs(y - cy)
                    if shape == 'круг':
                        inside = dx * dx + dy * dy <= r * r
                    elif shape == 'кольцо':
                        inside = (r - 1) ** 2 <= dx * dx + dy * dy <= r * r
                    elif shape == 'ромб':
                        inside = dx + dy <= r
                    else:
                        inside = (dx == 0 and dy <= r) or (dy == 0 and dx <= r)
                    if inside:
                        quarter[y][x] = color
        rows = [''.join('.' if not quarter[min(y, SIZE - 1 - y)][min(x, SIZE - 1 - x)]
                        else MARKS[quarter[min(y, SIZE - 1 - y)][min(x, SIZE - 1 - x)] - 1] for x in range(SIZE))
                for y in range(SIZE)]
        total = sum(c != '.' for row in rows for c in row)
        best = (rows, total)
        if 600 <= total <= 1100:
            break
    rows, total = best
    # Stitches go in reading order: the first `stitched` marks are done.
    return dict(number=number, motif=motif, colors=colors, size=SIZE, rows=rows, total=total, style='крестом')


def embroidery_title(pattern):
    return f'Вышивка «{pattern["motif"]}»'


def stitches(n):
    return f'{n} {plural(n, "стежок", "стежка", "стежков")}'


# --- Restoration of an old recording: stages only (real audio is stage P3) ---------------------

STAGES = (('осмотр', 'прослушать запись целиком и отметить дефекты'),
          ('очистка', 'убрать щелчки и шип'),
          ('пропуски', 'восстановить выпавшие места'),
          ('выравнивание', 'выровнять громкость и тембр'),
          ('сверка', 'дослушать целиком и сверить с черновиком'))
MEDIA = ('магнитная лента', 'кассета', 'катушка', 'пластинка')
CONTENTS = ('вальс на фортепиано', 'скрипка соло', 'струнный квартет', 'этюд для гитары',
            'мелодия на челесте', 'дуэт флейты и арфы')


def restoration_record(number):
    rng = random.Random(int(unit('restoration', number) * 2 ** 32))
    gaps = rng.randint(1, 4)
    return dict(number=number, medium=rng.choice(MEDIA), content=rng.choice(CONTENTS),
                seconds=rng.randint(40, 200), clicks=rng.randint(12, 60), gaps=gaps,
                stage_steps=[1, rng.randint(1, 2), min(gaps, rng.randint(1, 2)), 1, 1])


# --- Tea: tastes formed from her real reading notes on tea ------------------------------------

# item: (category, pattern in a note or a summary, fixed palate or None for a stable hash)
TEA = {
    'бергамот': ('аромат', r'бергамот', .9),
    'мята': ('добавка', r'\bмят(?:а|ы|у|е|ой|н\w*)\b', .15),
    'чернослив': ('добавка', r'чернослив', .5),
    'жасмин': ('аромат', r'жасмин', None),
    'роза': ('аромат', r'лепестк\w* роз|\bроз(?:а|ы|у|ой)\b', None),
    'лаванда': ('аромат', r'лаванд', None),
    'чабрец': ('добавка', r'чабрец|тимьян', None),
    'имбирь': ('добавка', r'имбир', None),
    'корица': ('добавка', r'кориц', None),
    'кардамон': ('добавка', r'кардамон', None),
    'лимон': ('добавка', r'лимон', None),
    'молоко': ('добавка', r'\bмолок|\bмолоч', None),
    'мёд': ('добавка', r'\bм[её]д(?:а|ом|у)?\b', None),
    'пуэр': ('сорт', r'пу[эе]р', None),
    'улун': ('сорт', r'улун', None),
    'сенча': ('сорт', r'сенч', None),
    'генмайча': ('сорт', r'генмайч', None),
    'лапсанг': ('сорт', r'лапсанг|копч[её]н\w* ча', None),
    'белый чай': ('сорт', r'бел\w+ ча[йяею]', None),
    'зелёный чай': ('сорт', r'зел[её]н\w+ ча[йяею]', None),
    'ройбуш': ('сорт', r'ройбуш', None),
    'иван-чай': ('сорт', r'иван-ча', None),
    'каркаде': ('сорт', r'каркаде', None),
    'холодное заваривание': ('способ', r'холодн\w+ (?:заварив|способ)', None),
}
VERDICTS = {'да': 'да — мой вкус', 'нет': 'нет — не моё', 'спорно': 'спорно — пока не решила'}
FLIP_CHANCE = .25


def palate(item):
    fixed = TEA[item][2]
    score = unit('palate', item) if fixed is None else fixed
    return 'да' if score >= .62 else 'нет' if score <= .38 else 'спорно'


def next_verdict(item, previous):
    """First taste, a second try of «спорно», or (rarely) second thoughts about a firm verdict."""
    if previous is None:
        return palate(item)
    if previous['verdict'] == 'спорно':
        return 'да' if unit('palate-again', item, previous['version']) >= .5 else 'нет'
    return 'спорно' if unit('flip', item, previous['version']) < FLIP_CHANCE else None


# --- Plans: the deterministic step the model only puts into words -----------------------------

@dataclass
class Plan:
    row: dict
    step: int
    kind: str
    diff: dict
    facts: dict
    state: dict
    progress: float
    before: float
    gap: tuple = (4, 16)
    close: str = None
    follow: dict = None
    follow_after: tuple = (6, 20)
    event_on_follow: bool = False
    mention: tuple = ()
    taste: dict = None

    @property
    def step_key(self):
        return f'{self.row["id"]}:{self.step}'


def initial(kind, number=1):
    if kind == 'embroidery':
        pattern = embroidery_pattern(number)
        return dict(kind=kind, title=embroidery_title(pattern), state=dict(pattern=pattern, stitched=0, steps=0),
                    progress=0.)
    if kind == 'restoration':
        record = restoration_record(number)
        return dict(kind=kind, title=f'Запись № {number}',
                    state=dict(record=record, stage=0, stage_step=0, steps=0), progress=0.)
    return dict(kind='tea', title='Вкусы в чае', state=dict(last_note_id=0, steps=0, tasted=0), progress=0.)


def plan_embroidery(row):
    s = row['state']
    p, step, done = s['pattern'], s['steps'], s['stitched']
    total = p['total']
    threads = ', '.join(p['colors'])
    if ('replaced' not in s and .15 <= done / total <= .6
            and unit('embroidery-change', p['number'], step) < CHANGE_CHANCE):
        code = sorted(CHANGE_REASONS)[int(unit('embroidery-reason', p['number'], step) * len(CHANGE_REASONS))]
        new = embroidery_pattern(p['number'] + 1)
        old = dict(pursuit_id=row['id'], number=p['number'], motif=p['motif'], stitched=done, total=total)
        facts = {'занятие': 'вышивка крестом',
                 'событие': 'сменила узор: прежний отложила недошитым, он остаётся в истории',
                 'прежний узор': f'«{p["motif"]}», вышито {done} из {total} стежков',
                 'причина': CHANGE_REASONS[code],
                 'новый узор': f'«{new["motif"]}», {stitches(new["total"])}, нитки: {", ".join(new["colors"])}'}
        follow = dict(kind='embroidery', title=embroidery_title(new), progress=0.,
                      state=dict(pattern=new, stitched=0, steps=0, replaced=dict(old, reason=code)))
        diff = dict(reason=code, **{'from': old, 'to': dict(number=new['number'], motif=new['motif'], total=new['total'])})
        return Plan(row, step, 'pattern_change', diff, facts, dict(s, steps=step + 1, set_aside=code),
                    done / total, done / total, close='abandoned', follow=follow, follow_after=(4, 16),
                    event_on_follow=True)
    n = min(total - done, STITCHES[0] + int((STITCHES[1] - STITCHES[0] + 1) * unit('embroidery-n', p['number'], step)))
    stitched = done + n
    finished = stitched == total
    kind = 'finished' if finished else 'started' if done == 0 else 'step'
    facts = {'занятие': 'вышивка крестом', 'узор': f'«{p["motif"]}», нитки: {threads}',
             'событие': (f'начала узор и вышила первые {stitches(n)}' if done == 0 else f'вышила ещё {stitches(n)}'),
             'всего': f'{stitched} из {total} стежков', 'готово': f'{round(100 * stitched / total)}%'}
    if finished:
        facts['итог'] = 'закончила узор: последний стежок на месте'
    diff = dict(pattern=p['number'], stitches=n, stitched=[done, stitched], total=total)
    return Plan(row, step, kind, diff, facts, dict(s, stitched=stitched, steps=step + 1), stitched / total,
                done / total, close='finished' if finished else None,
                follow=initial('embroidery', p['number'] + 1) if finished else None)


def plan_restoration(row):
    s = row['state']
    r, stage = s['record'], s['stage']
    k, K = s['stage_step'] + 1, r['stage_steps'][stage]
    total, steps = sum(r['stage_steps']), s['steps'] + 1
    name = STAGES[stage][0]
    facts = {'занятие': 'реставрация старой записи',
             'запись': f'№ {r["number"]}: {r["medium"]}, {r["content"]}, {r["seconds"]} секунд',
             'этап': f'{name}, {stage + 1} из {len(STAGES)}', 'работа': f'шаг {steps} из {total}'}
    new = dict(s, steps=steps, stage_step=k)
    diff = dict(record=r['number'], stage=stage + 1, step=steps, total=total)
    if stage == 0:
        facts['событие'] = (f'прослушала запись целиком и отметила дефекты: {r["clicks"]} '
                            f'{plural(r["clicks"], "щелчок", "щелчка", "щелчков")} и {r["gaps"]} '
                            f'{plural(r["gaps"], "пропуск", "пропуска", "пропусков")}')
    elif stage in (1, 2):
        whole, one, few, many = ((r['clicks'], 'щелчок', 'щелчка', 'щелчков') if stage == 1
                                 else (r['gaps'], 'пропуск', 'пропуска', 'пропусков'))
        done = round(whole * k / K)
        n, left = done - round(whole * (k - 1) / K), whole - done
        verb = 'убрала' if stage == 1 else 'восстановила'
        facts['событие'] = f'{verb} {n} {plural(n, one, few, many)}' + (
            f', {many} не осталось' + (', шип стал тише' if stage == 1 else '') if not left else f', осталось {left}')
        diff['fixed'] = n
    elif stage == 3:
        facts['событие'] = 'выровняла громкость и тембр'
    else:
        facts['событие'] = 'дослушала запись целиком и сверила с черновиком'
    last = k == K and stage + 1 == len(STAGES)
    if k == K and not last:
        new.update(stage=stage + 1, stage_step=0)
        facts['итог'] = f'закончила этап «{name}»'
    if last:
        facts['итог'] = f'закончила реставрацию записи № {r["number"]}'
    kind = 'finished' if last else 'started' if s['steps'] == 0 else 'stage' if k == K else 'step'
    return Plan(row, s['steps'], kind, diff, facts, new, steps / total, s['steps'] / total, gap=(5, 18),
                close='finished' if last else None, follow_after=(20, 44),
                follow=initial('restoration', r['number'] + 1) if last else None)


async def plan_tea(conn, row, now):
    """The first unread tea note naming an item that brings a verdict; returns (plan, last_note_id)."""
    s = row['state']
    consumed = s.get('last_note_id', 0)
    notes = await conn.fetch('''SELECT id,title,url,gist FROM arti_reading_notes
        WHERE topic='чай' AND id>$1 AND created_at>$2 AND created_at<=$3 ORDER BY id LIMIT 20''',
        consumed, utc(now) - NOTE_WINDOW, utc(now))
    if not notes:
        return None, consumed
    current = {r['item']: dict(r) for r in await conn.fetch(
        'SELECT item,verdict,version FROM arti_tastes WHERE superseded_at IS NULL')}
    for note in notes:
        consumed = note['id']
        text = f'{note["title"]} {note["gist"]}'
        for item, (category, pattern, _) in TEA.items():
            if not re.search(pattern, text, re.I):
                continue
            previous = current.get(item)
            verdict = next_verdict(item, previous)
            if verdict is None:
                continue
            tasted = len(current) + (previous is None)
            facts = {'занятие': 'чай: выясняю, какие вкусы выбираю сама',
                     'прочитано по-настоящему': f'«{note["title"]}»',
                     'что': f'{item} ({category})',
                     'событие': 'заварила и попробовала впервые' if previous is None else 'попробовала ещё раз',
                     'вердикт': VERDICTS[verdict], 'распробовано': f'{tasted} из {len(TEA)} вкусов в списке'}
            if previous is not None:
                facts['прежний вердикт'] = VERDICTS[previous['verdict']]
            taste = dict(item=item, category=category, verdict=verdict, basis=note['title'][:300], source=note['url'],
                         source_note_id=note['id'], version=(previous['version'] + 1) if previous else 1)
            diff = dict(item=item, category=category, verdict=verdict, note_id=note['id'],
                        previous=previous['verdict'] if previous else None)
            plan = Plan(row, s['steps'], 'verdict' if previous is None else 'verdict_change', diff, facts,
                        dict(s, last_note_id=consumed, steps=s['steps'] + 1, tasted=tasted), tasted / len(TEA),
                        len(current) / len(TEA), gap=(8, 24), mention=(pattern,), taste=taste)
            return plan, consumed
    return None, consumed


# --- Validation of her words ------------------------------------------------------------------

def _rx(pattern):
    return re.compile(pattern, re.I)


BANNED = (
    ('pain', _rx(r'\bбол(?:ь|и|ит|ят|ело|ела|ью|ьно|ьна|ьны\w*)\b|\bно(?:ет|ют)\b|страда|\bмуч|измуч|'
                 r'\bкров(?:ь|и|ью)\b|\bранк|ожог|обожгл|\bплак|\bплач(?!евн)|сл[её]з|устал|вымота|без сил|'
                 r'сил нет|выбилась из сил|невыносим|пожалей|посочувств')),
    ('promise', _rx(r'пришл[юё]|вышл[юе]|подар(?:ю|им)\b|принес(?:у|ём)\b|передам|покажу|встреч|встрет|увидимся|'
                    r'приед|приду\b|загляну|посылк|бандерол|по почте|доставк|отправлю')),
    ('address', _rx(r'\b(?:ты|тебе|тебя|тобой|тобою|твой|твоя|твоё|твое|твои|твоего|твоей|твоим|твоих|'
                    r'вы|вам|вас|вами|ваш\w*)\b')),
    ('weather', _rx(r'дожд|\bснег|снеж|погод|\bгроз(?:а|ы|у|е|ой)?\b|ветер|\bветр|туман|мороз|солн[цеч]|'
                    r'\bжар(?:а|е|у|ы)?\b|\bхолодно\b|слякот|за окном|метел|вьюг|оттепел|\bлив(?:ень|ня|нем)\b')),
    ('news', _rx(r'новост|выборы|\bвойн|санкц|президент|правительств|министр|'
                 r'\bкурс\w* (?:доллар|рубл|валют|евро)|бирж|в интернете пиш|по телевизор|в газет')),
    ('place', _rx(r'магазин|\bрын(?:ок|ка|ке|ку)\b|\bкафе\b|ресторан|\bулиц|\bпарк(?:а|е|у|ом)?\b|\bметро\b|'
                  r'вокзал|аэропорт|поехала|съездила|ездила|ходила (?:в|на|по)\b|гуляла|прогулк|вышла из дома|'
                  r'в городе|за город|\bдач(?:а|е|у|и)\b|путешеств|\bстран(?:а|е|у|ы)\b')),
    ('date', _rx(r'январ|феврал|\bмарт(?:а|е)?\b|апрел|\bма(?:й|я)\b|\bиюн|\bиюл|август|сентябр|октябр|ноябр|'
                 r'декабр|понедельник|вторник|\bсред(?:а|у|ы)\b|четверг|пятниц|суббот|воскресень|вчера|завтра|'
                 r'на прошлой неделе|на днях|в прошлом году|нов(?:ый|ым|ого) год|рождеств|праздник')),
    ('person', _rx(r'александр|телем|создател|\bмам(?:а|е|у|ы|ой)\b|\bпап(?:а|е|у|ы|ой)\b|подруг|'
                   r'сосед(?:ка|и|у|ом)?\b|\bгост(?:и|ья|ей|ям)\b')),
    ('meta', _rx(r'\bии\b|нейросет|\bмодел(?:ь|и|ью)\b|промпт|в образе|персонаж')),
    ('sensitive', SENSITIVE),
)
LATIN = re.compile(r'[A-Za-z]{2,}|https?://|www\.')
# A claim of progress is allowed only when the step's facts carry the same claim.
CLAIMS = (_rx(r'(?<!почти )(?<!не )законч|доделал|заверш|дошила|довышив|последн\w* стеж'),
          _rx(r'смени|нов\w+ узор|друг\w+ узор|распуст|начала заново|бросила'),
          _rx(r'щелч|\bшип(?:ени|\b)'),
          _rx(r'пропуск|выпавш'),
          _rx(r'громкост|тембр|выровня'),
          _rx(r'свери|сверк'))
FRACTIONS = ((_rx(r'две трети'), .58, .76), (_rx(r'(?<!две )\bтрет(?:ь|и|ью)\b'), .25, .42),
             (_rx(r'половин'), .4, .65), (_rx(r'четверт'), .18, .32),
             (_rx(r'почти (?:вс[её]|готов|законч|дошила)'), .8, 1.))
NUMBER_WORDS = ((r'\bтр(?:и|[её]х|ем|ём|емя)\b', 3), (r'\bчетыр\w*', 4), (r'\bпят(?:ь|и|ью)\b', 5),
                (r'\bшест(?:ь|и|ью)\b', 6), (r'\bсем(?:ь|и|ью)\b', 7), (r'\bвос[еь]м(?:ь|и|ью)\b', 8),
                (r'\bдевят(?:ь|и|ью)\b', 9), (r'\bдесят(?:ь|и|ью)\b', 10), (r'\w+надцат\w*', -1),
                (r'\bдвадцат\w*', 20), (r'\bтридцат\w*', 30), (r'\bсорок\w*', 40), (r'\bпят[ьи]десят\w*', 50),
                (r'\bшест[ьи]десят\w*', 60), (r'\bсем[ьи]десят\w*', 70), (r'\bвос[еь]м[ьи]десят\w*', 80),
                (r'\bдевяност\w*', 90), (r'\bст(?:о|а)\b', 100), (r'\bдв(?:ести|ухсот)\b', 200),
                (r'\bтр(?:иста|[её]хсот)\b', 300), (r'тысяч', 1000), (r'полсотн', 50), (r'дюжин', 12),
                (r'десятк|сотн', -1))


def numbers_in(text):
    found = [int(n) for n in re.findall(r'\d+', text)]
    for pattern, value in NUMBER_WORDS:
        found += [value] * len(re.findall(pattern, text, re.I))
    return found


def proper_nouns(text):
    """Capitalised words that do not start a sentence."""
    found = []
    for sentence in re.split(r'(?<=[.!?…])\s+', text):
        words = re.findall(r'[А-ЯЁа-яё][А-ЯЁа-яё-]*', sentence)
        found += [w.casefold() for w in words[1:] if w[0].isupper()]
    return found


def validate(text, plan):
    """Her words for this step, or ValueError(category). Nothing beyond the step's facts."""
    raw = str(text or '').strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', raw, re.S)
    if fenced:
        raw = fenced.group(1)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('not_json') from None
    if (not isinstance(body, dict) or set(body) != {'summary', 'feeling'}
            or not all(isinstance(v, str) for v in body.values())):
        raise ValueError('invalid_shape')
    summary = ' '.join(body['summary'].split())
    feeling = ' '.join(body['feeling'].split()).strip(' .!')
    if not 20 <= len(summary) <= SUMMARY_MAX:
        raise ValueError('summary_length')
    if not 2 <= len(feeling) <= 40 or len(feeling.split()) > 4 or re.search(r'\d', feeling):
        raise ValueError('feeling_shape')
    both = f'{summary} {feeling}'
    for name, pattern in BANNED:
        if pattern.search(both):
            raise ValueError('banned_' + name)
    if LATIN.search(both):
        raise ValueError('banned_latin')
    facts = ' '.join(str(v) for v in plan.facts.values())
    if not set(numbers_in(both)) <= {int(n) for n in re.findall(r'\d+', facts)}:
        raise ValueError('invented_number')
    vocabulary = set(re.findall(r'[а-яё-]+', f'{facts} {plan.row["title"]}'.casefold()))
    if any(word not in vocabulary for word in proper_nouns(summary)):
        raise ValueError('invented_name')
    for claim in CLAIMS:
        if claim.search(summary) and not claim.search(facts):
            raise ValueError('invented_progress')
    for pattern, low, high in FRACTIONS:
        if pattern.search(summary) and not any(low <= p <= high for p in (plan.before, plan.progress)):
            raise ValueError('invented_progress')
    for _, pattern, _ in TEA.values():
        if re.search(pattern, summary, re.I) and not re.search(pattern, facts, re.I):
            raise ValueError('off_state')
    if not all(re.search(pattern, summary, re.I) for pattern in plan.mention):
        raise ValueError('off_state')
    if plan.taste:
        negative = re.search(r'\bне\s+(?:мо[йеёяи]|понрав|по вкусу|нрав|люблю)|разочаров|невкусн|противн', summary, re.I)
        positive = re.search(r'(?<!не )(?:понравил|нравится|\bмо[йёя]\b|влюбил|восторг)', summary, re.I)
        if (plan.taste['verdict'] == 'да' and negative) or (plan.taste['verdict'] == 'нет' and positive and not negative):
            raise ValueError('verdict_mismatch')
    return {'summary': summary, 'feeling': feeling}


async def write_with_model(pool, system, facts):
    """One structured call with ARTI_PURSUITS_MODEL (or the default model); only the step's facts go out."""
    from ai.providers.connections import resolve_connection
    from ai.providers.structured import SelectedModelClient
    from config import DEFAULT_MODEL

    async def resolver(selection):
        async with pool.acquire() as conn:
            return await resolve_connection(selection, conn=conn)

    client = SelectedModelClient(connection_resolver=resolver, timeout=60)
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(facts, ensure_ascii=False)}]
    try:
        response = await client.complete(os.getenv('ARTI_PURSUITS_MODEL') or DEFAULT_MODEL, messages, 1024, .8)
    finally:
        await client.close()
    if response.status_code != 200:
        raise PursuitUnavailable('provider_unavailable')
    return response.json()['choices'][0]['message']['content']


# --- The service --------------------------------------------------------------------------------

def _row(record):
    row = dict(record)
    row['state'] = object_value(row['state'])
    return row


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


async def _insert(conn, spec, now, next_at):
    return _row(await conn.fetchrow('''INSERT INTO arti_pursuits(kind,title,status,state,progress,next_step_at,started_at,updated_at)
        VALUES($1,$2,'active',$3::jsonb,$4,$5,$6,$6) RETURNING *''',
        spec['kind'], spec['title'], _dumps(spec['state']), spec['progress'], next_at, now))


class Pursuits:
    def __init__(self, runtime, write=None):
        self.runtime = runtime
        self.write = write or (lambda system, facts: write_with_model(self.runtime.pool, system, facts))

    async def tick(self, *, kind=None, manual=False, dry_run=False, use_model=True):
        """At most one step. Scheduled ticks keep her hours, the daily quota and next_step_at;
        a manual step ignores those but not the model budget. A dry run writes nothing but the
        budget entry of its model call."""
        if not manual and not enabled():
            return {'outcome': 'disabled'}
        now = utc(self.runtime.clock())
        async with self.runtime.pool.acquire() as conn:
            # One life across bot processes.
            if not dry_run and not await conn.fetchval(LOCK):
                return {'outcome': 'locked'}
            try:
                return await self._tick(conn, now, kind, manual, dry_run, use_model)
            finally:
                if not dry_run:
                    await conn.execute(UNLOCK)

    async def _tick(self, conn, now, kind, manual, dry_run, use_model):
        rows = {r['kind']: r for r in map(_row, await conn.fetch(
            "SELECT * FROM arti_pursuits WHERE status IN ('active','paused') ORDER BY id"))}
        for missing in (k for k in KINDS if k not in rows):
            number = await conn.fetchval('SELECT count(*) FROM arti_pursuits WHERE kind=$1', missing) + 1
            spec = initial(missing, number)
            at = next_time(now, 1, 6, 'start', missing, number)
            rows[missing] = (dict(spec, id=None, status='active', next_step_at=at) if dry_run
                             else await _insert(conn, spec, now, at))
        day = day_start(now)
        if not manual:
            local = now.astimezone(LIFE_TZ)
            if not WAKING[0] <= local.hour < WAKING[1]:
                return {'outcome': 'asleep'}
            steps = await conn.fetchval('SELECT count(*) FROM arti_pursuit_events WHERE created_at>=$1', day)
            if steps >= daily_quota(local.date()):
                return {'outcome': 'quota', 'steps_today': steps}
        calls = await conn.fetchval('SELECT COALESCE(sum(model_calls),0) FROM arti_pursuit_runs WHERE created_at>=$1', day)
        if use_model and calls >= MAX_CALLS_PER_DAY:
            return {'outcome': 'budget', 'model_calls_today': calls}
        due = sorted((r for r in rows.values() if r['status'] == 'active' and kind in (None, r['kind'])
                      and (manual or r['next_step_at'] is None or r['next_step_at'] <= now)),
                     key=lambda r: (r['next_step_at'] or now, r['kind']))
        plan = None
        for row in due:
            plan = await self.plan(conn, row, now, dry_run)
            if plan is not None:
                break
        if plan is None:
            return {'outcome': 'idle'}
        report = {'pursuit': plan.row['title'], 'kind': plan.kind, 'step_key': plan.step_key, 'facts': plan.facts}
        if not use_model:
            return dict(report, outcome='dry_run', model_calls=0)
        try:
            text = await self.write(SYSTEM, plan.facts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Category only: no provider payload in logs. The same step is retried later.
            return dict(report, outcome=await self._failed(conn, plan, now, 'failed', type(exc).__name__, dry_run))
        try:
            words = validate(text, plan)
        except ValueError as exc:
            return dict(report, outcome=await self._failed(conn, plan, now, 'invalid', str(exc), dry_run))
        if dry_run:
            await self._run(conn, plan, 'dry_run', None, now)
            return dict(report, outcome='dry_run', model_calls=1, **words)
        try:
            event_id = await self._store(conn, plan, words, now)
        except (Stale, asyncpg.UniqueViolationError):
            await self._run(conn, plan, 'stale', None, now)
            return dict(report, outcome='stale')
        return dict(report, outcome='stored', event_id=event_id, **words)

    async def plan(self, conn, row, now, dry_run=False):
        if row['kind'] == 'embroidery':
            return plan_embroidery(row)
        if row['kind'] == 'restoration':
            return plan_restoration(row)
        plan, consumed = await plan_tea(conn, row, now)
        if plan is None and not dry_run and row['id'] is not None:
            # Nothing to taste: remember the notes looked at and come back later.
            await conn.execute('''UPDATE arti_pursuits SET state=state||jsonb_build_object('last_note_id',$2::bigint),
                next_step_at=$3,updated_at=$4 WHERE id=$1 AND status='active' ''',
                row['id'], consumed, next_time(now, 8, 24, row['id'], row['state']['steps'], consumed), now)
        return plan

    async def _run(self, conn, plan, outcome, error, now):
        await conn.execute('''INSERT INTO arti_pursuit_runs(pursuit_id,step_key,outcome,error,model_calls,created_at)
            VALUES($1,$2,$3,$4,$5,$6)''', plan.row['id'], plan.step_key, outcome,
            (error or None) and str(error)[:80], 1, now)

    async def _failed(self, conn, plan, now, outcome, error, dry_run):
        logger.info('pursuit_step_%s: %s', outcome, error)
        await self._run(conn, plan, 'dry_run' if dry_run else outcome, error, now)
        if not dry_run and plan.row['id'] is not None:
            await conn.execute('''UPDATE arti_pursuits SET next_step_at=$2,updated_at=$3
                WHERE id=$1 AND status='active' ''', plan.row['id'], now + RETRY, now)
        return outcome

    async def _store(self, conn, plan, words, now):
        row = plan.row
        async with conn.transaction():
            current = await conn.fetchrow('SELECT status,state FROM arti_pursuits WHERE id=$1 FOR UPDATE', row['id'])
            if current is None or current['status'] != 'active' or object_value(current['state']).get('steps') != plan.step:
                raise Stale()
            if plan.close:
                await conn.execute('''UPDATE arti_pursuits SET status=$2,state=$3::jsonb,progress=$4,next_step_at=NULL,
                    updated_at=$5,finished_at=$5 WHERE id=$1''', row['id'], plan.close, _dumps(plan.state), plan.progress, now)
            else:
                await conn.execute('''UPDATE arti_pursuits SET state=$2::jsonb,progress=$3,next_step_at=$4,updated_at=$5
                    WHERE id=$1''', row['id'], _dumps(plan.state), plan.progress,
                    next_time(now, *plan.gap, row['id'], plan.step), now)
            target = row['id']
            if plan.follow:
                follow = await _insert(conn, plan.follow, now, next_time(now, *plan.follow_after, 'follow', row['id'], plan.step))
                if plan.event_on_follow:
                    target = follow['id']
            event_id = await conn.fetchval('''INSERT INTO arti_pursuit_events(pursuit_id,step_key,kind,summary,feeling,diff,created_at)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,$7) RETURNING id''', target, plan.step_key, plan.kind,
                words['summary'], words['feeling'], _dumps(plan.diff), now)
            if plan.taste:
                t = plan.taste
                previous = await conn.fetchval('''UPDATE arti_tastes SET superseded_at=$2
                    WHERE item=$1 AND superseded_at IS NULL RETURNING version''', t['item'], now)
                if (previous or 0) + 1 != t['version']:
                    raise Stale()
                await conn.execute('''INSERT INTO arti_tastes(item,category,verdict,reason,basis,source,source_note_id,
                    event_id,version,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)''',
                    t['item'], t['category'], t['verdict'], words['summary'], t['basis'], t['source'],
                    t['source_note_id'], event_id, t['version'], now)
            await self._run(conn, plan, 'stored', None, now)
        return event_id


async def pursuits_scheduler(runtime):
    service = Pursuits(runtime)
    while True:
        if enabled() and runtime is not None and runtime.mode != 'legacy':
            try:
                await service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('pursuits_cycle_failed')
        await asyncio.sleep(TICK_SECONDS)


# --- Prompt block -------------------------------------------------------------------------------

async def recent_life(pool, now, *, conn=None):
    if conn is None:
        async with pool.acquire() as conn:
            return await recent_life(pool, now, conn=conn)
    pursuits = [_row(r) for r in await conn.fetch(
        "SELECT * FROM arti_pursuits WHERE status IN ('active','paused') ORDER BY id")]
    if not pursuits:
        return {}
    events = await conn.fetch('''SELECT kind,summary,feeling,created_at,pursuit_kind,pursuit_title FROM (
        SELECT e.kind,e.summary,e.feeling,e.created_at,p.kind AS pursuit_kind,p.title AS pursuit_title,
               row_number() OVER (PARTITION BY p.kind ORDER BY e.created_at DESC,e.id DESC) AS n
        FROM arti_pursuit_events e JOIN arti_pursuits p ON p.id=e.pursuit_id WHERE e.created_at<=$1) x
        WHERE n=1''', utc(now))
    tastes = await conn.fetch('''SELECT t.item,t.verdict,t.version,
        (SELECT o.verdict FROM arti_tastes o WHERE o.item=t.item AND o.version=t.version-1) AS previous
        FROM arti_tastes t WHERE t.superseded_at IS NULL AND t.created_at<=$1
        ORDER BY t.created_at DESC,t.id DESC LIMIT 5''', utc(now))
    return {'pursuits': pursuits, 'events': {r['pursuit_kind']: dict(r) for r in events},
            'tastes': [dict(t) for t in tastes]}


def progress_words(p):
    for limit, words in ((0, 'ещё не начата'), (.15, 'только начала'), (.3, 'примерно четверть'),
                         (.45, 'около трети'), (.55, 'примерно половина'), (.7, 'больше половины'),
                         (.9, 'большая часть позади'), (.999, 'почти готово')):
        if p <= limit:
            return words
    return 'готово'


def describe(row, tastes):
    s = row['state']
    if row['kind'] == 'embroidery':
        p = s['pattern']
        text = f'{row["title"]}: {progress_words(s["stitched"] / p["total"])}, {s["stitched"]} из {p["total"]} стежков'
        if 'replaced' in s:
            text += f'; прежний узор «{s["replaced"]["motif"]}» отложен'
        return text
    if row['kind'] == 'restoration':
        r = s['record']
        stage = STAGES[s['stage']][0]
        where = 'ещё не начинала' if not s['steps'] else f'{progress_words(row["progress"])}, этап {s["stage"] + 1} из {len(STAGES)} — {stage}'
        return f'{row["title"]} ({r["medium"]}, {r["content"]}): {where}'
    verdicts = '; '.join(f'{t["item"]} — {t["verdict"]}' + (f' (было «{t["previous"]}»)' if t.get('previous') else '')
                         for t in tastes)
    return f'{row["title"]}: {verdicts}' if verdicts else f'{row["title"]}: пока ничего не решила'


def intention(row):
    s = row['state']
    if row['kind'] == 'embroidery':
        return 'начать узор' if not s['stitched'] else 'вышивать дальше'
    if row['kind'] == 'restoration':
        return STAGES[s['stage']][1]
    return 'попробовать то, о чём прочитаешь о чае'


def pursuits_block(life, now):
    """Prompt block: her in-character life, declared fiction; empty when there is none."""
    if not life or not life.get('pursuits'):
        return ''
    from cognition.memory_render import ago
    entries = []
    for row in life['pursuits']:
        event = life.get('events', {}).get(row['kind'])
        plain = f'• {describe(row, life.get("tastes", ()))}.'
        tail = ' Отложено.' if row['status'] == 'paused' else f' Намерение: {intention(row)}.'
        said = ''
        if event:
            when = (ago(event['created_at'], now) or 'сегодня').capitalize()
            where = '' if event['pursuit_title'] == row['title'] else f' ({event["pursuit_title"]})'
            said = f' {when}{where}: «{event["summary"]}» Чувство: {event["feeling"]}.'
        entries.append((event['created_at'] if event else None, plain, said, tail))
    entries.sort(key=lambda e: e[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    entries = entries[:3]

    def size(lines):
        # The budget is for the lines under the heading (~230 bytes of its own).
        return len('\n'.join(lines).encode())

    lines = [plain + tail for _, plain, _, tail in entries]
    # Her own words go in, most recent first, while the block stays within budget.
    for n, (_, plain, said, tail) in enumerate(entries):
        candidate = lines[:n] + [plain + said + tail] + lines[n + 1:]
        if said and size(candidate) <= BLOCK_BYTES:
            lines = candidate
    while len(lines) > 1 and size(lines) > BLOCK_BYTES:
        lines.pop()
    return '\n'.join([HEADING] + lines)


# --- Mini App room (P2): the same state for a reader --------------------------------------------

# The thread names belong to the chart; these colours only draw it.
THREAD_COLORS = {'синий': '#3b5b92', 'охра': '#c18a2e', 'бордо': '#8c1d2e', 'серо-зелёный': '#768c74',
                 'кремовый': '#d6c49c', 'графит': '#55555e', 'медный': '#b0643a', 'лавандовый': '#9686c0'}
# The prompt's tea intention speaks to her («прочитаешь»); the room speaks about her.
ROOM_TEA_INTENTION = 'попробовать то, о чём прочитает о чае'
ROOM_TASTES = 12


def room_pursuit(row, event):
    """One live pursuit as the room shows it: progress, its stage or stitches, last event, intention."""
    s = row['state']
    view = {'kind': row['kind'], 'title': row['title'], 'status': row['status'],
            'progress': round(float(row['progress']), 3),
            'intention': None if row['status'] == 'paused' else ROOM_TEA_INTENTION if row['kind'] == 'tea' else intention(row),
            'last_event': None}
    if event:
        view['last_event'] = {'kind': event['kind'], 'summary': event['summary'], 'feeling': event['feeling'],
                              'at': _iso(event['created_at']), 'title': event['pursuit_title']}
    if row['kind'] == 'embroidery':
        p = s['pattern']
        view['embroidery'] = {'motif': p['motif'], 'stitched': s['stitched'], 'total': p['total'], 'replaced': None}
        if 'replaced' in s:
            old = s['replaced']
            view['embroidery']['replaced'] = {'motif': old['motif'], 'stitched': old['stitched'], 'total': old['total'],
                                              'reason': CHANGE_REASONS.get(old.get('reason'))}
    elif row['kind'] == 'restoration':
        r, stage = s['record'], s['stage']
        view['restoration'] = {
            'record': {k: r[k] for k in ('number', 'medium', 'content', 'seconds')},
            'stage': stage + 1, 'step': s['steps'], 'steps': sum(r['stage_steps']),
            'stages': [{'name': name, 'goal': goal, 'state': 'done' if n < stage else 'current' if n == stage else 'next'}
                       for n, (name, goal) in enumerate(STAGES)]}
    else:
        view['tea'] = {'tasted': s.get('tasted', 0), 'known': len(TEA)}
    return view


def room_pattern(row):
    """The live embroidery chart, compact: palette, rows of marks, and how many are stitched.
    rows[y][x] is '.' or a mark 'a'..'d' = palette[0..3]; the first `stitched` marks in reading
    order (row by row, left to right) are done."""
    s, p = row['state'], row['state']['pattern']
    return {'number': p['number'], 'motif': p['motif'], 'size': p['size'], 'rows': p['rows'],
            'palette': [{'thread': c, 'color': THREAD_COLORS.get(c, '#8c1d2e')} for c in p['colors']],
            'stitched': s['stitched'], 'total': p['total']}


async def room(pool, now, *, conn=None):
    """Her live pursuits with their last events, the hoop and current tastes; JSON-ready."""
    if conn is None:
        async with pool.acquire() as conn:
            return await room(pool, now, conn=conn)
    life = await recent_life(pool, now, conn=conn)
    rows, events = life.get('pursuits', []), life.get('events', {})
    tastes = await conn.fetch('''SELECT t.item,t.category,t.verdict,t.reason,t.basis,t.source,t.created_at,
        (SELECT o.verdict FROM arti_tastes o WHERE o.item=t.item AND o.version=t.version-1) AS previous
        FROM arti_tastes t WHERE t.superseded_at IS NULL AND t.created_at<=$1
        ORDER BY t.created_at DESC,t.id DESC LIMIT $2''', utc(now), ROOM_TASTES)
    embroidery = next((r for r in rows if r['kind'] == 'embroidery'), None)
    return {'pursuits': [room_pursuit(r, events.get(r['kind'])) for r in sorted(rows, key=lambda r: KINDS.index(r['kind']))],
            'embroidery': room_pattern(embroidery) if embroidery else None,
            'tastes': [{'item': t['item'], 'category': t['category'], 'verdict': t['verdict'], 'reason': t['reason'],
                        'basis': t['basis'], 'source': t['source'], 'previous': t['previous'], 'at': _iso(t['created_at'])}
                       for t in tastes]}


# --- Admin --------------------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


async def status(pool, now):
    now = utc(now)
    day = day_start(now)
    async with pool.acquire() as conn:
        pursuits = await conn.fetch('''SELECT id,kind,title,status,progress,next_step_at,started_at,finished_at
            FROM arti_pursuits ORDER BY id DESC LIMIT 12''')
        events = await conn.fetch('''SELECT e.id,p.title,e.kind,e.summary,e.feeling,e.created_at FROM arti_pursuit_events e
            JOIN arti_pursuits p ON p.id=e.pursuit_id ORDER BY e.created_at DESC,e.id DESC LIMIT 6''')
        tastes = await conn.fetch('''SELECT item,category,verdict,version,created_at FROM arti_tastes
            WHERE superseded_at IS NULL ORDER BY item''')
        runs = await conn.fetch('''SELECT outcome,count(*) AS n,COALESCE(sum(model_calls),0) AS calls
            FROM arti_pursuit_runs WHERE created_at>=$1 GROUP BY outcome''', day)
        steps = await conn.fetchval('SELECT count(*) FROM arti_pursuit_events WHERE created_at>=$1', day)
    clean = lambda rows: [{k: _iso(v) for k, v in dict(r).items()} for r in rows]
    return dict(enabled=enabled(), model=os.getenv('ARTI_PURSUITS_MODEL') or 'default',
                today=dict(steps=steps, quota=daily_quota(now.astimezone(LIFE_TZ).date()),
                           model_calls=sum(r['calls'] for r in runs), max_model_calls=MAX_CALLS_PER_DAY,
                           runs={r['outcome']: r['n'] for r in runs}),
                pursuits=clean(pursuits), events=clean(events), tastes=clean(tastes))


async def set_paused(pool, paused, kind, now):
    """Pause freezes a pursuit as it is (still shown, «отложено»); resume lets it continue."""
    async with pool.acquire() as conn:
        rows = await conn.fetch('''UPDATE arti_pursuits SET status=$1,updated_at=$2,
            next_step_at=CASE WHEN $1='active' THEN GREATEST(COALESCE(next_step_at,$2),$2) ELSE next_step_at END
            WHERE status=$3 AND ($4::text IS NULL OR kind=$4) RETURNING kind,title,status''',
            'paused' if paused else 'active', utc(now), 'active' if paused else 'paused', kind)
    return {'changed': [dict(r) for r in rows]}
