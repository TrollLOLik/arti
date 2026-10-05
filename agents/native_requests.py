"""Immutable request provenance for the opt-in native material task route.

ACL access is necessary, but never broadens the sources selected for this request.
Legacy task plans intentionally keep their existing explicit-command semantics.
"""
import re
import unicodedata
from dataclasses import asdict
from hashlib import sha256
from materials.types import MaterialError, canonical
from materials.derivatives import DerivativeRepository


def request_id(actor, message_id):
    if message_id is None:
        raise MaterialError('native_request_identity_required')
    # Retain the existing native/menu task identity across upgrades.
    return sha256(f'natural-task:{actor.realm}:{message_id}'.encode()).hexdigest()[:32]


def direct_spans(goal):
    # Keep exclusion boundaries: words around a quote are never concatenated.
    quoted = (r'```.*?(?:```|$)|`[^`]*(?:`|$)|«[^»]*(?:»|$)|"[^"]*(?:"|$)'
              r'|“[^”]*(?:”|$)|„[^“”]*(?:[“”]|$)|‘[^’]*(?:’|$)'
              r"|(?<!\w)'[^']*(?:'|$)|(?m:^[ \t]*>[^\n]*$)")
    return re.split(quoted, goal, flags=re.S)



def direct_query(goal, query):
    """A canonical contiguous phrase in one unquoted human-request span."""
    def normalized(value):
        return ' '.join(unicodedata.normalize('NFC', value).casefold().split())
    needle = normalized(query)
    if len(needle)<2 or not any(c.isalnum() for c in needle):
        return False
    return any(re.search(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', normalized(span)) is not None
               for span in direct_spans(goal))


class NativeRequestRepository:
    def __init__(self, materials):
        self.materials = materials
        self.pool = materials.pool
        self.derivatives = DerivativeRepository(materials)

    async def get(self, id, actor):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM arti_native_agent_requests WHERE id=$1', id)
        if row is None:
            return None
        if row['realm'] != actor.realm or row['owner_id'] != actor.user_id:
            raise MaterialError('native_request_unavailable')
        from projects.repository import ProjectRepository
        project = await ProjectRepository(self.materials).get(row['project_id'], actor)
        project.require('edit')
        if project.access_generation != row['access_generation']:
            raise MaterialError('task_access_changed')
        body = await self.derivatives.load(row['binding_id'], actor, 'native_agent_request')
        return dict(row), body

    async def reserve(self, id, actor, project, goal, kind, refs, assets, *, inputs=(), workflows=()):
        prior = await self.get(id, actor)
        if prior:
            self.check_identity(prior[1], goal, kind)
            return prior
        body = dict(goal=goal, kind=kind, assets=assets,
                    allowed_input_derivatives=sorted(set(inputs)), workflows=list(workflows))
        binding = await self.derivatives.save(actor, 'native_agent_request', body,
            [*refs, *[dict(asset_id=a['asset_id'], asset_version=a['asset_version']) for a in assets]], inputs=inputs)
        async with self.pool.acquire() as conn, conn.transaction():
            await self.materials._locks(conn, actor)
            from projects.repository import ProjectRepository
            fresh = await ProjectRepository(self.materials)._get(conn, project.id, actor, lock=True)
            fresh.require('edit')
            if fresh.access_generation != project.access_generation:
                raise MaterialError('task_access_changed')
            await self.derivatives._sources(conn, actor, await self.derivatives._chain(conn, binding, actor))
            await conn.execute('''INSERT INTO arti_native_agent_requests
                (id,realm,owner_id,project_id,access_generation,binding_id)
                VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO NOTHING''',
                id, actor.realm, actor.user_id, project.id, project.access_generation, binding)
        result = await self.get(id, actor)
        self.check_identity(result[1], goal, kind)
        return result

    @staticmethod
    def check_identity(body, goal, kind):
        if body['goal'] != goal or body['kind'] != kind:
            raise MaterialError('native_request_identity_conflict')


class RequestScope:
    """Load only persisted authority; planner context is never an authority source."""
    def __init__(self, materials, actor, row, body):
        self.materials, self.actor, self.row, self.body = materials, actor, row, body
        self.assets = {a['asset_id']: a['asset_version'] for a in body['assets']}
        self.derivative_ids = set(body['allowed_input_derivatives']) | {row['binding_id']}
        self.artifact_ids = set()
        from ai.intents import _constraints
        direct=' '.join(direct_spans(body['goal']))
        self.network_denied = _constraints(body['goal'])['search'] or bool(re.search(
            r'(?i)\b(?:не (?:открывай|переходи|скачивай|загружай|читай)|do not (?:search|browse|open|fetch)|no internet)\b',direct))
        self.search_allowed=bool(re.search(
            r'(?i)(?:^|[.;!?\n:])\s*(?:пожалуйста[, ]+|please\s+)?(?:(?:найди|поищи|ищи|погугли|проверь|посмотри)\b.{0,200}(?:в интернете|в сети|сайты|источники)|search|look up|browse)\b',direct))
        self.urls = {url for span in direct_spans(body['goal'])
                     for url in re.findall(r'https?://[^\s<>"«»]+', span)
                     if re.search(r'(?i)(?:^|[.;!?\n:])\s*(?:пожалуйста[, ]+|please\s+)?(?:открой|прочитай|проверь|изучи|посмотри|загрузи|fetch|open|read|check)\b.{0,160}'+re.escape(url),span)}
        self.urls = {url.rstrip('.,;!?)]}') for url in self.urls}

    @classmethod
    async def for_task(cls, materials, actor, task):
        binding_id = task.get('native_request_id')
        if not binding_id:
            return None
        found = await NativeRequestRepository(materials).get(binding_id, actor)
        if not found:
            raise MaterialError('native_request_unavailable')
        row, body = found
        if row['project_id'] != task['project_id'] or row['id'] != task['id']:
            raise MaterialError('native_request_identity_conflict')
        scope = cls(materials, actor, row, body)
        derivatives = DerivativeRepository(materials)
        async with materials.pool.acquire() as conn:
            original = await derivatives._chain(conn, row['binding_id'], actor)
            scope.assets.update({r['asset_id']: r['asset_version'] for r in original})
            calls = await conn.fetch("SELECT * FROM arti_task_calls WHERE task_id=$1 AND status IN ('success','obsolete') AND output_id IS NOT NULL ORDER BY created_at,step_id,attempt", task['id'])
        original_plan = await derivatives.load(task['native_origin_plan_id'], actor, 'task_plan')
        for id in original_plan.get('inputs', []):
            async with materials.pool.acquire() as conn:
                refs = await derivatives._chain(conn, id, actor)
            await scope.validate_sources(refs)
            scope.derivative_ids.add(id)
        for call in calls:
            result = await derivatives.load(call['output_id'], actor, 'tool_output')
            if result['outcome']!='success': continue
            scope.derivative_ids.add(call['output_id'])
            output = result['outputs']
            tool = call['tool']
            if tool == 'research.search':
                scope.urls.update(x['url'] for x in output.get('results', []))
            if tool == 'research.fetch':
                for ref in result['evidence']:
                    scope.assets[ref['asset_id']] = ref['asset_version']
            if tool == 'dataset.extract':
                scope.derivative_ids.update(x['id'] for x in output['datasets'])
            if tool in ('dataset.compute', 'dataset.transform'):
                scope.derivative_ids.add(output['id'])
            if tool == 'artifact.create' or (tool == 'workflow.plan' and output.get('kind') == 'artifact'):
                scope.artifact_ids.add(output['id'])
            for key in ('derivative_id', 'illustration_id'):
                if key in output and tool in ('artifact.create', 'artifact.patch', 'media.image'):
                    scope.derivative_ids.add(output[key])
        return scope

    async def validate_sources(self, refs):
        for ref in refs:
            value = asdict(ref) if not isinstance(ref, dict) else ref
            if self.assets.get(value['asset_id']) != value['asset_version']:
                raise MaterialError('native_source_not_selected')

    async def validate_derivative(self, id):
        if id not in self.derivative_ids:
            raise MaterialError('native_derivative_not_selected')
        derivatives = DerivativeRepository(self.materials)
        async with self.materials.pool.acquire() as conn:
            refs = await derivatives._chain(conn, id, self.actor)
        await self.validate_sources(refs)

    async def validate_args(self, tool, args, *, planning=False):
        async def check(value):
            if isinstance(value, dict):
                if '$step' in value and planning:
                    return
                for key, item in value.items():
                    if planning and isinstance(item, dict) and '$step' in item:
                        continue
                    if key == 'asset_id':
                        if item not in self.assets or ('asset_version' in value and value['asset_version'] != self.assets[item]):
                            raise MaterialError('native_source_not_selected')
                    elif key == 'reference_assets':
                        if not isinstance(item,list): raise MaterialError('native_source_not_selected')
                        for asset in item:
                            if planning and isinstance(asset,dict) and '$step' in asset: continue
                            if asset not in self.assets: raise MaterialError('native_source_not_selected')
                    elif key == 'illustrations':
                        for derivative in item:
                            if planning and isinstance(derivative,dict) and '$step' in derivative: continue
                            await self.validate_derivative(derivative)
                    elif key in ('dataset_id', 'computation_id', 'derivative_id', 'observation_id'):
                        await self.validate_derivative(item)
                    await check(item)
            elif isinstance(value, list):
                for item in value:
                    await check(item)
        await check(args)
        if tool in ('research.search','research.fetch') and self.network_denied:
            raise MaterialError('native_network_not_authorized')
        if tool == 'research.search' and not self.search_allowed:
            raise MaterialError('native_network_not_authorized')
        if tool == 'research.search' and isinstance(args.get('query'), str):
            if not direct_query(self.body['goal'], args['query']):
                raise MaterialError('native_search_query_not_authorized')
        if tool == 'research.fetch' and isinstance(args.get('url'), str) and args['url'] not in self.urls:
            raise MaterialError('native_fetch_url_not_authorized')
        if tool in ('artifact.patch', 'artifact.export', 'documents.report'):
            value = args.get('artifact_id', args.get('id'))
            if not (planning and isinstance(value, dict) and '$step' in value) and value not in self.artifact_ids:
                raise MaterialError('native_artifact_not_selected')

    async def validate_plan(self, plan):
        for id in plan.value.get('inputs', []):
            await self.validate_derivative(id)
        for step in plan.value['steps']:
            if step.get('grant_id'):
                raise MaterialError('native_model_grant_denied')
            await self.validate_args(step['tool'], step['args'], planning=True)
