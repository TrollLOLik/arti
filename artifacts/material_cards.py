"""Deterministic material diagnostics, never a generated claim or proof.

These compact previews read persisted extraction/dataset/computation contracts.
They do not use ArtifactSpec's factual proof schema: coverage and findings are
pipeline metadata, not evidence that a document's assertions are true.
"""
from dataclasses import asdict, dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from artifacts.rendering import FONT, wrap
from artifacts.styles import semantic_theme
from materials.types import MaterialError

WIDTH = 1080
MAX_HEIGHT = 1440
COVERAGE = {'complete': 'Все заявленные единицы', 'partial': 'Частично',
            'unknown': 'Полнота неизвестна', 'failed': 'Извлечение не удалось'}
UNIT_LABELS = {'native_unit': 'единиц', 'page': 'страниц', 'body_node': 'узлов документа',
               'paragraph': 'абзацев', 'row': 'строк', 'cell': 'ячеек', 'image': 'изображений',
               'millisecond': 'мс', 'segment': 'сегментов', 'frame': 'кадров'}
WARNINGS = {'missing_inputs_excluded': 'Пропуски исключены',
            'uncertain_source_input': 'Входные значения требуют проверки',
            'partial_dataset_selection_only': 'Извлечение частичное; учтён только выбранный диапазон',
            'excel_blank_reference_as_zero': 'Формула Excel трактует пустую ссылку как ноль'}


@dataclass(frozen=True)
class MaterialCard:
    kind: str
    title: str
    subtitle: str
    metrics: tuple[tuple[str, str], ...]
    details: tuple[str, ...]
    caution: str
    source: str


def locator_text(locator):
    """Only supplied coordinates; never infer a page, timestamp or URL."""
    value = asdict(locator) if not isinstance(locator, dict) else locator
    if value.get('sheet') and value.get('cell'):
        return str(value['sheet']) + '!' + str(value['cell'])
    parts = []
    if value.get('page') is not None:
        parts.append('стр. ' + str(value['page']))
    if value.get('paragraph') is not None:
        parts.append('абзац ' + str(value['paragraph']))
    if value.get('start_ms') is not None:
        parts.append(f'{value["start_ms"]}–{value["end_ms"]} мс')
    if value.get('bbox') is not None:
        parts.append('область ' + ', '.join(str(v) for v in value['bbox']))
    return '; '.join(parts) or 'документ'


def extraction_card(bundle, filename='Документ'):
    manifest = bundle.manifest
    # "Complete" describes the declared extraction pass, never understanding.
    details = [f'Покрытие: {COVERAGE[manifest.coverage]}',
               'Подсчёт относится к единицам манифеста извлечения.']
    if manifest.limitations:
        details.append(f'Ограничений извлечения: {len(manifest.limitations)}. Перечень в тексте.')
    uncertain = sum(b.quality in ('uncertain', 'unreadable') for b in bundle.blocks)
    if uncertain:
        details.append(f'Блоков, требующих проверки: {uncertain}')
    return MaterialCard('ИЗВЛЕЧЕНИЕ', str(filename), 'Что доступно для проверки',
        ((str(manifest.processed_units), 'Обработано ' + UNIT_LABELS[manifest.unit_kind]),
         (str(manifest.total_units), 'Всего по манифесту'),
         (str(len(bundle.blocks)), 'Сохранено блоков')),
        tuple(details), 'Покрытие не подтверждает смысл, точность распознавания или качество модели.',
        f'Источник: {bundle.asset_id[:12]} · версия {bundle.asset_version}')


def dataset_card(datasets, reports):
    datasets, reports = tuple(datasets), tuple(reports)
    if not datasets or len(datasets) != len(reports):
        raise MaterialError('material_card_dataset_mismatch')
    if any(report['dataset_id'] != dataset.id for dataset, report in zip(datasets, reports)):
        raise MaterialError('material_card_dataset_mismatch')
    findings = sum(len(report['findings']) for report in reports)
    details = [f'{dataset.name}: {COVERAGE[dataset.coverage]}; замечаний {len(report["findings"])}'
               for dataset, report in zip(datasets, reports)]
    limitations = set(code for dataset in datasets for code in dataset.limitations)
    if limitations:
        details.insert(0, f'Ограничений извлечения: {len(limitations)}. Подробности в тексте.')
    return MaterialCard('ТАБЛИЦЫ / КАЧЕСТВО', datasets[0].name if len(datasets) == 1 else 'Обзор таблиц',
        'Диагностика извлечённых данных',
        ((str(len(datasets)), 'Таблиц'), (str(sum(len(d.cells) for d in datasets)), 'Ячеек, включая заголовки'),
         (str(findings), 'Ячеек с замечаниями')),
        tuple(details), 'Отсутствие замечаний не доказывает точность данных. Исправления требуют подтверждения.',
        'Снимки: ' + ', '.join(d.id[:12] for d in datasets))


