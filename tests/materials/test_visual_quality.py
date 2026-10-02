"""Synthetic, local-only video sampling and measured artifact readability."""
import base64
from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch
from PIL import Image
from artifacts.spec import ArtifactSpec
from artifacts.rendering import scene,check_layout,HEIGHT
from materials.extractors.video_decoder import video_frames,reinspection_points
from materials.types import MaterialError
from tests.materials.test_artifacts import fixture
from tests.materials.video_fixtures import video


def sample(ms,color):
    stream=BytesIO(); Image.new('RGB',(24,24),color).save(stream,format='PNG')
    return dict(timestamp_ms=ms,image_base64=base64.b64encode(stream.getvalue()).decode())


class AdaptiveVideoTests(unittest.TestCase):
    def test_content_change_selects_midpoint_static_content_does_not(self):
        frames=[sample(0,'black'),sample(1000,'white')]
        self.assertEqual([dict(requested_ms=500,reason='changed_sample_gap')],reinspection_points(frames,[],0,1100,2))
        self.assertEqual([],reinspection_points([sample(0,'black'),sample(1000,'black')],[],0,1100,2))

    def test_transition_budget_interval_and_dedup_are_strict(self):
        frames=[sample(0,'black'),sample(900,'black'),sample(2000,'white')]
        points=reinspection_points(frames,[0,1000,1000,2000],0,2000,2)
        self.assertEqual([1900,100], [p['requested_ms'] for p in points])
        self.assertEqual([],reinspection_points(frames,[1000],0,2000,0))

    def test_actual_short_event_recaptured_with_native_pts_and_fixed_budget(self):
        result=video_frames(video(with_audio=False,transient_frame=9,transient_box=(360,40,410,80)),'video/mp4',max_frames=6)
        frames=result['frames']; adaptive=[f for f in frames if f['method'].startswith('adaptive_')]
        self.assertLessEqual(len(frames),6)
        self.assertEqual([900],[f['timestamp_ms'] for f in adaptive])
        self.assertNotIn(900,[f['timestamp_ms'] for f in frames if f['method']=='uniform'])
        with Image.open(BytesIO(base64.b64decode(adaptive[0]['image_base64']))) as image:
            r,g,b=image.getpixel((int(image.width*.8),int(image.height*.2)))
            self.assertGreater(min(r,g),180); self.assertLess(b,90)
        self.assertLessEqual(sum(len(base64.b64decode(f['image_base64'])) for f in frames),2*1024**2)
        self.assertIn('sparse_frames_do_not_prove_event_absence',result['limitations'])

    def test_dense_mode_does_not_recursively_reinspect(self):
        result=video_frames(video(with_audio=False),'video/mp4',max_frames=3,start_ms=1350,end_ms=1500,dense=True)
        self.assertEqual([],result['reinspection']); self.assertEqual(0,result['adaptive_frame_budget'])
        self.assertLessEqual(len(result['frames']),3)
        self.assertTrue(all(1350<=f['timestamp_ms']<1500 for f in result['frames']))

    def test_refinement_failure_preserves_initial_evidence(self):
        from materials.extractors import video_decoder
        original=video_decoder.captured
        def captured(command,**kwargs):
            if command[-1].endswith('adaptive.jpg'): raise MaterialError('video_decoder_failed')
            return original(command,**kwargs)
        with patch.object(video_decoder,'captured',side_effect=captured):
            result=video_frames(video(with_audio=False),'video/mp4',max_frames=6)
        self.assertTrue(result['frames'])
        self.assertIn('adaptive_reinspection_failed',result['limitations'])
        self.assertEqual('video_decoder_failed',result['reinspection'][0]['error'])


class ContentLayoutTests(unittest.TestCase):
    def spec(self): return ArtifactSpec(fixture())

    def test_content_omission_is_detected_even_when_element_id_remains(self):
        spec=self.spec(); pages,_=scene(spec)
        for p in pages:
            p.items[:]=[i for i in p.items if i.get('text')!='Неизвестное значение не заменяется нулём.']
        with self.assertRaisesRegex(MaterialError,'artifact_content_omitted'): check_layout(pages,spec)

    def test_real_glyph_vertical_overflow_is_detected(self):
        spec=self.spec(); pages,_=scene(spec)
        item=next(i for i in pages[0].items if i['kind']=='text')
        item['y']=HEIGHT-10
        with self.assertRaisesRegex(MaterialError,'artifact_text_overflow'): check_layout(pages,spec)

    def test_text_collision_and_unreadably_small_text_are_detected(self):
        spec=self.spec(); pages,_=scene(spec)
        pages[0].items.append(deepcopy(next(i for i in pages[0].items if i['kind']=='text')))
        with self.assertRaisesRegex(MaterialError,'artifact_text_overlap'): check_layout(pages,spec)
        pages,_=scene(spec); next(i for i in pages[0].items if i['kind']=='text')['size']=8
        with self.assertRaisesRegex(MaterialError,'artifact_text_too_small'): check_layout(pages,spec)

    def test_glyphs_must_fit_their_content_panel(self):
        spec=self.spec(); pages,_=scene(spec)
        item=next(i for i in pages[0].items if i.get('panel'))
        x,y,w,h=item['panel']; item['panel']=(x,y,10,h)
        with self.assertRaisesRegex(MaterialError,'artifact_text_outside_panel'): check_layout(pages,spec)

    def test_long_title_changes_overview_pagination_without_shrinking(self):
        value=fixture(); value['format']='process'
        value['elements']=[dict(id=f'e{i}',label=f'Этап {i}',status='proposed') for i in range(6)]
        short,_=scene(ArtifactSpec(value))
        value['title']='W'*300
        long,_=scene(ArtifactSpec(value))
        cards=lambda p: sum(i['kind']=='rect' and i.get('w')==460 for i in p.items)
        self.assertEqual(6,cards(short[0])); self.assertLess(cards(long[0]),6)
        self.assertEqual(6,sum(cards(p) for p in long))
        self.assertTrue(all(i['size']>=18 for p in long for i in p.items if i['kind']=='text'))
        # Verify rendered output, not just scene metadata: full labels survive PDF.
        from artifacts.export import export
        from pypdf import PdfReader
        outputs=export(ArtifactSpec(value))
        text=' '.join(p.extract_text() for p in PdfReader(BytesIO(outputs['report.pdf'])).pages)
        for i in range(6): self.assertIn(f'Этап {i}',text)
        for name,data in outputs.items():
            if name.endswith('.png'):
                with Image.open(BytesIO(data)) as image: image.verify()

    def test_wrapped_unicode_body_preserved_across_pages(self):
        value=fixture(); value['title']='W'*300; value['elements'][0]['text']='Очень длинный текст. '*95
        spec=ArtifactSpec(value); pages,_=scene(spec)
        self.assertGreater(len(pages),1); check_layout(pages,spec)
