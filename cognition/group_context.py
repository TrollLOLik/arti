"""Bounded public conversation hypotheses, rebuilt from permitted raw sources.

Reply links are evidence of conversational scope. Lexical branches and question
states are only hints for arbitration, never evidence that an answer succeeded.
"""
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import re


FRAME_MESSAGE_LIMIT = 64
PUBLIC_MESSAGE_LIMIT = 32
PUBLIC_PACKET_BYTE_LIMIT = 14999
QUESTION_LIMIT = 16
QUESTION_EVIDENCE_LIMIT = 6
REPLY_CHAIN_LIMIT = 8

_UNCERTAINTY = re.compile(
    r"\b(?:не знаю|не уверен\w*|не уверена|понятия не имею|тоже (?:интересно|хочу узнать|не знаю)|"
    r"сам\w* (?:ищу|пытаюсь|разбираюсь)|пока (?:не|ищу)|всё ещё|все еще|"
    r"i (?:don['’]?t know|do not know)|no (?:idea|clue)|not sure|unsure|"
    r"also wondering|wondering too|same question|still (?:looking|trying|broken)|"
    r"не (?:разобрались|получилось|работает)|ничего не получилось|not working|"
    r"(?:didn['’]?t|did not) work)\b", re.I)
_RESOLUTION = re.compile(
    r"^\s*(?:(?:арти|arti)[,!:\s]+)?(?:(?:спасибо|благодарю|thanks|thank you|да|ок|okay)[,!:.\s]+)?"
    r"(?:вопрос снят|(?:мы |я )?разобрались|я разобрался|я разобралась|"
    r"(?:мы )?решили\s+проблему|понял[а]?[,!:.\s]+(?:теперь |всё |все )?получилось|"
    r"(?:теперь |всё |все )?(?:получилось|работает)|problem solved|sorted it out|"
    r"(?:i |we )?fixed it|that worked|it works(?: now)?)\b", re.I)
_REFUSAL = re.compile(
    r"^\s*(?:(?:арти|arti)[,!:\s]+)?(?:не возвращайся к этому|не поднимай эту тему|"
    r"не вмешивайся|не надо вмешиваться|stop interrupting|"
    r"(?:don['’]?t|do not) bring (?:this|it) up|leave (?:this|it) alone)\b", re.I)
_REOPEN = re.compile(
    r"^\s*(?:(?:арти|arti)[,!:\s]+)?(?:(?:ладно|actually)[,!:\s]+)?(?:"
    r"верн[её]мся к (?:этому )?вопросу|давай(?:те)? вс[её]-таки обсудим|"
    r"можешь вс[её]-таки помочь|please help after all|let['’]?s revisit this|let us revisit this)\b", re.I)

_RHETORICAL = re.compile(r'кто бы мог подумать|ну и зачем|разве это|что за бред|who would have thought', re.I)


def explicit_refusal(text):
    """A conservative literal refusal hint, never a group-wide policy change."""
    return '?' not in text and bool(_REFUSAL.search(text))


def _explicit_resolution(text):
    # Hard-close only short, unequivocal declarations. Partial, hypothetical,
    # qualified and richer claims remain possible answers for semantic review.
    remaining = text.strip()
    for _ in range(3):
        match = _RESOLUTION.match(remaining)
        if not match:
            return False
        remaining = remaining[match.end():].strip(' \t\r\n.,!;')
        if not remaining:
            return True
    return False


def words(text):
    return set(re.findall(r'[a-zа-яё0-9]{3,}', text.lower())) - {'это', 'как', 'что', 'для', 'или', 'the', 'and', 'you'}


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def _public_norms(norms):
    """Never export arbitrary feedback payloads or participant-level records."""
    def scores(values):
        return {k: values[k] for k in ('receptivity', 'confidence')
                if isinstance(values.get(k), (int, float)) and not isinstance(values[k], bool)
                and math.isfinite(values[k])}
    result = scores(norms)
    count = norms.get('evidence_participants', 0)
    result['evidence_participants'] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    result['silence_is_rejection'] = False
    kinds = {'open_question', 'shared_task', 'social_moment', 'topic_seed', 'contextual', 'continuation', 'followup', 'unknown'}
    result['by_kind'] = {k: scores(value) for k, value in norms.get('by_kind', {}).items()
                         if k in kinds and isinstance(value, dict)}
    return result


