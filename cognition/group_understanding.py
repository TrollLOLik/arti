"""Pure, bounded validation of source-grounded public conversation hypotheses.

This schema conveys interpretation, never consent, permission or group consensus.
Every extraction is reconstructed from the exact raw messages of one model call;
previous model summaries are deliberately not accepted as input. Storage remains
responsible for public scope, raw-source lineage and freshness/epoch fencing.
"""
from datetime import datetime
import json
import math


VERSION = 'public-conversation-v1'
MAX_MESSAGES = 72
MAX_INPUT_BYTES = 48000
MAX_TEXT_CHARS = 2400
MAX_OUTPUT_BYTES = 12000
MAX_THREADS = 12
MAX_LINKS = 32
MAX_ITEMS = 24
MAX_EVIDENCE = 8
MAX_UPDATES = 8
MIN_CONFIDENCE = .75

MESSAGE_FIELDS = {'source_id', 'message_id', 'owner_id', 'sender_kind', 'directed',
                  'reply_to_id', 'at', 'text', 'text_truncated'}
ATTRIBUTIONS = {'speaker', 'reported', 'unknown'}
RELATIONS = {'continuation', 'question', 'answer', 'proposal', 'acceptance',
             'decline', 'correction', 'reopen', 'unknown'}
STATUSES = {
    'question': {'open', 'rhetorical', 'resolved', 'declined', 'reopened', 'unknown'},
    'proposal': {'proposed', 'accepted', 'declined', 'superseded', 'reopened', 'unknown'},
    'decision': {'proposed', 'accepted', 'declined', 'superseded', 'reopened', 'unknown'},
    'commitment': {'proposed', 'accepted', 'fulfilled', 'declined', 'cancelled', 'reopened', 'unknown'},
}
_INITIAL = {
    'question': {'open', 'rhetorical', 'unknown'},
    'proposal': {'proposed', 'unknown'},
    'decision': {'proposed', 'accepted', 'unknown'},
    'commitment': {'proposed', 'accepted', 'unknown'},
}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid_understanding_' + name + '_schema')


def _text(value, limit, name):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('invalid_understanding_' + name)
    return value


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('invalid_understanding_confidence')
    return float(value)


def _enum(value, values, name):
    if not isinstance(value, str) or value not in values:
        raise ValueError('invalid_understanding_' + name)
    return value


