"""Local printed-text OCR. Engine scores are observations, not probabilities."""
import csv
from hashlib import sha256
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from materials.types import MaterialError


def installation():
    executable = os.getenv('ARTI_TESSERACT_CMD') or shutil.which('tesseract')
    if not executable and os.name == 'nt':
        for base in (Path(os.getenv('LOCALAPPDATA', '')) / 'ArtiOCR', Path('C:/Program Files/Tesseract-OCR')):
            if (base / 'tesseract.exe').is_file():
                executable = str(base / 'tesseract.exe')
                break
    directory = os.getenv('ARTI_TESSDATA_DIR')
    if not directory and executable and (Path(executable).parent / 'tessdata').is_dir():
        directory = str(Path(executable).parent / 'tessdata')
    if not directory:
        candidates=[os.getenv('TESSDATA_PREFIX',''),'/usr/share/tesseract-ocr/5/tessdata',
            '/usr/share/tesseract-ocr/4.00/tessdata','/usr/share/tessdata','/usr/local/share/tessdata']
        directory=next((p for p in candidates if p and (Path(p)/'rus.traineddata').is_file()),'')
    return executable or '', directory or ''


def fingerprint(executable, directory, languages):
    import importlib.metadata
    parts = [importlib.metadata.version(n) for n in ('pdfplumber', 'pypdfium2', 'python-docx', 'Pillow', 'opencv-python-headless')]
    for p in [Path(executable)] + [Path(directory) / (lang + '.traineddata') for lang in languages.split('+') + ['osd']]:
        if p.is_file():
            # Cached by OS, but avoids stale extraction after replacing a model.
            parts.append(sha256(p.read_bytes()).hexdigest())
        else:
            parts.append('missing')
    return sha256('|'.join(parts).encode()).hexdigest()[:20]


