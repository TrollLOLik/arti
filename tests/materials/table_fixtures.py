"""Hand-authored OOXML fixtures: exact lexical values and deliberately stale caches."""
from io import BytesIO
import zipfile
from xml.sax.saxutils import escape


def xlsx(*,amount='1200.10',stale='999',huge_dimension=False,unsupported=False):
    namespace='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    def string(address,text): return f'<c r="{address}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
    def number(address,value): return f'<c r="{address}"><v>{value}</v></c>'
    def formula(address,expr,cache=None):
        return f'<c r="{address}"><f>{escape(expr)}</f>'+('' if cache is None else f'<v>{cache}</v>')+'</c>'
    sheet=f'''<worksheet xmlns="{namespace}"><dimension ref="{'A1:XFD1048576' if huge_dimension else 'A1:D7'}"/>
    <sheetData>
    <row r="1">{string('A1','Статья')}{string('B1','Сумма (RUB)')}{string('C1','Проверка')}{string('D1','Дата')}</row>
    <row r="2">{string('A2','Аренда')}{number('B2',amount)}{formula('C2',"'Rates'!B2*B2")}{string('D2','03/04/2026')}</row>
    <row r="3" hidden="1">{string('A3','Доставка')}{number('B3','300.20')}{formula('C3','ROUND(B3/3,2)')}</row>
    <row r="4">{string('A4','Итого')}{formula('B4','SUM(B2:B3)',stale)}{formula('C4','NOW()' if unsupported else 'B2/B3')}</row>
    <row r="5">{string('A5','Merged label')}</row>
    <row r="6">{number('B6','0')}{formula('C6','C7+1')}</row>
    <row r="7">{formula('C7','C6+1')}</row>
    </sheetData><mergeCells count="1"><mergeCell ref="A5:B5"/></mergeCells></worksheet>'''
    rates=f'<worksheet xmlns="{namespace}"><sheetData><row r="1">{string("A1","Currency")}{string("B1","Rate")}</row><row r="2">{string("A2","RUB/USD")}{number("B2","0.012")}</row></sheetData></worksheet>'
    workbook=f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Бюджет" sheetId="1" r:id="rId2"/><sheet name="Rates" sheetId="2" r:id="rId1"/></sheets></workbook>'
    rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/budget.xml"/><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/rates.xml"/></Relationships>'
    content='<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/budget.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/rates.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    root_rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    out=BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in {'[Content_Types].xml':content,'_rels/.rels':root_rels,'xl/workbook.xml':workbook,
            'xl/_rels/workbook.xml.rels':rels,'xl/worksheets/budget.xml':sheet,'xl/worksheets/rates.xml':rates}.items(): archive.writestr(name,data)
    return out.getvalue()


def csv_bytes():
    return '\ufeffСтатья;Сумма (RUB);Дата\r\n"Аренда; зал";"1 200,10";31/12/2026\r\nДоставка;300,20;03/04/2026\r\nПропуск;;\r\nТекст;=SUM(B2:B3);'.encode('utf-8')


def styled_xlsx():
    with zipfile.ZipFile(BytesIO(xlsx())) as archive:
        parts={name:archive.read(name).decode() for name in archive.namelist()}
    path='xl/worksheets/budget.xml'
    sheet=parts[path]
    def inline(address,text): return f'<c r="{address}" t="inlineStr"><is><t>{text}</t></is></c>'
    sheet=sheet.replace('</row>',inline('E1','Share')+inline('F1','Currency')+inline('G1','Amount (RUB)')+inline('H1','Date')+inline('I1','Literal')+'</row>',1)
    sheet=sheet.replace('</row>\n    <row r="3"', '<c r="E2" s="1"><v>0.25</v></c><c r="F2" s="2"><v>10</v></c><c r="G2" s="3"><v>10</v></c><c r="H2" s="4"><v>60</v></c><c r="I2" s="5"><v>0.25</v></c></row>\n    <row r="3"')
    sheet=sheet.replace('</row>\n    <row r="4"', '<c r="E3" s="1"><f>E2*2</f><v>0.5</v></c><c r="H3" s="4"><v>61</v></c></row>\n    <row r="4"')
    sheet=sheet.replace('</row>\n    <row r="5"', '<c r="E4"><f>E3*B2</f><v>600.05</v></c></row>\n    <row r="5"')
    parts[path]=sheet
    parts['xl/styles.xml']='''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="3"><numFmt numFmtId="164" formatCode="&quot;USD&quot; 0.00"/><numFmt numFmtId="165" formatCode="&quot;$&quot; 0.00"/><numFmt numFmtId="166" formatCode="0.00 &quot;%&quot;"/></numFmts><fonts count="1"><font/></fonts><fills count="1"><fill><patternFill/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="6"><xf/><xf numFmtId="10"/><xf numFmtId="165"/><xf numFmtId="164"/><xf numFmtId="14"/><xf numFmtId="166"/></cellXfs></styleSheet>'''
    parts['[Content_Types].xml']=parts['[Content_Types].xml'].replace('</Types>','<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    parts['xl/_rels/workbook.xml.rels']=parts['xl/_rels/workbook.xml.rels'].replace('</Relationships>','<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    out=BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in parts.items(): archive.writestr(name,data)
    return out.getvalue()
