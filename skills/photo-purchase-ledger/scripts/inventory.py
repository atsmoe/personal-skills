"""Inventory date-folder images without changing originals; paths are caller supplied."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from pathlib import Path

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_IMAGES = 2000


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(day, order_file=None):
    day = Path(day).resolve(strict=True)
    original = None
    if order_file is not None:
        order_file = Path(order_file)
        if order_file.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Order manifest exceeds 4 MiB')
        document = json.loads(order_file.read_text(encoding='utf-8-sig'))
        items = document.get('images') if isinstance(document, dict) else None
        if not isinstance(items, list) or len(items) > MAX_IMAGES:
            raise ValueError('Use an inventory exported on the original device')
        original = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('file'), str):
                raise ValueError('Invalid original inventory record')
            name, stamp = item['file'], item.get('creation_timestamp')
            if name in original or Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('Duplicate or unsafe image name in order manifest')
            if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
                raise ValueError('Original creation timestamps are required')
            original[name] = item
    images, total = [], 0
    for path in day.iterdir():
        if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
            continue
        if path.resolve().parent != day:
            raise ValueError('Image links must not escape the date folder')
        stat = path.stat()
        total += stat.st_size
        if stat.st_size > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES or len(images) >= MAX_IMAGES:
            raise ValueError('Image inventory exceeds documented resource limits; split the batch')
        digest = file_hash(path)
        if original is not None:
            item = original.get(path.name)
            if item is None or item.get('sha256') != digest:
                raise ValueError('Original inventory does not cover this image or its bytes changed: ' + path.name)
            created = item['creation_timestamp']
        else:
            created = getattr(stat, 'st_birthtime', stat.st_ctime if os.name == 'nt' else None)
            if created is None:
                raise RuntimeError('Creation time unavailable; use --order-file with an inventory exported on the original device. Do not substitute modification time or hash-name order.')
        images.append({'file': path.name, 'creation_timestamp': created,
                       'creation_time': dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat(),
                       'bytes': stat.st_size, 'sha256': digest})
    if original is not None and set(original) != {p['file'] for p in images}:
        raise ValueError('Original inventory and current image set differ')
    images.sort(key=lambda x: x['creation_timestamp'])
    times, hashes = {}, {}
    for i, item in enumerate(images, 1):
        item['index'] = i
        times.setdefault(item['creation_timestamp'], []).append(item['file'])
        hashes.setdefault(item['sha256'], []).append(item['file'])
    return {'schema': 'purchase-image-inventory/v1', 'folder': str(day), 'images': images,
            'time_ties': [v for v in times.values() if len(v) > 1],
            'duplicate_bytes': [v for v in hashes.values() if len(v) > 1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True, type=Path)
    parser.add_argument('--order-file', type=Path, help='Original-device inventory, verified by image SHA-256')
    parser.add_argument('--output', type=Path, help='New auxiliary JSON inside the date folder; existing files are never overwritten')
    args = parser.parse_args()
    day = args.day.resolve()
    if not day.is_dir():
        parser.error('Date folder does not exist')
    output = (args.output or day / ('.work_' + day.name) / 'inventory.json').resolve()
    if output.suffix.lower() != '.json' or day not in output.parents or output.exists():
        parser.error('Choose a NEW auxiliary .json inside the date folder; never an original or existing file')
    result = inventory(day, args.order_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'output': str(output), 'time_ties': result['time_ties'],
                      'duplicate_bytes': result['duplicate_bytes']}, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