def _public_contributions(outcomes):
    """Caller validates public scope/provenance; expose only bounded evidence.

    Feedback source IDs may be public reaction events rather than observations.
    A delivery/feedback record never asserts that the underlying task succeeded.
    """
    selected = []
    kinds = {'open_question', 'shared_task', 'social_moment', 'topic_seed', 'contextual', 'continuation', 'followup', 'reminder'}
    for raw in sorted(outcomes, key=lambda row: str(row.get('at', '')), reverse=True)[:8]:
        if raw.get('delivery_status') not in ('delivered', 'delivery_unknown'):
            continue
        sources = [sid for sid in raw.get('source_ids', ()) if isinstance(sid, str) and 0 < len(sid) <= 192]
        if not sources:
            continue
        text = str(raw.get('text', ''))
        snippet = text[:600].encode('utf-8')[:800].decode('utf-8', errors='ignore')
        item = dict(message_id=raw.get('message_id'), source_ids=sources[:8],
                    kind=raw.get('kind') if raw.get('kind') in kinds else 'unknown',
                    channel='text' if raw.get('channel') == 'message' else raw.get('channel') if raw.get('channel') in ('text', 'reaction') else 'unknown',
                    text=snippet, text_truncated=len(snippet) != len(text),
                    delivery_status=raw['delivery_status'], at=str(raw.get('at', ''))[:48],
                    effect='unknown', feedback=[])
        for feedback in raw.get('feedback', ())[:4]:
            source = feedback.get('source_id'); signal = feedback.get('signal')
            if (isinstance(source, str) and 0 < len(source) <= 192
                    and isinstance(signal, (int, float)) and not isinstance(signal, bool)
                    and math.isfinite(signal) and -1 <= signal <= 1):
                item['feedback'].append(dict(source_id=source, owner_id=feedback.get('owner_id'), signal=signal))
        item['source_ids_truncated'] = len(sources) > len(item['source_ids'])
        selected.append(item)
        if _json_size(selected) > 3500:
            selected.pop()
    return selected


@dataclass
class Branch:
    id: int
    sources: list = field(default_factory=list)
    participants: set = field(default_factory=set)
    keywords: set = field(default_factory=set)
    last_at: datetime | None = None


