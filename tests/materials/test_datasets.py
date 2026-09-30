import asyncio
from dataclasses import asdict,replace
from decimal import Decimal,localcontext
import unittest
from artifacts.computation import ComputationSpec,Conversion,FormulaEngine,Quantity,arithmetic,compute
from materials.datasets import ColumnPolicy,DatasetPolicy,Dataset,normalize,datasets_from_bundle,correction_proposal,apply_correction
from materials.extractors.tables import TableExtractor,XLSX
from materials.extractors.documents import DocumentExtractor,DOCX
from materials.types import MaterialError
from tests.materials.table_fixtures import xlsx,csv_bytes,styled_xlsx
from tests.materials.document_fixtures import structured_pdf,rich_docx


class NormalizationTests(unittest.TestCase):
    def test_unknown_locale_keeps_ambiguous_values_and_identifiers(self):
        for raw in ('1,234','1.234','03/04/2026'):
            self.assertEqual('ambiguous',normalize(raw).kind)
        self.assertEqual('text',normalize('001234').kind)
        self.assertEqual('number',normalize(0).kind)
        self.assertEqual('0',normalize(0).value)

    def test_declared_and_mixed_locales(self):
        pairs=[('1,234.50','en','1234.5'),('1.234,50','de','1234.5'),('1\u202f234,50','ru','1234.5')]
        for raw,locale,expected in pairs: self.assertEqual(expected,normalize(raw,ColumnPolicy(0,locale=locale)).value)
        for raw in ('1,234.50','1.234,50'): self.assertEqual('1234.5',normalize(raw).value)
        self.assertEqual('invalid',normalize('12 34,50').kind)
        self.assertEqual('invalid',normalize('1234.50',ColumnPolicy(0,locale='ru')).kind)

    def test_units_dates_missing_and_bounds_preserve_meaning(self):
        self.assertEqual('ambiguous',normalize('$12.50').kind)
        self.assertEqual('USD',normalize('$12.50',ColumnPolicy(0,unit='USD')).unit)
        self.assertEqual('CAD',normalize('$12.50',ColumnPolicy(0,unit='CAD')).unit)
        self.assertEqual('invalid',normalize('12 USD',ColumnPolicy(0,unit='RUB')).kind)
        self.assertEqual('2026-04-03',normalize('03/04/2026',ColumnPolicy(0,date_order='DMY')).value)
        self.assertEqual('2026-03-04',normalize('03/04/2026',ColumnPolicy(0,date_order='MDY')).value)
        self.assertEqual('invalid',normalize('31/02/2026').kind)
        self.assertEqual('missing',normalize('—').kind)
        interval=normalize('10 ± 2 kg')
        self.assertEqual(('10','8','12','kg'),(interval.value,interval.lower,interval.upper,interval.unit))
        self.assertIn('explicit_interval_not_probability',interval.notes)


class DatasetComputationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def prepare():
            extractor=TableExtractor()
            cls.csv_bundle=await extractor.extract_async('csv',1,csv_bytes(),'text/csv')
            cls.excel_bundle=await extractor.extract_async('xlsx',1,xlsx(huge_dimension=True),XLSX)
            cls.doc_bundle=await DocumentExtractor().extract_async('word',1,rich_docx(),DOCX)
            cls.pdf_bundle=await DocumentExtractor().extract_async('pdf',1,structured_pdf(),'application/pdf')
        asyncio.run(prepare())
        cls.csv=datasets_from_bundle('csv-e',cls.csv_bundle)[0]
        cls.budget,cls.rates=datasets_from_bundle('xlsx-e',cls.excel_bundle)
        cls.environment={(d.name,c.address):c for d in (cls.budget,cls.rates) for c in d.cells}

    def test_csv_delimiter_quoted_fields_physical_records_and_formula_text(self):
        self.assertEqual('Аренда; зал',self.csv.cell('A2').raw)
        self.assertEqual('1200.1',self.csv.cell('B2').normalized.value)
        self.assertEqual('RUB',self.csv.cell('B2').normalized.unit)
        self.assertEqual('ambiguous',self.csv.cell('C3').normalized.kind)
        self.assertIsNone(self.csv.cell('B5').formula)
        self.assertEqual('text',self.csv.cell('B5').normalized.kind)
        self.assertEqual('cell',self.csv.cell('B2').source.locator.kind.value)
        self.assertEqual('B2',self.csv.cell('B2').source.locator.cell)

    def test_xlsx_original_formula_cache_hidden_merge_and_lexical_decimal(self):
        self.assertEqual('1200.10',self.budget.cell('B2').raw)
        self.assertEqual('=SUM(B2:B3)',self.budget.cell('B4').formula)
        self.assertEqual('999',self.budget.cell('B4').cached_raw)
        self.assertEqual('formula',self.budget.cell('B4').normalized.kind)
        self.assertTrue(self.budget.cell('B3').metadata['hidden_row'])
        self.assertEqual('merged',self.budget.cell('B5').normalized.kind)
        self.assertEqual('missing',self.budget.cell('D7').normalized.kind)
        self.assertEqual(('Бюджет','Rates'),(self.budget.name,self.rates.name))

    def test_docx_and_pdf_cells_are_real_evidence_not_generated_addresses(self):
        docs=datasets_from_bundle('word-e',self.doc_bundle)
        self.assertEqual(2,len(docs)); self.assertNotEqual(docs[0].series_key,docs[1].series_key)
        self.assertEqual('840',docs[0].cell('C2').normalized.value)
        self.assertEqual('paragraph',docs[0].cell('C2').source.locator.kind.value)
        pages=datasets_from_bundle('pdf-e',self.pdf_bundle)
        self.assertEqual('region',pages[0].cell('B2').source.locator.kind.value)
        self.assertEqual(1,pages[0].cell('B2').source.locator.page)
        self.assertEqual(2,pages[1].cell('B2').source.locator.page)

    def test_roundtrip_and_decimal_reproducibility_ignore_global_context(self):
        self.assertEqual(self.budget,Dataset.from_dict(self.budget.to_dict()))
        spec=ComputationSpec('sum','B2:B3')
        first=compute(self.budget,spec)
        with localcontext() as context:
            context.prec=4
            second=compute(self.budget,spec)
        self.assertEqual(first.id,second.id)
        self.assertEqual('1500.3',first.result['value'])
        self.assertEqual('RUB',first.result['unit'])

    def test_recomputed_formula_disagrees_with_stale_cache_and_records_leaves(self):
        result=compute(self.budget,ComputationSpec('sum','B4'))
        self.assertEqual('1500.3',result.result['value'])
        self.assertEqual('stale',result.formula_steps[0]['cache_status'])
        self.assertEqual({'B2','B3','B4'},{i['address'] for i in result.inputs})
        self.assertEqual('999',self.budget.cell('B4').cached_raw)

    def test_cross_sheet_formula_explicit_environment_and_rounding(self):
        result=compute(self.budget,ComputationSpec('sum','C2'),formula_cells=self.environment)
        self.assertEqual('14.4012',result.result['value'])
        self.assertEqual(3,len(result.inputs))
        self.assertEqual('100.07',compute(self.budget,ComputationSpec('sum','C3')).result['value'])
        with self.assertRaisesRegex(MaterialError,'formula_reference_unavailable'):
            compute(self.budget,ComputationSpec('sum','C2'))

    def test_cycle_unsupported_external_and_zero_denominator(self):
        with self.assertRaisesRegex(MaterialError,'formula_cycle'): compute(self.budget,ComputationSpec('sum','C6'))
        with self.assertRaisesRegex(MaterialError,'zero_or_uncertain_denominator'):
            compute(self.budget,ComputationSpec('percent','B2',reference='B6'))
        for formula in ('=NOW()','=WEBSERVICE("https://example.com")',"='[other.xlsx]Sheet'!A1",'=__import__("os")','=SUM(B2:B1048576)'):
            cell=replace(self.budget.cell('B4'),formula=formula)
            dataset=replace(self.budget,cells=tuple(cell if c.address=='B4' else c for c in self.budget.cells))
            with self.assertRaises(MaterialError): compute(dataset,ComputationSpec('sum','B4'))

    def test_missing_exclusion_is_explicit_and_header_selection_rejected(self):
        with self.assertRaisesRegex(MaterialError,'missing_input'): compute(self.csv,ComputationSpec('sum','B2:B4'))
        result=compute(self.csv,ComputationSpec('sum','B2:B4',missing='exclude'))
        self.assertEqual('1500.3',result.result['value']); self.assertEqual(1,len(result.ignored))
        with self.assertRaisesRegex(MaterialError,'selection_contains_header'): compute(self.csv,ComputationSpec('sum','B1:B3'))
        with self.assertRaisesRegex(MaterialError,'non_numeric_input'): compute(self.csv,ComputationSpec('sum','B5'))

    def test_units_require_rate_evidence_and_physical_conversion_is_exact(self):
        c=replace(self.budget.cell('B3'),normalized=normalize('300.20',ColumnPolicy(1,unit='USD'),native_kind='number'))
        dataset=replace(self.budget,cells=tuple(c if x.address=='B3' else x for x in self.budget.cells))
        with self.assertRaises(MaterialError): compute(dataset,ComputationSpec('sum','B2:B3'))
        rate=Conversion('USD','RUB','80',asdict(self.rates.cell('B2').source))
        result=compute(dataset,ComputationSpec('sum','B2:B3',conversions=(rate,)))
        self.assertEqual('25216.1',result.result['value'])
        cells=tuple(replace(c,normalized=normalize('1',ColumnPolicy(c.column,unit='km'),native_kind='number')) if c.address=='B2' else c for c in self.budget.cells)
        result=compute(replace(self.budget,cells=cells),ComputationSpec('convert','B2',target_unit='m'))
        self.assertEqual('1000',result.result['value'])

    def test_intervals_and_quality_policies_do_not_invent_confidence(self):
        c=replace(self.budget.cell('B2'),normalized=normalize('100 ± 5',ColumnPolicy(1,unit='RUB')),quality='uncertain')
        dataset=replace(self.budget,cells=tuple(c if x.address=='B2' else x for x in self.budget.cells))
        with self.assertRaisesRegex(MaterialError,'uncertain_input'): compute(dataset,ComputationSpec('sum','B2:B3'))
        result=compute(dataset,ComputationSpec('sum','B2:B3',allow_uncertain=True))
        self.assertEqual(('400.2','395.2','405.2'),tuple(result.result[k] for k in ('value','lower','upper')))
        self.assertIn('uncertain_source_input',result.warnings)

    def test_percent_change_mean_comparison_and_reconciliation(self):
        with localcontext() as context:
            context.prec=50
            expected=Decimal('1200.1')/Decimal('1500.3')*100
        self.assertEqual(expected,Decimal(compute(self.csv,ComputationSpec('percent','B2',reference='B2:B3')).result['value']))
        self.assertEqual('750.15',compute(self.budget,ComputationSpec('mean','B2:B3')).result['value'])
        result=compute(self.budget,ComputationSpec('reconcile','B2:B3',reference='B4'))
        self.assertTrue(result.checks[0]['passes']); self.assertEqual('0',result.result['value'])
        result=compute(self.budget,ComputationSpec('compare','B2',reference='B3'))
        self.assertEqual('greater',result.checks[0]['relation'])

    def test_proposal_and_confirmation_preserve_original_and_create_revision(self):
        proposal=correction_proposal(self.budget,'B2','1300')
        self.assertEqual('1200.10',self.budget.cell('B2').raw)
        corrected=apply_correction(self.budget,proposal,author_ref='user:7')
        self.assertNotEqual(self.budget.id,corrected.id)
        self.assertEqual(self.budget.id,corrected.previous_id)
        self.assertEqual('1200.10',corrected.cell('B2').raw)
        self.assertEqual('1300',corrected.cell('B2').normalized.value)
        self.assertEqual('1600.2',compute(corrected,ComputationSpec('sum','B4')).result['value'])
        with self.assertRaisesRegex(MaterialError,'stale_correction'): apply_correction(corrected,proposal,author_ref='user:7')

    def test_extraction_budget_is_partial_and_stale_dimensions_are_ignored(self):
        self.assertEqual('complete',self.excel_bundle.manifest.coverage)
        limited=asyncio.run(TableExtractor(max_cells=6).extract_async('small',1,csv_bytes(),'text/csv'))
        self.assertEqual((5,2,'partial'),(limited.manifest.total_units,limited.manifest.processed_units,limited.manifest.coverage))
        self.assertIn('table_cell_budget_reached',limited.manifest.limitations)

    def test_chunked_csv_has_one_dataset_and_stable_cell_coordinates(self):
        data=('Name,Value\n'+''.join(f'row{i},{i}\n' for i in range(1,251))).encode()
        bundle=asyncio.run(TableExtractor().extract_async('many',1,data,'text/csv'))
        datasets=datasets_from_bundle('many-e',bundle)
        self.assertEqual(1,len(datasets)); self.assertEqual('250',datasets[0].cell('B251').raw)
        self.assertEqual('31375',compute(datasets[0],ComputationSpec('sum','B2:B251')).result['value'])

    def test_percent_style_formula_chaining_currency_conflict_and_excel_dates(self):
        bundle=asyncio.run(TableExtractor().extract_async('styled',1,styled_xlsx(),XLSX))
        budget,_=datasets_from_bundle('styled-e',bundle)
        self.assertEqual(('25','percent'),(budget.cell('E2').normalized.value,budget.cell('E2').normalized.unit))
        result=compute(budget,ComputationSpec('sum','E3'))
        self.assertEqual(('50','percent','matches_recomputed'),(result.result['value'],result.result['unit'],result.formula_steps[0]['cache_status']))
        self.assertEqual('600.05',compute(budget,ComputationSpec('sum','E4')).result['value'])
        self.assertEqual('ambiguous',budget.cell('F2').normalized.kind)
        self.assertEqual('invalid',budget.cell('G2').normalized.kind)
        self.assertEqual('error',budget.cell('H2').normalized.kind)
        self.assertEqual('1900-03-01',budget.cell('H3').normalized.value)
        self.assertEqual(('0.25','1'),(budget.cell('I2').normalized.value,budget.cell('I2').normalized.unit))

    def test_outward_rounding_encloses_fraction_and_physical_conversion(self):
        with localcontext() as context:
            context.prec=50
            fraction=arithmetic('/',Quantity.scalar(1),Quantity.scalar(3))
        with localcontext() as context:
            context.prec=100
            exact=Decimal(1)/3
            self.assertLessEqual(fraction.lower,exact); self.assertGreaterEqual(fraction.upper,exact)
        c=replace(self.budget.cell('B2'),normalized=normalize('1',ColumnPolicy(1,unit='min'),native_kind='number'))
        dataset=replace(self.budget,cells=tuple(c if x.address=='B2' else x for x in self.budget.cells))
        result=compute(dataset,ComputationSpec('convert','B2',target_unit='h'))
        with localcontext() as context:
            context.prec=100
            self.assertLessEqual(Decimal(result.result['lower']),Decimal(1)/60)
            self.assertGreaterEqual(Decimal(result.result['upper']),Decimal(1)/60)

    def test_blank_formula_semantics_count_and_outliers_do_not_rewrite_source(self):
        cell=replace(self.budget.cell('B4'),formula='=SUM(B2:B3,D7) ')
        dataset=replace(self.budget,cells=tuple(cell if c.address=='B4' else c for c in self.budget.cells))
        result=compute(dataset,ComputationSpec('sum','B4'))
        self.assertEqual('1500.3',result.result['value']); self.assertIn('excel_blank_reference_as_zero',result.warnings)
        cell=replace(cell,formula='=COUNT(B2:B3,D7)')
        dataset=replace(dataset,cells=tuple(cell if c.address=='B4' else c for c in dataset.cells))
        self.assertEqual('2',compute(dataset,ComputationSpec('sum','B4')).result['value'])
        data=('Value\n'+'\n'.join(map(str,(1,2,3,4,5,6,7,100)))+'\n').encode()
        bundle=TableExtractor().extract('outliers',1,data,'text/csv')
        dataset=datasets_from_bundle('outliers-e',bundle)[0]
        result=compute(dataset,ComputationSpec('sum','A2:A9'))
        self.assertEqual(['A9'],result.checks[0]['addresses']); self.assertEqual('100',dataset.cell('A9').raw)

    def test_numeric_limits_and_corrected_column_summary(self):
        self.assertEqual('invalid',normalize('1e101',ColumnPolicy(0,dtype='number')).kind)
        cell=replace(self.budget.cell('B4'),formula='=1e101')
        dataset=replace(self.budget,cells=tuple(cell if c.address=='B4' else c for c in self.budget.cells))
        with self.assertRaisesRegex(MaterialError,'numeric_budget'): compute(dataset,ComputationSpec('sum','B4'))
        corrected=apply_correction(self.rates,correction_proposal(self.rates,'B2','confirmed text'),author_ref='user:7')
        self.assertEqual('text',corrected.columns[1]['dtype']); self.assertEqual([],corrected.columns[1]['units'])
