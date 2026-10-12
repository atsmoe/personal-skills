"""Synthetic regression cases for portable paths and audited input/output boundaries."""
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
PHOTO = ROOT / 'skills/photo-purchase-ledger/scripts'
REIMBURSE = ROOT / 'skills/prepare-reimbursement-submission/scripts'
sys.path.insert(0, str(PHOTO))
sys.path.insert(0, str(REIMBURSE))
from inventory import inventory
from bounded_xlsx import BoundedXlsx, XlsxLimits
from process_railway_tickets import csv_display, process
from render_pdf_evidence import RenderLimits, render_one


class PortabilitySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill-portable-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = self.root / '中文业务' / '0601'
        self.day.mkdir(parents=True)
        self.cwd = self.root / 'unrelated-working-directory'
        self.cwd.mkdir()

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', str(script), *map(str, args)], cwd=self.cwd,
                              capture_output=True, text=True, encoding='utf-8')

    def image_manifest(self):
        # Bytes are synthetic: inventory hashes files and does not decode pixels.
        entries = []
        for name, stamp, data in [('hash_z.png', 1, b'first image'), ('hash_a.png', 2, b'second image')]:
            (self.day / name).write_bytes(data)
            entries.append({'file': name, 'creation_timestamp': stamp,
                            'sha256': hashlib.sha256(data).hexdigest()})
        path = self.root / 'original-device.json'
        path.write_text(json.dumps({'images': entries}), encoding='utf-8')
        return path

    def pdf(self, name='ticket.pdf', width=144, height=72, pages=1):
        source = self.root / 'source'
        source.mkdir(exist_ok=True)
        path = source / name
        with pymupdf.open() as doc:
            for _ in range(pages):
                doc.new_page(width=width, height=height)
            doc.save(path)
        return path

    def metadata(self, name='ticket.pdf', note=''):
        path = self.root / 'metadata.json'
        path.write_text(json.dumps([{'source': name, 'name': '合成甲', 'amount': '23.00',
                                    'origin': '起点', 'destination': '终点',
                                    'status': '完成', 'note': note}], ensure_ascii=False), encoding='utf-8')
        return path

    def test_inventory_refuses_original_and_existing_json_output(self):
        order = self.image_manifest()
        original = self.day / 'hash_z.png'
        before = original.read_bytes()
        result = self.run_cli(PHOTO / 'inventory.py', '--day', self.day, '--output', original)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(original.read_bytes(), before)
        existing = self.day / 'unrelated.json'
        existing.write_text('{"keep": true}', encoding='utf-8')
        result = self.run_cli(PHOTO / 'inventory.py', '--day', self.day, '--order-file', order, '--output', existing)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(encoding='utf-8'), '{"keep": true}')

    def test_migrated_order_uses_original_timestamps_and_checks_bytes(self):
        order = self.image_manifest()
        # Explicit order works even if the OS offers no birth time.
        result = inventory(self.day, order)
        self.assertEqual([p['file'] for p in result['images']], ['hash_z.png', 'hash_a.png'])
        (self.day / 'hash_z.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            inventory(self.day, order)

    def test_install_and_custom_sheet_from_unrelated_directory(self):
        destination = self.root / '任意设备技能目录'
        result = self.run_cli(ROOT / 'scripts/install_skill.py', '--skill', 'photo-purchase-ledger', '--destination', destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = destination / 'photo-purchase-ledger'
        result = self.run_cli(ROOT / 'scripts/install_skill.py', '--skill', 'photo-purchase-ledger', '--destination', destination)
        self.assertNotEqual(result.returncode, 0)
        workbook = self.root / '不同项目.xlsx'
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        with zipfile.ZipFile(workbook, 'w') as z:
            z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="六月采购" sheetId="1" r:id="r1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/custom.xml"/></Relationships>')
            z.writestr('xl/worksheets/custom.xml', f'<worksheet xmlns="{ns}"><sheetData><row r="6"><c r="D6" t="inlineStr"><is><t>材料甲</t></is></c><c r="K6"><v>23</v></c></row><row r="7"><c r="B7" t="inlineStr"><is><t>合计</t></is></c><c r="K7"><v>23</v></c></row></sheetData></worksheet>')
        order = self.image_manifest()
        output = self.root / 'context.json'
        result = self.run_cli(installed / 'scripts/ledger_context.py', '--workbook', workbook,
                              '--sheet', '六月采购', '--day', self.day, '--order-file', order,
                              '--name-column', 'D', '--label-column', 'B', '--amount-column', 'K',
                              '--header-row', '5', '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['last_detail_row'], 6)
        self.assertEqual(summary['totals']['items'][0]['cached_amount'], '23')
        self.assertEqual(summary['sheet'], '六月采购')
        result = self.run_cli(installed / 'scripts/check_xlsx.py', '--workbook', workbook, '--sheet', '六月采购', '--expect', 'K7=23')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_csv_formula_text_is_inert_display_and_json_preserves_original(self):
        for value in ('=1+1', '+SUM(A1)', '-1+1', '@cmd', '\t=1', '\r=1', '\n=1',
                      '   =1', '＝1', '＋1', '－1', '＠1'):
            with self.subTest(value=value):
                self.assertTrue(csv_display(value).startswith('[文本] '))
        source = self.pdf()
        note = '=1+1'
        output = self.root / 'candidate'
        process(source.parent, self.metadata(note=note), output)
        with (output / '处理报告.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[1][-1], '[文本] =1+1')
        document = json.loads((output / '来源清单.json').read_text(encoding='utf-8'))
        self.assertEqual(document['documents'][0]['metadata']['note'], note)

    def test_renderer_manifest_cannot_write_back_into_source(self):
        source = self.pdf()
        before = source.read_bytes()
        manifest = source.parent / 'new-manifest.json'
        output = self.root / 'new-output'
        result = self.run_cli(REIMBURSE / 'render_pdf_evidence.py', source.parent, output, '--manifest', manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(manifest.exists())
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), before)

    def test_renderer_rejects_huge_page_before_pixmap_or_output(self):
        source = self.pdf(width=10000, height=10000)
        output = self.root / 'candidate' / 'PNG'
        with patch.object(pymupdf.Page, 'get_pixmap', side_effect=AssertionError('must not allocate')):
            with self.assertRaisesRegex(RuntimeError, 'pixel limit'):
                render_one(source, source.parent, output)
        self.assertFalse(output.exists())

    def test_batch_pixel_limit_rejected_before_creating_ticket_output(self):
        source = self.pdf(pages=2)
        output = self.root / 'candidate'
        with self.assertRaisesRegex(ValueError, 'pixel limit'):
            process(source.parent, self.metadata(), output, limits=RenderLimits(total_pixels=200_000))
        self.assertFalse(output.exists())

    def test_zip_decompression_budget_and_entity_declarations_rejected(self):
        workbook = self.root / 'synthetic.xlsx'
        with zipfile.ZipFile(workbook, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('xl/workbook.xml', b'x' * 4096)
        with self.assertRaisesRegex(ValueError, 'resource limit'):
            BoundedXlsx(workbook, XlsxLimits(xml_bytes=1024))
        with zipfile.ZipFile(workbook, 'w') as z:
            z.writestr('xl/workbook.xml', b'<!DOCTYPE root [<!ENTITY a "bad">]><root>&a;</root>')
        with BoundedXlsx(workbook) as z:
            with self.assertRaisesRegex(ValueError, 'DTD/entity'):
                z.read('xl/workbook.xml')

    def test_link_escape_does_not_touch_original_or_external_output(self):
        source = self.pdf()
        external = self.root / 'outside'
        external.mkdir()
        outside_pdf = external / 'outside.pdf'
        shutil.copy2(source, outside_pdf)
        link = source.parent / 'linked.pdf'
        try:
            link.symlink_to(outside_pdf)
        except OSError as exc:
            # Windows directory junctions need no symlink privilege.
            shell = shutil.which('pwsh') if os.name == 'nt' else None
            if not shell:
                self.skipTest('OS does not allow file links: ' + str(exc))
            junction = source.parent / 'linked-source'
            env = {**os.environ, 'TASK_SKILL_LINK': str(junction), 'TASK_SKILL_TARGET': str(external)}
            result = subprocess.run([shell, '-NoProfile', '-Command',
                                     'New-Item -ItemType Junction -Path $env:TASK_SKILL_LINK -Target $env:TASK_SKILL_TARGET | Out-Null'],
                                    env=env, capture_output=True, text=True)
            if result.returncode:
                self.skipTest('Cannot create test junction: ' + result.stderr)
            link = junction / outside_pdf.name
        output = self.root / 'output' / 'PNG'
        with self.assertRaisesRegex(ValueError, 'inside source root'):
            render_one(link, source.parent, output)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
