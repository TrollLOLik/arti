"""Explicit, deterministic table actions in Telegram; no LLM action inference."""
import asyncio
import logging
import shlex
from artifacts.computation import ComputationSpec
from artifacts.material_cards import dataset_card, computation_card, WARNINGS
from bot.material_search import send_material_card, _bounded, bounded_report
from materials.datasets import DatasetPolicy,ColumnPolicy
from materials.dataset_repository import DatasetRepository
from materials.dataset_quality import diagnose
from materials.runtime import enabled,capture_document,actor_for_current,service_for_bot,CURRENT_MATERIAL_USE,CURRENT_COMPUTATION_USE,CURRENT_DERIVATIVE_USE,ComputationUse,DerivativeUse,guard_current
from materials.types import MaterialError
logger=logging.getLogger(__name__)

HELP='Ответь на CSV, XLSX, PDF или DOCX:\n/dataset — таблицы и качество\n/calc sum B2:B4 sheet=1\n/calc percent B2 reference=B2:B4 sheet=1\n/datafix B2 1300 sheet=1 — подтвердить исправление значения\nДоступны sum, mean, min, max, count, percent, change, ratio, difference, compare, reconcile, convert. Для неоднозначных данных: locale=ru/en/de, date_order=DMY/MDY; missing=exclude, uncertain=yes — явно принять эти ограничения.'
ERRORS={'missing_input_requires_explicit_policy':'Есть пропуски. Для расчёта с исключением пропусков укажи missing=exclude.',
    'uncertain_input_requires_explicit_policy':'Есть неуверенно распознанные ячейки. Проверь источник или явно укажи uncertain=yes.',
    'non_numeric_input':'В диапазоне есть текст, ошибка или неоднозначное значение. Посмотри /dataset и уточни локаль либо исправь ячейку.',
    'zero_or_uncertain_denominator':'Знаменатель равен нулю или его диапазон включает ноль.',
    'conversion_source_required':'Для преобразования нужны совместимые единицы или подтверждённый источник коэффициента.',
    'sheet_required':'Укажи sheet=номер или sheet="имя листа". /dataset покажет доступные таблицы.',
    'not_author':'Исправить данные может автор исходного материала.',
    'stale_dataset_head':'Данные уже изменились. Повтори команду для текущей версии.',
    'formula_cycle':'Обнаружен цикл формул.',
    'formula_function_not_supported':'Эта функция Excel пока не поддерживается для проверяемого расчёта; сохранённое значение не использовано.',
    'selection_contains_header':'Диапазон включает заголовок. Выбери строки с данными.',
    'table_extraction_disabled':'Работа с таблицами сейчас отключена.'}
QUALITY={'date_order_required':'неоднозначная дата; укажи порядок дня и месяца',
    'decimal_or_thousands':'неясен десятичный разделитель; укажи локаль',
    'missing_not_zero':'пропущенное значение', 'source_requires_review':'проверь распознавание по источнику',
    'formula_cache_stale':'сохранённый итог формулы устарел', 'formula_cache_missing':'формула вычислена заново; сохранённого итога нет',
    'formula_cycle':'цикл формул', 'hidden_source_cell':'ячейка скрыта в оригинале',
    'formula_reference_unavailable':'для формулы не хватает извлечённых ячеек',
    'formula_function_not_supported':'функция формулы не поддерживается для проверяемого расчёта',
    'locale_decimal_mismatch':'число не соответствует выбранной локали', 'invalid_date':'недопустимая дата'}
def quality_text(codes): return '; '.join(QUALITY.get(c,'значение требует проверки') for c in codes)
def source_text(item,dataset_name):
    locator=item['source']['locator']
    if locator.get('sheet'): return f'{locator["sheet"]}!{locator["cell"]}'
    if locator.get('page'): return f'стр. {locator["page"]}, ячейка {item["address"]}'
    return f'абзац {locator.get("paragraph", "?")}, ячейка {item["address"]}'