class OCR:
    def __init__(self, executable='', tessdata='', languages='rus+eng', timeout=20):
        if languages not in ('rus+eng', 'eng+rus', 'rus', 'eng'):
            raise MaterialError('unsupported_ocr_language')
        self.executable, self.tessdata, self.languages, self.timeout = executable, tessdata, languages, timeout
        self.deadline = None

    def _call(self, image, *, psm=3, osd=False):
        if not self.executable or not Path(self.executable).is_file():
            raise MaterialError('ocr_unavailable')
        remaining=self.deadline-time.monotonic() if self.deadline is not None else self.timeout
        if remaining<=0:
            raise MaterialError('ocr_deadline_reached')
        with tempfile.TemporaryDirectory(prefix='ocr-') as directory:
            path = Path(directory) / 'input.png'
            image.save(path)
            command = [self.executable, str(path), 'stdout', '-l', 'osd' if osd else self.languages, '--psm', str(psm)]
            if self.tessdata:
                command += ['--tessdata-dir', self.tessdata]
            if not osd:
                command += ['tsv']
            try:
                process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=min(self.timeout,remaining), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except subprocess.TimeoutExpired as exc:
                raise MaterialError('ocr_timeout') from exc
            if len(process.stdout) > 6 * 1024**2:
                raise MaterialError('ocr_output_budget')
            if process.returncode:
                raise MaterialError('ocr_orientation_unavailable' if osd else 'ocr_failed')
            return process.stdout.decode('utf-8', errors='replace')

    @staticmethod
    def words(tsv):
        words = []
        for row in csv.DictReader(StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
            if row.get('level') != '5' or not (row.get('text') or '').strip():
                continue
            words.append(dict(text=row['text'], confidence=float(row['conf']),
                box=[int(row[k]) for k in ('left','top','width','height')],
                group=[int(row[k]) for k in ('block_num','par_num','line_num')]))
            if len(words) > 20000:
                raise MaterialError('ocr_word_budget')
        return words

    def read(self, image, *, targeted=False, orientation_hint=None):
        import cv2
        import numpy as np
        from PIL import Image
        image = image.convert('RGB')
        if image.width * image.height > 16_000_000:
            raise MaterialError('ocr_pixel_budget')
        original_size = image.size
        rotation, orientation = 0, 'unknown'
        if orientation_hint in (0,90,180,270):
            rotation,orientation=orientation_hint,'source_observation_hint'
        if not targeted and orientation_hint is None:
            try:
                values = dict(line.split(': ',1) for line in self._call(image, psm=0, osd=True).splitlines() if ': ' in line)
                if float(values.get('Orientation confidence',0)) >= 3:
                    rotation = int(values.get('Rotate',0)) % 360
                    orientation = 'engine_osd'
            except MaterialError:
                pass
        def rotated(angle):
            w, h = original_size
            if angle == 90:
                return image.transpose(Image.Transpose.ROTATE_270), np.array([[0,-1,h],[1,0,0],[0,0,1]],float)
            if angle == 180:
                return image.transpose(Image.Transpose.ROTATE_180), np.array([[-1,0,w],[0,-1,h],[0,0,1]],float)
            if angle == 270:
                return image.transpose(Image.Transpose.ROTATE_90), np.array([[0,1,0],[-1,0,w],[0,0,1]],float)
            return image, np.eye(3)
        working, transform = rotated(rotation)
        words = self.words(self._call(working, psm=6 if targeted else 3))
        def score(rows):
            return sum(max(0,r['confidence']) * len(r['text']) for r in rows) / max(1,sum(len(r['text']) for r in rows))
        # Short sparse scans may not provide enough text for OSD. Do not assume upright.
        if orientation == 'unknown' and not targeted:
            candidates = [(score(words), rotation, working, transform, words)]
            for angle in (90,180,270):
                candidate, matrix = rotated(angle)
                rows = self.words(self._call(candidate))
                candidates.append((score(rows),angle,candidate,matrix,rows))
            best = max(candidates, key=lambda x: (x[0],sum(len(r['text']) for r in x[4])))
            if best[0] > score(words) + 8:
                _, rotation, working, transform, words = best
                orientation = 'heuristic_score_comparison'
        # Projection profile deskew for modest skew; keep full inverse geometry.
        grey = cv2.cvtColor(np.array(working), cv2.COLOR_RGB2GRAY)
        scale = min(1, 650/max(grey.shape))
        thumb = cv2.resize(grey, None, fx=scale, fy=scale)
        _, ink = cv2.threshold(thumb,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        def projection(angle):
            m = cv2.getRotationMatrix2D((ink.shape[1]/2,ink.shape[0]/2),angle,1)
            warped = cv2.warpAffine(ink,m,(ink.shape[1],ink.shape[0]))
            return float(np.var(np.sum(warped,axis=1)))
        baseline = projection(0)
        skew = max([i/2 for i in range(-8,9)], key=projection)
        if skew and projection(skew) > baseline * 1.15:
            matrix = cv2.getRotationMatrix2D((working.width/2,working.height/2),skew,1)
            corrected = Image.fromarray(cv2.warpAffine(np.array(working),matrix,working.size,borderValue=(255,255,255)))
            reread = self.words(self._call(corrected, psm=6 if targeted else 3))
            if score(reread) >= score(words):
                working, words = corrected, reread
                transform = np.vstack([matrix,[0,0,1]]) @ transform
            else:
                skew = 0
        else:
            skew = 0
        inverse = np.linalg.inv(transform)
        def bbox(box):
            x,y,w,h = box
            points = np.array([[x,y,1],[x+w,y,1],[x+w,y+h,1],[x,y+h,1]]) @ inverse.T
            x0,y0 = points[:,:2].min(axis=0); x1,y1 = points[:,:2].max(axis=0)
            ow,oh = original_size
            return [max(0,float(x0/ow)),max(0,float(y0/oh)),min(1,float(x1/ow)),min(1,float(y1/oh))]
        for word in words:
            word['bbox'] = bbox(word['box'])
        for word in words:
            x,y,w,h=word['box']
            word['reading_bbox']=[x/working.width,y/working.height,(x+w)/working.width,(y+h)/working.height]
        tables = self._tables(working, words, bbox)
        return dict(words=words, tables=tables, rotation_clockwise=rotation, deskew_degrees=skew,
                    orientation_method=orientation, engine_score=round(score(words),2),
                    score_kind='tesseract_confidence_not_probability')

    def _tables(self,image, words, convert):
        import cv2
        import numpy as np
        grey = cv2.cvtColor(np.array(image),cv2.COLOR_RGB2GRAY)
        _, ink = cv2.threshold(grey,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        vertical = cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(20,image.height//35))))
        horizontal = cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(20,image.width//35),1)))
        contours,_ = cv2.findContours(vertical|horizontal,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        tables = []
        def centres(values):
            groups = []
            for value in values:
                if not groups or value-groups[-1][-1]>4: groups.append([value])
                else: groups[-1].append(value)
            return [int(sum(g)/len(g)) for g in groups]
        for contour in contours:
            if len(tables)>=8: break
            x,y,w,h = cv2.boundingRect(contour)
            if w < image.width*.15 or h < 40:
                continue
            xs = centres(np.where(np.sum(vertical[y:y+h,x:x+w]>0,axis=0)>h*.65)[0]+x)
            ys = centres(np.where(np.sum(horizontal[y:y+h,x:x+w]>0,axis=1)>w*.65)[0]+y)
            if len(xs)<3 or len(ys)<3 or (len(xs)-1)*(len(ys)-1)>500:
                continue
            # Page segmentation often rejects text enclosed by a grid. Remove
            # only detected rules and reread this table, rather than the page.
            from PIL import Image
            clean=np.array(image).copy()
            mask=cv2.dilate(vertical|horizontal,np.ones((3,3),np.uint8))
            clean[mask>0]=(255,255,255)
            table_image=Image.fromarray(clean[y:y+h,x:x+w])
            try:
                table_words=self.words(self._call(table_image,psm=6))
                for word in table_words:
                    word['box'][0]+=x; word['box'][1]+=y
            except MaterialError:
                table_words=words
            finally:
                table_image.close()
            cells=[]; rows=[]
            for ri,(y0,y1) in enumerate(zip(ys,ys[1:])):
                row=[]
                for ci,(x0,x1) in enumerate(zip(xs,xs[1:])):
                    chosen=[r for r in table_words if x0 <= r['box'][0]+r['box'][2]/2 < x1 and y0 <= r['box'][1]+r['box'][3]/2 < y1]
                    text=' '.join(r['text'] for r in sorted(chosen,key=lambda r:(r['box'][1]//12,r['box'][0])))
                    row.append(text)
                    cells.append(dict(row=ri,column=ci,text=text,bbox=convert([x0,y0,x1-x0,y1-y0]),
                        quality='uncertain' if not chosen or any(r['confidence']<70 for r in chosen) else 'unassessed',
                        engine_scores=[r['confidence'] for r in chosen]))
                rows.append(row)
            if any(c['text'] for c in cells):
                tables.append(dict(rows=rows,cells=cells,bbox=convert([x,y,w,h]),method='raster_grid',structure_quality='heuristic',
                    reading_bbox=[x/image.width,y/image.height,(x+w)/image.width,(y+h)/image.height]))
        return tables
