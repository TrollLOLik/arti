"""Bounded Decimal arithmetic and a closed spreadsheet formula grammar.

No eval, subprocess, network, macro, external workbook, named or volatile inputs.
Intervals describe supplied bounds; they are not statistical confidence intervals.
"""
from dataclasses import asdict,dataclass,field
from decimal import Decimal,DecimalException,localcontext,ROUND_HALF_UP,ROUND_FLOOR,ROUND_CEILING
from hashlib import sha256
import re
from materials.datasets import UNITS,decimal,decstr,unit_code,percent_format
from materials.types import MaterialError,canonical

ENGINE_VERSION='decimal-evidence-1'


def bound(operation,*,upper=False):
    with localcontext() as context:
        context.prec=50
        context.rounding=ROUND_CEILING if upper else ROUND_FLOOR
        return operation()


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    lower: Decimal
    upper: Decimal
    unit: str='1'
    blank: bool=False
    def __post_init__(self):
        unit_code(self.unit)
        for v in (self.value,self.lower,self.upper): decimal(v)
        if not self.lower<=self.value<=self.upper: raise MaterialError('invalid_result_interval')
    @classmethod
    def scalar(cls,value,unit='1'):
        value=decimal(value); return cls(value,value,value,unit)
    def scaled(self,factor,target):
        factor=decimal(factor)
        first,last=(self.lower,self.upper) if factor>=0 else (self.upper,self.lower)
        return Quantity(self.value*factor,bound(lambda:first*factor),bound(lambda:last*factor,upper=True),target)
    def to_dict(self): return dict(value=decstr(self.value),lower=decstr(self.lower),upper=decstr(self.upper),unit=self.unit)


def compatible(left,right):
    a,b=UNITS[left.unit],UNITS[right.unit]
    if a[0]!=b[0]: raise MaterialError('incompatible_units')
    return arithmetic('/',right.scaled(b[1],left.unit),Quantity.scalar(a[1]))


def arithmetic(operator,left,right):
    if operator in ('+','-'):
        right=compatible(left,right)
        if operator=='+': return Quantity(left.value+right.value,bound(lambda:left.lower+right.lower),bound(lambda:left.upper+right.upper,upper=True),left.unit)
        return Quantity(left.value-right.value,bound(lambda:left.lower-right.upper),bound(lambda:left.upper-right.lower,upper=True),left.unit)
    if operator=='*':
        if left.unit!='1' and right.unit!='1': raise MaterialError('compound_units_not_supported')
        unit=right.unit if left.unit=='1' else left.unit
        lows=[bound(lambda:a*b) for a in (left.lower,left.upper) for b in (right.lower,right.upper)]
        highs=[bound(lambda:a*b,upper=True) for a in (left.lower,left.upper) for b in (right.lower,right.upper)]
        return Quantity(left.value*right.value,min(lows),max(highs),unit)
    if operator=='/':
        if right.lower<=0<=right.upper: raise MaterialError('zero_or_uncertain_denominator')
        if right.unit=='1': unit=left.unit
        else: right=compatible(left,right); unit='1'
        lows=[bound(lambda:a/b) for a in (left.lower,left.upper) for b in (right.lower,right.upper)]
        highs=[bound(lambda:a/b,upper=True) for a in (left.lower,left.upper) for b in (right.lower,right.upper)]
        return Quantity(left.value/right.value,min(lows),max(highs),unit)
    raise MaterialError('unsupported_arithmetic')


