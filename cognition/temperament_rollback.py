"""Prepare stored cognition for code from before H4a (temperament 2 -> 1).

Run with the bot stopped, before starting the older code:
    python -m tools.cognition_admin temperament-rollback          # count only
    python -m tools.cognition_admin temperament-rollback --apply  # rewrite
New emotions map to their nearest old ones, the character's own goals are
removed from stored perceptions (old code rejects unknown goals), and the
optional humor dimension is dropped. Relationship ruptures need nothing:
older code ignores the extra key. The rewrite is idempotent.
"""
import json

from cognition.serialization import object_value

OLD_TEMPERAMENT = 'arti-temperament-1'
NEW_TO_OLD = {'amusement':'joy','relief':'joy','admiration':'gratitude','tenderness':'gratitude','boredom':'disappointment'}
OWN_GOALS = ('curiosity','aesthetics','being_understood','autonomy')


def downgrade_state(state):
    """Return (state, changed) in the version-1 format."""
    state = dict(state)
    changed = state.get('temperament_version') != OLD_TEMPERAMENT
    state['temperament_version'] = OLD_TEMPERAMENT
    episodes = []
    for ep in state.get('episodes', ()):
        old = NEW_TO_OLD.get(ep['emotion'])
        changed |= old is not None
        episodes.append({**ep, 'emotion': old} if old else ep)
    state['episodes'] = episodes
    residues = {}
    for r in state.get('residues', ()):
        name = NEW_TO_OLD.get(r['emotion'], r['emotion'])
        changed |= name != r['emotion']
        if name in residues:
            kept = residues[name]
            residues[name] = {**kept, 'intensity': kept['intensity'] + r['intensity'],
                              'cause_count': kept['cause_count'] + r['cause_count']}
        else:
            residues[name] = {**r, 'emotion': name}
    state['residues'] = list(residues.values())
    return state, changed


def downgrade_perception(perception):
    """Return (perception, changed) without own-goal appraisals or humor."""
    if not isinstance(perception, dict) or not isinstance(perception.get('appraisals'), list):
        return perception, False
    appraisals = []
    changed = False
    for a in perception['appraisals']:
        if a.get('goal_id') in OWN_GOALS:
            changed = True
            continue
        if 'humor' in a:
            changed = True
            a = {k: v for k, v in a.items() if k != 'humor'}
        appraisals.append(a)
    return {**perception, 'appraisals': appraisals}, changed


async def prepare_rollback(pool, apply=False):
    counts = dict(contexts=0, personal_states=0, perceptions=0, reappraisals=0, applied=apply)
    async with pool.acquire() as conn, conn.transaction():
        for row in await conn.fetch('SELECT id,state FROM cognitive_contexts WHERE state IS NOT NULL FOR UPDATE'):
            state, changed = downgrade_state(object_value(row['state']))
            if changed:
                counts['contexts'] += 1
                if apply:
                    await conn.execute('UPDATE cognitive_contexts SET state=$2::jsonb WHERE id=$1', row['id'], json.dumps(state))
        for row in await conn.fetch("SELECT id,payload FROM cognitive_artifacts WHERE kind='personal_state' AND payload IS NOT NULL FOR UPDATE"):
            payload = object_value(row['payload'])
            state, changed = downgrade_state(payload['state'])
            if changed:
                counts['personal_states'] += 1
                if apply:
                    await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1', row['id'],
                                       json.dumps({**payload, 'state': state}))
        for row in await conn.fetch('SELECT id,perception FROM cognitive_events WHERE perception IS NOT NULL FOR UPDATE'):
            perception, changed = downgrade_perception(object_value(row['perception']))
            if changed:
                counts['perceptions'] += 1
                if apply:
                    await conn.execute('UPDATE cognitive_events SET perception=$2::jsonb WHERE id=$1', row['id'], json.dumps(perception))
        for row in await conn.fetch('''SELECT context_id,cause_event_id,support_event_id,perception FROM cognitive_reappraisals
                WHERE perception IS NOT NULL FOR UPDATE'''):
            perception, changed = downgrade_perception(object_value(row['perception']))
            if changed:
                counts['reappraisals'] += 1
                if apply:
                    await conn.execute('''UPDATE cognitive_reappraisals SET perception=$4::jsonb
                        WHERE context_id=$1 AND cause_event_id=$2 AND support_event_id=$3''',
                        row['context_id'], row['cause_event_id'], row['support_event_id'], json.dumps(perception))
    return counts
