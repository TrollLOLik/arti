"""Operational logs retain categories/IDs, never conversational payloads."""
import logging
import re


class PrivatePayloadFilter(logging.Filter):
    prefixes = ('ai.','bot.','memory.','utils.','database.','httpx','httpcore','openai','google')

    def filter(self,record):
        if record.name.startswith(self.prefixes):
            from cognition.runtime import CURRENT_TURN
            turn = CURRENT_TURN.get()
            source = turn.event.event_id if turn else 'none'
            category = record.exc_info[0].__name__ if record.exc_info else 'operation'
            record.msg = f'component={record.name} category={category} source={source}'
            event = getattr(record,'arti_event',None)
            if isinstance(event,str) and re.fullmatch(r'[a-z_]{1,64}',event):
                record.msg += ' event='+event
            duration = getattr(record,'duration_ms',None)
            if isinstance(duration,(int,float)) and 0<=duration<=3600000:
                record.msg += ' duration_ms='+str(round(duration))
            if record.exc_info:
                status = getattr(record.exc_info[1],'status_code',None) or getattr(record.exc_info[1],'code',None)
                if isinstance(status,int) and 100<=status<=599:
                    record.msg += ' http_status='+str(status)
                trace = record.exc_info[2]
                if trace:
                    while trace.tb_next:
                        trace = trace.tb_next
                    from pathlib import Path
                    record.msg += f' at={Path(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}'
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