class FormulaEngine:
    """Arithmetic, cell/range refs and SUM/AVERAGE/MIN/MAX/COUNT/ABS/ROUND.

    Sheets are quoted or ASCII names; union/3D/array/external/named references,
    dynamic arrays and unsupported functions are rejected. Every leaf is traced.
    """
    token=re.compile(r"\s*(?:(?P<ref>(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z_0-9]*)!)?(?P<cell>\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)(?::(?P<end>\$?[A-Za-z]{1,3}\$?[1-9][0-9]*))?|(?P<number>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol>[+\-*/(),%]))")
    def __init__(self,cells,*,allow_uncertain=False):
        self.cells=cells; self.allow_uncertain=allow_uncertain; self.memo={}; self.active=set(); self.dependencies={}; self.steps=[]; self.warnings=[]
        self.visits=0; self.tokens_used=0
    def evaluate(self,sheet,address):
        with localcontext() as context:
            context.prec=50
            try:
                result=self._cell(sheet,address)
                return self._present(self.cells[(sheet,address.replace('$','').upper())],result)
            except DecimalException as exc: raise MaterialError('arithmetic_precision_budget') from exc
    @staticmethod
    def _present(cell,result):
        if result.unit=='1' and (cell.normalized.unit=='percent' or percent_format(cell.metadata.get('number_format'))):
            return result.scaled(100,'percent')
        return result
    def _cell(self,sheet,address):
        key=(sheet,address.replace('$','').upper())
        if key in self.memo: return self.memo[key]
        if key in self.active: raise MaterialError('formula_cycle')
        self.visits+=1
        if self.visits>4000 or len(self.active)>=48: raise MaterialError('formula_budget')
        cell=self.cells.get(key)
        if cell is None: raise MaterialError('formula_reference_unavailable')
        self.dependencies[key]=cell
        if cell.quality in ('uncertain','unreadable') and not self.allow_uncertain: raise MaterialError('uncertain_input_requires_explicit_policy')
        if cell.quality in ('uncertain','unreadable'): self.warnings.append('uncertain_source_input')
        if cell.formula:
            self.active.add(key)
            try:
                result=self._formula(sheet,cell.formula)
                presented=self._present(cell,result)
                self.steps.append(dict(sheet=sheet,address=address,formula=cell.formula,result=presented.to_dict(),
                    cached_raw=cell.cached_raw,cache_status=self._cache_status(cell.cached_raw,presented)))
            finally: self.active.remove(key)
        elif cell.normalized.kind=='number':
            n=cell.normalized; result=Quantity(decimal(n.value),decimal(n.lower or n.value),decimal(n.upper or n.value),n.unit)
            # Excel references use the stored fraction, including references to a
            # formula with a percent display. Presentation happens at the boundary.
            if n.unit=='percent': result=result.scaled(Decimal('.01'),'1')
        elif cell.normalized.kind in ('missing','merged'):
            # Excel arithmetic blank semantics are explicit in the formula trace;
            # direct dataset aggregation has its own strict missing policy.
            if cell.normalized.kind=='merged': raise MaterialError('merged_cell_requires_anchor')
            self.warnings.append('excel_blank_reference_as_zero'); result=Quantity(Decimal(0),Decimal(0),Decimal(0),cell.normalized.unit,True)
        else: raise MaterialError('formula_non_numeric_input')
        self.memo[key]=result; return result
    @staticmethod
    def _cache_status(raw,result):
        if raw is None: return 'missing'
        expected=result.value*Decimal('.01') if result.unit=='percent' else result.value
        try: return 'matches_recomputed' if decimal(raw)==expected else 'stale'
        except MaterialError: return 'unverified_non_numeric_cache'
    def _formula(self,sheet,formula):
        formula=formula.rstrip()
        if not formula.startswith('=') or len(formula)>2048: raise MaterialError('formula_not_supported')
        tokens=[]; position=1
        while position<len(formula):
            match=self.token.match(formula,position)
            if not match: raise MaterialError('formula_not_supported')
            tokens.append(match.groupdict()); position=match.end()
            self.tokens_used+=1
            if len(tokens)>512 or self.tokens_used>100000: raise MaterialError('formula_budget')
        index=0
        def peek(symbol): return index<len(tokens) and tokens[index]['symbol']==symbol
        def take(symbol):
            nonlocal index
            if not peek(symbol): raise MaterialError('formula_syntax')
            index+=1
        def atom(depth=0):
            nonlocal index
            if depth>48 or index>=len(tokens): raise MaterialError('formula_syntax_or_budget')
            if peek('+') or peek('-'):
                sign=-1 if peek('-') else 1; index+=1
                return negate(atom(depth+1),sign)
            if peek('('):
                index+=1; value=expression(depth+1); take(')'); return value
            token=tokens[index]; index+=1
            if token['number']:
                result=Quantity.scalar(token['number'])
            elif token['cell']:
                target=(token['ref'] or '')[:-1] if token['ref'] else sheet
                if target.startswith("'"): target=target[1:-1].replace("''", "'")
                if token['end']:
                    from openpyxl.utils.cell import range_boundaries,get_column_letter
                    c0,r0,c1,r1=range_boundaries(token['cell'].replace('$','')+':'+token['end'].replace('$',''))
                    if c1<c0 or r1<r0 or (c1-c0+1)*(r1-r0+1)>4000: raise MaterialError('formula_range_budget')
                    result=[self._cell(target,get_column_letter(c)+str(r)) for r in range(r0,r1+1) for c in range(c0,c1+1)]
                else: result=self._cell(target,token['cell'])
            elif token['name']:
                name=token['name'].upper()
                if name not in ('SUM','AVERAGE','MIN','MAX','COUNT','ABS','ROUND'): raise MaterialError('formula_function_not_supported')
                take('('); args=[]
                if not peek(')'):
                    while True:
                        value=expression(depth+1); args.extend(value if isinstance(value,list) else [value])
                        if len(args)>4000: raise MaterialError('formula_arguments_budget')
                        if not peek(','): break
                        index+=1
                take(')'); result=self._function(name,args)
            else: raise MaterialError('formula_syntax')
            if peek('%'):
                index+=1
                if isinstance(result,list) or result.unit!='1': raise MaterialError('formula_percent_unit')
                result=result.scaled(Decimal('.01'),'1')
            return result
        def negate(value,sign):
            if isinstance(value,list): raise MaterialError('formula_range_in_arithmetic')
            return value.scaled(sign,value.unit)
        def product(depth):
            nonlocal index
            left=atom(depth)
            while peek('*') or peek('/'):
                operator=tokens[index]['symbol']; index+=1; right=atom(depth)
                if isinstance(left,list) or isinstance(right,list): raise MaterialError('formula_range_in_arithmetic')
                left=arithmetic(operator,left,right)
            return left
        def expression(depth=0):
            nonlocal index
            left=product(depth)
            while peek('+') or peek('-'):
                operator=tokens[index]['symbol']; index+=1; right=product(depth)
                if isinstance(left,list) or isinstance(right,list): raise MaterialError('formula_range_in_arithmetic')
                left=arithmetic(operator,left,right)
            return left
        result=expression()
        if index!=len(tokens) or isinstance(result,list): raise MaterialError('formula_syntax')
        return result
    @staticmethod
    def _function(name,args):
        if name=='COUNT': return Quantity.scalar(sum(not v.blank for v in args),'item')
        if name in ('SUM','AVERAGE','MIN','MAX'): args=[v for v in args if not v.blank]
        if not args:
            if name in ('SUM','MIN','MAX'): return Quantity.scalar(0)
            raise MaterialError('empty_formula_aggregate')
        if name in ('ABS','ROUND'):
            if name=='ABS':
                if len(args)!=1: raise MaterialError('formula_arity')
                value=args[0]; low=0 if value.lower<=0<=value.upper else min(abs(value.lower),abs(value.upper))
                return Quantity(abs(value.value),decimal(low),max(abs(value.lower),abs(value.upper)),value.unit)
            if len(args)!=2 or args[1].unit!='1' or args[1].value!=args[1].value.to_integral_value() or abs(args[1].value)>12:
                raise MaterialError('formula_round_digits')
            quantum=Decimal(1).scaleb(-int(args[1].value)); value=args[0]
            return Quantity(*(n.quantize(quantum,rounding=ROUND_HALF_UP) for n in (value.value,value.lower,value.upper)),value.unit)
        normalized=[args[0]]+[compatible(args[0],v) for v in args[1:]]
        if name in ('SUM','AVERAGE'):
            result=normalized[0]
            for value in normalized[1:]: result=arithmetic('+',result,value)
            return arithmetic('/',result,Quantity.scalar(len(args))) if name=='AVERAGE' else result
        operation=min if name=='MIN' else max
        return Quantity(operation(v.value for v in normalized),operation(v.lower for v in normalized),operation(v.upper for v in normalized),normalized[0].unit)


