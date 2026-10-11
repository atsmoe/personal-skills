"""Exclusive run ownership and atomic, crash-recoverable state files."""
import contextlib
import json
import os
import tempfile
from pathlib import Path

from bounded_xlsx import file_hash
from ledger_io import read_json
from ledger_model import digest, recognition_from


def atomic_json(path, data):
    path = Path(path)
    if path.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise ValueError('Run state must not traverse links')
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.state-', suffix='.tmp', delete=False) as stream:
            name = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked_run(directory):
    directory = Path(directory).resolve(strict=True)
    lock = directory / '.run.lock'
    if lock.is_symlink():
        raise ValueError('Invalid run lock')
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('Another writer owns this run') from error
        try:
            path = directory / 'run.json'
            if path.is_symlink():
                raise ValueError('Invalid run state')
            state = read_json(path)
            if state.get('schema') != 'purchase-ledger-run/v1' or digest(state['plan']) != state['plan_hash']:
                raise ValueError('Run schema or plan integrity check failed')
            yield directory, state
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save(directory, state, stage):
    state['stage'] = stage
    state['history'].append({'stage': stage})
    atomic_json(directory / 'run.json', state)


def check_inputs(state, workbook=True):
    for name, source in state['inputs'].items():
        if name == 'workbook' and not workbook:
            continue
        if file_hash(source['path']) != source['sha256']:
            raise ValueError('Input changed: ' + name + '; prepare a fresh plan against the latest state')
    record = recognition_from(state['inputs']['recognition']['path'], state['source_root'])
    if any(g['_changed_sources'] or g['_unresolved'] for g in record['groups']):
        raise ValueError('Source evidence changed or remains unresolved')
    return record
