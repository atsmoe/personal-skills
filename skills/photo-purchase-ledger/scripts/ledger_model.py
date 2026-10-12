"""Evidence validation and position-independent purchase matching (stdlib only)."""
import datetime as dt
import hashlib
import json
import posixpath
import re
import math
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bounded_xlsx import BoundedXlsx, file_hash
from ledger_context import NS, workbook_snapshot
from ledger_io import read_json

FIELDS = ('name', 'spec', 'quantity', 'unit', 'unit_price', 'amount', 'scene')
NUMERIC = {'quantity', 'unit_price', 'amount'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError('A finite decimal value is required')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('A finite decimal value is required') from error
    if not result.is_finite() or result.copy_abs() > Decimal('1e15'):
        raise ValueError('A finite decimal within the ledger range is required')
    return result


def normal(value, field):
    if value in (None, ''):
        return None
    return str(decimal(value).quantize(Decimal('0.000000001')).normalize()) if field in NUMERIC else str(value).strip()


def column_number(column):
    if not isinstance(column, str) or not re.fullmatch('[A-Z]{1,3}', column):
        raise ValueError('Invalid column mapping')
    value = 0
    for letter in column:
        value = value * 26 + ord(letter) - 64
    if value > 16384:
        raise ValueError('Column mapping exceeds Excel limits')
    return value


def address(reference):
    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', reference)
    if not match or int(match[2]) > 1048576:
        raise ValueError('Invalid cell reference')
    return int(match[2]), column_number(match[1])


def config_from(path):
    config = read_json(path, 1024 * 1024)
    if config.get('schema') != 'purchase-ledger-project/v1':
        raise ValueError('Use an explicit purchase-ledger-project/v1 config')
    columns = config.get('columns', {})
    for field in ('label', 'date', *FIELDS, 'photo'):
        column_number(columns.get(field))
    if len(set(columns.values())) != len(columns):
        raise ValueError('Column mappings must be distinct')
    if not isinstance(config.get('sheet'), str) or not config['sheet']:
        raise ValueError('Select an explicit worksheet')
    if not isinstance(config.get('header_row'), int) or not 1 <= config['header_row'] <= 1048576:
        raise ValueError('Invalid header row')
    return config


def recognition_from(path, source_root):
    record = read_json(path)
    if record.get('schema') != 'purchase-recognition/v1':
        raise ValueError('Use purchase-recognition/v1 evidence')
    groups = record.get('groups')
    if not isinstance(groups, list) or not 1 <= len(groups) <= 500:
        raise ValueError('Recognition requires 1..500 purchase groups')
    root = Path(source_root).resolve(strict=True)
    seen, checked, total_bytes = set(), {}, 0
    order = None
    if record.get('order_file'):
        order_path=(root/record['order_file']).resolve(strict=True)
        if root not in order_path.parents:
            raise ValueError('Order manifest must remain inside the source root')
        order={entry['file']:entry for entry in read_json(order_path)['images']}
    for group in groups:
        group_id = group.get('id')
        if not isinstance(group_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', group_id) or group_id in seen:
            raise ValueError('Purchase group IDs must be stable, safe and unique')
        seen.add(group_id)
        dt.date.fromisoformat(group['date'])
        sources = group.get('sources')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
            raise ValueError('Every group requires source evidence')
        source_ids, changed = set(), []
        for source in sources:
            source_id = source.get('id')
            if not isinstance(source_id, str) or source_id in source_ids:
                raise ValueError('Source IDs must be unique within a group')
            source_ids.add(source_id)
            if source.get('role') not in ('receipt', 'item'):
                raise ValueError('Source role must be receipt or item')
            relative = Path(source.get('path', ''))
            resolved = (root / relative).resolve(strict=True)
            if relative.is_absolute() or root not in resolved.parents or not resolved.is_file():
                raise ValueError('Evidence paths must stay inside the explicit source root')
            if resolved.stat().st_size > 100 * 1024 * 1024:
                raise ValueError('Source image exceeds 100 MiB')
            if resolved not in checked:
                total_bytes += resolved.stat().st_size
                if len(checked) >= 2000 or total_bytes > 1024 * 1024 * 1024:
                    raise ValueError('Source batch exceeds 2000 images or 1 GiB')
                checked[resolved] = file_hash(resolved)
            if not re.fullmatch(r'[a-f0-9]{64}', source.get('sha256', '')):
                raise ValueError('Every source requires its original SHA-256')
            if checked[resolved] != source['sha256']:
                changed.append(source_id)
        group['_changed_sources'] = changed
        times=[]
        for source in sources:
            expected=source.get('creation_timestamp')
            if not isinstance(expected,(int,float)) or isinstance(expected,bool) or not math.isfinite(expected):
                changed.append(source['id']+':missing_original_order')
                continue
            path=root/source['path']
            if order is not None:
                original=order.get(source['path'],order.get(path.name,{}))
                actual=original.get('creation_timestamp') if original.get('sha256')==source['sha256'] else None
            else:
                actual=getattr(path.stat(),'st_birthtime',None)
            if actual is None or abs(actual-expected)>0.001:
                changed.append(source['id']+':original_order_changed')
            times.append(expected)
        if times!=sorted(times) or len(set(times))!=len(times) and not group.get('order_note'):
            changed.append('source_order_requires_review')
        items = group.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise ValueError('Every group requires 1..500 purchase lines')
        unresolved = []
        for index, item in enumerate(items):
            evidence = item.get('evidence', {})
            if evidence.get('source') not in source_ids or not evidence.get('description'):
                raise ValueError('Every line requires a source and evidence description')
            for field in FIELDS:
                status = item.get('verification', {}).get(field)
                value = item.get(field)
                if status not in ('verified', 'not_provided', 'unresolved'):
                    raise ValueError('Every field requires an explicit verification state')
                if status == 'unresolved':
                    unresolved.append([index, field])
                elif status == 'not_provided' and value is not None:
                    raise ValueError('Not-provided fields must remain null')
                elif status == 'verified' and value in (None, ''):
                    raise ValueError('Verified fields require a value')
                if field in NUMERIC and value is not None:
                    number = decimal(value)
                    if len(str(value)) > 64 or len(number.normalize().as_tuple().digits) > 15 or number.as_tuple().exponent < -9:
                        raise ValueError('Source decimal exceeds Excel precision')
                elif value is not None and (not isinstance(value, str) or len(value) > 2000):
                    raise ValueError('Text fields must be bounded strings')
            if item.get('name') in (None, '') or item.get('amount') is None:
                unresolved.append([index, 'required_name_or_amount'])
        group['_unresolved'] = unresolved
    return record


class WorkbookView:
    def __init__(self, path, config):
        self.path, self.config = Path(path), config
        columns = config['columns']
        self.snapshot, facts = workbook_snapshot(self.path, config['sheet'], None,
                                                  columns['label'], columns['amount'])
        self.rows = {int(row): {re.sub(r'\d+', '', cell['cell']): cell for cell in cells}
                     for row, cells in facts['rows'].items()}
        self.formula_errors, self.missing_caches = facts['formula_errors'], facts['missing_formula_caches']
        self.row_dimensions, self.cell_styles, self.merges, self.pictures = {}, {}, [], []
        with BoundedXlsx(path) as z:
            styles = self._styles(z)
            book = ET.fromstring(z.read('xl/workbook.xml'))
            book_properties = book.find('s:workbookPr', NS)
            self.date_epoch = dt.date(1904, 1, 1) if book_properties is not None and book_properties.get('date1904') in ('1', 'true') else dt.date(1899, 12, 30)
            sheet = next(s for s in book.findall('s:sheets/s:sheet', NS) if s.get('name') == config['sheet'])
            rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
            target = rels[sheet.get('{' + NS['r'] + '}id')]
            self.member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            xml = ET.fromstring(z.read(self.member))
            fmt = xml.find('s:sheetFormatPr', NS)
            self.default_height = float(fmt.get('defaultRowHeight', 15)) if fmt is not None else 15
            for row in xml.findall('s:sheetData/s:row', NS):
                number = int(row.get('r'))
                self.row_dimensions[number] = {k: v for k, v in row.attrib.items() if k != 'r'}
                self.cell_styles[number] = {re.sub(r'\d+', '', c.get('r')): styles[int(c.get('s', '0'))] for c in row.findall('s:c', NS)}
            shared = {}
            for node in xml.findall('s:sheetData/s:row/s:c', NS):
                formula = node.find('s:f',NS)
                if formula is not None and formula.get('t') == 'shared' and formula.text:
                    shared[formula.get('si')] = (address(node.get('r')),formula.text)
            for node in xml.findall('s:sheetData/s:row/s:c', NS):
                formula=node.find('s:f',NS)
                row,col=address(node.get('r'))
                column=re.sub(r'\d+','',node.get('r'))
                if formula is not None and formula.get('t')=='shared' and not formula.text:
                    (base_row,base_col),text=shared[formula.get('si')]
                    if '[' in text or ']' in text:
                        raise ValueError('Shared external/structured formulas need a dedicated formula adapter')
                    def shift_reference(match):
                        letters,absolute_row,number=match[1],match[2],int(match[3])
                        if not letters.startswith('$'):
                            value=column_number(letters)+col-base_col
                            letters=''
                            while value:
                                value,digit=divmod(value-1,26);letters=chr(65+digit)+letters
                        return letters+absolute_row+str(number if absolute_row else number+row-base_row)
                    parts=re.split(r'("(?:[^"]|"")*")',text)
                    expanded=''.join(part if i%2 else re.sub(r'(?<![A-Za-z0-9_])(\$?[A-Z]{1,3})(\$?)([1-9][0-9]*)(?![A-Za-z0-9_(])',shift_reference,part) for i,part in enumerate(parts))
                    self.rows[row][column]['formula']=expanded
            for merge in xml.findall('s:mergeCells/s:mergeCell', NS):
                ends = merge.get('ref').split(':')
                self.merges.append((*address(ends[0]), *address(ends[-1])))
            self.styles_hash = z.part_hash('xl/styles.xml') if 'xl/styles.xml' in z.namelist() else None
            cols = xml.find('s:cols', NS)
            self.column_widths={}
            if cols is not None:
                for col in cols:
                    for number in range(int(col.get('min')),int(col.get('max'))+1):
                        self.column_widths[number]=float(col.get('width',8.43))
            self.columns_layout = ET.tostring(cols, encoding='unicode') if cols is not None else None
            self._pictures(z, xml)
        self._visible_styles()

    def _visible_styles(self):
        raw = {(row, column_number(col)): style for row,cells in self.cell_styles.items() for col,style in cells.items()}
        for row,cells in self.cell_styles.items():
            for col,style in cells.items():
                if style is None:
                    continue
                edges = dict(style['edges'])
                number = column_number(col)
                # Excel may store a shared grid edge on either adjacent cell.
                for side,dr,dc,opposite in [('top',-1,0,'bottom'),('bottom',1,0,'top'),('left',0,-1,'right'),('right',0,1,'left')]:
                    adjacent = raw.get((row+dr,number+dc))
                    if edges.get(side) is None and adjacent:
                        edges[side] = adjacent['edges'].get(opposite)
                cells[col] = digest([style['other'],edges])

    @staticmethod
    def _styles(z):
        if 'xl/styles.xml' not in z.namelist():
            return [None]
        xml = ET.fromstring(z.read('xl/styles.xml'))
        default_name=xml.find('s:fonts/s:font/s:name',NS)
        default_name=default_name.get('val','Calibri') if default_name is not None else 'Calibri'
        def canonical(node):
            tag=node.tag.rsplit('}',1)[-1]
            if tag=='fill':
                pattern=node.find('s:patternFill',NS)
                fg=pattern.find('s:fgColor',NS) if pattern is not None else None
                if pattern is not None and (pattern.get('patternType','none')=='none' or fg is not None and fg.get('rgb','').startswith('00')):
                    return ['fill',[],[['patternFill',[('patternType','none')],[]]]]
            children=[canonical(child) for child in node if child.tag.rsplit('}',1)[-1] not in ('charset','family') and not (child.tag.rsplit('}',1)[-1]=='u' and child.get('val')=='none')]
            if tag=='font' and not any(c[0]=='sz' for c in children):
                children.append(['sz',[('val','11')],[]])
            if tag=='font' and not any(c[0]=='name' for c in children):
                children.append(['name',[('val',default_name)],[]])
            attrs=dict(node.attrib)
            if tag in ('b','i','strike') and attrs.get('val')=='1':
                attrs.pop('val')
            return [tag, sorted(attrs.items()), sorted(children,key=str)]
        def collection(tag):
            node = xml.find('s:' + tag, NS)
            return list(node) if node is not None else []
        fonts, fills, borders = collection('fonts'), collection('fills'), collection('borders')
        formats = {node.get('numFmtId'): node.get('formatCode') for node in collection('numFmts')}
        result = []
        for xf in collection('cellXfs'):
            attrs = {k:v for k,v in xf.attrib.items() if k not in ('fontId','fillId','borderId','numFmtId','xfId') and not k.startswith('apply')}
            border = borders[int(xf.get('borderId',0))]
            edges = {side:(canonical(n) if n is not None and n.get('style') else None)
                     for side in ('left','right','top','bottom') for n in [border.find('s:'+side,NS)]}
            for side,value in edges.items():
                if value:
                    value[0] = 'edge'
            result.append({'edges':edges,'other':[attrs, canonical(fonts[int(xf.get('fontId',0))]),
                canonical(fills[int(xf.get('fillId',0))]),
                [canonical(n) for n in border if n.tag.rsplit('}',1)[-1] not in edges and (n.attrib or len(n))],
                re.sub(r'\\+(?=-)','',formats.get(xf.get('numFmtId','0'), {'0':'General','1':'0','2':'0.00'}.get(xf.get('numFmtId','0'),xf.get('numFmtId','0')))),
                sorted([canonical(c) for c in xf], key=str)]})
        return result

    def _pictures(self, z, xml):
        relpath = posixpath.join(posixpath.dirname(self.member), '_rels', posixpath.basename(self.member) + '.rels')
        if relpath not in z.namelist():
            return
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read(relpath))}
        x = {'x': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
             'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        for node in xml.findall('s:drawing', NS):
            target = rels[node.get('{' + NS['r'] + '}id')]
            part = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(self.member), target))
            relpart = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
            images = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read(relpart))} if relpart in z.namelist() else {}
            for anchor in ET.fromstring(z.read(part)):
                start = anchor.find('x:from', x)
                blip = anchor.find('.//a:blip', x)
                if blip is not None and start is None:
                    raise ValueError('Absolute photo anchors require a dedicated preservation adapter')
                if blip is None:
                    continue
                target = images.get(blip.get('{' + NS['r'] + '}embed'))
                if not target:
                    raise ValueError('External or missing photo data cannot be verified')
                media = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
                row = int(start.find('x:row', x).text) + 1
                col = int(start.find('x:col', x).text) + 1
                geometry = []
                for child in anchor:
                    tag = child.tag.rsplit('}', 1)[-1]
                    if tag in ('from', 'to', 'ext'):
                        values = {n.tag.rsplit('}', 1)[-1]: n.text for n in child}
                        if 'row' in values:
                            values['row'] = str(int(values['row']) - row + 1)
                        geometry.append([tag, values, child.attrib])
                self.pictures.append({'row': row, 'col': col, 'sha256': z.part_hash(media), 'geometry': geometry})

    def value(self, row, field):
        column = self.config['columns'][field]
        cell = self.rows.get(row, {}).get(column)
        if (cell is None or cell.get('value') in (None, '')) and field == 'date':
            col = column_number(column)
            for r1, c1, r2, c2 in self.merges:
                if r1 <= row <= r2 and c1 <= col <= c2:
                    cell = self.rows.get(r1, {}).get(column)
                    break
        return cell.get('value') if cell else None

    def date(self, row, year):
        value = self.value(row, 'date')
        if value is None:
            return None
        text = str(value).strip()
        if re.fullmatch(r'\d+(?:\.\d+)?', text):
            return (self.date_epoch + dt.timedelta(days=int(Decimal(text)))).isoformat()
        match = re.fullmatch(r'(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?', text)
        if match:
            return dt.date(int(match[1] or year), int(match[2]), int(match[3])).isoformat()
        return text

    def detail_rows(self):
        return [row for row in sorted(self.rows) if row > self.config['header_row'] and self.value(row, 'name') not in (None, '')]

    def line(self, row):
        return [normal(self.value(row, field), field) for field in FIELDS]

    def fingerprints(self, rows):
        first = rows[0]
        formulas = []
        for row in rows:
            for col, cell in self.rows[row].items():
                formula = (cell.get('formula') or '').lstrip('=')
                if formula:
                    relative = re.sub(r'(\$?[A-Z]{1,3})(\$?)(\d+)',
                                      lambda m: m[1] + (m[2] + m[3] if m[2] else '[%+d]' % (int(m[3]) - row)), formula)
                    cached=cell.get('value')
                    if cached is not None and cell.get('type')=='n':
                        cached=normal(cached,'amount')
                    formulas.append([row-first, col, relative, cached])
        images = [p for p in self.pictures if p['row'] in rows]
        content = digest([self.line(row) for row in rows] + [formulas])
        photo = digest(sorted(p['sha256'] for p in images))
        layout = digest([[self.row_dimensions.get(row, {}), self.cell_styles.get(row, {})] for row in rows] +
                        [[(a-first, b, c-first, d) for a,b,c,d in self.merges if a <= rows[-1] and c >= first],
                         [{**p, 'row': p['row']-first} for p in images], self.columns_layout])
        return content, photo, layout