@dataclass
class ConversationFrame:
    context_id: int
    chat_id: int
    topic_id: int
    messages: list
    branches: dict
    questions: dict
    revision: int
    closed: bool
    tension: float
    serious: bool
    last_bot: dict | None
    norms: dict
    outcomes: list = field(default_factory=list)
    suppression_epoch: int = 0
    question_index: dict = field(default_factory=dict)

    def question(self, message_id):
        # Retain anchor-specific state within the same raw window even if many
        # newer question hints displaced it from the small general summary.
        return self.question_index.get(message_id) or self.questions.get(message_id)

    def public_packet(self, anchor_message_id=None):
        """Keep the anchor and structural evidence before filling recent context.

        Only the caller's already permitted 64-message frame is used. The entire
        JSON packet, including hints and UTF-8 text, shares one hard byte budget.
        Missing/truncated context is explicit rather than silently summarized.
        """
        available = self.messages[-FRAME_MESSAGE_LIMIT:]
        by_id = {m['message_id']: m for m in available}
        anchor = by_id.get(anchor_message_id)
        ancestors = []
        seen = {anchor_message_id}
        current = anchor
        while current and current.get('reply_to_id') is not None and len(ancestors) < REPLY_CHAIN_LIMIT:
            parent = current['reply_to_id']
            if parent in seen or parent not in by_id:
                break
            ancestors.append(parent)
            seen.add(parent)
            current = by_id[parent]
        chain_incomplete = bool(current and current.get('reply_to_id') is not None)
        # Find descendants using exact reply ancestry, not shared vocabulary.
        related = {anchor_message_id, *ancestors} if anchor else set()
        for _ in range(REPLY_CHAIN_LIMIT):
            expanded = related | {m['message_id'] for m in available if m.get('reply_to_id') in related}
            if expanded == related:
                break
            related = expanded
        replies = [m['message_id'] for m in reversed(available) if m['message_id'] in related and m['message_id'] != anchor_message_id]
        index = self.question_index or self.questions
        anchor_questions = [index[mid] for mid in [anchor_message_id, *ancestors] if mid in index]
        hint_questions = {q['message_id']: q for q in anchor_questions}
        for q in reversed(list(self.questions.values())):
            if len(hint_questions) >= QUESTION_LIMIT:
                break
            hint_questions.setdefault(q['message_id'], q)
        unresolved = [q for q in reversed(list(index.values())) if q['status'] != 'closed']
        priority = []

        def prioritize(ids):
            for mid in ids:
                if mid in by_id and mid not in priority:
                    priority.append(mid)

        prioritize([anchor_message_id])
        # Latest outcome/refusal can invalidate a tempting old question.
        for q in anchor_questions:
            prioritize(e['message_id'] for e in reversed(q.get('evidence', ())))
        prioritize(ancestors[:2])
        prioritize(replies[:4])
        prioritize(m['message_id'] for m in available[-8:][::-1])
        prioritize(ancestors)
        for q in unresolved[:4]:
            prioritize([q['message_id']])
            prioritize(e['message_id'] for e in reversed(q.get('evidence', ())))
        prioritize(replies)
        prioritize(m['message_id'] for m in reversed(available))
        selected = []
        contributions = _public_contributions(self.outcomes)

        def packet():
            ids = {m['message_id'] for m in selected}
            questions = []
            for q in hint_questions.values():
                if q['message_id'] not in ids:
                    continue
                item = {k: q[k] for k in ('message_id', 'source_id', 'branch', 'status', 'owner_id', 'confidence', 'outcome') if k in q}
                evidence = q.get('evidence', ())
                item['evidence'] = [dict(e) for e in evidence if e['message_id'] in ids]
                item['evidence_complete'] = len(item['evidence']) == len(evidence) and not q.get('evidence_truncated', False)
                if len(item['evidence']) != len(evidence):
                    item.update(status='unverified', outcome='unknown')
                questions.append(item)
            return dict(chat_id=self.chat_id, topic_id=self.topic_id, visibility='observed_public_only',
                        messages=sorted(selected, key=lambda m: m['at']), questions=questions,
                        tension=self.tension, serious=self.serious, norms=_public_norms(self.norms),
                        recent_contributions=contributions,
                        context_bounds=dict(raw_message_limit=FRAME_MESSAGE_LIMIT,
                                            omitted_message_count=len(available) - len(selected),
                                            anchor_message_id=anchor_message_id,
                                            anchor_available=anchor is not None if anchor_message_id is not None else None,
                                            reply_chain_incomplete=chain_incomplete or any(mid not in ids for mid in ancestors)),
                        branch_ids_are_hints=True)

        for mid in priority:
            if len(selected) >= PUBLIC_MESSAGE_LIMIT:
                break
            m = by_id[mid]
            # Keep only fields observed in this public topic, never arbitrary
            # raw-event/private-memory fields or internal datetime objects.
            item = {k: m.get(k) for k in ('source_id', 'message_id', 'owner_id', 'sender_kind', 'sender_ref', 'reply_to_id', 'branch', 'at', 'directed', 'is_bot', 'addressed_elsewhere')}
            text = str(m.get('text', ''))
            item['text'] = text.encode('utf-8')[:160].decode('utf-8', errors='ignore')
            item['text_truncated'] = len(item['text']) < len(text)
            selected.append(item)
            if _json_size(packet()) > PUBLIC_PACKET_BYTE_LIMIT:
                selected.pop()
        # Expand retained text in priority order without starving reply evidence.
        for item in selected:
            text = str(by_id[item['message_id']].get('text', ''))
            low, high = len(item['text']), min(1600, len(text))
            while low < high:
                middle = (low + high + 1) // 2
                item['text'] = text[:middle]
                item['text_truncated'] = middle < len(text)
                if _json_size(packet()) <= PUBLIC_PACKET_BYTE_LIMIT:
                    low = middle
                else:
                    high = middle - 1
            item['text'] = text[:low]
            item['text_truncated'] = low < len(text)
        return packet()


