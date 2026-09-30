"""Bounded public conversation hypotheses, rebuilt from permitted raw sources."""
from dataclasses import dataclass,field
from datetime import datetime,timedelta
import re
import json


def words(text):
    return set(re.findall(r'[a-zа-яё0-9]{3,}',text.lower()))-{'это','как','что','для','или','the','and','you'}


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

    def public_packet(self,anchor_message_id=None):
        selected=[]; size=0
        ordered=sorted(self.messages[-32:],key=lambda m:(m['message_id']==anchor_message_id,m['at']),reverse=True)
        for m in ordered:
            item={k:m.get(k) for k in ('source_id','message_id','owner_id','sender_kind','sender_ref','text','reply_to_id','branch','at','directed','is_bot')}
            item['text']=item['text'][:1600]
            amount=len(json.dumps(item,ensure_ascii=False).encode('utf-8'))
            if size+amount>12000: continue
            size+=amount; selected.append(item)
        selected.sort(key=lambda m:m['at'])
        return dict(chat_id=self.chat_id,topic_id=self.topic_id,visibility='observed_public_only',
                    messages=selected,
                    questions=list(self.questions.values())[-16:],tension=self.tension,serious=self.serious,norms=self.norms)


def build_frame(cid,chat_id,topic_id,messages,revision=0,closed=False,feedback=()):
    branches={}; mapping={}; questions={}; last_bot=None
    for raw in messages[-64:]:
        m=dict(raw); at=datetime.fromisoformat(m['at']); tokens=words(m['text'])
        reply=m.get('reply_to_id'); branch_id=mapping.get(reply)
        if branch_id is None:
            matches=[(len(tokens&b.keywords)/max(1,len(tokens|b.keywords)),b.id) for b in branches.values()
                     if b.last_at and (at-b.last_at).total_seconds()<600]
            score,found=max(matches,default=(0,None))
            branch_id=found if score>=.12 else m['message_id']
        if branch_id not in branches:
            if len(branches)>=8:
                old=min(branches,key=lambda x:branches[x].last_at)
                del branches[old]
            branches[branch_id]=Branch(branch_id)
        b=branches[branch_id]; b.last_at=at; b.sources=(b.sources+[m['source_id']])[-16:]
        b.keywords=set(sorted(b.keywords|tokens)[:80]); b.participants.add(m.get('owner_id')); m['branch']=branch_id
        mapping[m['message_id']]=branch_id
        if m.get('is_bot'): last_bot=m
        text=m['text'].lower()
        addressed_elsewhere=m.get('addressed_elsewhere',False)
        if '?' in text and not m.get('is_bot') and not m.get('directed') and not addressed_elsewhere:
            rhetorical=bool(re.search(r'кто бы мог подумать|ну и зачем|разве это|что за бред|who would have thought',text))
            if not rhetorical:
                questions[m['message_id']]=dict(message_id=m['message_id'],source_id=m['source_id'],branch=branch_id,
                                               status='open',owner_id=m.get('owner_id'),confidence=.55)
        if reply in questions and not m.get('is_bot') and m.get('owner_id')!=questions[reply]['owner_id'] and '?' not in text:
            # Treat a reply as a possible answer, not certainty; the arbiter sees both.
            questions[reply]['status']='possibly_answered'
        if reply in questions and m.get('is_bot'):
            questions[reply]['status']='answered'
        if re.search(r'разобрались|решили|спасибо.*(?:понял|получилось)|вопрос снят|не возвращайся к этому|не поднимай эту тему|problem solved|sorted it out',text):
            own=[q for q in questions.values() if q['owner_id']==m.get('owner_id') and q['status']=='open']
            for q in questions.values():
                if q['branch']==branch_id or len(own)==1 and q is own[0]: q['status']='closed'
        m['_at']=at; raw.update(m)
    questions=dict(list(questions.items())[-16:])
    recent=messages[-12:]
    tension=min(1.,sum(bool(re.search(r'заткнись|идиот|ненавижу|shut up|moron',m['text'],re.I)) for m in recent)/3)
    serious=any(re.search(r'умер|больниц|похорон|bereave|hospital|grief',m['text'],re.I) for m in recent)
    per_user={}
    for item in feedback:
        per_user.setdefault(item['user_id'],[]).append(item['signal'])
    values=[sum(v[:3])/len(v[:3]) for v in per_user.values()]
    # Equal contribution per observed person; no reward for the loudest member.
    norms=dict(receptivity=sum(values)/(len(values)+5) if values else 0.,confidence=min(.8,len(values)/20),
               evidence_participants=len(values),silence_is_rejection=False)
    by_kind={}
    for item in feedback:
        by_kind.setdefault(item.get('kind','unknown'),{}).setdefault(item['user_id'],[]).append(item['signal'])
    norms['by_kind']={kind:dict(receptivity=sum(sum(v[:3])/len(v[:3]) for v in people.values())/(len(people)+5),
                              confidence=min(.8,len(people)/20)) for kind,people in by_kind.items()}
    return ConversationFrame(cid,chat_id,topic_id,messages[-64:],branches,questions,revision,closed,tension,serious,last_bot,norms)


def candidate_kind(frame,message,policy):
    if policy.mode=='mentions' or not policy.full_visibility or message.get('is_bot') or message.get('directed'): return None
    text=message['text'].lower(); q=frame.questions.get(message['message_id'])
    if q and q['status']=='open': return 'open_question'
    if message.get('addressed_elsewhere'): return None
    if any(w in text for w in ('давайте','нам нужно','дедлайн','запланируем','we need','let us')): return 'shared_task'
    if policy.mode=='social' and not frame.serious and not frame.tension:
        if any(w in text for w in ('ура','получилось!','мы сделали','hooray','we did it')): return 'social_moment'
        if policy.topic_seeds and any(w in text for w in ('новая тема','о чём поговорим','new topic')): return 'topic_seed'
    return None
