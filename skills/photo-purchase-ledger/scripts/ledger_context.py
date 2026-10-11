"""Observe an XLSX and optional date-folder images; emit bounded context, never edit sources."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import xml.etree.ElementTree as ET

from inventory import inventory
from bounded_xlsx import BoundedXlsx, file_hash
from ledger_io import add_summary_arguments, emit_summary

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def text_nodes(node):
    return ''.join(t.text or '' for t in node.findall('.//s:t', NS))


def workbook_snapshot(path, sheet_name=None, source_id=None, label_column='A', amount_column='H'):
    with BoundedXlsx(path) as z:
        book = ET.fromstring(z.read('xl/workbook.xml'))
        sheets = book.findall('s:sheets/s:sheet', NS)
        sheet = next((s for s in sheets if s.get('name') == sheet_name), None) if sheet_name else sheets[0]
        if sheet is None:
            raise ValueError('Worksheet not found: ' + str(sheet_name))
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        target = rels[sheet.get('{' + NS['r'] + '}id')]
        member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        root = ET.fromstring(z.read(member))
        shared = [text_nodes(n) for n in ET.fromstring(z.read('xl/sharedStrings.xml'))] if 'xl/sharedStrings.xml' in z.namelist() else []
        rows, style_refs, errors, uncached, totals, row_dimensions = {}, [], [], [], [], []
        for row in root.findall('s:sheetData/s:row', NS):
            records = []
            row_dimensions.append(dict(row.attrib))
            for cell in row.findall('s:c', NS):
                ref, kind = cell.get('r'), cell.get('t', 'n')
                node = cell.find('s:v', NS)
                value = node.text if node is not None else None
                if kind == 's':
                    value, kind = shared[int(value)] if value is not None else None, 'text'
                elif kind == 'inlineStr':
                    value, kind = text_nodes(cell), 'text'
                formula = cell.find('s:f', NS)
                style_refs.append([ref, cell.get('s', '0')])
                if value is not None or formula is not None:
                    records.append({'cell': ref, 'type': kind, 'value': value,
                                    'formula': formula.text if formula is not None else None,
                                    'formula_attributes': dict(formula.attrib) if formula is not None else {}})
                if kind == 'e':
                    errors.append({'cell': ref, 'error': value})
                if formula is not None and value is None:
                    uncached.append(ref)
            if records:
                row_number = row.get('r')
                rows[row_number] = records
                label = next((c['value'] for c in records if c['cell'] == label_column + row_number), None)
                if isinstance(label, str) and any(t in label for t in ['小结', '总计', '合计']):
                    amount = next((c for c in records if c['cell'] == amount_column + row_number), {})
                    totals.append({'row': int(row_number), 'label': label,
                                   'cached_amount': amount.get('value'), 'formula': amount.get('formula')})
        layout_nodes = {}
        for tag in ['cols', 'sheetFormatPr', 'mergeCells', 'drawing']:
            node = root.find('s:' + tag, NS)
            layout_nodes[tag] = ET.tostring(node, encoding='unicode') if node is not None else None
        part_hashes = {n: z.part_hash(n) for n in z.namelist()
                       if n == 'xl/styles.xml' or n.startswith(('xl/drawings/', 'xl/media/', 'xl/worksheets/_rels/'))}
        snapshot = {'source_id': source_id or identity(path), 'file': str(path.resolve()),
                    'sheet': sheet.get('name'), 'sha256': file_hash(path),
                    'row_hashes': {r: digest(cells) for r, cells in rows.items()},
                    'layout_hash': digest([layout_nodes, row_dimensions, style_refs, part_hashes])}
        return snapshot, {'rows': rows, 'totals': totals, 'formula_errors': errors, 'missing_formula_caches': uncached}


def row_ranges(rows):
    result = []
    for row in sorted(set(map(int, rows))):
        if result and row == result[-1][1] + 1:
            result[-1][1] = row
        else:
            result.append([row, row])
    return [str(a) if a == b else f'{a}:{b}' for a, b in result]


def bounded(items, maximum):
    return {'items': items[:maximum], 'omitted': max(0, len(items) - maximum)}


def short(value):
    if isinstance(value, str) and len(value) > 100:
        return value[:100] + '…[truncated]'
    return value


def observe(workbook, day=None, sheet=None, source_id=None, baseline=None, tail=4, maximum=8,
            order_file=None, label_column='A', amount_column='H', name_column='C', header_row=2):
    current, facts = workbook_snapshot(workbook, sheet, source_id, label_column, amount_column)
    current['mapping'] = [label_column, amount_column, name_column, header_row]
    previous, baseline_status = None, 'missing'
    if baseline is not None:
        try:
            if baseline.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('Baseline exceeds 16 MiB')
            document = json.loads(baseline.read_text(encoding='utf-8-sig'))
            previous = document['snapshot']
            old = previous['workbook']
            valid_hashes = isinstance(old['row_hashes'], dict) and all(
                str(k).isdigit() and isinstance(v, str) and re.fullmatch(r'[a-f0-9]{64}', v)
                for k, v in old['row_hashes'].items())
            if previous.get('schema') != 1 or not valid_hashes:
                baseline_status = 'incompatible'
            else:
                baseline_status = 'matched' if (old['source_id'], old['sheet'], old.get('mapping', ['A', 'H', 'C', 2])) == (current['source_id'], current['sheet'], current['mapping']) else 'incompatible'
        except (OSError, ValueError, KeyError, TypeError):
            previous, baseline_status = None, 'invalid'
    matched = baseline_status == 'matched'
    old_rows = previous['workbook']['row_hashes'] if matched else {}
    changed = sorted((r for r, h in current['row_hashes'].items() if h != old_rows.get(r)), key=int)
    removed = sorted(set(old_rows) - set(current['row_hashes']), key=int)
    changes = {'baseline': baseline_status, 'content_rows': row_ranges(changed), 'removed_rows': row_ranges(removed),
               'workbook_bytes_changed': current['sha256'] != previous['workbook'].get('sha256') if matched else None,
               'layout_or_images_changed': current['layout_hash'] != previous['workbook'].get('layout_hash') if matched else None}
    photos, photo_changes = None, None
    if day is not None:
        inv = inventory(day, order_file)
        photos = {'folder': identity(day), 'files': {p['file']: {'sha256': p['sha256'], 'creation_timestamp': p['creation_timestamp']} for p in inv['images']}}
        old_photos = previous.get('photos') if matched and previous is not None else None
        photo_match = isinstance(old_photos, dict) and old_photos.get('folder') == photos['folder'] and isinstance(old_photos.get('files'), dict)
        old_files = old_photos['files'] if photo_match else {}
        photo_changes = {'baseline_matched': photo_match,
                         'new_or_changed': [n for n, v in photos['files'].items() if v != old_files.get(n)],
                         'removed': sorted(set(old_files) - set(photos['files'])),
                         'creation_time_ties': inv['time_ties'], 'duplicate_bytes': inv['duplicate_bytes']}
    row_numbers = sorted(facts['rows'], key=int)
    def meaningful(r):
        cells = facts['rows'][r]
        # Templates may pre-fill A with sequence numbers far below actual purchases.
        return any(c['cell'] != label_column + r or (c['value'] is not None and
                   not re.fullmatch(r'-?\d+(?:\.\d+)?', str(c['value']))) for c in cells)
    ledger_rows = [r for r in row_numbers if meaningful(r)]
    detail_rows = [r for r in row_numbers if int(r) > header_row and
                   any(c['cell'] == name_column + r and c['value'] not in [None, ''] for c in facts['rows'][r])]
    selected = ledger_rows[-tail:] if tail else []
    tail_rows = [{'row': int(r), 'cells': [{'cell': c['cell'], 'value': short(c['value']), 'formula': short(c['formula'])} for c in facts['rows'][r][:10]],
                  'omitted_cells': max(0, len(facts['rows'][r]) - 10)} for r in selected]
    warnings = []
    if not matched:
        warnings.append('No compatible baseline: build an observation baseline; do not infer prior verification or re-read all historical images automatically.')
    if facts['missing_formula_caches']:
        warnings.append('Missing formula caches require recalculation before verifying totals; this script never calculates formulas.')
    summary = {'sheet': current['sheet'], 'last_nonempty_row': int(row_numbers[-1]) if row_numbers else None,
               'last_ledger_row': int(ledger_rows[-1]) if ledger_rows else None,
               'last_detail_row': int(detail_rows[-1]) if detail_rows else None,
               'workbook_sha256': current['sha256'], 'baseline': baseline_status,
               'changed_row_ranges': bounded(changes['content_rows'], maximum),
               'removed_row_ranges': bounded(changes['removed_rows'], maximum),
               'layout_or_images_changed': changes['layout_or_images_changed'],
               'totals': bounded([{k: short(v) for k, v in t.items()} for t in facts['totals']], maximum),
               'tail': tail_rows, 'formula_errors': bounded(facts['formula_errors'], maximum),
               'missing_formula_caches': bounded(facts['missing_formula_caches'], maximum), 'warnings': warnings,
               'observation_only': True, 'recognition_and_acceptance_not_verified': True}
    if photo_changes is not None:
        summary['photos'] = {k: bounded([bounded(group, maximum) for group in v], maximum)
                             if k in ['creation_time_ties', 'duplicate_bytes'] else
                             bounded(v, maximum) if isinstance(v, list) else v
                             for k, v in photo_changes.items()}
    return {'snapshot': {'schema': 1, 'workbook': current, 'photos': photos},
            'changes': changes, 'photo_changes': photo_changes,
            'formula_errors': facts['formula_errors'], 'missing_formula_caches': facts['missing_formula_caches'], 'summary': summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workbook', required=True, type=Path)
    parser.add_argument('--day', type=Path, help='Only scan this date folder; no recursive image scan')
    parser.add_argument('--sheet')
    parser.add_argument('--order-file', type=Path, help='Original-device image inventory for migrated images')
    parser.add_argument('--label-column', default='A')
    parser.add_argument('--amount-column', default='H')
    parser.add_argument('--name-column', default='C')
    parser.add_argument('--header-row', type=int, default=2)
    parser.add_argument('--source-id', help='Stable document+sheet identity when export filenames change; not a freshness guarantee')
    parser.add_argument('--baseline', type=Path, help='Earlier observation JSON; never an OCR verification certificate')
    parser.add_argument('--output', required=True, type=Path, help='Auxiliary JSON containing the snapshot and all differences')
    parser.add_argument('--tail', type=int, default=4)
    parser.add_argument('--max-items', type=int, default=8)
    add_summary_arguments(parser)
    args = parser.parse_args()
    for key in ('label_column', 'amount_column', 'name_column'):
        value = getattr(args, key).upper()
        if not re.fullmatch(r'[A-Z]{1,3}', value) or not 1 <= sum((ord(c)-64)*26**i for i,c in enumerate(reversed(value))) <= 16384:
            parser.error('Column mapping must use Excel column letters A..XFD')
        setattr(args, key, value)
    if not 1 <= args.header_row <= 1048576:
        parser.error('Invalid header row')
    if not 0 <= args.tail <= 20 or not 1 <= args.max_items <= 50:
        parser.error('tail must be 0..20; max-items must be 1..50')
    if not 512 <= args.max_chars <= 5000:
        parser.error('max-chars must be 512..5000')
    if args.output.suffix.lower() != '.json' or any(identity(args.output) == identity(p) for p in [args.workbook, args.baseline] if p is not None):
        parser.error('Output must be a separate auxiliary JSON, not the workbook or baseline')
    if args.output.exists():
        try:
            if args.output.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('Existing auxiliary JSON exceeds 16 MiB')
            existing = json.loads(args.output.read_text(encoding='utf-8-sig'))
            if existing.get('snapshot', {}).get('schema') != 1:
                raise ValueError('Not an observation snapshot')
        except (OSError, ValueError, AttributeError, TypeError):
            parser.error('Refusing to overwrite an unrelated JSON; choose a context snapshot output path')
    report = observe(args.workbook, args.day, args.sheet, args.source_id, args.baseline, args.tail, args.max_items,
                     args.order_file, args.label_column, args.amount_column, args.name_column, args.header_row)
    report['summary']['details_path'] = str(args.output.resolve())
    report['summary'].update(status='observed', check_complete=True,
                             error_count=len(report['formula_errors']),
                             missing_cache_count=len(report['missing_formula_caches']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    emit_summary(report['summary'], args.max_chars)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