def _record_evidence(question, message, kind):
    evidence = question.setdefault('evidence', [])
    evidence.append(dict(message_id=message['message_id'], source_id=message['source_id'], kind=kind))
    if len(evidence) > QUESTION_EVIDENCE_LIMIT:
        question['evidence_truncated'] = True
        decisive = next((e for e in reversed(evidence) if e['kind'] in
                         ('owner_refusal', 'owner_resolution', 'owner_reopened')), None)
        retained = evidence[-QUESTION_EVIDENCE_LIMIT:]
        if decisive is not None and decisive not in retained:
            retained[0] = decisive
        evidence[:] = retained


def build_frame(cid, chat_id, topic_id, messages, revision=0, closed=False, feedback=(), outcomes=(), suppression_epoch=0):
    branches = {}; mapping = {}; questions = {}; last_bot = None
    by_id = {}; last_owner_message = {}

    def reply_question(message):
        parent = message.get('reply_to_id')
        visited = set()
        while parent is not None and parent not in visited:
            if parent in questions:
                return questions[parent]
            visited.add(parent)
            parent = by_id.get(parent, {}).get('reply_to_id')
        return None

    for raw in messages[-FRAME_MESSAGE_LIMIT:]:
        m = dict(raw); at = datetime.fromisoformat(m['at']); tokens = words(m['text'])
        reply = m.get('reply_to_id'); branch_id = mapping.get(reply)
        if branch_id is None:
            matches = [(len(tokens & b.keywords) / max(1, len(tokens | b.keywords)), b.id) for b in branches.values()
                       if b.last_at and 0 <= (at - b.last_at).total_seconds() < 600]
            score, found = max(matches, default=(0, None))
            branch_id = found if score >= .12 else m['message_id']
        if branch_id not in branches:
            if len(branches) >= 8:
                del branches[min(branches, key=lambda x: branches[x].last_at)]
            branches[branch_id] = Branch(branch_id)
        b = branches[branch_id]; b.last_at = at; b.sources = (b.sources + [m['source_id']])[-16:]
        b.keywords = set(sorted(b.keywords | tokens)[:80]); b.participants.add(m.get('owner_id')); m['branch'] = branch_id
        mapping[m['message_id']] = branch_id
        if m.get('is_bot'):
            last_bot = m
        text = m['text'].lower()
        target = reply_question(m)
        human = not m.get('is_bot') and m.get('sender_kind', 'user') == 'user' and m.get('owner_id') is not None
        uncertain = bool(_UNCERTAINTY.search(text))
        refusal = human and explicit_refusal(text)
        resolved = human and '?' not in text and not uncertain and _explicit_resolution(text)
        if target:
            if m.get('is_bot'):
                _record_evidence(target, m, 'bot_reply')
                if target['status'] != 'closed':
                    target['status'] = 'possibly_answered'
            elif human:
                if target['owner_id'] == m['owner_id'] and _REOPEN.search(text):
                    _record_evidence(target, m, 'owner_reopened')
                    target.update(status='open', outcome='unknown')
                elif uncertain or '?' in text:
                    _record_evidence(target, m, 'uncertainty' if uncertain else 'question_reply')
                    if target['owner_id'] == m['owner_id'] and (target['status'] != 'closed' or target['outcome'] == 'resolved'):
                        target.update(status='open', outcome='unknown')
                elif (target['owner_id'] == m['owner_id'] and target['status'] == 'closed'
                      and target['outcome'] == 'resolved' and text.strip() and not refusal and not resolved):
                    # New owner content in this exact reply chain can correct a
                    # previous success without matching an uncertainty phrase.
                    # Let the arbiter distinguish a correction from thanks; a
                    # refusal is a separate boundary and is not relaxed here.
                    _record_evidence(target, m, 'owner_followup')
                    target.update(status='possibly_answered', outcome='unknown')
                elif target['status'] != 'closed' and not refusal and not resolved and text.strip():
                    _record_evidence(target, m, 'possible_answer')
                    target['status'] = 'possibly_answered'
        if refusal or resolved:
            # A refusal/confirmation is local to its speaker and reply chain.
            # A lexical branch match never lets one participant close another's
            # question, or close several unrelated questions by the same owner.
            if target is None and reply is None:
                own = [q for q in questions.values() if q['owner_id'] == m['owner_id'] and q['status'] != 'closed']
                previous = last_owner_message.get(m['owner_id'])
                if len(own) == 1 and previous:
                    previous_question = questions.get(previous['message_id']) or reply_question(previous)
                    if previous_question is own[0] and 0 <= (at - previous['_at']).total_seconds() < 600:
                        target = own[0]
            if target and target['owner_id'] == m['owner_id']:
                _record_evidence(target, m, 'owner_refusal' if refusal else 'owner_resolution')
                target.update(status='closed', outcome='declined' if refusal else 'resolved')
            elif target:
                _record_evidence(target, m, 'participant_refusal' if refusal else 'participant_resolution')
                if resolved and target['status'] != 'closed':
                    target['status'] = 'possibly_answered'
        addressed_elsewhere = m.get('addressed_elsewhere', False)
        if '?' in text and human and not m.get('directed') and not addressed_elsewhere and not _RHETORICAL.search(text):
            questions[m['message_id']] = dict(message_id=m['message_id'], source_id=m['source_id'], branch=branch_id,
                                              status='open', owner_id=m.get('owner_id'), confidence=.55,
                                              outcome='unknown', evidence=[])
        m['_at'] = at; raw.update(m); by_id[m['message_id']] = m
        if human:
            last_owner_message[m['owner_id']] = m
    question_index = questions
    questions = dict(list(questions.items())[-QUESTION_LIMIT:])
    recent = messages[-12:]
    tension = min(1., sum(bool(re.search(r'заткнись|идиот|ненавижу|shut up|moron', m['text'], re.I)) for m in recent) / 3)
    serious = any(re.search(r'умер|больниц|похорон|bereave|hospital|grief', m['text'], re.I) for m in recent)
    per_user = {}
    for item in feedback:
        per_user.setdefault(item['user_id'], []).append(item['signal'])
    values = [sum(v[:3]) / len(v[:3]) for v in per_user.values()]
    # Equal contribution per observed person; no reward for the loudest member.
    norms = dict(receptivity=sum(values) / (len(values) + 5) if values else 0., confidence=min(.8, len(values) / 20),
                 evidence_participants=len(values), silence_is_rejection=False)
    by_kind = {}
    for item in feedback:
        by_kind.setdefault(item.get('kind', 'unknown'), {}).setdefault(item['user_id'], []).append(item['signal'])
    norms['by_kind'] = {kind: dict(receptivity=sum(sum(v[:3]) / len(v[:3]) for v in people.values()) / (len(people) + 5),
                                confidence=min(.8, len(people) / 20)) for kind, people in by_kind.items()}
    return ConversationFrame(cid, chat_id, topic_id, messages[-FRAME_MESSAGE_LIMIT:], branches, questions,
                             revision, closed, tension, serious, last_bot, norms, list(outcomes)[-8:], suppression_epoch, question_index)


