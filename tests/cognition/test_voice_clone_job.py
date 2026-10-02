"""Voice worker boundaries using synthetic files/providers only."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock,patch
import wave

from ai.voice_clone_job import generate_clone,_write_json,_read_json,_inside_file,MAX_MANIFEST_BYTES
from ai import voice_clone_worker as worker


def wav(path,seconds=1):
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
        stream.writeframes(b'\0\0'*(8000*seconds))


def manifest(root,**changes):
    wav(root/'reference.wav')
    data=dict(version=1,reference='reference.wav',text='(calm) Synthetic speech')
    data.update(changes)
    path=root/'clone-input.json'; _write_json(path,data,MAX_MANIFEST_BYTES)
    return path


def provider(root,seconds=1):
    module=types.ModuleType('ai.voice_clone')
    module.normalize_text_via_llm=AsyncMock(side_effect=lambda text:text)
    async def synthesize(*args):
        output=root/'generated.wav'; wav(output,seconds); return output
    module.synthesize_with_clone=AsyncMock(side_effect=synthesize)
    return module


CHILD='''import asyncio,sys,types,wave
from pathlib import Path
module=types.ModuleType('ai.voice_clone')
async def normalize(text): return text
async def synthesize(reference,text,work,direction):
    assert reference.parent==work and reference.name=='reference.wav'
    output=work/'generated.wav'
    with wave.open(str(output),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\\0\\0'*8000)
    return output
module.normalize_text_via_llm=normalize;module.synthesize_with_clone=synthesize
sys.modules['ai.voice_clone']=module
from ai.voice_clone_worker import run
asyncio.run(run(Path(sys.argv[1])))
'''


class CloneWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_stops_fake_gpu_thread_and_its_owned_descendant(self):
        import psutil
        from utils.process_limits import create_owned_subprocess_exec as actual
        child_code='''import asyncio,json,os,subprocess,sys,time,types
from pathlib import Path
module=types.ModuleType('ai.voice_clone')
async def normalize(text): return text
async def synthesize(reference,text,work,direction):
 p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
 (work/'live-pids.json').write_text(json.dumps([os.getpid(),p.pid]))
 await asyncio.to_thread(time.sleep,60)
 return None
module.normalize_text_via_llm=normalize;module.synthesize_with_clone=synthesize
sys.modules['ai.voice_clone']=module
from ai.voice_clone_worker import run
asyncio.run(run(Path(sys.argv[1])))
'''
        async def spawn(*args,**kwargs): return await actual(sys.executable,'-c',child_code,args[-1],**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'attempt'; work.mkdir(); source=root/'original.wav'; wav(source)
            with patch('utils.process_limits.create_owned_subprocess_exec',side_effect=spawn):
                task=asyncio.create_task(generate_clone(source,'synthetic',work))
                try:
                    async with asyncio.timeout(5):
                        while not (work/'live-pids.json').exists():
                            if task.done(): await task
                            await asyncio.sleep(.01)
                    pids=json.loads((work/'live-pids.json').read_text())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,5)
                    for _ in range(40):
                        alive=[pid for pid in pids if psutil.pid_exists(pid) and psutil.Process(pid).status()!=psutil.STATUS_ZOMBIE]
                        if not alive: break
                        await asyncio.sleep(.025)
                    self.assertEqual([],alive)
                finally:
                    if not task.done(): task.cancel()
                    await asyncio.gather(task,return_exceptions=True)

    async def test_fake_generator_child_end_to_end_uses_relative_copy_and_real_conversion(self):
        from utils.process_limits import create_owned_subprocess_exec as actual
        async def spawn(*args,**kwargs):
            return await actual(sys.executable,'-c',CHILD,args[-1],**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'attempt'; work.mkdir(); source=root/'original.wav'; wav(source)
            before=source.read_bytes()
            with patch('utils.process_limits.create_owned_subprocess_exec',side_effect=spawn):
                result,channel=await generate_clone(source,'Synthetic speech',work)
            self.assertEqual('voice',channel); self.assertEqual(work/'result.ogg',result)
            self.assertEqual(b'OggS',result.read_bytes()[:4])
            self.assertEqual(before,source.read_bytes()); self.assertEqual(before,(work/'reference.wav').read_bytes())
            data=_read_json(work/'clone-input.json',MAX_MANIFEST_BYTES)
            self.assertEqual('reference.wav',data['reference']); self.assertNotIn(str(source),json.dumps(data))

    async def test_long_audio_selects_mp3_and_keeps_direction_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=manifest(root); fake=provider(root,31)
            with patch.dict(sys.modules,{'ai.voice_clone':fake}): await worker.run(path)
            self.assertEqual({'leaf':'result.mp3','channel':'audio'},_read_json(root/'clone-result.json',1024))
            self.assertGreater((root/'result.mp3').stat().st_size,0)
            self.assertEqual('calm',fake.synthesize_with_clone.await_args.args[3])

    async def test_invalid_manifest_rejected_before_any_provider(self):
        for changes in ({'reference':'../outside.wav'},{'reference':'C:\\private.wav'},{'version':True},{'text':[]},{'extra':'ignored?'}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); path=manifest(root,**changes); fake=provider(root)
                with patch.dict(sys.modules,{'ai.voice_clone':fake}),self.assertRaises(ValueError): await worker.run(path)
                fake.normalize_text_via_llm.assert_not_awaited(); fake.synthesize_with_clone.assert_not_awaited()

    async def test_provider_output_cannot_escape_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'attempt'; work.mkdir(); path=manifest(work); fake=provider(work)
            outside=root/'outside.wav'; wav(outside); fake.synthesize_with_clone=AsyncMock(return_value=outside)
            with patch.dict(sys.modules,{'ai.voice_clone':fake}),patch('asyncio.create_subprocess_exec',new=AsyncMock()) as spawn:
                with self.assertRaisesRegex(ValueError,'outside_attempt'): await worker.run(path)
            spawn.assert_not_awaited()

    async def test_preexisting_result_never_overwritten_or_sent_to_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=manifest(root); fake=provider(root); (root/'result.ogg').write_bytes(b'keep')
            with patch.dict(sys.modules,{'ai.voice_clone':fake}),self.assertRaisesRegex(ValueError,'already_exists'): await worker.run(path)
            self.assertEqual(b'keep',(root/'result.ogg').read_bytes()); fake.normalize_text_via_llm.assert_not_awaited()

    async def test_probe_output_is_bounded_and_process_stopped(self):
        probe=types.SimpleNamespace(stdout=types.SimpleNamespace(read=AsyncMock(return_value=b'x'*257)),returncode=None,wait=AsyncMock())
        with patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=probe)),patch('utils.process_limits.stop_process',new=AsyncMock()) as stop:
            with self.assertRaisesRegex(RuntimeError,'output_budget'): await worker._duration(Path('/synthetic.wav'))
        stop.assert_awaited_once_with(probe); probe.wait.assert_not_awaited()

    async def test_probe_rejects_nonfinite_invalid_and_failed_results(self):
        for raw,code in ((b'nan',0),(b'inf',0),(b'0',0),(b'-1',0),(b'garbage',0),(b'1',1)):
            probe=types.SimpleNamespace(stdout=types.SimpleNamespace(read=AsyncMock(side_effect=[raw,b''])),returncode=code,wait=AsyncMock())
            with self.subTest(raw=raw,code=code),patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=probe)):
                with self.assertRaisesRegex(RuntimeError,'clone_probe_failed'): await worker._duration(Path('/synthetic.wav'))

    async def test_conversion_failure_does_not_publish_metadata_and_is_noninteractive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=manifest(root); fake=provider(root)
            process=types.SimpleNamespace(returncode=1,communicate=AsyncMock(return_value=(b'',b'')))
            with patch.dict(sys.modules,{'ai.voice_clone':fake}),patch.object(worker,'_duration',new=AsyncMock(return_value=1)),patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)) as spawn:
                with self.assertRaisesRegex(RuntimeError,'conversion_failed'): await worker.run(path)
            command=spawn.await_args.args
            self.assertIn('-n',command); self.assertIn('-nostdin',command); self.assertNotIn('-y',command)
            self.assertIn('file,pipe',command); self.assertFalse((root/'clone-result.json').exists())

    async def test_copy_cancellation_leaves_no_background_writer_or_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'attempt'; work.mkdir(); source=root/'source.wav'; source.write_bytes(b'x'*(3*1024**2))
            with patch('utils.process_limits.create_owned_subprocess_exec',new=AsyncMock()) as spawn:
                task=asyncio.create_task(generate_clone(source,'synthetic',work))
                await asyncio.sleep(0); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                size=(work/'reference.wav').stat().st_size
                await asyncio.sleep(.02)
                self.assertEqual(size,(work/'reference.wav').stat().st_size); spawn.assert_not_awaited()

    async def test_result_json_schema_and_budget_are_strict(self):
        for value in ({'leaf':'../outside','channel':'voice'}, {'leaf':'result.ogg','channel':'voice','extra':'bad'}):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory); work=root/'attempt'; work.mkdir(); source=root/'source.wav'; wav(source)
                async def spawn(*args,**kwargs):
                    _write_json(work/'clone-result.json',value,1024)
                    return types.SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'',b'')))
                with patch('utils.process_limits.create_owned_subprocess_exec',side_effect=spawn),self.assertRaises(ValueError):
                    await generate_clone(source,'synthetic',work)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'metadata.json'; path.write_bytes(b'x'*1025)
            with self.assertRaisesRegex(ValueError,'budget'): _read_json(path,1024)

    async def test_output_regular_file_cap_and_nonempty_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'result.ogg'; path.write_bytes(b'')
            with self.assertRaises(ValueError): _inside_file(path,root)
            path.write_bytes(b'0123456789')
            with self.assertRaises(ValueError): _inside_file(path,root,maximum=8)