def computation_card(dataset, result):
    if result.dataset_id != dataset.id:
        raise MaterialError('material_card_dataset_mismatch')
    value = result.result
    details = [f'Лист: {dataset.name}', f'Операция: {result.spec.operation} {result.spec.selection}']
    if result.spec.reference:
        details.append('Сравнение с: ' + result.spec.reference)
    if value['lower'] != value['upper']:
        details.append(f'Границы: {value["lower"]}…{value["upper"]} {value["unit"]}')
    details.extend(WARNINGS.get(w, 'Ограничение: ' + w) for w in result.warnings)
    for check in result.checks:
        if check['kind'] == 'reconciliation':
            details.append('Сверка номинального итога: ' + ('сходится' if check['passes'] else 'не сходится'))
    return MaterialCard('РАСЧЁТ', value['value'] + ' ' + value['unit'],
        'Детерминированный результат по выбранным ячейкам',
        ((str(len(result.inputs)), 'Входных ячеек'), (str(len(result.formula_steps)), 'Шагов формул'),
         (str(len(result.warnings)), 'Предупреждений расчёта')),
        tuple(details), 'Границы учитывают входные интервалы и округление. Это не вероятность и не оценка модели.',
        'Расчёт: ' + result.id[:12] + ' · источники и диапазоны в тексте')


def _lines(value, width, size, limit):
    lines = wrap(str(value), width, size)
    if len(lines) > limit:
        lines = lines[:limit]
        font = ImageFont.truetype(str(FONT), size)
        last = lines[-1]
        while last and font.getlength(last + '…') > width:
            last = last[:-1]
        lines[-1] = last + '…'
    return lines


def card_scene(card, style=None):
    """Measured single preview; overflow is disclosed, full values stay in text."""
    theme = semantic_theme(style)
    items = []
    def text(x, y, value, size=26, color='text', width=920, limit=4):
        lines = _lines(value, width, size, limit)
        for n, line in enumerate(lines):
            items.append(dict(kind='text', x=x, y=y+n*(size+10), text=line,
                              size=size, color=theme[color]))
        return y + len(lines)*(size+10)
    def panel(x, y, w, h, color='surface'):
        items.append(dict(kind='rect', x=x, y=y, w=w, h=h, color=theme[color]))
    y = text(64, 52, 'АРТИ / ' + card.kind, 22, 'heading', limit=1)
    y = text(64, y+20, card.title, 44, 'heading', limit=3)
    y = text(64, y+8, card.subtitle, 26, 'secondary', limit=2) + 24
    tile_width, tile_gap = 306, 17
    for n, (value, label) in enumerate(card.metrics):
        x = 64 + n*(tile_width+tile_gap)
        panel(x, y, tile_width, 180, 'soft_surface')
        text(x+20, y+22, value, 46, 'heading', tile_width-40, 1)
        text(x+20, y+91, label, 23, 'secondary', tile_width-40, 2)
    y += 210
    details = []
    for detail in card.details:
        details.extend(wrap(str(detail), 896, 26))
    available = max(2, min(9, int((MAX_HEIGHT-y-340)/36)))
    if len(details) > available:
        details = details[:available-1] + ['Продолжение и полные значения — в тексте.']
    h = len(details)*36 + 48
    panel(64, y, 952, h)
    for n, line in enumerate(details):
        text(88, y+24+n*36, line, 26, width=896, limit=1)
    y += h+24
    y = text(64, y, card.caution, 24, 'secondary', width=952, limit=3)+20
    y = text(64, y, card.source, 22, 'secondary', width=952, limit=2)+24
    height = max(760, y+24)
    if height > MAX_HEIGHT:
        raise MaterialError('material_card_overflow')
    for item in items:
        if item['kind'] != 'text':
            continue
        font = ImageFont.truetype(str(FONT), item['size'])
        l, t, r, b = font.getbbox(item['text'], anchor='lt')
        if item['x']+l < 0 or item['x']+r > WIDTH-32 or item['y']+b > height-20:
            raise MaterialError('material_card_overflow')
    return items, (WIDTH, height), theme


def render_card(card, style=None):
    items, size, theme = card_scene(card, style)
    image = Image.new('RGB', size, theme['background'])
    draw = ImageDraw.Draw(image)
    for item in items:
        if item['kind'] == 'rect':
            x, y = item['x'], item['y']
            draw.rounded_rectangle((x, y, x+item['w'], y+item['h']), radius=18, fill=item['color'])
        else:
            draw.text((item['x'], item['y']), item['text'], anchor='lt', fill=item['color'],
                      font=ImageFont.truetype(str(FONT), item['size']))
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