def _integer(value, name, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError('invalid_understanding_' + name)
    return value


def _array(value, limit, name, nonempty=False):
    if not isinstance(value, list) or len(value) > limit or (nonempty and not value):
        raise ValueError('invalid_understanding_' + name)
    return value


def normalize_messages(messages):
    """Copy a bounded raw-only packet, ordered by actual source chronology.

    No shortening, coercion or normalization of text is allowed: code-point
    offsets and quotes must refer to exactly the bytes selected by the caller.
    Unsupported fields (including old summaries) fail before any provider call.
    """
    _array(messages, MAX_MESSAGES, 'messages')
    result, sources, message_ids = [], set(), set()
    for message in messages:
        _object(message, MESSAGE_FIELDS, 'message')
        source = _text(message['source_id'], 192, 'source_id')
        mid = _integer(message['message_id'], 'message_id')
        owner = _integer(message['owner_id'], 'owner_id', nullable=True)
        _integer(message['reply_to_id'], 'reply_to_id', nullable=True)
        if source in sources or mid in message_ids:
            raise ValueError('duplicate_understanding_source')
        _enum(message['sender_kind'], ('user', 'bot', 'chat'), 'sender_kind')
        if message['sender_kind'] == 'user' and owner is None:
            raise ValueError('missing_understanding_author')
        if message['sender_kind'] == 'chat' and owner is not None:
            raise ValueError('invalid_understanding_chat_author')
        if type(message['directed']) is not bool or type(message['text_truncated']) is not bool:
            raise ValueError('invalid_understanding_message_flag')
        text = message['text']
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or '\x00' in text:
            raise ValueError('invalid_understanding_message_text')
        at = _text(message['at'], 48, 'message_time')
        try:
            date = datetime.fromisoformat(at)
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError()
        except (ValueError, OverflowError):
            raise ValueError('invalid_understanding_message_time') from None
        sources.add(source)
        message_ids.add(mid)
        result.append((date, mid, dict(message)))
    result = [message for _, _, message in sorted(result, key=lambda row: (row[0], row[1]))]
    if _size(result) > MAX_INPUT_BYTES:
        raise ValueError('understanding_input_too_large')
    return result


def _source(value, by_source):
    if not isinstance(value, str) or value not in by_source:
        raise ValueError('unsupported_understanding_source')
    return value


def _actor(value, actors):
    if value is not None and (type(value) is not int or value not in actors):
        raise ValueError('unsupported_understanding_actor')
    return value


def _evidence(value, by_source, *, required=(), only=None):
    spans, seen = [], set()
    for span in _array(value, MAX_EVIDENCE, 'evidence', nonempty=True):
        _object(span, {'source_id', 'start', 'end', 'quote'}, 'span')
        source = _source(span['source_id'], by_source)
        start, end, quote = span['start'], span['end'], span['quote']
        if (type(start) is not int or type(end) is not int or not isinstance(quote, str)
                or not quote.strip() or not 0 <= start < end <= len(by_source[source]['text'])
                or by_source[source]['text'][start:end] != quote):
            raise ValueError('unsupported_understanding_quote')
        if only is not None and source != only:
            raise ValueError('misattributed_understanding_evidence')
        key = (source, start, end)
        if key in seen:
            raise ValueError('duplicate_understanding_evidence')
        seen.add(key)
        spans.append(dict(span))
    if not set(required) <= {span['source_id'] for span in spans}:
        raise ValueError('missing_understanding_evidence')
    return spans


def _speaker_attribution(raw, message, actors):
    actor = _actor(raw['actor_id'], actors)
    attribution = _enum(raw['attribution'], ATTRIBUTIONS, 'attribution')
    # A source author may report another person's words but cannot commit them.
    # Channel/anonymous and bot text can never establish a human's own outcome.
    own = (attribution == 'speaker' and actor is not None
           and message['sender_kind'] == 'user' and message['owner_id'] == actor)
    if attribution == 'speaker' and not own:
        attribution = 'unknown'
    if attribution == 'unknown':
        actor = None
    return actor, attribution, own


def _sources(evidence, *anchors):
    return sorted({*anchors, *(span['source_id'] for span in evidence)} - {None})


def parse_understanding(data, messages):
    """Reject invented structure/support; conservatively demote weak outcomes.

    The parser cannot prove a paraphrase's semantic accuracy. Confidence, labels,
    addressees and relations remain model hypotheses, even after strict source
    validation. Only source authors can establish their own status; nobody's
    silence, a bot reply or another participant's answer establishes resolution.
    """
    messages = normalize_messages(messages)
    by_source = {message['source_id']: message for message in messages}
    order = {message['source_id']: index for index, message in enumerate(messages)}
    actors = {message['owner_id'] for message in messages
              if message['sender_kind'] == 'user' and message['owner_id'] is not None}
    _object(data, {'threads', 'links', 'items'}, 'root')
    result = {'threads': [], 'links': [], 'items': []}
    threads = {}
    for raw in _array(data['threads'], MAX_THREADS, 'threads'):
        _object(raw, {'thread_id', 'label', 'confidence', 'evidence'}, 'thread')
        tid = _source(raw['thread_id'], by_source)
        if tid in threads:
            raise ValueError('duplicate_understanding_thread')
        evidence = _evidence(raw['evidence'], by_source, required=(tid,))
        thread = dict(thread_id=tid, label=_text(raw['label'], 96, 'thread_label'),
                      confidence=_score(raw['confidence']), evidence=evidence,
                      source_ids=_sources(evidence, tid))
        result['threads'].append(thread)
        threads[tid] = thread

    def thread_id(value, nullable=False):
        if nullable and value is None:
            return None
        if not isinstance(value, str) or value not in threads:
            raise ValueError('unsupported_understanding_thread')
        return value

    links = set()
    for raw in _array(data['links'], MAX_LINKS, 'links'):
        _object(raw, {'source_id', 'target_source_id', 'thread_id', 'relation',
                      'addressee_ids', 'confidence', 'evidence'}, 'link')
        source = _source(raw['source_id'], by_source)
        target = _source(raw['target_source_id'], by_source) if raw['target_source_id'] is not None else None
        tid = thread_id(raw['thread_id'], nullable=True)
        if tid is not None and order[tid] > order[source]:
            raise ValueError('noncausal_understanding_thread')
        if target is not None and order[target] >= order[source]:
            raise ValueError('noncausal_understanding_link')
        relation = _enum(raw['relation'], RELATIONS, 'relation')
        confidence = _score(raw['confidence'])
        addressees = _array(raw['addressee_ids'], 8, 'addressees')
        for actor in addressees:
            if _actor(actor, actors) is None:
                raise ValueError('unsupported_understanding_actor')
            if not any(message['owner_id'] == actor and message['sender_kind'] == 'user'
                       and order[message['source_id']] < order[source] for message in messages):
                raise ValueError('unobserved_understanding_addressee')
        if len(set(addressees)) != len(addressees):
            raise ValueError('duplicate_understanding_addressee')
        evidence = _evidence(raw['evidence'], by_source, required=(source, target) if target else (source,))
        cited_actors = {by_source[span['source_id']]['owner_id'] for span in evidence
                        if order[span['source_id']] < order[source]
                        and by_source[span['source_id']]['sender_kind'] == 'user'}
        if not set(addressees) <= cited_actors:
            raise ValueError('missing_understanding_addressee_evidence')
        if confidence < MIN_CONFIDENCE or relation == 'unknown':
            relation, target, addressees = 'unknown', None, []
        # Preserve cited support even when the relationship is uncertain.
        key = (source, target, relation)
        if key in links:
            raise ValueError('duplicate_understanding_link')
        links.add(key)
        result['links'].append(dict(source_id=source, target_source_id=target, thread_id=tid,
                                    relation=relation, addressee_ids=list(addressees), confidence=confidence,
                                    evidence=evidence, source_ids=_sources(evidence, source, target, tid)))

    item_ids = set()
    for raw in _array(data['items'], MAX_ITEMS, 'items'):
        _object(raw, {'kind', 'thread_id', 'origin_source_id', 'summary', 'actor_id',
                      'attribution', 'status', 'confidence', 'evidence', 'updates'}, 'item')
        kind = _enum(raw['kind'], STATUSES, 'item_kind')
        origin = _source(raw['origin_source_id'], by_source)
        tid = thread_id(raw['thread_id'])
        if order[tid] > order[origin]:
            raise ValueError('noncausal_understanding_thread')
        item_id = kind + ':' + origin
        if item_id in item_ids:
            raise ValueError('duplicate_understanding_item')
        item_ids.add(item_id)
        summary = _text(raw['summary'], 240, 'item_summary')
        confidence = _score(raw['confidence'])
        actor, attribution, own = _speaker_attribution(raw, by_source[origin], actors)
        evidence = _evidence(raw['evidence'], by_source, required=(origin,), only=origin)
        claimed = _enum(raw['status'], STATUSES[kind], 'item_status')
        status = claimed
        if confidence < MIN_CONFIDENCE or not own or by_source[origin]['text_truncated']:
            status = 'unknown'
        elif claimed not in _INITIAL[kind]:
            # An origin cannot itself demonstrate a later resolution/acceptance.
            status = 'open' if kind == 'question' else 'proposed'
        initial = status
        updates, previous_order = [], order[origin]
        for update in _array(raw['updates'], MAX_UPDATES, 'updates'):
            _object(update, {'source_id', 'status', 'actor_id', 'attribution', 'confidence', 'evidence'}, 'update')
            sid = _source(update['source_id'], by_source)
            if order[sid] <= previous_order:
                raise ValueError('noncausal_understanding_update')
            previous_order = order[sid]
            update_status = _enum(update['status'], STATUSES[kind], 'update_status')
            update_confidence = _score(update['confidence'])
            update_actor, update_attribution, update_own = _speaker_attribution(update, by_source[sid], actors)
            support = _evidence(update['evidence'], by_source, required=(sid,), only=sid)
            # Preserve refusal/resolution unless the same actor supplies a
            # supported later change. Weak claims never erase a terminal state.
            if (not own or not update_own or update_actor != actor
                    or confidence < MIN_CONFIDENCE or by_source[origin]['text_truncated']
                    or update_confidence < MIN_CONFIDENCE or by_source[sid]['text_truncated']):
                update_status = 'unknown'
            if (status in {'resolved', 'declined', 'cancelled', 'fulfilled', 'superseded'}
                    and update_status in {'open', 'proposed', 'accepted', 'rhetorical'}):
                # Repeating a question/proposal cannot revoke a refusal or
                # closed outcome. A relevant explicit reopening is required.
                update_status = 'unknown'
            if update_status != 'unknown':
                status = update_status
            updates.append(dict(source_id=sid, status=update_status, actor_id=update_actor,
                                attribution=update_attribution, confidence=update_confidence, evidence=support))
        support_ids = _sources(evidence, origin, tid)
        support_ids = sorted(set(support_ids).union(*(set(_sources(update['evidence'], update['source_id']))
                                                    for update in updates)))
        result['items'].append(dict(item_id=item_id, kind=kind, thread_id=tid, origin_source_id=origin,
                                    summary=summary, actor_id=actor, attribution=attribution,
                                    initial_status=initial, status=status, status_scope='actor_only',
                                    confidence=confidence, evidence=evidence, updates=updates, source_ids=support_ids))
    if _size(result) > MAX_OUTPUT_BYTES:
        raise ValueError('understanding_output_too_large')
    return result


def understanding_sources(payload):
    """Collect complete cited support from raw or normalized schema objects.

    Full call-input lineage is broader and must be retained separately by the
    store. This helper does not authorize, validate or prune public sources.
    """
    sources = set()
    for kind in ('threads', 'links', 'items'):
        for row in payload.get(kind, ()):
            for field in ('thread_id', 'source_id', 'target_source_id', 'origin_source_id'):
                if isinstance(row.get(field), str):
                    sources.add(row[field])
            sources.update(sid for sid in row.get('source_ids', ()) if isinstance(sid, str))
            for evidence in row.get('evidence', ()):
                if isinstance(evidence.get('source_id'), str):
                    sources.add(evidence['source_id'])
            for update in row.get('updates', ()):
                if isinstance(update.get('source_id'), str):
                    sources.add(update['source_id'])
                for evidence in update.get('evidence', ()):
                    if isinstance(evidence.get('source_id'), str):
                        sources.add(evidence['source_id'])
    return sources


def retained_sources(payload, limit=MAX_MESSAGES):
    """Select complete terminal evidence groups before other raw anchors.

    A terminal group's origin, every update and required thread evidence travel
    together. If it cannot fit, omit the whole group rather than retaining an
    origin that a later extraction could incorrectly call open. This chooses
    raw messages only; previous summaries never enter a model request.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('invalid_understanding_retention_limit')
    priorities = []
    threads = {row['thread_id']: row for row in payload.get('threads', ())}
    terminals = {'resolved', 'declined', 'cancelled', 'fulfilled', 'superseded'}
    excluded = set()

    def append_group(group):
        additional = [source for source in group if source not in priorities]
        if len(priorities) + len(additional) > limit:
            return False
        priorities.extend(additional)
        return True

    def item_sources(item):
        required_thread = threads.get(item.get('thread_id'))
        group = {'threads': [required_thread] if required_thread else [], 'items': [item], 'links': []}
        sources = understanding_sources(group)
        first = [item.get('origin_source_id')]
        decisive = [update for update in item.get('updates', ()) if update.get('status') != 'unknown']
        if decisive:
            first.append(decisive[-1].get('source_id'))
        return list(dict.fromkeys([source for source in first if isinstance(source, str)] + sorted(sources)))

    for item in payload.get('items', ()):
        if item.get('status') in terminals:
            group = item_sources(item)
            if not append_group(group):
                excluded.update(group)
    for item in payload.get('items', ()):
        if item.get('status') not in terminals:
            append_group(item_sources(item))
    for thread in payload.get('threads', ()):
        group = sorted(understanding_sources({'threads': [thread]}))
        if not set(group) & excluded:
            append_group(group)
    for link in payload.get('links', ()):
        group = sorted(understanding_sources({'links': [link]}))
        if not set(group) & excluded:
            append_group(group)
    return priorities


def understanding_wire(payload):
    """Remove only known computed fields from a normalized projection.

    This representation is for local structural revalidation, NEVER for feeding
    old summaries into a new model call. Unknown fields remain invalid. The
    conservative normalized attribution and update statuses stay unchanged.
    """
    _object(payload, {'threads', 'links', 'items'}, 'root')
    if _size(payload) > MAX_OUTPUT_BYTES:
        raise ValueError('understanding_output_too_large')
    raw = {'threads': [], 'links': [], 'items': []}
    for row in _array(payload['threads'], MAX_THREADS, 'threads'):
        fields = {'thread_id', 'label', 'confidence', 'evidence'}
        _object(row, fields | {'source_ids'}, 'stored_thread')
        raw['threads'].append({key: row[key] for key in fields})
    for row in _array(payload['links'], MAX_LINKS, 'links'):
        fields = {'source_id', 'target_source_id', 'thread_id', 'relation',
                  'addressee_ids', 'confidence', 'evidence'}
        _object(row, fields | {'source_ids'}, 'stored_link')
        raw['links'].append({key: row[key] for key in fields})
    for row in _array(payload['items'], MAX_ITEMS, 'items'):
        fields = {'kind', 'thread_id', 'origin_source_id', 'summary', 'actor_id',
                  'attribution', 'status', 'confidence', 'evidence', 'updates'}
        _object(row, fields | {'item_id', 'initial_status', 'source_ids', 'status_scope'}, 'stored_item')
        restored = {key: row[key] for key in fields}
        restored['status'] = row['initial_status']
        raw['items'].append(restored)
    return raw


def revalidate_understanding(payload, messages):
    """Reconstruct and compare a stored normalized projection against raw input.

    This is deliberately separate from the provider parser: accepting computed
    fields there would let model output masquerade as trusted normalization.
    Caller must additionally revalidate complete input lineage, scope and epoch.
    """
    parsed = parse_understanding(understanding_wire(payload), messages)
    # JSON comparison retains type distinctions (e.g. True is not integer 1)
    # that Python container equality alone would silently accept.
    expected = json.dumps(parsed, ensure_ascii=False, sort_keys=True, allow_nan=False)
    actual = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if expected != actual:
        raise ValueError('inconsistent_stored_understanding')
    return parsed


def reconcile_understanding(previous, current, messages):
    """Preserve source-backed durable items across fresh model omissions.

    Previous summaries are consulted locally only. Any prior durable object may
    survive omission only after ALL its quoted raw support and its thread are
    revalidated against this call's exact messages. Missing support means
    omission, never an invented terminal status or an open replacement. Only a
    later source-backed, same-actor explicit reopened update can resume a prior
    terminal item. Whole-group capacity omissions imply no closure; the store
    must continue exposing bounded/incomplete historical coverage.
    """
    current = revalidate_understanding(current, messages)
    if previous is None:
        return current
    understanding_wire(previous)  # Reject unexpected stored structure first.
    messages = normalize_messages(messages)
    order = {message['source_id']: index for index, message in enumerate(messages)}
    old_threads = {thread['thread_id']: thread for thread in previous['threads']}
    new_threads = {thread['thread_id']: thread for thread in current['threads']}
    new_items = {item['item_id']: item for item in current['items']}
    terminals = {'resolved', 'declined', 'cancelled', 'fulfilled', 'superseded'}
    protected, groups = set(), []

    # Preserve terminal states first when all complete groups cannot fit.
    # Relative order within each category remains deterministic.
    prior_items = sorted(previous['items'], key=lambda item: not (
        isinstance(item.get('status'), str) and item['status'] in terminals))
    for old in prior_items:
        identity = old['item_id']
        protected.add(identity)
        prior_thread = old_threads.get(old['thread_id'])
        if prior_thread is None:
            raise ValueError('unsupported_stored_understanding_thread')
        prior = dict(threads=[prior_thread], links=[], items=[old])
        try:
            prior = revalidate_understanding(prior, messages)
        except ValueError:
            # No partial carry-forward, and no optimistic replacement when a
            # decisive old quote is unavailable or was truncated out of input.
            continue
        old = prior['items'][0]
        candidate = new_items.get(identity)
        if old['status'] not in terminals:
            # A fresh, supported reassessment is allowed; an empty extraction
            # alone is not evidence that an unanswered need or promise ended.
            if candidate is not None:
                groups.append((candidate, new_threads[candidate['thread_id']]))
            else:
                groups.append((old, prior['threads'][0]))
            continue
        decisive = [update for update in old['updates'] if update['status'] != 'unknown']
        latest = decisive[-1]['source_id'] if decisive else old['origin_source_id']
        reopened = bool(candidate and candidate['actor_id'] == old['actor_id'] and any(
            update['status'] == 'reopened' and update['actor_id'] == old['actor_id']
            and update['attribution'] == 'speaker' and order[update['source_id']] > order[latest]
            for update in candidate['updates']))
        if reopened:
            groups.append((candidate, new_threads[candidate['thread_id']]))
        else:
            groups.append((old, prior['threads'][0]))

    # Complete retained/refreshed durable objects precede unrelated new items.
    groups.extend((item, new_threads[item['thread_id']]) for item in current['items']
                  if item['item_id'] not in protected)
    result = {'threads': [], 'links': [], 'items': []}
    selected_threads = {}

    def fits(candidate):
        return (len(candidate['threads']) <= MAX_THREADS and len(candidate['links']) <= MAX_LINKS
                and len(candidate['items']) <= MAX_ITEMS and _size(candidate) <= MAX_OUTPUT_BYTES)

    for item, thread in groups:
        new_thread = thread['thread_id'] not in selected_threads
        candidate = dict(threads=result['threads'] + ([thread] if new_thread else []),
                         links=list(result['links']), items=result['items'] + [item])
        if fits(candidate):
            result = candidate
            selected_threads[thread['thread_id']] = thread
    # Unused thread/link hypotheses are secondary to complete durable items.
    for thread in current['threads']:
        if thread['thread_id'] not in selected_threads:
            candidate = dict(result, threads=result['threads'] + [thread])
            if fits(candidate):
                result = candidate
                selected_threads[thread['thread_id']] = thread
    for link in current['links']:
        if link['thread_id'] is None or link['thread_id'] in selected_threads:
            candidate = dict(result, links=result['links'] + [link])
            if fits(candidate):
                result = candidate
    return revalidate_understanding(result, messages)
