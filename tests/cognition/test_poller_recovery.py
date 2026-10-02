"""Offline lifecycle coverage: no Telegram, configured DB, or external providers."""
import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import main
from utils.instance_lock import AlreadyRunning


class PollerHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_and_timed_out_health_requests_recovery(self):
        async def slow():
            await asyncio.Event().wait()
        for health in (AsyncMock(return_value=False), AsyncMock(side_effect=OSError()), slow):
            app=NS(bot_data={'poller_lease':NS(healthy=health)},stop_running=Mock())
            with patch.object(main,'_instance_lock',None):
                await main.watch_instance(app,interval=0,health_timeout=.01)
            self.assertEqual(app.bot_data['stop_reason'],'lease_lost')
            app.stop_running.assert_called_once()

    async def test_explicit_stop_wins_over_health_failure(self):
        app=NS(bot_data={'poller_lease':NS(healthy=AsyncMock(return_value=False))},stop_running=Mock())
        with patch.object(main,'_instance_lock',NS(stop_requested=lambda:True)):
            await main.watch_instance(app,interval=0)
        self.assertEqual(app.bot_data['stop_reason'],'requested')

    async def test_conflict_during_health_check_stays_terminal(self):
        from bot.handlers import error_handler
        from telegram.error import Conflict
        entered=asyncio.Event(); finish=asyncio.Event()
        async def health():
            entered.set(); await finish.wait(); return False
        app=NS(bot_data={'poller_lease':NS(healthy=health)},stop_running=Mock())
        with patch.object(main,'_instance_lock',None):
            task=asyncio.create_task(main.watch_instance(app,interval=0))
            await entered.wait()
            await error_handler(None,NS(error=Conflict('synthetic'),application=app))
            finish.set(); await task
        self.assertEqual(app.bot_data['stop_reason'],'polling_conflict')


class PollerRestartTests(unittest.TestCase):
    def run_lifecycle(self,reasons,*,deny_reacquire=False,stop_during_backoff=False):
        events=[]; apps=[]; stop={'requested':False}
        class App:
            def __init__(self):
                self.bot=NS(id=99,username='synthetic_bot')
                self.bot_data={}; self.handlers={}; self.hooks={}
            def add_handler(self,*args,**kwargs): pass
            def add_error_handler(self,*args,**kwargs): pass
            def stop_running(self): pass
            def run_polling(self,**kwargs):
                loop=asyncio.get_event_loop()
                try:
                    loop.run_until_complete(self.hooks['post_init'](self))
                    self.bot_data['stop_reason']=reasons[len(apps)-1]
                    events.append('poll')
                    loop.run_until_complete(self.hooks['post_stop'](self))
                finally:
                    loop.run_until_complete(self.hooks['post_shutdown'](self))
        class Builder:
            def __init__(self): self.app=App(); apps.append(self.app)
            def bot(self,*args): return self
            def concurrent_updates(self,*args): return self
            def post_init(self,f): self.app.hooks['post_init']=f; return self
            def post_stop(self,f): self.app.hooks['post_stop']=f; return self
            def post_shutdown(self,f): self.app.hooks['post_shutdown']=f; return self
            def build(self): return self.app
        class Lease:
            def __init__(self,*args): pass
            async def acquire(self):
                events.append('lease_acquire')
                if deny_reacquire and len(apps)>1: raise AlreadyRunning()
                return self
            async def close(self): events.append('lease_close')
            async def healthy(self): return True
        async def idle(*args): await asyncio.Event().wait()
        async def init_db(): events.append('db_init')
        async def close_db(): events.append('db_close')
        def sleep(_):
            events.append('backoff')
            if stop_during_backoff: stop['requested']=True
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(main,'ApplicationBuilder',Builder))
                stack.enter_context(patch.object(main,'RetryBot',return_value=NS()))
                stack.enter_context(patch.object(main,'HTTPXRequest',return_value=NS()))
                stack.enter_context(patch.object(main,'run_supervised',new=idle))
                stack.enter_context(patch.object(main,'_instance_lock',NS(stop_requested=lambda:stop['requested'])))
                stack.enter_context(patch('utils.instance_lock.PollerLease',Lease))
                stack.enter_context(patch('time.sleep',sleep))
                stack.enter_context(patch('database.connection.init_db',new=init_db))
                stack.enter_context(patch('database.connection.close_db',new=close_db))
                # Location lifecycle is tested independently; keep this service
                # inert while exercising main's actual init/shutdown callbacks.
                stack.enter_context(patch('utils.location_manager.maintenance_worker',new=idle,create=True))
                for target,result in [('cognition.runtime.start_runtime',NS()),('cognition.runtime.stop_runtime',None),
                                      ('bot.menu.install',None),('bot.queue.drain_background_tasks',None)]:
                    stack.enter_context(patch(target,new=AsyncMock(return_value=result)))
                main.run_with_restart()
        finally:
            loop=asyncio.get_event_loop()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close(); asyncio.set_event_loop(None)
            main.application=None
        return apps,events

    def test_lease_loss_restarts_only_after_cleanup_and_reacquisition(self):
        apps,events=self.run_lifecycle(['lease_lost','requested'])
        self.assertEqual(len(apps),2)
        self.assertEqual(events,['db_init','lease_acquire','poll','lease_close','db_close','backoff',
                                 'db_init','lease_acquire','poll','lease_close','db_close'])

    def test_explicit_stop_and_conflict_do_not_restart(self):
        for reason in ('requested','polling_conflict',None):
            apps,events=self.run_lifecycle([reason])
            self.assertEqual(len(apps),1)
            self.assertNotIn('backoff',events)

    def test_rival_poller_prevents_recovery_polling(self):
        apps,events=self.run_lifecycle(['lease_lost','requested'],deny_reacquire=True)
        self.assertEqual(len(apps),2)
        self.assertEqual(events.count('poll'),1)
        self.assertEqual(events.count('lease_acquire'),2)

    def test_stop_during_backoff_prevents_next_start(self):
        apps,events=self.run_lifecycle(['lease_lost'],stop_during_backoff=True)
        self.assertEqual(len(apps),1)
        self.assertEqual(events.count('poll'),1)
