"""Local Excel adapter. The source is immutable until explicitly published."""
import os
import shutil
import subprocess
import re
import zipfile
from pathlib import Path

from bounded_xlsx import file_hash
from ledger_io import read_json, write_new_json
from ledger_model import WorkbookView, decimal, inspect_ledger, normal, column_number, digest
from ledger_state import check_inputs, locked_run, save, atomic_json
from ledger_preservation import historical_layout_errors, shift_formula


def apply_local(run_dir):
    with locked_run(run_dir) as (directory, state):
        check_inputs(state)
        local = state['local']
        if local['status'] in ('applied', 'verified', 'accepted', 'published'):
            if file_hash(local['candidate']) != local['sha256']:
                raise ValueError('Candidate changed; inspect the saved run before continuing')
            return state
        # Never reinsert into a partly edited candidate. Each retry starts from the unchanged input.
        if os.name != 'nt' or not shutil.which('pwsh'):
            raise ValueError('Local adapter requires Windows, PowerShell 7 and Excel; keep the prepared plan for a compatible editor')
        attempt = directory / ('local-' + str(len(state['history'])))
        attempt.mkdir(exist_ok=False)
        candidate, preview = attempt / 'candidate.xlsx', attempt / 'preview.pdf'
        source = Path(state['inputs']['workbook']['path'])
        if source.suffix.lower() != '.xlsx':
            raise ValueError('Native adapter accepts .xlsx only; macros require a separate preservation review')
        with source.open('rb') as src, candidate.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        # Excel otherwise downsamples newly inserted photos on save. Preserve the
        # original OOXML namespace declarations while setting this native option.
        staged = attempt / 'uncompressed.xlsx'
        with zipfile.ZipFile(candidate) as src, zipfile.ZipFile(staged, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == 'xl/workbook.xml':
                    text = data.decode('utf-8')
                    def preserve_photos(match):
                        tag = re.sub(r' autoCompressPictures="[^"]*"', '', match[0])
                        return re.sub(r'(/?>)$', r' autoCompressPictures="0"\1', tag)
                    text,count = re.subn(r'<(?:\w+:)?workbookPr\b[^>]*>', preserve_photos, text, count=1)
                    if not count:
                        raise ValueError('Workbook lacks native picture-preservation settings')
                    data = text.encode('utf-8')
                if info.filename == 'xl/styles.xml':
                    # Tencent exports transparent solid fills. Excel discards their
                    # alpha channel and makes them black unless represented as none.
                    # Only use this conversion on an authenticated export receipt.
                    receipt=source.with_suffix('.receipt.json')
                    if receipt.exists():
                        proof=read_json(receipt)
                        target=state['plan']['config'].get('online_target',{})
                        if proof.get('sha256')==state['inputs']['workbook']['sha256'] and proof.get('file_id')==target.get('file_id'):
                            text=data.decode('utf-8')
                            def transparent_fill(match):
                                part=match[0]
                                if re.search(r'<(?:\w+:)?fgColor\b[^>]*rgb="00[0-9A-Fa-f]{6}"',part):
                                    return re.sub(r'<((?:\w+:)?patternFill)\b[^>]*>.*?</\1>',lambda m:'<'+m[1]+' patternType="none"/>',part,flags=re.S)
                                return part
                            data=re.sub(r'<(?:\w+:)?fill>.*?</(?:\w+:)?fill>',transparent_fill,text,flags=re.S).encode('utf-8')
                dst.writestr(info, data)
        os.replace(staged, candidate)
        local.update(status='applying', candidate=str(candidate), preview=str(preview))
        save(directory, state, 'local_applying')
        request, result = attempt / 'request.json', attempt / 'result.json'
        write_new_json(request, {'plan': state['plan'], 'candidate': str(candidate), 'preview': str(preview)})
        try:
            completed = subprocess.run(['pwsh', '-NoProfile', '-File', str(Path(__file__).with_name('excel_apply.ps1')),
                                        '-RequestPath', str(request), '-ResultPath', str(result)],
                                       capture_output=True, timeout=240)
            if completed.returncode or not result.exists():
                # Persist diagnostics privately; only the path is returned to the model.
                (attempt / 'editor.log').write_bytes(completed.stderr[-65536:])
                raise ValueError('Excel edit failed; inspect ' + str(attempt / 'editor.log'))
            proof = read_json(result)
            if not proof.get('normal_open') or not proof.get('recalculated') or proof.get('candidate_sha256') != file_hash(candidate):
                raise ValueError('Excel read-back did not confirm the saved candidate')
            local.update(status='applied', sha256=proof['candidate_sha256'], editor_result=str(result))
            save(directory, state, 'local_applied')
        except (ValueError, OSError, subprocess.TimeoutExpired):
            local['status'] = 'failed'
            save(directory, state, 'local_failed')
            raise
        return state


def verify_candidate(state, path, online_assets=None):
    plan, errors = state['plan'], []
    config = plan['config']
    before = WorkbookView(state['inputs']['workbook']['path'], config)
    after = WorkbookView(path, config)
    errors.extend('Formula error: ' + str(e) for e in after.formula_errors)
    errors.extend('Missing formula cache: ' + str(e) for e in after.missing_caches)
    mutable = {(f['row'], f['column']) for f in plan['formulas']}
    extra_merges = []
    columns = config['columns']
    for entry in plan['rows']:
        if entry['role'] != 'detail':
            extra_merges.append((entry['row'], column_number(columns['label']), entry['row'], column_number(columns['amount'])-1))
    extra_pictures = []
    for group in plan['groups']:
        for photo in group['photos']:
            col = column_number(columns['photo'])
            wanted = (online_assets or {}).get((photo['start_row'], photo['sha256']), photo['sha256'])
            extra_pictures.append((photo['start_row'], col, wanted))
            if photo['end_row'] > photo['start_row']:
                extra_merges.append((photo['start_row'], col, photo['end_row'], col))
    errors.extend(historical_layout_errors(before, after, plan['insert_before'], plan['insert_count'], extra_merges, extra_pictures))
    for old, new in plan['historical_row_map'].items():
        old = int(old)
        for col in before.rows[old].keys() | after.rows.get(new, {}).keys():
            cell = before.rows[old].get(col, {})
            if (new, col) in mutable:
                continue
            current = after.rows.get(new, {}).get(col, {})
            expected_formula = shift_formula(cell.get('formula'), plan['insert_before'], plan['insert_count'])
            actual_formula = shift_formula(current.get('formula'), 1, 0)
            if expected_formula != actual_formula:
                errors.append(f'Historical formula changed: {col}{old} -> {col}{new}')
            if expected_formula:
                continue
            same=cell.get('value') == current.get('value')
            if not same and cell.get('type')=='n' and current.get('type')=='n':
                try:
                    same=abs(decimal(cell.get('value'))-decimal(current.get('value')))<=decimal('0.000000001')
                except ValueError:
                    pass
            if not same:
                errors.append(f'Historical value changed: {col}{old} -> {col}{new}')
    for formula in plan['formulas']:
        actual = after.rows.get(formula['row'], {}).get(formula['column'], {}).get('formula')
        if shift_formula(actual, 1, 0) != shift_formula(formula['formula'], 1, 0):
            errors.append(f'Planned summary formula differs: {formula["column"]}{formula["row"]}')
    for entry in plan['rows']:
        row = entry['row']
        for field, value in entry['values'].items():
            got = after.date(row, int(plan['date'][:4])) if field == 'date' else after.value(row, field)
            if normal(got, field) != normal(value, field):
                errors.append(f'Written field differs: row {row}, {field}')
        if entry['role'] == 'detail' and config.get('format', {}).get('detail_height_pt') is not None:
            if decimal(after.row_dimensions.get(row, {}).get('ht', after.default_height)) != decimal(config['format']['detail_height_pt']):
                errors.append(f'Detail height differs at row {row}')
    for row, expected in [(plan['subtotal_row'], plan['expected_day_amount']), (plan['total_row'], plan['expected_total_amount'])]:
        if row is None or after.value(row, 'amount') is None or abs(decimal(after.value(row, 'amount')) - decimal(expected)) > decimal('0.001'):
            errors.append('Daily or cumulative amount differs from independent calculation')
    for group in plan['groups']:
        for photo in group['photos']:
            hosts = [p for p in after.pictures if p['row'] == photo['start_row'] and p['col'] == column_number(config['columns']['photo'])]
            wanted=(online_assets or {}).get((photo['start_row'],photo['sha256']),photo['sha256'])
            if len(hosts) != 1 or hosts[0]['sha256'] != wanted:
                errors.append('Item photo missing, changed or duplicated: ' + group['id'])
            col = column_number(config['columns']['photo'])
            if photo['end_row'] > photo['start_row'] and (photo['start_row'],col,photo['end_row'],col) not in after.merges:
                errors.append('Missing photo merge: ' + group['id'])
    return {'schema': 'purchase-ledger-check/v1', 'check_complete': True,
            'status': 'failed' if errors else 'passed', 'errors': errors,
            'workbook_sha256': file_hash(path)}


def verify_local(run_dir):
    with locked_run(run_dir) as (directory, state):
        check_inputs(state)
        local = state['local']
        if local['status'] not in ('applied', 'verified', 'accepted'):
            raise ValueError('Apply and recalculate the candidate before verifying')
        local['status'] = 'verifying'
        save(directory, state, 'local_verifying')
        if file_hash(local['candidate']) != local['sha256']:
            raise ValueError('Candidate changed after Excel read-back')
        proof = read_json(local['editor_result'])
        photos = [photo for group in state['plan']['groups'] for photo in group['photos']]
        if proof.get('candidate_sha256') != local['sha256'] or len(proof.get('geometry', [])) != len(photos) or any(not p['centered'] or not p['inside'] for p in proof['geometry']):
            raise ValueError('Native geometry read-back is incomplete')
        report = verify_candidate(state, local['candidate'])
        atomic_json(directory / 'local-check.json', report)
        if report['errors']:
            local['status'] = 'failed'
            save(directory, state, 'local_check_failed')
            raise ValueError(f"{len(report['errors'])} checks failed; see {directory / 'local-check.json'}")
        local['status'] = 'verified'
        save(directory, state, 'local_verified')
        return state


def publish_local(run_dir):
    with locked_run(run_dir) as (directory, state):
        local = state['local']
        source = Path(state['inputs']['workbook']['path'])
        backup = directory / 'original.xlsx'
        def confirmed_publish():
            local.update(status='published', backup=str(backup))
            save(directory, state, 'complete')
            pointer = read_json(directory / 'success.json')
            pointer['published'] = True
            atomic_json(directory / 'success.json', pointer)
            return state
        if local['status'] == 'published':
            if file_hash(source) != local['sha256']:
                raise ValueError('Published file changed; refresh before further work')
            return confirmed_publish()
        intent = state.get('publish_intent', {})
        if intent.get('after') == local.get('sha256') and file_hash(source) == local['sha256']:
            check_inputs(state, workbook=False)
            if not backup.exists() or file_hash(backup) != state['inputs']['workbook']['sha256']:
                raise ValueError('Interrupted publish lacks the exact original backup')
            return confirmed_publish()
        check_inputs(state)
        if state['stage'] != 'complete' or local['status'] != 'accepted':
            raise ValueError('Finish all required acceptance before publishing')
        if file_hash(local['candidate']) != local['sha256']:
            raise ValueError('Candidate changed after acceptance')
        if not backup.exists():
            with source.open('rb') as src, backup.open('xb') as dst:
                shutil.copyfileobj(src, dst)
        if file_hash(backup) != state['inputs']['workbook']['sha256']:
            raise ValueError('Backup does not match the original input')
        state['publish_intent'] = {'before': state['inputs']['workbook']['sha256'], 'after': local['sha256']}
        save(directory, state, 'complete')
        # Atomic replace on the same filesystem; Excel locks cause a recoverable failure.
        import tempfile
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=source.parent, suffix='.xlsx', prefix='.ledger-', delete=False) as dst:
                temporary = dst.name
                with open(local['candidate'], 'rb') as src:
                    shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            check_inputs(state)
            os.replace(temporary, source)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        if file_hash(source) != local['sha256']:
            raise ValueError('Published file read-back failed')
        return confirmed_publish()


