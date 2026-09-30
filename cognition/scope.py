"""Provider-free Telegram scope, captured before any background work."""
import contextvars
import re
from collections import defaultdict
from dataclasses import dataclass
from collections.abc import MutableMapping


@dataclass(frozen=True)
class TransportScope:
    chat_id: int
    topic_id: int = -1
    chat_type: str = 'unknown'
    user_id: int | None = None
    message_id: int | None = None
    addressed: bool = True
    sender_kind: str = 'user'
    reply_to_id: int | None = None
    sender_ref: str | None = None

    @property
    def group(self):
        return self.chat_type in ('group','supergroup')

    def send_kwargs(self):
        return {'message_thread_id':self.topic_id} if self.topic_id>0 else {}


CURRENT_SCOPE = contextvars.ContextVar('arti_transport_scope',default=None)


def requested(fallback=False):
    scope=CURRENT_SCOPE.get()
    return scope.addressed if scope and scope.group else fallback


def addressing(message,bot_id,bot_username=''):
    """Use source text/entities, never an appended quotation or display name."""
    reply = getattr(message,'reply_to_message',None)
    if reply and getattr(getattr(reply,'from_user',None),'id',None)==bot_id:
        return True
    raw = getattr(message,'text',None) or getattr(message,'caption',None) or ''
    entities = getattr(message,'entities',None) or getattr(message,'caption_entities',None) or []
    for entity in entities:
        if getattr(entity,'type','')=='text_mention' and getattr(getattr(entity,'user',None),'id',None)==bot_id:
            return True
        if getattr(entity,'type','')=='mention' and bot_username:
            part = raw.encode('utf-16-le')[entity.offset*2:(entity.offset+entity.length)*2].decode('utf-16-le')
            if part.lower()=='@'+bot_username.lower():
                return True
    # A bare name in a report about Arti is not a vocative.
    text = '\n'.join(line for line in raw.splitlines() if not line.lstrip().startswith(('>','«','"'))).strip()
    return bool(re.search(r'(?im)(?:^|[.!?]\s+)арти\s*(?:[,!?]|$|\s+(?:помоги|скажи|объясни|подскажи|сделай|можешь|давай|что|как|найди|привет)\b)',text)
                or re.search(r'(?i),\s*арти[.!?]*$',text))


def from_update(update,bot_id,bot_username=''):
    message = (getattr(update,'effective_message',None) or getattr(update,'message',None)
               or getattr(update,'edited_message',None) or getattr(getattr(update,'callback_query',None),'message',None))
    chat = getattr(update,'effective_chat',None) or getattr(message,'chat',None)
    if not chat:
        return None
    user = getattr(update,'effective_user',None) or getattr(message,'from_user',None)
    topic = getattr(message,'message_thread_id',None)
    # -1 means unknown historic scope; 0 is an observed non-forum group.
    if topic is None:
        topic = -1 if getattr(chat,'is_forum',False) or chat.type=='private' else 0
    sender_chat = getattr(message,'sender_chat',None)
    sender_kind = 'chat' if sender_chat else 'bot' if getattr(user,'is_bot',False) else 'user'
    return TransportScope(chat.id,topic,chat.type,getattr(user,'id',None) if not sender_chat else None,
                          getattr(message,'message_id',None),chat.type=='private' or bool(getattr(update,'callback_query',None)) or addressing(message,bot_id,bot_username),
                          sender_kind,getattr(getattr(message,'reply_to_message',None),'message_id',None),
                          'chat:'+str(sender_chat.id) if sender_chat else sender_kind+':'+str(getattr(user,'id',None)))


def scoped_key(key):
    scope = CURRENT_SCOPE.get()
    if not scope or scope.topic_id<=0:
        return key
    if key==scope.chat_id:
        return ('topic',key,scope.topic_id)
    if isinstance(key,tuple) and key and key[0]==scope.chat_id and len(key)==2:
        return ('topic',key[0],scope.topic_id,*key[1:])
    return key


class ScopedDict(dict):
    """Existing flows retain their API but do not share state across topics."""
    def __getitem__(self,key): return super().__getitem__(scoped_key(key))
    def __setitem__(self,key,value): return super().__setitem__(scoped_key(key),value)
    def __delitem__(self,key): return super().__delitem__(scoped_key(key))
    def __contains__(self,key): return super().__contains__(scoped_key(key))
    def get(self,key,default=None): return super().get(scoped_key(key),default)
    def pop(self,key,*args): return super().pop(scoped_key(key),*args)
    def setdefault(self,key,default=None): return super().setdefault(scoped_key(key),default)


class ScopedDefaultDict(defaultdict):
    def __getitem__(self,key): return super().__getitem__(scoped_key(key))
    def __setitem__(self,key,value): return dict.__setitem__(self,scoped_key(key),value)
    def __contains__(self,key): return super().__contains__(scoped_key(key))
    def get(self,key,default=None): return super().get(scoped_key(key),default)
    def pop(self,key,*args): return super().pop(scoped_key(key),*args)
    def setdefault(self,key,default=None): return super().setdefault(scoped_key(key),default)


class TopicUserData(MutableMapping):
    """Per-conversation view; simultaneous callbacks never swap shared dicts."""
    def __init__(self,data,scope): self.data=data; self.prefix=('conversation',scope.chat_id,scope.topic_id)
    def __getitem__(self,key): return self.data[(*self.prefix,key)]
    def __setitem__(self,key,value): self.data[(*self.prefix,key)]=value
    def __delitem__(self,key): del self.data[(*self.prefix,key)]
    def __iter__(self): return (key[-1] for key in self.data if isinstance(key,tuple) and key[:3]==self.prefix)
    def __len__(self): return sum(1 for _ in self)


class ScopedContext:
    def __init__(self,context,scope):
        self.context=context
        self.user_data=TopicUserData(context.user_data,scope)
    def __getattr__(self,key): return getattr(self.context,key)


def wrap_callback(callback):
    async def wrapped(update,context):
        scope=CURRENT_SCOPE.get()
        if scope and scope.group and scope.sender_kind=='bot': return
        if scope and scope.group and scope.topic_id>=0:
            context=ScopedContext(context,scope)
        return await callback(update,context)
    return wrapped
