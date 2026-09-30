"""Operational logs retain categories/IDs, never conversational payloads."""
import logging


class PrivatePayloadFilter(logging.Filter):
    prefixes = ('ai.','bot.','memory.','utils.','database.','httpx','httpcore','openai','google')

    def filter(self,record):
        if record.name.startswith(self.prefixes):
            from cognition.runtime import CURRENT_TURN
            turn = CURRENT_TURN.get()
            source = turn.event.event_id if turn else 'none'
            category = record.exc_info[0].__name__ if record.exc_info else 'operation'
            record.msg = f'component={record.name} category={category} source={source}'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_private_log_filter():
    handlers = list(logging.getLogger().handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger,logging.Logger):
            handlers.extend(logger.handlers)
    for handler in set(handlers):
        if not any(isinstance(f,PrivatePayloadFilter) for f in handler.filters):
            handler.addFilter(PrivatePayloadFilter())
