"""Prepare deterministic append jobs and preserve recoverable task state."""
import datetime as dt
import os
import re
from decimal import Decimal
from pathlib import Path

from bounded_xlsx import file_hash
from ledger_io import read_json, write_new_json
from ledger_model import (FIELDS, WorkbookView, config_from, decimal, digest,
                          inspect_ledger, recognition_from)
from ledger_preservation import shift_formula


def sum_formula(column, rows):
    spans = []
    for row in sorted(set(rows)):
        if spans and spans[-1][1] + 1 == row:
            spans[-1][1] = row
        else:
            spans.append([row, row])
    if not spans:
        return '=0'
    return '=SUM(' + ','.join(f'{column}{a}:{column}{b}' if a != b else f'{column}{a}' for a,b in spans) + ')'


def validate_amounts(record):
    for group in record['groups']:
        for item in group['items']:
            if item.get('quantity') is not None and item.get('unit_price') is not None:
                difference = decimal(item['quantity']) * decimal(item['unit_price']) - decimal(item['amount'])
                if abs(difference) > Decimal('0.01') and not item.get('settlement_note'):
                    raise ValueError('Quantity times unit price conflicts with the evidenced amount; a source-backed settlement note is required')
        if group.get('receipt_total') is not None:
            total = sum((decimal(item['amount']) for item in group['items']), Decimal(0))
            if total != decimal(group['receipt_total']):
                raise ValueError('Receipt total conflicts with its purchase lines')


