"""Isolated image OCR and optional capability-routed visual observations."""
from dataclasses import replace
from hashlib import sha256
import time
from materials.extractors.documents import ocr_lines,DocumentExtractor
from materials.extractors.isolation import WorkerLimits,run_worker
from materials.extractors.ocr import OCR,installation,fingerprint
from materials.regions import displayed_image,crop,map_box,RegionRequest
from materials.types import ContentBlock,ExtractionBundle,ExtractionManifest,Locator,MaterialError,block_id,canonical
from materials.validation import inspect_bytes


class ImageExtractor:
    version='structured-image-1'
    def __init__(self,*,ocr_enabled=True,executable=None,tessdata=None,languages='rus+eng',max_lines=512,analyzer=None,stack_fingerprint=None):
        if not 1<=max_lines<=1024: raise MaterialError('invalid_image_options')
        command,directory=installation()
        self.options=dict(ocr_enabled=ocr_enabled,executable=command if executable is None else executable,
            tessdata=directory if tessdata is None else tessdata,languages=languages,max_lines=max_lines)
        self.options['stack_fingerprint']=stack_fingerprint or fingerprint(self.options['executable'],self.options['tessdata'],languages)
        self.analyzer=analyzer; self.limits=WorkerLimits()
    @property
    def cache_version(self):
        return self.version+':'+sha256(canonical([self.options,getattr(self.analyzer,'identity','local_ocr_only')]).encode()).hexdigest()[:24]
    async def extract_async(self,aid,version,data,mime):
        if self.analyzer is not None: raise MaterialError('visual_authorization_callback_required')
        return await self.extract_authorized(aid,version,data,mime,validate=None)
    async def extract_authorized(self,aid,version,data,mime,*,validate):
        value=await run_worker(dict(operation='image_extract',options=self.options,asset_id=aid,version=version,mime=mime),data,self.limits)
        bundle=ExtractionBundle.from_dict(value)
        if self.analyzer is None: return replace(bundle,extractor=self.cache_version)
        if validate is None: raise MaterialError('visual_authorization_callback_required')
        preview=await run_worker(dict(operation='image_preview',options=self.options,mime=mime),data,self.limits)
        await validate()
        import base64
        try: observations=await self.analyzer.observe(base64.b64decode(preview['image_base64']),'image/png')
        except (MaterialError,TimeoutError) as exc:
            await validate()
            return replace(bundle,extractor=self.cache_version,manifest=replace(bundle.manifest,
                limitations=bundle.manifest.limitations+(getattr(exc,'code','visual_provider_timeout'),)))
        await validate()
        from materials.visual import observation_blocks
        parent=bundle.blocks[0]
        visual=observation_blocks(aid,version,observations,parent.block_id,len(bundle.blocks),method=self.analyzer.identity)
        parent=replace(parent,text=observations['summary'],observation='observed',quality='uncertain',metadata={**parent.metadata,'visual_status':'observed','visual_method':self.analyzer.identity})
        limitations=tuple(dict.fromkeys(bundle.manifest.limitations+observations['limitations']+('model_visual_observation_not_verified',)))
        if preview['downsampled']: limitations+=('visual_input_downsampled',)
        return replace(bundle,blocks=(parent,)+bundle.blocks[1:]+visual,extractor=self.cache_version,
            manifest=replace(bundle.manifest,limitations=limitations))
    def preview(self,data,mime):
        inspect_bytes(data,'image',mime)
        with displayed_image(data) as image:
            original_size=image.size; image.thumbnail((1536,1536))
            encoded,_=DocumentExtractor._encode_preview(image)
            return dict(image_base64=encoded,mime='image/png',downsampled=image.size!=original_size,
                coordinate_space='exif_displayed_original_normalized')
    def extract(self,aid,version,data,mime):
        inspect_bytes(data,'image',mime)
        if not mime.startswith('image/'): raise MaterialError('image_mime_required')
        image=displayed_image(data)
        try:
            locator=Locator('region',bbox=(0,0,1,1)); parent=block_id(aid,version,'image',locator)
            metadata=dict(role='original_image',dimensions=list(image.size),original_sha256=sha256(data).hexdigest(),
                coordinate_space='exif_displayed_original_normalized',visual_status='uninterpreted')
            blocks=[ContentBlock(parent,'image',locator,metadata=metadata)]; issues=['visual_scene_uninterpreted']
            if self.options['ocr_enabled']:
                ocr=OCR(self.options['executable'],self.options['tessdata'],self.options['languages']); ocr.deadline=time.monotonic()+65
                observed=ocr.read(image)
                blocks[0]=replace(blocks[0],metadata={**metadata,'ocr_preprocessing':{k:v for k,v in observed.items() if k not in ('words','tables')}})
                entries=ocr_lines(observed['words'])
                for line in entries[:self.options['max_lines']]:
                    loc=Locator('region',bbox=tuple(line['bbox'])); index=len(blocks)
                    blocks.append(ContentBlock(block_id(aid,version,'text',loc,index),'text',loc,line['text'],parent,index,'extracted','uncertain',
                        ('ocr_not_human_verified',),dict(role='ocr_line',method='tesseract',words=line['words'],score_kind=observed['score_kind'])))
                if len(entries)>self.options['max_lines']: issues.append('ocr_line_budget_reached')
                for table in observed['tables']:
                    loc=Locator('region',bbox=tuple(table['bbox'])); index=len(blocks); bid=block_id(aid,version,'table',loc,index)
                    cells=[]; children=[]
                    for cell in table['cells']:
                        cloc=Locator('region',bbox=tuple(cell['bbox'])); ci=index+1+len(children); cid=block_id(aid,version,'text',cloc,ci)
                        cells.append({**cell,'block_id':cid})
                        children.append(ContentBlock(cid,'text',cloc,cell['text'],bid,ci,'extracted','uncertain',('ocr_not_human_verified',),dict(role='table_cell',**cell)))
                    blocks.append(ContentBlock(bid,'table',loc,'\n'.join('\t'.join(r) for r in table['rows']),parent,index,'extracted','uncertain',
                        ('raster_grid_heuristic',),dict(role='table',method='raster_grid',cells=cells,rows=table['rows'])))
                    blocks.extend(children)
                issues.append('ocr_not_human_verified')
            else: issues.append('ocr_disabled')
            return ExtractionBundle(aid,version,self.cache_version,tuple(blocks),ExtractionManifest(1,1,'unknown',tuple(issues),'image'))
        finally: image.close()
    async def region_async(self,data,mime,locator,reread=False,resource=None,orientation_hint=None):
        return await run_worker(dict(operation='image_region',options=self.options,mime=mime,locator=locator,reread=reread,orientation_hint=orientation_hint),data,self.limits)
    def region(self,data,mime,locator,reread=False,orientation_hint=None):
        loc=Locator.from_dict(locator)
        if loc.kind.value!='region' or loc.page is not None: raise MaterialError('image_region_required')
        inspect_bytes(data,'image',mime)
        with displayed_image(data) as original:
            x0,y0,x1,y1=loc.bbox; area=max(1,original.width*original.height*(x1-x0)*(y1-y0))
        scale=min(2,(8_000_000/area)**.5)
        if scale<1: raise MaterialError('region_pixel_budget')
        image,geometry=crop(data,RegionRequest(tuple(loc.bbox),scale))
        try:
            encoded,downsampled=DocumentExtractor._encode_preview(image)
            result=dict(mime='image/png',image_base64=encoded,locator=locator,preview_downsampled=downsampled,geometry=geometry)
            if reread:
                observed=OCR(self.options['executable'],self.options['tessdata'],self.options['languages']).read(image,targeted=True,orientation_hint=orientation_hint)
                w,h=geometry['original_display_size']; box=geometry['pixel_box']; actual=(box[0]/w,box[1]/h,box[2]/w,box[3]/h)
                for word in observed['words']: word['bbox']=list(map_box(word['bbox'],actual))
                result['observation']=observed
            return result
        finally: image.close()
