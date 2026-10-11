"""Small, bounded CLI summaries and protected auxiliary JSON files (stdlib only)."""
import copy
import json
from pathlib import Path

MAX_CHARS = 5000


def read_json(path, limit=16 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('JSON exceeds the documented resource limit')
    return json.loads(path.read_text(encoding='utf-8-sig'),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))


def write_new_json(path, data, protected=()):
    path = Path(path)
    resolved = path.resolve()
    if path.suffix.lower() != '.json' or path.exists() or path.is_symlink():
        raise ValueError('Choose a NEW auxiliary .json; existing files are never overwritten')
    if any(resolved == Path(source).resolve() for source in protected if source is not None):
        raise ValueError('Auxiliary output must be separate from all inputs')
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(payload + '\n')
    return str(resolved)


def summary_json(summary, max_chars=2000):
    """Keep status/counts/paths intact; omit payload entries, never JSON syntax."""
    if not 512 <= max_chars <= MAX_CHARS:
        raise ValueError('max-chars must be 512..5000')
    result = copy.deepcopy(summary)
    protected = {'status', 'check_complete', 'error_count', 'errors_omitted', 'missing_cache_count',
                 'details_path', 'output', 'observation_only',
                 'recognition_and_acceptance_not_verified',
                 'summary_truncated', 'summary_omitted_items'}
    result['summary_truncated'] = False
    result['summary_omitted_items'] = 0

    def dumps(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

    def shorten(value):
        if isinstance(value, str) and len(value) > 240:
            result['summary_truncated'] = True
            return value[:200] + '…[see full report]'
        if isinstance(value, list):
            return [shorten(item) for item in value]
        if isinstance(value, dict):
            return {key: shorten(item) for key, item in value.items()}
        return value

    for key in list(result):
        if key not in protected:
            result[key] = shorten(result[key])
    while len(dumps(result)) > max_chars:
        choices = []

        def collect(value, parent=None):
            if isinstance(value, list) and value:
                choices.append((len(dumps(value[-1])), value, parent))
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item, value)

        for key, value in result.items():
            if key not in protected:
                collect(value, result)
        if choices:
            _, sequence, parent = max(choices, key=lambda item: item[0])
            sequence.pop()
            if isinstance(parent, dict) and parent.get('items') is sequence and 'omitted' in parent:
                parent['omitted'] += 1
        else:
            keys = [key for key in result if key not in protected]
            if not keys:
                raise ValueError('Required summary metadata exceeds max-chars; choose a shorter report path or larger budget')
            del result[max(keys, key=lambda key: len(dumps(result[key])))]
        result['summary_truncated'] = True
        result['summary_omitted_items'] += 1
        if 'error_count' in result:
            result['errors_omitted'] = result['error_count'] - len(result.get('errors', []))
    return dumps(result)


def emit_summary(summary, max_chars=2000):
    print(summary_json(summary, max_chars))


def add_summary_arguments(parser):
    parser.add_argument('--max-chars', type=int, default=2000,
                        help='Maximum JSON summary characters, 512..5000 (default 2000)')


def error_category(error):
    text = str(error).lower()
    for marker, category in [('formula error', 'formula'), ('no cached', 'missing_cache'),
                             ('expected ', 'amount'), ('forbidden', 'placeholder'),
                             ('unwanted', 'summary_counts'), ('drawing', 'drawing')]:
        if marker in text:
            return category
    return 'other'


def error_summary(errors, maximum=10):
    categories, representatives = {}, []
    for index, error in enumerate(errors):
        category = error_category(error)
        if category not in categories:
            representatives.append(index)
        categories[category] = categories.get(category, 0) + 1
    chosen = representatives[:maximum]
    chosen_set = set(chosen)
    for index in range(len(errors)):
        if len(chosen) >= maximum:
            break
        if index not in chosen_set:
            chosen.append(index)
    return {'error_count': len(errors), 'errors': [errors[index] for index in chosen],
            'errors_omitted': len(errors) - len(chosen), 'error_types': categories}
