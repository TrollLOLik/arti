"""Deterministic rose-theme layouts; no providers, accounts or real documents."""
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
import unittest
import base64
import re
import xml.etree.ElementTree as ET
from PIL import Image
from pypdf import PdfReader
from artifacts.export import export
from artifacts.rendering import scene, check_layout, source_lines, FONT, font_for
from artifacts.spec import ArtifactSpec, FORMATS
from artifacts.styles import StyleProfile, STYLES, semantic_theme, contrast
from materials.types import EvidenceRef, Locator, LocatorKind, MaterialError


def example(format='cards'):
    value=dict(contract='artifact-1', title='Северный сад / проверяемый обзор', format=format,
               elements=[],relations=[],style={})
    for i in range(3):
        value['elements'].append(dict(id=f'e{i}',label=['Почва и растения','Вода и маршруты','Что ещё проверить'][i],
            text='Предложение из синтетического примера. Решение пока не принято.',status='proposed',order=i,
            when=f'2026-10-{6+i:02d}'))
    if format in ('statistical','comparison','table'):
        for i,element in enumerate(value['elements']):
            element.pop('when',None)
            element['quantity']=dict(value=['-0.000001','2400','0'][i],unit='RUB')
            element['proof']=dict(kind='dataset',dataset_id='d'*64,address=f'B{i+1}')
        value['axis']=dict(unit='RUB',scale='linear') if format=='statistical' else {}
    return value