def prepare(workbook, recognition, source_root, config_path, run_dir, baseline=None, source_id=None):
    run_dir = Path(run_dir).resolve()
    if run_dir.exists():
        raise ValueError('Run directory already exists; inspect/resume that run instead of overwriting it')
    config = config_from(config_path)
    record = recognition_from(recognition, source_root)
    inspection = inspect_ledger(workbook, recognition, source_root, config_path, baseline, source_id)
    blocked = [g['id'] for g in inspection['groups'] if g['action'] in ('recognize', 'review_conflict', 'review_match', 'review_photos')]
    if blocked:
        raise ValueError('Resolve source or matching evidence before writing: ' + ', '.join(blocked[:10]))
    validate_amounts(record)
    view = WorkbookView(workbook, config)
    if view.formula_errors or view.missing_caches:
        raise ValueError('Recalculate and resolve existing formula errors before preparing a write')
    actions = {g['id']: g['action'] for g in inspection['groups']}
    for match in inspection['groups']:
        if match['action']=='verify_binding':
            group=next(g for g in record['groups'] if g['id']==match['id'])
            binding=group.get('binding',{})
            if binding.get('workbook_sha256')!=inspection['workbook_sha256'] or binding.get('rows')!=match['rows'] or not binding.get('evidence'):
                raise ValueError('Existing content needs an explicit source-to-table binding; identical purchases must not be silently deduplicated')
    additions = [g for g in record['groups'] if actions[g['id']] == 'append']
    dates = {g['date'] for g in record['groups']}
    if len(dates) != 1:
        raise ValueError('Prepare one purchase date per run; preserve separate date ownership')
    date = next(iter(dates))
    when = dt.date.fromisoformat(date)
    details = view.detail_rows()
    old_total = sum((decimal(view.value(r, 'amount')) for r in details), Decimal(0))
    day_rows = [r for r in details if view.date(r, when.year) == date]
    old_day = sum((decimal(view.value(r, 'amount')) for r in day_rows), Decimal(0))
    total_rows = [r for r in sorted(view.rows) if view.value(r, 'name') in (None, '') and
                  re.search(r'累计|总计', str(view.value(r, 'label') or ''))]
    day_totals = [r for r in sorted(view.rows) if view.value(r, 'name') in (None, '') and
                  re.search(fr'(?:{when.year}年)?0?{when.month}月0?{when.day}日.*小[结计]', str(view.value(r, 'label') or ''))]
    if len(total_rows) > 1 or len(day_totals) > 1:
        raise ValueError('Ambiguous summary rows; select a supported ledger layout before writing')
    if day_rows and not day_totals and additions:
        raise ValueError('Existing date has no unique subtotal; reconcile its layout before appending')
    inserted = sum(len(g['items']) for g in additions)
    new_day_total = bool(additions and not day_totals)
    new_total = bool(additions and not total_rows)
    row_count = inserted + int(new_day_total) + int(new_total)
    insert_before = day_totals[0] if day_totals else total_rows[0] if total_rows else max([config['header_row'], *view.rows]) + 1
    if insert_before + row_count > 1048576:
        raise ValueError('Append exceeds worksheet row limit')
    for cells in view.rows.values():
        for cell in cells.values():
            shift_formula(cell.get('formula'), insert_before, row_count)
    shift = lambda row: row + row_count if row >= insert_before else row
    current = insert_before
    groups, new_rows = [], []
    sequence = max([0] + [int(view.value(row, 'label')) for row in details
                         if re.fullmatch(r'\d+', str(view.value(row, 'label') or ''))])
    for group in additions:
        rows = []
        for item in group['items']:
            sequence += 1
            rows.append(current)
            new_rows.append({'row': current, 'role': 'detail',
                             'values': {'label': sequence, 'date': date, **{field: item.get(field) for field in FIELDS}}})
            current += 1
        photos = group.get('photos')
        item_sources = [s for s in group['sources'] if s['role'] == 'item']
        if photos is None:
            if len(item_sources) > 1:
                raise ValueError('Multiple item photos require explicit non-overlapping item ranges')
            photos = [{'source': s['id'], 'first_item': 0, 'last_item': len(rows)-1} for s in item_sources]
        resolved_photos, occupied, used = [], set(), set()
        for photo in photos:
            source = next((s for s in item_sources if s['id'] == photo.get('source')), None)
            a, b = photo.get('first_item'), photo.get('last_item')
            if source is None or not isinstance(a, int) or not isinstance(b, int) or not 0 <= a <= b < len(rows):
                raise ValueError('Photo ranges must reference item sources within this purchase group')
            if occupied.intersection(range(a, b+1)):
                raise ValueError('Photo hosts must not overlap')
            occupied.update(range(a, b+1))
            used.add(source['id'])
            resolved_photos.append({'path': str((Path(source_root) / source['path']).resolve()),
                                    'sha256': source['sha256'], 'start_row': rows[a], 'end_row': rows[b]})
        if used != {s['id'] for s in item_sources}:
            raise ValueError('Every item photo must be mapped; receipt images must not be embedded')
        groups.append({'id': group['id'], 'rows': rows, 'photos': resolved_photos})
    amount_column = config['columns']['amount']
    new_details = [r['row'] for r in new_rows]
    subtotal_row = shift(day_totals[0]) if day_totals else current if additions else None
    if new_day_total:
        new_rows.append({'row': current, 'role': 'subtotal', 'values': {'label': f'{when.month}月{when.day}日小结'}})
        current += 1
    total_row = shift(total_rows[0]) if total_rows else current if additions else None
    if new_total:
        new_rows.append({'row': current, 'role': 'total', 'values': {'label': '累计总计'}})
    formulas = []
    if additions:
        formulas = [{'row': subtotal_row, 'column': amount_column,
                     'formula': sum_formula(amount_column, [*map(shift, day_rows), *new_details])},
                    {'row': total_row, 'column': amount_column,
                     'formula': sum_formula(amount_column, [*map(shift, details), *new_details])}]
    added_amount = sum((decimal(item['amount']) for group in additions for item in group['items']), Decimal(0))
    plan = {'schema': 'purchase-ledger-plan/v1', 'target_id': inspection['target_id'],
            'date': date, 'sheet': config['sheet'], 'config': config, 'insert_before': insert_before,
            'insert_count': row_count, 'rows': new_rows, 'groups': groups, 'formulas': formulas,
            'subtotal_row': subtotal_row, 'total_row': total_row,
            'expected_day_amount': str((old_day + added_amount).quantize(Decimal('0.01'))),
            'expected_total_amount': str((old_total + added_amount).quantize(Decimal('0.01'))),
            'historical_row_map': {str(row): shift(row) for row in view.rows}}
    inputs = {name: {'path': str(Path(path).resolve()), 'sha256': file_hash(path)}
              for name, path in [('workbook', workbook), ('recognition', recognition), ('config', config_path)]}
    state = {'schema': 'purchase-ledger-run/v1', 'stage': 'prepared',
             'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
             'inputs': inputs, 'source_root': str(Path(source_root).resolve()),
             'plan': plan, 'plan_hash': digest(plan), 'inspection': inspection,
             'local': {'status': 'pending'}, 'online': {'status': 'not_requested'},
             'history': [{'stage': 'prepared'}]}
    run_dir.mkdir(parents=True, exist_ok=False)
    write_new_json(run_dir / 'run.json', state, protected=[workbook, recognition, config_path])
    write_new_json(run_dir / 'context_before.json', inspection)
    return state
