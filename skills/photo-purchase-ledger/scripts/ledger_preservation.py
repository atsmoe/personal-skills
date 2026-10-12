"""Shared preservation checks for completed edits and uncertain online writes."""
import json
import re

from ledger_model import decimal


def shift_formula(formula, start, count):
    """Translate local A1 references after whole-row insertion, including $ rows.

    Cross-sheet/structured references require a dedicated adapter. Never guess at
    those, and never rewrite A1-looking text inside a quoted string.
    """
    if not formula:
        return None
    parts = re.split(r'("(?:[^"]|"")*")', str(formula).lstrip('='))
    for index in range(0, len(parts), 2):
        text = parts[index]
        if count and any(char in text for char in '![]'):
            raise ValueError('Cross-sheet or structured formulas require a dedicated insertion adapter')
        def shifted(match):
            row = int(match[2])
            return match[1] + str(row + count if row >= start else row)
        parts[index] = re.sub(r'(?<![A-Za-z0-9_])(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)(?![A-Za-z0-9_(])', shifted, text)
    return ''.join(parts)


def shifted_picture(picture, start, count):
    result = json.loads(json.dumps(picture))
    old_row = picture['row']
    result['row'] = old_row + count if old_row >= start else old_row
    for tag, values, _ in result['geometry']:
        if 'row' in values:
            absolute = old_row + int(values['row'])
            new = absolute + count if absolute >= start else absolute
            values['row'] = str(new - result['row'])
    return result


def historical_layout_errors(before, after, start, count, extra_merges=(), extra_pictures=()):
    errors = []
    shift = lambda row: row + count if row >= start else row
    expected_merges = sorted((shift(a), b, shift(c), d) for a, b, c, d in before.merges)
    if sorted([*expected_merges, *extra_merges]) != sorted(after.merges):
        errors.append('Merged regions changed outside the planned edits')
    # Native editors quantize imported width units on save (under half a pixel).
    if before.column_widths.keys() != after.column_widths.keys() or any(abs(before.column_widths[c]-after.column_widths[c]) > 0.05 for c in before.column_widths):
        errors.append('Historical column widths changed')
    for old in before.rows:
        new = shift(old)
        if before.cell_styles.get(old, {}) != after.cell_styles.get(new, {}):
            errors.append(f'Historical style changed at row {old}')
        old_dims, new_dims = before.row_dimensions.get(old, {}), after.row_dimensions.get(new, {})
        if decimal(old_dims.get('ht', before.default_height)) != decimal(new_dims.get('ht', after.default_height)):
            errors.append(f'Historical row height changed at row {old}')
        for key in ('hidden', 'outlineLevel', 'collapsed'):
            if old_dims.get(key, '0') != new_dims.get(key, '0'):
                errors.append(f'Historical row visibility changed at row {old}')
    expected = [shifted_picture(p, start, count) for p in before.pictures]
    current = list(after.pictures)
    # Planned pictures are checked separately; exclude precisely their host/hash.
    for row, col, sha in extra_pictures:
        match = next((p for p in current if (p['row'], p['col'], p['sha256']) == (row, col, sha)), None)
        if match is None:
            errors.append('Planned photo is missing or changed')
        else:
            current.remove(match)
    key = lambda p: json.dumps(p, sort_keys=True)
    if sorted(expected, key=key) != sorted(current, key=key):
        errors.append('Historical photo content, binding or geometry changed')
    return errors