class VisualRenderingTests(unittest.TestCase):
    def test_semantic_palette_separates_text_from_decoration(self):
        theme=semantic_theme()
        self.assertEqual('#E4D8D8',theme['background'])
        self.assertEqual('#840018',theme['heading'])
        self.assertEqual('#D87890',theme['series'])
        self.assertNotIn('#E47854',theme.values())
        self.assertNotIn('#403020',theme.values())
        for style in STYLES.values():
            tokens=semantic_theme(style)
            for role in ('text','heading','secondary'):
                for fill in ('background','surface'):
                    self.assertGreaterEqual(contrast(tokens[role],tokens[fill]),4.5)
            self.assertGreaterEqual(contrast(tokens['inverse'],tokens['heading']),4.5)
        legacy=dict(background='#F4F3ED',foreground='#192A35',accent='#176C78',muted='#445660',
                    density='comfortable',illustration_tone='calm',font='DejaVuSans')
        self.assertEqual(legacy,StyleProfile.from_dict(legacy).to_dict())
        self.assertNotIn('series',StyleProfile().to_dict())

    def test_nine_formats_have_distinct_geometry_and_full_text(self):
        signatures=[]
        for format in sorted(FORMATS):
            spec=ArtifactSpec(example(format))
            pages,_=scene(spec)
            check_layout(pages,spec)
            signatures.append(tuple((i['kind'],i['x'],i['y'],i.get('w'),i.get('h'),i.get('radius'))
                                    for p in pages for i in p.items if i['kind']!='text'))
            self.assertTrue(any(i.get('radius',0)>0 for p in pages for i in p.items))
            for p in pages:
                self.assertTrue(all(i['size']>=18 for i in p.items if i['kind']=='text'))
        self.assertEqual(9,len(set(signatures)))

    def test_source_revision_locator_and_entire_identifier_survive(self):
        ref=EvidenceRef('a'*64,7,'b'*64,'paragraph-12',Locator(LocatorKind.PARAGRAPH,paragraph=12))
        value=example(); element=value['elements'][0]
        element.pop('when',None)
        element.update(status='observed',proof=dict(kind='quote',source=asdict(ref),quote=element['text']))
        value['elements']=[element]
        spec=ArtifactSpec(value)
        pages,_=scene(spec)
        rendered=''.join(''.join(i['text'].split()) for p in pages for i in p.items if i.get('owner')=='e0' or str(i.get('owner','')).startswith('source:'))
        for label in source_lines(element['proof']):
            self.assertIn(''.join(label.split()),rendered)
        self.assertIn('версия:7',rendered)
        self.assertIn('абзац:12',rendered)
        self.assertIn('a'*64,rendered)
        self.assertIn('b'*64,rendered)
        target=next(i for p in pages for i in p.items if i.get('text','').startswith('Источник:'))
        target['text']=''
        with self.assertRaisesRegex(MaterialError,'artifact_content_omitted'):
            check_layout(pages,spec)

    def test_negative_zero_tiny_and_interval_geometry_is_honest(self):
        value=example('statistical')
        value['elements'][1]['quantity'].update(lower='2000',upper='2600')
        pages,_=scene(ArtifactSpec(value))
        bars=[i for p in pages for i in p.items if i.get('semantic')=='value_bar']
        self.assertEqual(['2400'],[i['value'] for i in bars])
        self.assertTrue(any(i.get('semantic')=='interval' for p in pages for i in p.items))
        texts=' '.join(i['text'] for p in pages for i in p.items if i['kind']=='text')
        for text in ('-0.000001','[2000; 2600]','меньше пикселя','0 RUB'):
            self.assertIn(text,texts)
        value['elements'][0]['quantity']['value']='-1200'
        pages,_=scene(ArtifactSpec(value))
        bar=next(i for p in pages for i in p.items if i.get('value')=='-1200')
        self.assertAlmostEqual(bar['x']+bar['w'],bar['baseline'])

    def test_log_scale_uses_points_never_proportional_bars(self):
        value=example('statistical');value['axis']['scale']='log'
        for i,e in enumerate(value['elements']):e['quantity']['value']=str(10**i)
        pages,_=scene(ArtifactSpec(value))
        self.assertFalse(any(i.get('semantic')=='value_bar' for p in pages for i in p.items))
        self.assertTrue(any(i['kind']=='circle' for p in pages for i in p.items))

    def test_long_russian_and_long_identifiers_paginate_without_loss(self):
        for format in ('cards','comparison','timeline','table','teaching','process','arguments','roadmap'):
            value=example(format);value['title']='Длинное название с кириллицей. '*8
            value['elements'][0]['label']='Неопределённость и подробное описание. '*7
            value['elements'][0]['text']='Подробности необходимо сохранить полностью. '*43
            value['elements'][0]['id']='e'+'x'*63
            value['relations']=[dict(id='relation',kind='supports',**{'from':value['elements'][0]['id'],'to':'e1'},label='Основание. '*180)]
            value['questions']=['Вопрос о следующем шаге. '*35]
            spec=ArtifactSpec(value);pages,_=scene(spec)
            self.assertGreater(len(pages),1)
            check_layout(pages,spec)


    def test_svg_font_subset_keeps_original_unicode_glyphs_and_small_payload(self):
        value=example('comparison')
        value['elements'][0]['label']='Ёлки, résumé, façade: кириллица и составные символы'
        outputs=export(ArtifactSpec(value))
        total_svg=0
        for name,payload in outputs.items():
            if not name.endswith('.svg'):
                continue
            total_svg+=len(payload)
            encoded=re.search(rb'base64,([A-Za-z0-9+/=]+)',payload).group(1)
            font=font_for(28)
            from PIL import ImageFont
            subset=ImageFont.truetype(BytesIO(base64.b64decode(encoded)),28)
            root=ET.fromstring(payload)
            for node in root.findall('{http://www.w3.org/2000/svg}text'):
                text=node.text or ''
                self.assertEqual(bytes(font.getmask(text,features=['-kern','-liga'])),
                                 bytes(subset.getmask(text,features=['-kern','-liga'])))
                self.assertEqual(font.getlength(text,features=['-kern','-liga']),
                                 subset.getlength(text,features=['-kern','-liga']))
        self.assertLess(total_svg,400_000)

    def test_png_svg_pdf_use_the_same_glyph_top_and_stable_bytes(self):
        value=example();value['elements']=value['elements'][:1]
        spec=ArtifactSpec(value)
        outputs=export(spec)
        self.assertEqual(outputs,export(spec))
        with Image.open(BytesIO(outputs['page-1.png'])) as image:
            self.assertEqual((1080,1440),image.size)
        svg=ET.fromstring(outputs['page-1.svg'])
        pages,_=scene(spec)
        text_items=[i for i in pages[0].items if i['kind']=='text']
        svg_text=svg.findall('{http://www.w3.org/2000/svg}text')
        self.assertEqual(len(text_items),len(svg_text))
        for item,node in zip(text_items,svg_text):
            expected=item['y']-font_for(item['size']).getbbox(item['text'],anchor='ls',features=['-kern','-liga'])[1]
            self.assertAlmostEqual(expected,float(node.attrib['y']))
            self.assertEqual(item['text'],node.text or '')
        pdf_text=''.join(''.join(p.extract_text().split()) for p in PdfReader(BytesIO(outputs['report.pdf'])).pages)
        for element in value['elements']:
            for key in ('label','text'):
                self.assertIn(''.join(element[key].split()),pdf_text)
