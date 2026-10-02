"""Foreground deadlines and local child cleanup, no Telegram/provider calls."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from bot.intake import bounded_intake,note_accepted,ACCEPTED_REQUESTS
from utils.process_limits import communicate_bounded


class IntakeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_claim_unsaved_work_completed(self):
        async def intake(*args): await asyncio.sleep(10)
        update=NS(effective_chat=NS(id=1),effective_message=NS(message_thread_id=None))
        with patch('telegram.ext.ExtBot.send_message',new=AsyncMock()) as send:
            await bounded_intake(intake,seconds=.01)(update,NS(bot=object()))
        self.assertIn('Не удалось',send.await_args.kwargs['text'])
        self.assertIsNone(ACCEPTED_REQUESTS.get())

    async def test_committed_job_survives_foreground_timeout(self):
        async def intake(*args): note_accepted('fixture_id'); await asyncio.sleep(10)
        update=NS(effective_chat=NS(id=1),effective_message=NS(message_thread_id=4))
        with patch('telegram.ext.ExtBot.send_message',new=AsyncMock()) as send:
            await bounded_intake(intake,seconds=.01)(update,NS(bot=object()))
        self.assertIn('/request fixture_id',send.await_args.kwargs['text'])
        self.assertEqual(send.await_args.kwargs['message_thread_id'],4)

    async def test_external_cancellation_propagates_without_notice(self):
        started=asyncio.Event()
        async def intake(*args): started.set(); await asyncio.sleep(10)
        with patch('telegram.ext.ExtBot.send_message',new=AsyncMock()) as send:
            task=asyncio.create_task(bounded_intake(intake)(NS(),NS()))
            await started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            send.assert_not_awaited()

    async def test_subprocess_deadline_stops_owned_process(self):
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(30)',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        with self.assertRaises(TimeoutError): await communicate_bounded(process,.03)
        self.assertIsNotNone(process.returncode)

    async def test_dubbing_cancellation_and_timeout_stop_process(self):
        import ai.dubbing as dubbing
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'main.py').write_text('import time; time.sleep(30)')
            for cancel in (False,True):
                created=[]; original=asyncio.create_subprocess_exec
                async def spawn(*args,**kwargs):
                    process=await original(*args,**kwargs); created.append(process); return process
                with patch.object(dubbing,'VIDEOTRANS_DIR',root),patch.object(dubbing,'VIDEOTRANS_RUNS',root/'runs'),\
                     patch.object(dubbing,'_resolve_python',return_value=Path(sys.executable)),patch('asyncio.create_subprocess_exec',spawn):
                    task=asyncio.create_task(dubbing.run_dubbing('',str(cancel),input_file=root/'synthetic.wav',timeout_seconds=.06 if not cancel else 30))
                    if cancel:
                        while not created: await asyncio.sleep(.001)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError): await task
                    else:
                        result=await task; self.assertFalse(result[0]); self.assertIn('срок',result[2])
                    self.assertIsNotNone(created[0].returncode)
