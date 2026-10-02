"""Explicit timezone parsing, including DST gap/fold rejection."""
import re
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError


class OrganizerError(ValueError): pass


def zone(name):
    try: return ZoneInfo(name)
    except (ZoneInfoNotFoundError,ValueError,TypeError): raise OrganizerError('timezone_required') from None


def scheduled_at(value, timezone_name=None, *, now=None):
    now=now or datetime.now(timezone.utc)
    match=re.fullmatch(r'(\d{1,6})(s|m|h|d)',value)
    if match:
        seconds=int(match[1])*{'s':1,'m':60,'h':3600,'d':86400}[match[2]]
        due=now+timedelta(seconds=seconds); label='UTC'
    else:
        try:
            if 'T' not in value: raise ValueError()
            local=datetime.fromisoformat(value.replace('Z','+00:00'))
        except (ValueError,TypeError): raise OrganizerError('invalid_datetime') from None
        if local.tzinfo is not None:
            due=local.astimezone(timezone.utc); label=str(local.tzinfo)
        else:
            if not timezone_name: raise OrganizerError('timezone_required')
            tz=zone(timezone_name); candidates=set()
            for fold in (0,1):
                candidate=local.replace(tzinfo=tz,fold=fold).astimezone(timezone.utc)
                if candidate.astimezone(tz).replace(tzinfo=None)==local: candidates.add(candidate)
            if not candidates: raise OrganizerError('nonexistent_local_time')
            if len(candidates)!=1: raise OrganizerError('ambiguous_local_time')
            due=candidates.pop(); label=timezone_name
    if not now<due<=now+timedelta(days=365): raise OrganizerError('schedule_out_of_range')
    return due,label


def format_scheduled(value,label=None):
    """Display original local schedule with an explicit offset and zone label."""
    tz=timezone.utc; shown='UTC'
    if label:
        try:
            tz=ZoneInfo(label); shown=label
        except (ZoneInfoNotFoundError,ValueError,TypeError):
            offset=re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})(?::(\d{2}))?',str(label))
            if offset:
                try:
                    delta=timedelta(hours=int(offset[2]),minutes=int(offset[3]),seconds=int(offset[4] or 0))
                    tz=timezone(delta if offset[1]=='+' else -delta); shown=str(tz)
                except ValueError: pass
    return value.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S %z')+' ['+shown+']'
