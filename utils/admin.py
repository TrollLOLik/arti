"""
Проверка прав администратора
"""
import logging
from telegram import ChatMember

from config import PRIVILEGED_USER_IDS

logger = logging.getLogger(__name__)


async def is_admin(user, chat_id, context):
    from cognition.scope import CURRENT_SCOPE
    scope=CURRENT_SCOPE.get()
    if scope and scope.chat_id==chat_id and scope.chat_type=='private' and scope.user_id==user.id:
        return True
    if user.id in PRIVILEGED_USER_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        logger.info(f"Member status for {user.username}: {member.status}")
        return member.status in [ChatMember.CREATOR, ChatMember.ADMINISTRATOR]
    except Exception as e:
        logger.error(f"Ошибка при проверке администратора: {e}")
        return False