def inspect_ledger(workbook, recognition, source_root, config_path, baseline=None, source_id=None):
    config = config_from(config_path)
    record = recognition_from(recognition, source_root)
    view = WorkbookView(workbook, config)
    target = source_id or digest([str(Path(config_path).resolve()), config['sheet'], config.get('online_target')])
    config_hash = digest(config)
    old = read_json(baseline) if baseline else {}
    compatible = old.get('schema') == 'purchase-ledger-inspection/v1' and old.get('target_id') == target
    previous = {group['id']: group for group in old.get('groups', [])} if compatible else {}
    results = []
    for group in record['groups']:
        result = {'id': group['id'], 'date': group['date'], 'rows': [], 'moved': False,
                  'needs_recognition': False, 'acceptance_required': True,
                  'source_fingerprint': digest([group['sources'],group.get('photos'),group.get('order_note'),
                                                [item.get('evidence') for item in group['items']],
                                                [item.get('verification') for item in group['items']]])}
        if group['_changed_sources'] or group['_unresolved']:
            result.update(action='recognize', needs_recognition=True,
                          changed_sources=group['_changed_sources'], unresolved=group['_unresolved'])
            results.append(result)
            continue
        expected = [[normal(item.get(field), field) for field in FIELDS] for item in group['items']]
        matches = []
        for start in view.detail_rows():
            rows = list(range(start, start + len(expected)))
            if all(row in view.rows and view.line(row) == line and view.date(row,int(group['date'][:4]))==group['date'] for row, line in zip(rows, expected)):
                matches.append(rows)
        if len(matches) != 1:
            prior = previous.get(group['id'], {})
            similar = [row for row in view.detail_rows()
                       if view.date(row, int(group['date'][:4])) == group['date'] and
                       normal(view.value(row, 'name'), 'name') in [line[0] for line in expected]]
            conflict = not matches and (prior.get('rows') or similar)
            result.update(action='review_match' if matches else 'review_conflict' if conflict else 'append',
                          candidates=matches, conflict_rows=similar,
                          needs_recognition=bool(conflict))
            results.append(result)
            continue
        rows = matches[0]
        content, photos, layout = view.fingerprints(rows)
        prior = previous.get(group['id'], {})
        result.update(rows=rows, content_fingerprint=content, photo_fingerprint=photos,
                      layout_fingerprint=layout, action='verify_binding')
        trusted = old.get('acceptance_verified') is True and old.get('observation_only') is False
        if trusted and prior.get('date') == group['date'] and prior.get('content_fingerprint') == content and prior.get('source_fingerprint') == result['source_fingerprint']:
            result['moved'] = rows != prior.get('rows')
            if prior.get('photo_fingerprint') != photos:
                result.update(action='review_photos', needs_recognition=True)
            elif prior.get('layout_fingerprint') != layout or old.get('config_hash') != config_hash:
                result['action'] = 'verify_layout'
            else:
                result['action'] = 'reuse_evidence'
        elif prior and prior.get('source_fingerprint') != result['source_fingerprint']:
            result.update(action='recognize', needs_recognition=True)
        results.append(result)
    owners = {}
    for result in results:
        for row in result['rows']:
            owners.setdefault(row, []).append(result)
    for claims in owners.values():
        if len(claims) > 1:
            for claim in claims:
                claim.update(action='review_match', needs_recognition=True)
    return {'schema': 'purchase-ledger-inspection/v1', 'target_id': target,
            'config_hash': config_hash, 'workbook_sha256': view.snapshot['sha256'],
            'recognition_sha256': file_hash(recognition), 'sheet': config['sheet'],
            'baseline_compatible': compatible, 'groups': results,
            'formula_errors': view.formula_errors, 'missing_formula_caches': view.missing_caches,
            'observation_only': True, 'acceptance_verified': False}
