"""Human-owned group settings; ordinary conversation cannot change them."""
from dataclasses import asdict
from datetime import timedelta
import uuid
from cognition.runtime import get_runtime
from cognition.scope import CURRENT_SCOPE
from cognition.types import Origin


def policy_status(policy,now):
    reason=policy.reason(now)
    labels=dict(unknown_timezone='нужен часовой пояс: /proactivity tz <IANA zone>',
                quiet_hours='тихие часы',mentions_only='только обращения',partial_visibility='неполная видимость',
                disabled='выключены',paused='на паузе')
    availability=labels.get(reason,'разрешены в пределах лимитов')
    return (f'Участие: {policy.mode}; исполнение: {policy.execution}; полная видимость: {policy.full_visibility}.\n'
            f'Часовой пояс: {policy.timezone or "не задан"}. Инициативы: {availability}.')


async def is_admin(user,chat_id,context):
    if user is None: return False
    try:
        member=await context.bot.get_chat_member(chat_id,user.id)
        return member.status in ('administrator','creator')
    except Exception: return False


async def proactivity_command(update,context):
    scope=CURRENT_SCOPE.get(); runtime=get_runtime()
    if not runtime or not scope or not scope.group or scope.topic_id<0:
        await update.effective_message.reply_text('Настройки участия доступны в группе с определённой темой.'); return
    args=list(context.args or [])
    if args and args[0] in ('optout','optin'):
        await runtime.groups.policies.opt_out(scope.chat_id,update.effective_user.id,args[0]=='optout')
        await update.effective_message.reply_text('Самостоятельные обращения ко мне выключены.' if args[0]=='optout' else 'Самостоятельные обращения ко мне разрешены в рамках правил группы.'); return
    policy,_=await runtime.groups.policies.get(scope.chat_id,scope.topic_id)
    if not args:
        await update.effective_message.reply_text(policy_status(policy,runtime.clock())+'\n'+
            '/proactivity [group|topic] mentions|useful|social [shadow|live]\n'
            '/proactivity [group|topic] off|on — выключатель инициатив, включая напоминания\n'
            '/proactivity visibility full|partial; tz Europe/Moscow; limits 3 900; assessments 12; hours 23 9; reactions on|off; seeds on|off\n'
            '/proactivity optout|optin — мои предпочтения. /quiet 60 — пауза в минутах.'); return
    if not await is_admin(update.effective_user,scope.chat_id,context):
        await update.effective_message.reply_text('Правила группы меняет администратор.'); return
    target=None
    if args[0] in ('group','topic'):
        target=scope.topic_id if args.pop(0)=='topic' else None
    changes={}
    try:
        cmd=args.pop(0)
        if cmd in ('mentions','useful','social'):
            changes['mode']=cmd
            if args: changes['execution']=args.pop(0)
        elif cmd in ('off','on'): changes['disabled']=cmd=='off'
        elif cmd=='visibility':
            choice=args.pop(0)
            if choice not in ('full','partial'): raise ValueError()
            observed=False
            if choice=='full':
                try:
                    me=await context.bot.get_me()
                    member=await context.bot.get_chat_member(scope.chat_id,me.id)
                    observed=member.status in ('administrator','creator') or bool(getattr(me,'can_read_all_group_messages',False))
                except Exception: observed=False
                if not observed:
                    await update.effective_message.reply_text('Полную видимость подтвердить не удалось. Проверь права и Privacy Mode в BotFather.'); return
            changes['full_visibility']=observed
        elif cmd=='tz': changes['timezone']=args.pop(0)
        elif cmd=='limits': changes.update(daily_limit=int(args.pop(0)),spacing_seconds=int(args.pop(0)))
        elif cmd=='assessments': changes['assessment_hourly_limit']=int(args.pop(0))
        elif cmd=='hours': changes.update(quiet_start=int(args.pop(0)),quiet_end=int(args.pop(0)))
        elif cmd in ('reactions','seeds'):
            value=args.pop(0)
            if value not in ('on','off'): raise ValueError()
            changes['reactions' if cmd=='reactions' else 'topic_seeds']=value=='on'
        else: raise ValueError()
        if args: raise ValueError()
        await runtime.groups.policies.set(scope.chat_id,changes,target)
    except (ValueError,IndexError,KeyError):
        await update.effective_message.reply_text('Некорректные настройки. /proactivity показывает формат и текущий режим.'); return
    if changes.get('execution')=='live' and runtime.mode!='legacy':
        from config import rp_mode_state
        mode='rp' if rp_mode_state.get(scope.chat_id) else 'default'
        cid,_,_=await runtime.ingest(scope.chat_id,update.effective_user.id,'','control:'+uuid.uuid4().hex,mode,origin=Origin.SYSTEM,event_kind='system',addressed_to_arti=False)
        await runtime.set_authority(cid,'active')
    policy,_=await runtime.groups.policies.get(scope.chat_id,scope.topic_id)
    await update.effective_message.reply_text('Сохранено. '+policy_status(policy,runtime.clock()))


async def quiet_command(update,context):
    scope=CURRENT_SCOPE.get(); runtime=get_runtime()
    if not runtime or not scope or not scope.group or not await is_admin(update.effective_user,scope.chat_id,context):
        await update.effective_message.reply_text('Пауза групповых инициатив доступна администратору.'); return
    try:
        if len(context.args)!=1: raise ValueError()
        minutes=int(context.args[0])
        if not 0<=minutes<=10080: raise ValueError()
        until=(runtime.clock()+timedelta(minutes=minutes)).isoformat() if minutes else None
        await runtime.groups.policies.set(scope.chat_id,dict(paused_until=until),scope.topic_id)
    except (ValueError,IndexError):
        await update.effective_message.reply_text('/quiet 60 — пауза на час; /quiet 0 — снять паузу.'); return
    await update.effective_message.reply_text('Пауза самостоятельных сообщений сохранена.' if minutes else 'Пауза снята.')