@dataclass(frozen=True)
class Conversion:
    from_unit: str
    to_unit: str
    factor: str
    source: dict  # EvidenceRef serialized; checked by service against the source cell.
    dataset_id: str | None=None
    lower: str | None=None
    upper: str | None=None
    def __post_init__(self):
        unit_code(self.from_unit); unit_code(self.to_unit)
        if not 0<decimal(self.lower or self.factor)<=decimal(self.factor)<=decimal(self.upper or self.factor) or not self.source: raise MaterialError('invalid_conversion')


@dataclass(frozen=True)
class ComputationSpec:
    operation: str
    selection: str
    reference: str | None=None
    missing: str='error'
    allow_uncertain: bool=False
    target_unit: str | None=None
    conversions: tuple[Conversion,...]=()
    tolerance: str='0'
    def __post_init__(self):
        if self.operation not in ('sum','mean','min','max','count','percent','change','ratio','difference','convert','compare','reconcile') or self.missing not in ('error','exclude') or decimal(self.tolerance)<0:
            raise MaterialError('invalid_computation_spec')
        if self.operation in ('percent','change','ratio','difference','compare','reconcile') and not self.reference: raise MaterialError('reference_selection_required')
        if self.operation=='convert' and not self.target_unit: raise MaterialError('target_unit_required')
        if self.target_unit: unit_code(self.target_unit)


