"""Command-boundary tests with synthetic Telegram metadata and no providers."""
import ast
import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot.media_provenance import reference_source
from cognition.scope import CURRENT_SCOPE, TransportScope


def functions(*names):
    tree = ast.parse(Path('bot/commands.py').read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    ns = dict(asyncio=asyncio, asynccontextmanager=asynccontextmanager, CURRENT_SCOPE=CURRENT_SCOPE,
              _media_reference_source=reference_source, _time=NS(time=lambda: 1),
              _vclone_cleanup_keyboard=lambda: None, vclone_flow_state=defaultdict(dict))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes,
                            type_ignores=[])), 'bot/commands.py', 'exec'), ns)
    return ns


def message(mid, uid, bot=False):
    return NS(message_id=mid, chat_id=10, chat=NS(id=10, is_forum=False),
              from_user=NS(id=uid, is_bot=bot), sender_chat=None, message_thread_id=None,
              reply_to_message=None, reply_text=AsyncMock(return_value=NS(message_id=999)))


class CommandProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = CURRENT_SCOPE.set(TransportScope(10, 0, 'supergroup', 7, 100))
    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.token)

    async def test_reply_source_owner_survives_requester_and_menu(self):
        ns = functions('_vclone_setup_cleanup_choice', '_vclone_build_task')
        msg = message(100, 7); msg.reply_to_message = message(90, 8)
        await ns['_vclone_setup_cleanup_choice'](NS(message=msg, effective_chat=NS(id=10)), None,
            reference_path=Path('synthetic.wav'), synthesis_text='hello', source_kind='reply_voice')
        source = ns['vclone_flow_state'][10][7]['reference_source']
        self.assertEqual((source['message_id'], source['user_id']), (90, 8))
        task = ns['_vclone_build_task'](chat_id=10, user_id=7, user_name='requester', message_id=100,
            reference_path='synthetic.wav', synthesis_text='hello', cleaned_path=None,
            cleaned=False, source_kind='reply_voice', context=None, reference_source=source)
        self.assertEqual(task['reference_source'], source)

    async def test_actual_attachment_override_and_bot_source_exclusion(self):
        ns = functions('_vclone_setup_cleanup_choice')
        msg = message(100, 7); msg.reply_to_message = message(90, 8)
        update = NS(message=msg, effective_chat=NS(id=10))
        await ns['_vclone_setup_cleanup_choice'](update, None, reference_path=Path('synthetic.wav'),
            synthesis_text=None, source_kind='reply_voice', reference_message=msg)
        self.assertEqual(ns['vclone_flow_state'][10][7]['reference_source']['message_id'], 100)
        msg.reply_to_message = message(90, 99, bot=True)
        await ns['_vclone_setup_cleanup_choice'](update, None, reference_path=Path('synthetic.wav'),
            synthesis_text=None, source_kind='reply_voice')
        self.assertIsNone(ns['vclone_flow_state'][10][7]['reference_source'])

    async def test_retained_reference_revalidates_and_holds_during_upload(self):
        ns = functions('_vclone_save_reference')
        retained = {'namespace': 'owned', 'descriptor': {'verified': True}}
        retention = NS(load=AsyncMock(return_value=retained))
        held = []
        @contextmanager
        def hold(namespace):
            held.append(namespace)
            try: yield
            finally: held.pop()
        disk = NS(hold=hold, resolve=Mock(return_value=Path('/synthetic/verified.wav')))
        with patch('bot.media_retention.Retention', return_value=retention), \
             patch('bot.media_jobs.spool', return_value=disk), \
             patch('bot.request_runtime.store', return_value=NS(pool=object())):
            async with ns['_vclone_save_reference']({'media_retained_id': 'request', 'reference_path': '/stale'}, 10, 7) as path:
                self.assertEqual(path, '/synthetic/verified.wav')
                self.assertEqual(held, ['owned'])
            self.assertEqual(held, [])
            self.assertEqual(retention.load.await_count, 2)
            retention.load.assert_awaited_with('request', 'voice_reference', 7, 10, 0)

    async def test_expired_or_forgotten_reference_never_yields_stale_path(self):
        ns = functions('_vclone_save_reference')
        from bot.media_retention import RetentionUnavailable
        for results in ([None], [{'namespace': 'owned', 'descriptor': {}}, None]):
            retention = NS(load=AsyncMock(side_effect=results))
            @contextmanager
            def hold(namespace): yield
            disk = NS(hold=hold, resolve=Mock())
            with patch('bot.media_retention.Retention', return_value=retention), \
                 patch('bot.media_jobs.spool', return_value=disk), \
                 patch('bot.request_runtime.store', return_value=NS(pool=object())):
                with self.assertRaises(RetentionUnavailable):
                    async with ns['_vclone_save_reference']({'media_retained_id': 'request', 'reference_path': '/stale'}, 10, 7):
                        self.fail('stale offer reached upload')
                disk.resolve.assert_not_called()

    def test_both_task_builders_and_delayed_dub_pass_stored_source(self):
        tree = ast.parse(Path('bot/commands.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_vclone_build_task':
                self.assertIn('reference_source', [kw.arg for kw in node.keywords])
        flow = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_dub_flow')
        call = next(n for n in ast.walk(flow) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_enqueue_dub_task')
        value = next(kw.value for kw in call.keywords if kw.arg == 'reference_source')
        self.assertEqual(ast.unparse(value), "state.get('reference_source')")
