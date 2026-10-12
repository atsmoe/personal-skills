"""Behavioral checks using synthetic workbooks; no private ledger fixtures."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/photo-purchase-ledger/scripts'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def workbook(path, rows, layout=''):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Test" sheetId="1" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{NS}"><sheetData>{rows}</sheetData>{layout}</worksheet>')


class LedgerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-workflow-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, name, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPTS / name), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, encoding='utf-8')

    def recognition(self):
        source = self.root / 'receipt.png'
        source.write_bytes(b'synthetic receipt evidence for non-visual tests')
        record = {'schema': 'purchase-recognition/v1', 'groups': [{
            'id': 'purchase-a', 'date': '2026-06-01',
            'sources': [{'id': 'receipt', 'path': source.name, 'role': 'receipt',
                         'creation_timestamp': getattr(source.stat(),'st_birthtime',1),
                         'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}],
            'items': [{'name': 'Material A', 'spec': None, 'quantity': '2', 'unit': 'box',
                       'unit_price': '6', 'amount': '12', 'scene': None,
                       'verification': {k: ('not_provided' if k in ('spec', 'scene') else 'verified')
                                        for k in ('name', 'spec', 'quantity', 'unit', 'unit_price', 'amount', 'scene')},
                       'evidence': {'source': 'receipt', 'description': 'First purchase line'}}]}]}
        path = self.root / 'recognition.json'
        if not hasattr(source.stat(),'st_birthtime'):
            order=self.root/'order.json'
            order.write_text(json.dumps({'images':[{'file':source.name,'creation_timestamp':1,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}))
            record['order_file']=order.name
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def config(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'schema': 'purchase-ledger-project/v1', 'sheet': 'Test',
                                   'header_row': 2, 'columns': dict(label='A', date='B', name='C', spec='D', quantity='E', unit='F', unit_price='G', amount='H', scene='I', photo='J'),
                                   'format': {'detail_height_pt': 90}}), encoding='utf-8')
        return path

    def purchase_row(self, row, height=90):
        return f'<row r="{row}" ht="{height}"><c r="B{row}" t="inlineStr"><is><t>2026-06-01</t></is></c><c r="C{row}" t="inlineStr"><is><t>Material A</t></is></c><c r="E{row}"><v>2</v></c><c r="F{row}" t="inlineStr"><is><t>box</t></is></c><c r="G{row}"><v>6</v></c><c r="H{row}"><f>E{row}*G{row}</f><v>12</v></c></row>'

    def test_all_errors_fail_even_when_only_a_bounded_summary_is_returned(self):
        path, report = self.root / 'errors.xlsx', self.root / 'report.json'
        workbook(path, ''.join(f'<row r="{i}"><c r="H{i}" t="e"><v>#VALUE!</v></c></row>' for i in range(1, 61)))
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.cli('check_xlsx.py', '--workbook', path, '--report', report,
                          '--max-errors', 3, '--max-chars', 1200)
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stdout)
        self.assertLessEqual(len(result.stdout.rstrip()), 1200)
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(summary['error_count'], 60)
        self.assertLessEqual(len(summary['errors']), 3)
        self.assertEqual(len(json.loads(report.read_text(encoding='utf-8'))['errors']), 60)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_inventory_bounds_duplicate_groups_without_losing_full_manifest(self):
        day = self.root / '0601'
        day.mkdir()
        images = []
        content = b'synthetic bytes for hashing, not visual recognition'
        for index in range(80):
            name = f'image-{index:03}.png'
            (day / name).write_bytes(content)
            images.append({'file': name, 'creation_timestamp': 1,
                           'sha256': hashlib.sha256(content).hexdigest()})
        order = self.root / 'original-order.json'
        order.write_text(json.dumps({'images': images}), encoding='utf-8')
        output = day / 'manifest.json'
        result = self.cli('inventory.py', '--day', day, '--order-file', order,
                          '--output', output, '--max-items', 2, '--max-chars', 900)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.rstrip()), 900)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['duplicate_group_count'], 1)
        self.assertEqual(summary['duplicate_bytes']['items'][0]['omitted'], 78)
        self.assertEqual(len(json.loads(output.read_text(encoding='utf-8'))['images']), 80)

    def test_observation_keeps_failure_counts_when_long_evidence_is_omitted(self):
        path, report = self.root / 'long.xlsx', self.root / 'context.json'
        rows = ''.join(f'<row r="{r}"><c r="C{r}" t="inlineStr"><is><t>{"材料" * 500}</t></is></c><c r="H{r}"><f>1+1</f></c></row>' for r in range(3, 65))
        workbook(path, rows)
        result = self.cli('ledger_context.py', '--workbook', path, '--output', report,
                          '--tail', 20, '--max-items', 50, '--max-chars', 900)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.rstrip()), 900)
        summary = json.loads(result.stdout)
        self.assertTrue(summary['observation_only'])
        self.assertTrue(summary['recognition_and_acceptance_not_verified'])
        self.assertEqual(summary['missing_cache_count'], 62)
        self.assertEqual(len(json.loads(report.read_text(encoding='utf-8'))['missing_formula_caches']), 62)

    def test_shifted_purchase_is_relocated_and_height_change_only_invalidates_layout(self):
        before, after = self.root / 'before.xlsx', self.root / 'after.xlsx'
        workbook(before, self.purchase_row(3))
        workbook(after, self.purchase_row(9, height=100))
        rec, config = self.recognition(), self.config()
        first, second = self.root / 'first.json', self.root / 'second.json'
        result = self.cli('ledger_run.py', 'inspect', '--workbook', before, '--recognition', rec,
                          '--source-root', self.root, '--config', config, '--output', first)
        self.assertEqual(result.returncode, 0, result.stderr)
        approved = json.loads(first.read_text(encoding='utf-8'))
        approved.update(acceptance_verified=True, observation_only=False)
        first.write_text(json.dumps(approved), encoding='utf-8')
        result = self.cli('ledger_run.py', 'inspect', '--workbook', after, '--recognition', rec,
                          '--source-root', self.root, '--config', config, '--baseline', first, '--output', second)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(second.read_text(encoding='utf-8'))['groups'][0]
        self.assertEqual(group['rows'], [9])
        self.assertEqual(group['action'], 'verify_layout')
        self.assertFalse(group['needs_recognition'])
        self.assertTrue(group['moved'])

    def test_edited_historical_purchase_cannot_be_mistaken_for_a_new_purchase(self):
        before, after = self.root / 'before.xlsx', self.root / 'after.xlsx'
        workbook(before, self.purchase_row(3))
        workbook(after, self.purchase_row(3).replace('<v>12</v>', '<v>13</v>'))
        rec, config = self.recognition(), self.config()
        first, second = self.root / 'first.json', self.root / 'second.json'
        self.assertEqual(self.cli('ledger_run.py', 'inspect', '--workbook', before,
                                 '--recognition', rec, '--source-root', self.root,
                                 '--config', config, '--output', first).returncode, 0)
        result = self.cli('ledger_run.py', 'inspect', '--workbook', after,
                          '--recognition', rec, '--source-root', self.root,
                          '--config', config, '--baseline', first, '--output', second)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(second.read_text(encoding='utf-8'))['groups'][0]
        self.assertEqual(group['action'], 'review_conflict')
        self.assertTrue(group['needs_recognition'])

    def test_prepare_creates_a_recoverable_plan_without_changing_the_workbook(self):
        path = self.root / 'empty.xlsx'
        workbook(path, '')
        rec, config = self.recognition(), self.config()
        before = path.read_bytes()
        run = self.root / 'run'
        result = self.cli('ledger_run.py', 'prepare', '--workbook', path, '--recognition', rec,
                          '--source-root', self.root, '--config', config, '--run-dir', run)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((run / 'run.json').read_text(encoding='utf-8'))
        self.assertEqual(state['stage'], 'prepared')
        self.assertEqual(state['plan']['expected_day_amount'], '12.00')
        self.assertEqual(state['plan']['expected_total_amount'], '12.00')
        self.assertEqual(state['plan']['insert_before'], 3)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((run / 'success.json').exists())

    def test_failed_or_unreviewed_run_cannot_advance_success_baseline(self):
        path = self.root / 'empty.xlsx'
        workbook(path, '')
        rec, config = self.recognition(), self.config()
        run = self.root / 'run'
        self.assertEqual(self.cli('ledger_run.py', 'prepare', '--workbook', path, '--recognition', rec,
                                 '--source-root', self.root, '--config', config, '--run-dir', run).returncode, 0)
        result = self.cli('ledger_run.py', 'finish', '--run-dir', run)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((run / 'success.json').exists())
        self.assertEqual(json.loads((run / 'run.json').read_text(encoding='utf-8'))['stage'], 'prepared')

    def test_two_groups_cannot_silently_claim_the_same_existing_purchase(self):
        path = self.root / 'one.xlsx'
        workbook(path, self.purchase_row(3))
        rec, config = self.recognition(), self.config()
        record = json.loads(rec.read_text())
        second = json.loads(json.dumps(record['groups'][0]))
        second['id'] = 'different-purchase'
        record['groups'].append(second)
        rec.write_text(json.dumps(record))
        result = self.cli('ledger_run.py', 'prepare', '--workbook', path, '--recognition', rec,
                          '--source-root', self.root, '--config', config, '--run-dir', self.root / 'run')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse((self.root / 'run').exists())

    def test_identical_values_without_a_source_binding_are_not_silently_deduplicated(self):
        path=self.root/'existing.xlsx'
        workbook(path,self.purchase_row(3))
        result=self.cli('ledger_run.py','prepare','--workbook',path,'--recognition',self.recognition(),
                        '--source-root',self.root,'--config',self.config(),'--run-dir',self.root/'run')
        self.assertEqual(result.returncode,1)
        self.assertIn('binding',result.stdout)

    def test_observation_baseline_cannot_bypass_the_source_binding_gate(self):
        path, baseline = self.root/'existing.xlsx', self.root/'observed.json'
        workbook(path,self.purchase_row(3))
        rec,config=self.recognition(),self.config()
        self.assertEqual(self.cli('ledger_run.py','inspect','--workbook',path,'--recognition',rec,
            '--source-root',self.root,'--config',config,'--output',baseline).returncode,0)
        result=self.cli('ledger_run.py','prepare','--workbook',path,'--recognition',rec,
            '--source-root',self.root,'--config',config,'--baseline',baseline,'--run-dir',self.root/'run')
        self.assertEqual(result.returncode,1)
        self.assertIn('binding',result.stdout)

    def test_preservation_covers_old_and_new_formulas_merges_and_column_widths(self):
        sys.path.insert(0,str(SCRIPTS))
        from ledger_local import verify_candidate
        from ledger_model import config_from
        before,after=self.root/'before.xlsx',self.root/'after.xlsx'
        rows=self.purchase_row(3)+'<row r="4"><c r="H4"><f>SUM(H3)</f><v>12</v></c></row>'
        layout='<cols><col min="10" max="10" width="40"/></cols><mergeCells><mergeCell ref="A4:G4"/></mergeCells>'
        workbook(before,rows,layout)
        plan={'config':config_from(self.config()),'insert_before':5,'insert_count':0,'historical_row_map':{'3':3,'4':4},
              'rows':[],'groups':[],'date':'2026-06-01','formulas':[],'subtotal_row':4,'total_row':4,
              'expected_day_amount':'12','expected_total_amount':'12'}
        state={'plan':plan,'inputs':{'workbook':{'path':str(before)}}}
        for change in ('unchanged','merge','width','old_formula','new_formula'):
            with self.subTest(change=change):
                actual_rows,actual_layout=rows,layout
                plan['formulas']=[]
                if change=='merge':actual_layout=layout.replace('<mergeCell ref="A4:G4"/>','')
                if change=='width':actual_layout=layout.replace('width="40"','width="2"')
                if change=='old_formula':actual_rows=rows.replace('SUM(H3)','12')
                if change=='new_formula':
                    plan['formulas']=[{'row':4,'column':'H','formula':'=SUM(H3)'}]
                    actual_rows=rows.replace('<f>SUM(H3)</f>','')
                workbook(after,actual_rows,actual_layout)
                report=verify_candidate(state,after)
                self.assertEqual(report['status'],'passed' if change=='unchanged' else 'failed',report)

    def test_insert_adjusts_absolute_references_but_not_quoted_text(self):
        sys.path.insert(0,str(SCRIPTS))
        from ledger_preservation import shift_formula
        self.assertEqual(shift_formula('=IF(A5="H9",SUM($H$5:H9),LOG10(10))',5,2),
                         'IF(A7="H9",SUM($H$7:H11),LOG10(10))')
        with self.assertRaises(ValueError):shift_formula('=SUM(Other!H5:H9)',5,2)

    def test_changed_date_cannot_reuse_another_purchase_binding(self):
        sys.path.insert(0,str(SCRIPTS))
        from ledger_model import inspect_ledger
        path=self.root/'dates.xlsx'
        workbook(path,self.purchase_row(3)+self.purchase_row(9).replace('2026-06-01','2026-06-02'))
        rec,config=self.recognition(),self.config()
        report=inspect_ledger(path,rec,self.root,config)
        report.update(acceptance_verified=True,observation_only=False)
        baseline=self.root/'accepted.json';baseline.write_text(json.dumps(report),encoding='utf-8')
        record=json.loads(rec.read_text());record['groups'][0]['date']='2026-06-02';rec.write_text(json.dumps(record))
        self.assertEqual(inspect_ledger(path,rec,self.root,config,baseline)['groups'][0]['action'],'verify_binding')

    def test_writer_claim_excludes_other_callers_and_recovers_a_completed_merge(self):
        import datetime as dt
        sys.path.insert(0,str(SCRIPTS))
        from ledger_jobs import prepare
        from ledger_model import digest
        before,after=self.root/'empty.xlsx',self.root/'merged.xlsx'
        workbook(before,'');workbook(after,'','<mergeCells><mergeCell ref="A4:G4"/></mergeCells>')
        run=self.root/'run'
        state=prepare(before,self.recognition(),self.root,self.config(),run)
        plan={'target':{'file_id':'test'},'operations':[{'tool':'sheet_merge_cell','args':{'start_row':3,'end_row':3,'start_col':0,'end_col':6}}]}
        state['online']={'status':'pending','operations_hash':digest(plan)}
        (run/'run.json').write_text(json.dumps(state),encoding='utf-8')
        (run/'online-plan.json').write_text(json.dumps(plan),encoding='utf-8')
        (run/'online-checkpoint.json').write_text(json.dumps({'plan_hash':state['plan_hash'],'cursor':0,'in_flight':0}),encoding='utf-8')
        after.with_suffix('.receipt.json').write_text(json.dumps({'file_id':'test','sha256':hashlib.sha256(after.read_bytes()).hexdigest(),'completed_at':dt.datetime.now(dt.timezone.utc).isoformat()}),encoding='utf-8')
        def bridge(op,request):
            return subprocess.run([sys.executable,'-X','utf8','-B',str(SCRIPTS/'ledger_bridge.py'),op,'--run-dir',str(run)],
                input=json.dumps(request),capture_output=True,text=True,encoding='utf-8')
        claim=bridge('claim',{});self.assertEqual(claim.returncode,0,claim.stdout)
        token=json.loads(claim.stdout)['token']
        self.assertEqual(bridge('claim',{}).returncode,1)
        self.assertEqual(bridge('refresh',{'export':str(after)}).returncode,1)
        recovered=bridge('refresh',{'writer_token':token,'export':str(after)})
        self.assertEqual(recovered.returncode,0,recovered.stdout)
        self.assertEqual(json.loads(recovered.stdout)['checkpoint']['cursor'],1)
        self.assertEqual(bridge('release',{'writer_token':token}).returncode,0)

    def test_unresolved_field_and_changed_original_block_writes(self):
        for change in ('unresolved','source_changed','order_missing','amount_conflict'):
            with self.subTest(change=change):
                path=self.root/'empty.xlsx'
                workbook(path,'')
                rec=self.recognition();record=json.loads(rec.read_text())
                if change=='unresolved':record['groups'][0]['items'][0]['verification']['spec']='unresolved'
                if change=='source_changed':(self.root/'receipt.png').write_bytes(b'changed evidence')
                if change=='order_missing':record['groups'][0]['sources'][0].pop('creation_timestamp')
                if change=='amount_conflict':record['groups'][0]['items'][0]['amount']='20'
                rec.write_text(json.dumps(record))
                run=self.root/change
                result=self.cli('ledger_run.py','prepare','--workbook',path,'--recognition',rec,
                                '--source-root',self.root,'--config',self.config(),'--run-dir',run)
                self.assertEqual(result.returncode,1,result.stdout)
                self.assertFalse(run.exists())

    def test_shared_formulas_and_explicit_formulas_have_the_same_purchase_fingerprint(self):
        # A shared-formula follower has no <f> text; its references still need verification.
        sys.path.insert(0,str(SCRIPTS))
        from ledger_model import WorkbookView,config_from
        a,b=self.root/'a.xlsx',self.root/'b.xlsx'
        rows=self.purchase_row(3)+self.purchase_row(4)
        workbook(a,rows)
        shared=rows.replace('<f>E3*G3</f>','<f t="shared" si="0" ref="H3:H4">E3*G3</f>').replace('<f>E4*G4</f>','<f t="shared" si="0"/>')
        workbook(b,shared)
        config=config_from(self.config())
        self.assertEqual(WorkbookView(a,config).fingerprints([3,4])[0],WorkbookView(b,config).fingerprints([3,4])[0])


if __name__ == '__main__':
    unittest.main()
