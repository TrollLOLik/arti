"""Whole-prompt budgeting, with explicit tokenizer identity and safe fallbacks."""
from dataclasses import dataclass
from html import escape
import json


class TokenCounter:
    def __init__(self,model):
        self.model = model
        self.encoding = None
        self.name = 'utf8-upper-bound'
        # Only claim exact local tokenization for models recognized by tiktoken.
        try:
            import tiktoken
            self.encoding = tiktoken.encoding_for_model(model)
            self.name = self.encoding.name
        except (ImportError,KeyError):
            pass

    def count(self,text):
        if self.encoding:
            return len(self.encoding.encode(str(text),disallowed_special=()))
        return len(str(text).encode('utf-8'))

    def fit(self,text,budget,tail=False):
        text = str(text or '')
        if self.count(text)<=budget:
            return text
        low,high = 0,len(text)
        while low<high:
            mid = (low+high+1)//2
            candidate = text[-mid:] if tail and mid else text[:mid]
            if self.count(candidate)<=budget:
                low = mid
            else:
                high = mid-1
        return text[-low:] if tail and low else text[:low]


@dataclass(frozen=True)
class PromptBudget:
    context_tokens: int = 32768
    output_tokens: int = 8192
    tool_tokens: int = 2048
    transport_tokens: int = 1024


def assemble_prompt(system,task,dialogue='',memory='',model='',budget=PromptBudget()):
    counter = TokenCounter(model)
    available = budget.context_tokens-budget.output_tokens-budget.tool_tokens-budget.transport_tokens
    if available<=0 or counter.count(system)+counter.count(task)>available:
        raise ValueError('System instructions and current task exceed the declared model context budget')
    remaining = available-counter.count(system)-counter.count(task)
    # Reserve recent dialogue independently; never trim the current task.
    memory_budget = min(remaining//3,5000)
    opening,closing = '<user_memory>\n','\n</user_memory>'
    if memory:
        # Escape raw memory as data; fences are assembled after truncation.
        capacity = max(0,memory_budget-counter.count(opening+closing))
        lines = str(memory).splitlines()
        try:
            rich = all(isinstance(json.loads(line),dict) and 'source_id' in json.loads(line) for line in lines)
        except ValueError:
            rich = False
        if rich:
            selected = []
            for line in lines:
                escaped = escape(line,quote=False)
                if counter.count('\n'.join(selected+[escaped]))<=capacity:
                    selected.append(escaped)
            data = '\n'.join(selected)
        else:
            data = counter.fit(escape(str(memory),quote=False),capacity)
        memory = opening+data+closing if data else ''
    dialogue = counter.fit(dialogue,max(0,remaining-counter.count(memory)-256),tail=True)
    context = ('[Недавний диалог]\n'+dialogue+'\n\n' if dialogue else '')+memory
    final = 'Контекст:\n'+context+'\n\nТекущее сообщение:\n'+task
    if counter.count(system)+counter.count(final)>available:
        raise ValueError('Prompt framing exceeded its reserved budget')
    return final,dict(tokenizer=counter.name,input_tokens=counter.count(system)+counter.count(final),
                      input_limit=available,output_reserve=budget.output_tokens,tool_reserve=budget.tool_tokens)


def memory_for_prompt(recollections,beliefs,limit=4000,model=''):
    counter = TokenCounter(model)
    blocks,ids = [],[]
    for r in recollections:
        details = r['details']
        if not details:
            block = dict(artifact_id=r['artifact_id'],source_id=r['source_id'],familiarity=r['familiarity'],uncertainty=True)
        else:
            block = dict(artifact_id=r['artifact_id'],source_id=r['source_id'],details=[dict(text=d['text'],confidence=d['confidence'],kind=d['kind'],verbatim_verified=d.get('verbatim_verified',False)) for d in details],
                         time_precision=r['time_precision'],modality=r['modality'],interpretation=r['interpretation'])
        text = json.dumps(block,ensure_ascii=False)
        if counter.count('\n'.join(blocks+[text]))>limit:
            continue
        blocks.append(text)
        ids.append(r['artifact_id'])
    for belief in beliefs:
        p = belief['payload']
        text = json.dumps(dict(artifact_id=belief['id'],**{k:p[k] for k in ('subject','predicate','value','condition','confidence','assertion','source_id')}),ensure_ascii=False)
        if counter.count('\n'.join(blocks+[text]))<=limit:
            blocks.append(text)
            ids.append(belief['id'])
    return '\n'.join(blocks),ids
