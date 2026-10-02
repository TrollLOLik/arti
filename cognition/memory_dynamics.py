"""Pure selective encoding, interference, spaced retrieval and reconstruction."""
import hashlib
import math
import re
from datetime import datetime

STABILITY = {'gist':45.,'action':24.,'name':30.,'place':20.,'date':8.,'wording':3.}


def tokens(text):
    return set(re.findall(r'[\w]{2,}',str(text).casefold()))


def lexical_vector(text, dimensions=192):
    """Versioned deterministic subword projection; provider vectors are optional."""
    vector = [0.] * dimensions
    for word in tokens(text):
        pieces = {word} | {word[i:i+3] for i in range(max(1,len(word)-2))}
        for piece in pieces:
            digest = hashlib.blake2b(piece.encode(),digest_size=4).digest()
            vector[int.from_bytes(digest[:2],'big') % dimensions] += 1 if digest[2] & 1 else -1
    norm = math.sqrt(sum(v*v for v in vector)) or 1.
    return [v/norm for v in vector]


def cosine(left,right):
    if len(left) != len(right) or not left:
        return 0.
    return sum(a*b for a,b in zip(left,right))


def encode_details(event,situation,attention=1.,salience=0.):
    selected = []
    proposals = situation.details if situation else ()
    for item in proposals:
        span = situation.spans[item['span']]
        score = .65 * item['centrality'] + .2 * attention + .15 * salience
        if score < .35:
            continue
        selected.append(dict(text=span.text,start=span.start,end=span.end,kind=item['kind'],
                             centrality=item['centrality'],confidence=item['confidence'],fidelity=1.,
                             strength=.35 + .5*score,stability_days=STABILITY[item['kind']]*(.7+score),
                             vividness=.3+.6*salience,last_recalled=None,recall_count=0))
    # Unknown meaning is kept as a source-backed observation, not promoted to a fact.
    if not selected and event.text:
        text = event.text[:420]
        selected.append(dict(text=text,start=0,end=len(text),kind='gist',centrality=.5,
                             confidence=.5,fidelity=1.,strength=.45,stability_days=30.,vividness=.3,
                             last_recalled=None,recall_count=0))
    return selected[:12]


def detail_state(detail,created_at,at,competition=0.,cue=0.):
    origin = datetime.fromisoformat(detail['last_recalled']) if detail.get('last_recalled') else created_at
    age_days = max(0.,(at-origin).total_seconds())/86400
    stability = max(.5,detail['stability_days'])
    accessibility = detail['strength'] * (1 + age_days/stability)**(-.8)
    accessibility *= math.exp(-.35*max(0.,competition))
    accessibility = min(1.,accessibility + min(.55,max(0.,cue)*.55))
    fidelity = detail['fidelity'] * math.exp(-age_days/(stability*12))
    vividness = detail['vividness'] * math.exp(-age_days/(stability*3))
    return dict(accessibility=accessibility,fidelity=fidelity,vividness=vividness,
                confidence=detail['confidence'],strength=detail['strength'])


def reactivate(detail,created_at,at,replay=False):
    result = dict(detail)
    last = datetime.fromisoformat(detail['last_recalled']) if detail.get('last_recalled') else created_at
    spacing = max(0.,(at-last).total_seconds())/86400
    benefit = min(.25,math.log1p(spacing)*.06)
    if replay:
        benefit *= .35
    result['stability_days'] = min(3650.,detail['stability_days']*(1+benefit))
    result['strength'] = min(.95,detail['strength'] + (.025 if replay else .06))
    result['last_recalled'] = at.isoformat()
    result['recall_count'] += 1
    # Confidence/fidelity are not improved by repetition of the same observation.
    return result


def reconstruct(trace,at,cue=0.,competition=0.,archive=False):
    created = datetime.fromisoformat(trace['observed_at'])
    details = []
    for d in trace['details']:
        state = detail_state(d,created,at,competition,cue)
        threshold = .18 if d['kind']=='gist' else .28
        if archive or (state['accessibility'] >= threshold and state['fidelity'] >= .5):
            details.append(dict(text=d['text'],kind=d['kind'],**state))
    if not archive:
        visible = {(d['kind'],d['text']) for d in details}
        # A broad gist span can contain a date or name whose separate trace has
        # faded. Mask that detail rather than leaking its exact value via gist.
        unavailable = [d for d in trace['details'] if d['kind'] not in ('gist','wording') and (d['kind'],d['text']) not in visible]
        for item in details:
            if item['kind']=='gist':
                for missing in unavailable:
                    item['text'] = item['text'].replace(missing['text'],'[деталь не вспоминается]')
            item['verbatim_verified'] = item['kind']=='wording'
    else:
        for item in details:
            item['verbatim_verified'] = True
    occurred = datetime.fromisoformat(trace.get('occurred_at',trace['observed_at']))
    elapsed = max(0.,(at-occurred).total_seconds()) / 86400
    precision = 'day' if elapsed<30 else ('month' if elapsed<365 else 'year')
    # A dated source is always exact during explicit record verification.
    return dict(details=details,time_precision='source_record' if archive else precision,
                source_id=trace['source_id'],modality=trace['modality'],
                interpretation=trace.get('interpretation',''),version=trace.get('version',1),
                familiarity=min(.95,.25 + math.log1p(sum(d['recall_count'] for d in trace['details']))*.1),
                uncertainty=not bool(details))
