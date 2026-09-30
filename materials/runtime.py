"""Optional Telegram intake and late access checks for derived responses."""
import contextvars
import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from materials.types import AccessContext, MaterialError, MaterialScope
from materials.repository import MaterialRepository
from materials.service import MaterialService
from materials.storage import LocalBlobStore
from materials.extractors.basic import render_text
from materials.extractors.documents import configured_extractor

CURRENT_MATERIAL_USE = contextvars.ContextVar('arti_material_use', default=())
CURRENT_COMPUTATION_USE = contextvars.ContextVar('arti_computation_use', default=())
logger = logging.getLogger(__name__)


class MaterialText(str):
    def __new__(cls, text, use):
        result = super().__new__(cls, text)
        result.material_uses = (use,)
        return result


@dataclass(frozen=True)
class MaterialUse:
    asset_id: str
    actor: AccessContext
    version: int
    generation: int
    service: MaterialService
    source_context_id: int | None = None
    source_event_id: int | None = None

    async def validate(self):
        row, _ = await self.service.repository.read(self.asset_id, self.actor, self.version)
        if row['generation'] != self.generation or row['current_version'] != self.version:
            raise MaterialError('stale_material_result')


@dataclass(frozen=True)
class ComputationUse:
    computation_id: str
    actor: AccessContext
    repository: object
    async def validate(self):
        await self.repository.load_computation(self.computation_id,self.actor)


def enabled():
    return os.getenv('ARTI_MATERIALS_ENABLED', '0').lower() in ('1', 'true', 'yes')


async def service_for_bot():
    from cognition.runtime import get_runtime
    from database import connection
    runtime = get_runtime()
    pool = runtime.pool if runtime else connection._pool
    if pool is None:
        raise MaterialError('materials_runtime_unavailable')
    from cognition.repositories import ensure_schema
    await ensure_schema(pool)
    return MaterialService(MaterialRepository(pool), LocalBlobStore(os.getenv('ARTI_MATERIALS_DIR', 'data/materials')))


async def actor_for_current():
    from cognition.scope import CURRENT_SCOPE
    from cognition.runtime import get_runtime
    from config import rp_mode_state
    scope = CURRENT_SCOPE.get()
    if scope is None:
        raise MaterialError('missing_scope')
    mode = 'rp' if rp_mode_state.get(scope.chat_id) else 'default'
    scene = ''
    if mode == 'rp':
        runtime = get_runtime()
        if runtime is None:
            raise MaterialError('unknown_scene')
        scene = (await runtime.context(scope.chat_id, mode)).scene_id
    return AccessContext(MaterialScope.from_transport(scope, mode, scene), scope.user_id, scope.sender_ref or '')


async def capture_document(context, document, message):
    actor = await actor_for_current()
    if message is None or message.chat_id != actor.scope.chat_id:
        raise MaterialError('unknown_document_origin')
    actual_topic = getattr(message, 'message_thread_id', None)
    if actor.scope.topic_id > 0 and actual_topic != actor.scope.topic_id:
        raise MaterialError('document_topic_mismatch')
    sender_chat = getattr(message, 'sender_chat', None)
    uploader = getattr(message, 'from_user', None)
    uid = None if sender_chat else getattr(uploader, 'id', None)
    sender_ref = 'chat:' + str(sender_chat.id) if sender_chat else 'user:' + str(uid)
    source_actor = AccessContext(actor.scope, uid, sender_ref)
    service = await service_for_bot()
    if document.file_size and document.file_size > service.repository.quotas.max_file_bytes:
        raise MaterialError('file_size_limit')
    file = await context.bot.get_file(document.file_id)
    if file.file_size and file.file_size > service.repository.quotas.max_file_bytes:
        raise MaterialError('file_size_limit')
    data = bytes(await file.download_as_bytearray())
    origin = 'system' if sender_chat else 'user'
    source = f'telegram:{message.chat_id}:{message.message_id}:{origin}'
    asset = await service.ingest(data, document.file_name or 'document', source_actor,
        source + ':document', source, getattr(document, 'mime_type', None))
    # Register the real original author as causal support for delivered actions.
    # The cognitive source is metadata/caption, not a copied extraction body.
    from cognition.runtime import get_runtime
    from cognition.scope import CURRENT_SCOPE
    from cognition.types import Origin
    from dataclasses import replace
    runtime = get_runtime()
    cid = event_id = None
    if runtime is not None and runtime.mode != 'legacy':
        original_scope = replace(CURRENT_SCOPE.get(),user_id=uid,sender_ref=sender_ref,
            sender_kind='chat' if sender_chat else 'user',message_id=message.message_id)
        token = CURRENT_SCOPE.set(original_scope)
        try:
            cid,event_id,_ = await runtime.ingest(actor.scope.chat_id,uid,
                getattr(message,'caption',None) or 'Материал: ' + (document.file_name or 'документ'),
                message.message_id,actor.scope.mode,origin=Origin.SYSTEM if sender_chat else Origin.USER)
        finally:
            CURRENT_SCOPE.reset(token)
    from materials.validation import inspect_bytes
    mime = await asyncio.to_thread(inspect_bytes,data,document.file_name or 'document',getattr(document,'mime_type',None))
    eid, bundle = await service.extract(asset['id'], actor, configured_extractor(mime))
    if not any(b.text.strip() for b in bundle.blocks):
        raise MaterialError('document_has_no_readable_text')
    use = MaterialUse(asset['id'], actor, bundle.asset_version, asset['generation'], service,cid,event_id)
    await use.validate()
    return MaterialText(render_text(bundle), use)


async def guard_current(destination=None):
    from cognition.scope import CURRENT_SCOPE
    scope = CURRENT_SCOPE.get()
    for use in CURRENT_MATERIAL_USE.get()+CURRENT_COMPUTATION_USE.get():
        if destination is not None and destination != use.actor.scope.chat_id:
            raise MaterialError('material_destination_mismatch')
        if scope is not None and (scope.chat_id != use.actor.scope.chat_id or scope.topic_id != use.actor.scope.topic_id):
            raise MaterialError('material_topic_mismatch')
        await use.validate()


def invalidate_pending(ids):
    """Drop retained plaintext immediately; in-flight uses fail their late guard."""
    import sys
    config = sys.modules.get('config')
    if config is None:
        return
    pending_doc_action = config.pending_doc_action
    for key, value in list(pending_doc_action.items()):
        if any(u.asset_id in ids for u in getattr(value.get('text'), 'material_uses', ())):
            dict.pop(pending_doc_action, key, None)
    queue_module = sys.modules.get('bot.queue')
    if queue_module is not None:
        for queue in queue_module._user_queues.values():
            for request in list(queue._queue):
                if any(u.asset_id in ids for u in request.get('_material_uses', ())):
                    request['document_text'] = None
                    request['_material_revoked'] = True


async def maintenance_worker():
    import asyncio
    from materials.lifecycle import MaterialLifecycle
    CURRENT_MATERIAL_USE.set(())
    while True:
        if enabled():
            service = await service_for_bot()
            report = await MaterialLifecycle(service.repository, service.store).collect()
            logger.info('Materials maintenance: expired=%d deleted_blobs=%d', report['expired'], report['deleted_blobs'])
        await asyncio.sleep(60)