def parse(text):
    parts=shlex.split(text)[1:]; options={}; positional=[]
    allowed={'sheet','locale','date_order','missing','uncertain','unit','reference','tolerance'}
    for part in parts:
        if '=' in part:
            key,value=part.split('=',1)
            if key not in allowed or key in options: raise MaterialError('invalid_table_command')
            options[key]=value
        else: positional.append(part)
    if options.get('uncertain','no') not in ('yes','no'): raise MaterialError('invalid_table_command')
    return positional,options


def choose(snapshots,sheet):
    if sheet:
        if sheet.isdigit() and 1<=int(sheet)<=len(snapshots): return snapshots[int(sheet)-1]
        match=next((d for d in snapshots if d.name==sheet),None)
        if match: return match
    if len(snapshots)==1 and not sheet: return snapshots[0]
    raise MaterialError('sheet_required')


async def _run(update,context,action):
    message=update.effective_message
    if not enabled():
        await message.reply_text('Сохраняемые расчёты сейчас отключены.'); return
    original=getattr(message,'reply_to_message',None)
    document=getattr(original,'document',None)
    if document is None:
        await message.reply_text(HELP); return
    token=computation_token=derivative_token=None
    try:
        positional,options=parse(message.text)
        material=await capture_document(context,document,original)
        token=CURRENT_MATERIAL_USE.set(material.material_uses)
        actor=await actor_for_current(); service=await service_for_bot()
        snapshots=await service.datasets(material.material_uses[0].asset_id,actor)
        if not snapshots: raise MaterialError('no_tables_found')
        if 'locale' in options or 'date_order' in options:
            maxcol=max(c.column for d in snapshots for c in d.cells)
            policies=[]
            for c in range(maxcol+1):
                # Keep source header units while changing parse locale/date order.
                policies.append(ColumnPolicy(c,locale=options.get('locale','unknown'),date_order=options.get('date_order','unknown')))
            snapshots=await service.datasets(material.material_uses[0].asset_id,actor,policy=DatasetPolicy(columns=tuple(policies)))
        derivative_repository=DatasetRepository(service.repository)
        derivative_token=CURRENT_DERIVATIVE_USE.set(tuple(DerivativeUse(d.id,actor,derivative_repository,'dataset') for d in snapshots))
        # Findings can become stale after a correction even when the original
        # material is unchanged. Recheck every formula environment snapshot.
        async def validate_datasets():
            for snapshot in snapshots:
                await derivative_repository.load_dataset(snapshot.id,actor)
        card=None; footer=''
        if action=='dataset' and not options.get('sheet'):
            lines=['Таблицы:']
            reports=[]
            for i,dataset in enumerate(snapshots,1):
                report=await asyncio.to_thread(diagnose,dataset,formula_cells={(d.name,c.address):c for d in snapshots for c in d.cells})
                reports.append(report)
                lines.append(f'{i}. {dataset.name}: {len(dataset.cells)} ячеек; покрытие {dataset.coverage}; замечаний {len(report["findings"])}')
                for finding in report['findings'][:4]: lines.append(f'  {finding["address"]}: '+quality_text(finding['codes']))
            output='\n'.join(lines)+'\nДля конкретного листа: sheet=номер.'
            card=dataset_card(snapshots,reports)
        else:
            dataset=choose(snapshots,options.get('sheet'))
            if action=='dataset':
                report=await asyncio.to_thread(diagnose,dataset,formula_cells={(d.name,c.address):c for d in snapshots for c in d.cells})
                output=f'{dataset.name}; покрытие {dataset.coverage}\n'+'\n'.join(f'{c["index"]+1}. {c["name"]}: {c["dtype"]}; единицы {", ".join(c["units"])}' for c in dataset.columns)
                output+='\nЗамечания:\n'+'\n'.join(f'{f["address"]}: '+quality_text(f['codes']) for f in report['findings'][:15])
                if not report['findings']: output+='Не обнаружены в извлечённых ячейках; это не гарантия точности.'
                card=dataset_card((dataset,),(report,))
            elif action=='calc':
                if len(positional)!=2: raise MaterialError('invalid_table_command')
                spec=ComputationSpec(positional[0],positional[1].upper(),reference=options.get('reference','').upper() or None,
                    missing=options.get('missing','error'),allow_uncertain=options.get('uncertain')=='yes',
                    target_unit=options.get('unit'),tolerance=options.get('tolerance','0'))
                result=await service.compute(dataset.id,actor,spec,formula_dataset_ids=tuple(d.id for d in snapshots))
                await DatasetRepository(service.repository).load_computation(result.id,actor)
                computation_token=CURRENT_COMPUTATION_USE.set((ComputationUse(result.id,actor,DatasetRepository(service.repository)),))
                r=result.result
                output=f'{spec.operation} {spec.selection}: {r["value"]} {r["unit"]}\nЛист: {dataset.name}'
                if r['lower']!=r['upper']: output+=f'\nДиапазон: {r["lower"]}…{r["upper"]}; учитывает границы входов и округление, это не вероятность.'
                output+='\nИсточники: '+', '.join(source_text(i,dataset.name) for i in result.inputs[:12])
                if result.formula_steps: output+='\nФормулы: '+', '.join(f'{s["address"]}: '+{'stale':'сохранённый итог устарел','missing':'рассчитано заново','matches_recomputed':'итог подтверждён повторным расчётом'}.get(s['cache_status'],'сохранённый итог не подтверждён') for s in result.formula_steps)
                for check in result.checks:
                    if check['kind']=='reconciliation': output+='\nСверка номинального итога: '+('сходится' if check['passes'] else 'не сходится')
                    if check['kind']=='tukey_hinges_outliers' and check['addresses']:
                        output+='\nПроверь возможные выбросы: '+', '.join(check['addresses'])+'. Исходные значения сохранены.'
                card=computation_card(dataset,result)
                footer='\nРасчёт: '+result.id[:12]
                if result.warnings: footer+='\nОграничения расчёта: '+_bounded('; '.join(WARNINGS.get(w,w) for w in result.warnings),600)
                footer+='\nОригинал: '+_bounded(document.file_name or 'документ',180)+'; документ над командой.'
            else:
                if len(positional)!=2: raise MaterialError('invalid_table_command')
                proposal=await service.propose_correction(dataset.id,actor,positional[0].upper(),positional[1])
                corrected=await service.confirm_correction(actor,proposal)
                output=f'{positional[0].upper()}: принято значение {positional[1]}. Исходная ячейка сохранена; зависимые расчёты отозваны.'
                await DatasetRepository(service.repository).load_dataset(corrected.id,actor)
                # The correction intentionally invalidated the old snapshot.
                CURRENT_DERIVATIVE_USE.reset(derivative_token); derivative_token=None
        if action=='dataset':
            limitations=tuple(dict.fromkeys(code for d in snapshots for code in d.limitations))
            if limitations: footer+='\nОграничения извлечения ('+str(len(limitations))+'): '+_bounded('; '.join(limitations),600)
            footer+='\nИсточники: '+_bounded(', '.join(d.name+' / '+d.id[:12] for d in snapshots),500)
            footer+='\nОригинал — документ, на который отвечает команда. Заголовки включены в число ячеек.'
        await send_material_card(message,context,card,bounded_report(output,footer),
            validate=validate_datasets if action!='fix' else None)
    except (ValueError,MaterialError) as exc:
        code=getattr(exc,'code','invalid_table_command')
        logger.info('Table command refused: %s',code)
        if computation_token is not None:
            CURRENT_COMPUTATION_USE.reset(computation_token); computation_token=None
        if derivative_token is not None:
            CURRENT_DERIVATIVE_USE.reset(derivative_token); derivative_token=None
        if token is not None:
            CURRENT_MATERIAL_USE.reset(token); token=None
        await message.reply_text(ERRORS.get(code,'Не удалось выполнить проверяемую операцию. Проверь диапазон, доступ к источнику и данные.\n'+HELP[:700]))
    finally:
        if token is not None: CURRENT_MATERIAL_USE.reset(token)
        if computation_token is not None: CURRENT_COMPUTATION_USE.reset(computation_token)
        if derivative_token is not None: CURRENT_DERIVATIVE_USE.reset(derivative_token)


async def dataset_command(update,context): await _run(update,context,'dataset')
async def calc_command(update,context): await _run(update,context,'calc')
async def datafix_command(update,context): await _run(update,context,'fix')