def accept_visual(run_dir, evidence_path, target='local'):
    with locked_run(run_dir) as (directory, state):
        check_inputs(state)
        status = state[target]
        if status['status'] not in ('verified', 'accepted'):
            raise ValueError('Machine verification must pass before visual acceptance')
        evidence = read_json(evidence_path)
        required = ('content', 'photo_binding', 'merges', 'row_heights', 'borders', 'preview')
        if evidence.get('schema') != 'purchase-ledger-visual/v1' or evidence.get('target_id') != state['plan']['target_id'] or evidence.get('workbook_sha256') != status['sha256']:
            raise ValueError('Visual evidence must identify this target and current verified file')
        if any(evidence.get('checks', {}).get(key) is not True for key in required) or not evidence.get('reviewer'):
            raise ValueError('Every required visual check needs an actual reviewer')
        artifact = Path(evidence['artifact']).resolve(strict=True)
        if file_hash(artifact) != evidence.get('artifact_sha256'):
            raise ValueError('Visual artifact changed or lacks its fingerprint')
        if file_hash(status.get('candidate', status.get('export'))) != status['sha256']:
            raise ValueError('Workbook changed after verification')
        status.update(status='accepted', visual_evidence=str(Path(evidence_path).resolve()), visual_sha256=file_hash(evidence_path))
        save(directory, state, target + '_accepted')
        return state


