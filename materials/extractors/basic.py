"""Bounded native extraction. OCR/layout reconstruction belongs to A05."""
from io import BytesIO
import zipfile
from materials.types import ContentBlock, ExtractionBundle, ExtractionManifest, Locator, LocatorKind, MaterialError, block_id


class BasicExtractor:
    version = 'native-document-1'

    def __init__(self, max_units=256, max_chars=300_000):
        if not 1 <= max_units <= 4096 or not 1 <= max_chars <= 2_000_000:
            raise MaterialError('invalid_extractor_budget')
        self.max_units, self.max_chars = max_units, max_chars

    def extract(self, aid, version, data, mime):
        blocks, issues, processed, used = [], [], 0, 0
        total = 0
        def add(text, locator, kind='text'):
            nonlocal used, processed
            if processed >= self.max_units or used + len(text) > self.max_chars:
                issues.append('extraction_budget_reached')
                return False
            ordinal = processed
            blocks.append(ContentBlock(block_id(aid, version, kind, locator, ordinal), kind, locator, text, ordinal=ordinal))
            processed += 1
            used += len(text)
            return True
        if mime.startswith('text/') or mime == 'application/json':
            paragraphs = data.decode('utf-8-sig').splitlines() or ['']
            total = len(paragraphs)
            for i, text in enumerate(paragraphs, 1):
                if not add(text, Locator(LocatorKind.PARAGRAPH, paragraph=i)):
                    break
        elif mime == 'application/pdf':
            import pypdf
            try:
                reader = pypdf.PdfReader(BytesIO(data))
                if reader.is_encrypted:
                    raise MaterialError('encrypted_document')
                total = len(reader.pages)
                for i, page in enumerate(reader.pages, 1):
                    if processed >= self.max_units:
                        issues.append('extraction_budget_reached')
                        break
                    text = page.extract_text() or ''
                    if not text.strip():
                        issues.append('page_requires_ocr:' + str(i))
                    if not add(text, Locator(LocatorKind.PAGE, page=i), 'page'):
                        break
                issues.append('native_pdf_layout_unassessed')
            except MaterialError:
                raise
            except Exception as exc:
                raise MaterialError('document_parse_failed') from exc
        elif mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
            from docx import Document
            from docx.oxml.ns import qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            try:
                document = Document(BytesIO(data))
                nodes = [n for n in document.element.body if n.tag in (qn('w:p'), qn('w:tbl'))]
                total = len(nodes)
                for i, node in enumerate(nodes, 1):
                    if node.tag == qn('w:p'):
                        text, kind = Paragraph(node, document).text, 'text'
                    else:
                        text = '\n'.join('\t'.join(c.text for c in row.cells) for row in Table(node, document).rows)
                        kind = 'table'
                    if not add(text, Locator(LocatorKind.PARAGRAPH, paragraph=i), kind):
                        break
                if document.inline_shapes:
                    issues.append('embedded_images_not_extracted')
                issues.append('docx_headers_footers_and_layout_unassessed')
            except Exception as exc:
                raise MaterialError('document_parse_failed') from exc
        else:
            raise MaterialError('extractor_mime_unsupported')
        omissions = any(i.startswith('page_requires_ocr:') or i == 'embedded_images_not_extracted' for i in issues)
        coverage = 'partial' if processed < total or omissions else 'complete'
        # Complete means the native text pass covered all units, not that a scan
        # or every image/layout feature was understood. Limitations remain visible.
        manifest = ExtractionManifest(total, processed, coverage, tuple(dict.fromkeys(issues)))
        return ExtractionBundle(aid, version, self.version + ':' + str(self.max_units) + ':' + str(self.max_chars), tuple(blocks), manifest)

    @property
    def cache_version(self):
        return self.version + ':' + str(self.max_units) + ':' + str(self.max_chars)


def render_text(bundle, max_chars=30000):
    fragments, used, truncated = [], 0, False
    for block in bundle.blocks:
        if block.metadata.get('role') in ('table_cell','timed_word','timeline_chunk'):
            continue  # Parent table projects values once; cells remain resolvable.
        label = 'block:' + block.block_id
        if block.locator.page is not None:
            label += '; page:' + str(block.locator.page)
        if block.locator.bbox is not None:
            label += '; region:' + ','.join(str(round(v,4)) for v in block.locator.bbox)
        if block.locator.start_ms is not None:
            label+='; time_ms:'+str(block.locator.start_ms)+'-'+str(block.locator.end_ms)
            if block.metadata.get('speaker'): label+='; speaker:'+block.metadata['speaker']+' (local, identity unconfirmed)'
        label += '; role:' + block.metadata.get('role',block.kind) + '; quality:' + block.quality
        if not block.text.strip():
            if block.kind in ('audio','video'):
                content='['+block.kind+' observation; method='+str(block.metadata.get('method','decoder'))+'; acoustic features/voice similarity do not identify a person or prove an emotion]'
            elif block.kind != 'image':
                continue
            else: content = '[Image: visual content uninterpreted; caption=' + str(block.metadata.get('caption_id','unknown')) + ']'
        else:
            content = block.text
        value = '[' + label + ']\n' + content
        if used + len(value) > max_chars:
            available = max(0, max_chars - used)
            if available:
                fragments.append(value[:available])
            truncated = True
            break
        fragments.append(value)
        used += len(value)
    if bundle.manifest.coverage != 'complete' or bundle.manifest.limitations or truncated:
        fragments.append('[Extraction coverage: ' + bundle.manifest.coverage + '; limitations: ' + ', '.join(bundle.manifest.limitations) + ('; prompt_text_truncated' if truncated else '') + ']')
    return '\n\n'.join(fragments)