def candidate_kind(frame, message, policy):
    if policy.mode == 'mentions' or not policy.full_visibility or message.get('is_bot') or message.get('directed'):
        return None
    text = message['text'].lower(); q = frame.questions.get(message['message_id'])
    if q and q['status'] == 'open':
        return 'open_question'
    if message.get('addressed_elsewhere'):
        return None
    if any(w in text for w in ('давайте', 'нам нужно', 'дедлайн', 'запланируем', 'we need', 'let us')):
        return 'shared_task'
    if policy.mode == 'social' and not frame.serious and not frame.tension:
        if any(w in text for w in ('ура', 'получилось!', 'мы сделали', 'hooray', 'we did it')):
            return 'social_moment'
        if policy.topic_seeds and any(w in text for w in ('новая тема', 'о чём поговорим', 'new topic')):
            return 'topic_seed'
    return None


def contextual_candidate(frame, message, policy):
    """Eligibility for bounded semantic assessment, never permission to speak.

    No question mark, overlap, emotion word or command dictionary is required.
    Reply/addressing/visibility boundaries stay conservative; the arbiter receives
    the actual recent public conversation and can abstain.
    """
    return (policy.mode in ('useful', 'social') and policy.full_visibility
            and not message.get('is_bot') and not message.get('directed')
            and not message.get('addressed_elsewhere') and not message.get('edited')
            and message.get('sender_kind') == 'user'
            and bool(str(message.get('text', '')).strip()))