def finish(run_dir):
    with locked_run(run_dir) as (directory, state):
        check_inputs(state, workbook=state['local']['status'] != 'published')
        if state['local']['status'] not in ('accepted', 'published') or state['online']['status'] not in ('not_requested', 'accepted'):
            raise ValueError('Required local/online acceptance is incomplete; success baseline unchanged')
        for name in ('local', 'online'):
            target = state[name]
            if target['status'] == 'not_requested':
                continue
            if file_hash(target.get('candidate', target.get('export'))) != target['sha256'] or file_hash(target['visual_evidence']) != target['visual_sha256']:
                raise ValueError('Acceptance evidence changed; success baseline unchanged')
            evidence = read_json(target['visual_evidence'])
            if file_hash(evidence['artifact']) != evidence['artifact_sha256']:
                raise ValueError('Visual preview artifact changed; success baseline unchanged')
            if name == 'online':
                import datetime as dt
                receipt = read_json(target['receipt'])
                when = dt.datetime.fromisoformat(receipt['completed_at'])
                if file_hash(target['receipt']) != target.get('receipt_sha256') or receipt.get('sha256') != target['sha256'] or not when.tzinfo or not 0 <= (dt.datetime.now(dt.timezone.utc)-when).total_seconds() <= 300:
                    raise ValueError('Refresh online export and acceptance before finishing; latest receipt must be within five minutes')
        report = inspect_ledger(state['local']['candidate'], state['inputs']['recognition']['path'],
                                state['source_root'], state['inputs']['config']['path'], source_id=state['plan']['target_id'])
        report.update(acceptance_verified=True, observation_only=False)
        atomic_json(directory / 'context_after.json', report)
        save(directory, state, 'complete')
        atomic_json(directory / 'success.json', {'schema': 'purchase-ledger-success/v1', 'target_id': state['plan']['target_id'],
                    'baseline': str(directory / 'context_after.json'), 'state': str(directory / 'run.json'),
                    'candidate': state['local']['candidate'], 'published': state['local']['status'] == 'published'})
        return state