@dataclass(frozen=True)
class ComputationResult:
    dataset_id: str
    spec: ComputationSpec
    result: dict
    inputs: tuple[dict,...]
    formula_steps: tuple[dict,...]
    warnings: tuple[str,...]
    ignored: tuple[dict,...]=()
    checks: tuple[dict,...]=()
    dependency_datasets: tuple[str,...]=()
    engine: str=ENGINE_VERSION
    precision: int=50
    interval_basis: str='supplied_bounds_and_directed_decimal_rounding'
    @property
    def id(self): return sha256(canonical(self.to_dict()).encode()).hexdigest()
    def to_dict(self): return asdict(self)


def compute(dataset,spec,*,formula_cells=None):
    with localcontext() as context:
        context.prec=50
        try: return _compute(dataset,spec,formula_cells=formula_cells)
        except DecimalException as exc: raise MaterialError('arithmetic_precision_budget') from exc


def _compute(dataset,spec,*,formula_cells=None):
    environment=formula_cells or {(dataset.name,c.address):c for c in dataset.cells}
    engine=FormulaEngine(environment,allow_uncertain=spec.allow_uncertain)
    inputs={}; ignored=[]; warnings=[]
    def convert(value,target):
        if value.unit==target: return value
        if UNITS[value.unit][0]==UNITS[target][0]: return arithmetic('/',value.scaled(UNITS[value.unit][1],target),Quantity.scalar(UNITS[target][1]))
        rate=next((r for r in spec.conversions if r.from_unit==value.unit and r.to_unit==target),None)
        if rate is None: raise MaterialError('conversion_source_required')
        warnings.append('external_conversion_rate')
        factor=Quantity(decimal(rate.factor),decimal(rate.lower or rate.factor),decimal(rate.upper or rate.factor))
        converted=arithmetic('*',value,factor)
        return Quantity(converted.value,converted.lower,converted.upper,target)
    def values(selection):
        result=[]
        for cell in dataset.select(selection):
            if cell.row==dataset.policy.header_row: raise MaterialError('selection_contains_header')
            if cell.normalized.kind=='missing' and not cell.formula:
                if spec.missing=='exclude': ignored.append(asdict(cell.source)); continue
                raise MaterialError('missing_input_requires_explicit_policy')
            if cell.quality in ('uncertain','unreadable') and not spec.allow_uncertain: raise MaterialError('uncertain_input_requires_explicit_policy')
            inputs[cell.address]=cell
            if cell.formula: value=engine.evaluate(dataset.name,cell.address)
            elif cell.normalized.kind=='number':
                n=cell.normalized; value=Quantity(decimal(n.value),decimal(n.lower or n.value),decimal(n.upper or n.value),n.unit)
            else: raise MaterialError('non_numeric_input')
            if cell.quality in ('uncertain','unreadable'): warnings.append('uncertain_source_input')
            result.append(value)
        if not result: raise MaterialError('empty_selection')
        target=spec.target_unit or result[0].unit
        return result if spec.operation=='count' else [convert(v,target) for v in result]
    def total(items):
        result=items[0]
        for value in items[1:]: result=arithmetic('+',result,value)
        return result
    lefts=values(spec.selection); left=lefts[0] if spec.operation in ('count','min','max') else total(lefts); checks=[]
    if spec.operation=='count': answer=Quantity.scalar(len(lefts),'item')
    elif spec.operation in ('sum','convert'): answer=left
    elif spec.operation=='mean': answer=arithmetic('/',left,Quantity.scalar(len(lefts)))
    elif spec.operation in ('min','max'): answer=FormulaEngine._function(spec.operation.upper(),lefts)
    else:
        right=total(values(spec.reference)); right=convert(right,left.unit)
        if spec.operation in ('difference','reconcile','compare'): answer=arithmetic('-',left,right)
        elif spec.operation=='ratio': answer=arithmetic('/',left,right)
        elif spec.operation=='percent': answer=arithmetic('/',left,right).scaled(100,'percent')
        else: answer=arithmetic('/',arithmetic('-',left,right),right).scaled(100,'percent')
        if spec.operation=='reconcile': checks.append(dict(kind='reconciliation',expected=right.to_dict(),actual=left.to_dict(),
            tolerance=decstr(spec.tolerance),passes=abs(left.value-right.value)<=decimal(spec.tolerance),
            intervals_overlap=not(left.upper<right.lower or right.upper<left.lower)))
        if spec.operation=='compare': checks.append(dict(kind='comparison',relation='greater' if left.lower>right.upper else ('less' if left.upper<right.lower else 'overlapping_intervals')))
    if len(lefts)>=4:
        ordered=sorted(v.value for v in lefts)
        # Tukey hinges, deterministic and labelled; flags propose review, no mutation.
        def median(xs):
            n=len(xs); return xs[n//2] if n%2 else (xs[n//2-1]+xs[n//2])/2
        half=len(ordered)//2; q1=median(ordered[:half]); q3=median(ordered[-half:]); iqr=q3-q1
        if iqr>0:
            outliers=[c.address for c,v in zip([c for c in dataset.select(spec.selection) if c.address in inputs],lefts) if v.value<q1-Decimal('1.5')*iqr or v.value>q3+Decimal('1.5')*iqr]
            checks.append(dict(kind='tukey_hinges_outliers',addresses=outliers,q1=decstr(q1),q3=decstr(q3)))
    all_inputs=list(inputs.values())+list(engine.dependencies.values())
    unique={canonical(asdict(c.source)):c for c in all_inputs}
    if dataset.coverage!='complete': warnings.append('partial_dataset_selection_only')
    warnings.extend(engine.warnings)
    if ignored: warnings.append('missing_inputs_excluded')
    return ComputationResult(dataset.id,spec,answer.to_dict(),tuple(dict(address=c.address,raw=c.raw,normalized=asdict(c.normalized),source=asdict(c.source)) for c in unique.values()),
        tuple(engine.steps),tuple(dict.fromkeys(warnings)),tuple(ignored),tuple(checks))
