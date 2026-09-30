"""Deterministic review findings. Suspicion proposes review, not replacement."""
from dataclasses import asdict
from artifacts.computation import FormulaEngine
from materials.types import MaterialError


def diagnose(dataset,*,formula_cells=None):
    findings=[]
    environment=formula_cells or {(dataset.name,c.address):c for c in dataset.cells}
    engine=FormulaEngine(environment)
    for cell in dataset.cells:
        if cell.row==dataset.policy.header_row: continue
        codes=list(cell.normalized.notes) if cell.normalized.kind in ('ambiguous','invalid','missing','error') else []
        if cell.quality in ('uncertain','unreadable'): codes.append('source_requires_review')
        if cell.metadata.get('hidden_row') or cell.metadata.get('hidden_column'): codes.append('hidden_source_cell')
        if cell.formula:
            try:
                result=engine.evaluate(dataset.name,cell.address)
                status=engine._cache_status(cell.cached_raw,result)
                if status!='matches_recomputed': codes.append('formula_cache_'+status)
            except MaterialError as exc: codes.append(exc.code)
        if codes:
            findings.append(dict(address=cell.address,raw=cell.raw,codes=tuple(dict.fromkeys(codes)),
                source=asdict(cell.source),actions=('inspect_source','set_column_policy','propose_correction'),replacement=None))
    return dict(dataset_id=dataset.id,coverage=dataset.coverage,columns=dataset.columns,findings=tuple(findings),
        limitations=dataset.limitations,corrections=dataset.corrections)
