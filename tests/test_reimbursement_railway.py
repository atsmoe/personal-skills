"""Verify attachment integrity and non-destructive processing using synthetic PDFs."""

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import pymupdf

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/prepare-reimbursement-submission/scripts'
sys.path.insert(0, str(SCRIPTS))
from process_railway_tickets import normalize_amount, process
from render_pdf_evidence import sha256


class RailwayAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.metadata = self.root / 'metadata.json'
        self.output = self.root / 'candidate'
        self.archive = self.root / 'attachments.zip'

    def pdf(self, name, pages=1):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as document:
            for number in range(pages):
                page = document.new_page(width=144, height=72)
                page.insert_text((10, 25), f'Synthetic ticket {number + 1}')
            document.save(path)
        return path

    def item(self, source, **updates):
        record = {'source': source, 'name': '测试甲', 'amount': '23.00',
                  'origin': '起点', 'destination': '终点', 'status': '完成', 'note': ''}
        record.update(updates)
        return record

    def write_metadata(self, items):
        self.metadata.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')

    def test_copy_hash_page_count_duplicate_names_and_archive_scope(self):
        originals = [self.pdf('first.pdf'), self.pdf('nested/second.pdf', pages=2)]
        hashes = [sha256(p) for p in originals]
        self.write_metadata([self.item('first.pdf'), self.item('nested/second.pdf')])
        result = process(self.source, self.metadata, self.output, self.archive)
        self.assertEqual((result['pdf_count'], result['png_count'], result['pending']), (2, 3, 0))
        self.assertEqual(hashes, [sha256(p) for p in originals])
        manifest = json.loads((self.output / '来源清单.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['render_parameters']['dpi'], 300)
        names = [record['pdf'] for record in manifest['documents']]
        self.assertNotEqual(names[0], names[1])
        self.assertIn('（2）', names[1])
        for record, expected in zip(manifest['documents'], hashes):
            self.assertEqual(sha256(self.output / record['pdf']), expected)
            for page in record['render']['pages']:
                png = self.output / page['rendered_relative_path']
                self.assertEqual(sha256(png), page['png_sha256'])
                pixmap = pymupdf.Pixmap(png)
                self.assertEqual((pixmap.width, pixmap.height, pixmap.n), (600, 300, 3))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), 5)
            self.assertTrue(all(Path(name).suffix in ('.pdf', '.png') for name in archive.namelist()))

    def test_existing_output_and_zip_are_preserved(self):
        self.pdf('first.pdf')
        self.write_metadata([self.item('first.pdf')])
        self.output.mkdir()
        marker = self.output / 'keep.txt'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            process(self.source, self.metadata, self.output)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        other_output = self.root / 'other'
        self.archive.write_bytes(b'keep existing archive')
        with self.assertRaises(FileExistsError):
            process(self.source, self.metadata, other_output, self.archive)
        self.assertFalse(other_output.exists())
        self.assertEqual(self.archive.read_bytes(), b'keep existing archive')

    def test_source_overlap_escape_and_missing_coverage_fail_before_writing(self):
        self.pdf('first.pdf')
        self.write_metadata([self.item('first.pdf')])
        with self.assertRaises(ValueError):
            process(self.source, self.metadata, self.source / 'output')
        self.assertFalse((self.source / 'output').exists())
        outside = self.root / 'outside.pdf'
        outside.write_bytes(b'%PDF-not a real ticket')
        self.write_metadata([self.item('../outside.pdf')])
        with self.assertRaises(ValueError):
            process(self.source, self.metadata, self.output)
        self.pdf('second.pdf')
        self.write_metadata([self.item('first.pdf')])
        with self.assertRaises(ValueError):
            process(self.source, self.metadata, self.output)
        self.write_metadata([self.item('first.pdf'), self.item('first.pdf'), self.item('second.pdf')])
        with self.assertRaises(ValueError):
            process(self.source, self.metadata, self.output)
        self.assertFalse(self.output.exists())

    def test_unverified_fields_remain_pending_and_cli_returns_two(self):
        self.pdf('first.pdf')
        self.write_metadata([self.item('first.pdf', name='', amount='', origin='', destination='', status='待确认')])
        result = subprocess.run([sys.executable, str(SCRIPTS / 'process_railway_tickets.py'),
                                 '--input-dir', str(self.source), '--metadata', str(self.metadata),
                                 '--output-dir', str(self.output)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary['completed'], summary['pending']), (0, 1))
        self.assertTrue((self.output / 'PDF/待确认-first.pdf').exists())

    def test_bad_pdf_is_pending_and_does_not_create_zip(self):
        (self.source / 'broken.pdf').write_bytes(b'%PDF-broken')
        self.write_metadata([self.item('broken.pdf')])
        summary = process(self.source, self.metadata, self.output, self.archive)
        self.assertEqual((summary['pending'], summary['render_failures']), (1, 1))
        self.assertFalse(self.archive.exists())
        self.assertIn('zip_not_created', summary)

    def test_invalid_completed_fields_do_not_create_output(self):
        self.pdf('first.pdf')
        for amount in ('NaN', 'Infinity', '-1', '1.001'):
            with self.subTest(amount=amount):
                self.write_metadata([self.item('first.pdf', amount=amount)])
                with self.assertRaises(ValueError):
                    process(self.source, self.metadata, self.output)
                self.assertFalse(self.output.exists())
        self.assertEqual(normalize_amount('￥1,234.5'), '1234.50')


if __name__ == '__main__':
    unittest.main()
