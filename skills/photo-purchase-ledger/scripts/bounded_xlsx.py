"""Bound ZIP/XML input before XLSX observation; no extraction or external fetching."""
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile


@dataclass(frozen=True)
class XlsxLimits:
    file_bytes: int = 256 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    member_bytes: int = 64 * 1024 * 1024
    xml_bytes: int = 16 * 1024 * 1024
    members: int = 10000
    compression_ratio: int = 1000


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class BoundedXlsx(ZipFile):
    def __init__(self, path, limits=None):
        self.limits = limits or XlsxLimits()
        if Path(path).stat().st_size > self.limits.file_bytes:
            raise ValueError('XLSX compressed file exceeds resource limit')
        super().__init__(path, 'r')
        try:
            infos = self.infolist()
            if len(infos) > self.limits.members or len({i.filename for i in infos}) != len(infos):
                raise ValueError('Too many or duplicate XLSX ZIP members')
            if sum(i.file_size for i in infos) > self.limits.total_bytes:
                raise ValueError('XLSX total decompressed bytes exceed resource limit')
            for info in infos:
                limit = self.limits.xml_bytes if info.filename.lower().endswith(('.xml', '.rels')) else self.limits.member_bytes
                if info.file_size > limit or info.file_size > max(1, info.compress_size) * self.limits.compression_ratio:
                    raise ValueError('XLSX member exceeds resource limit: ' + info.filename)
        except Exception:
            self.close()
            raise

    def read(self, name, pwd=None):
        data = super().read(name, pwd)
        filename = name.filename if hasattr(name, 'filename') else str(name)
        if filename.lower().endswith(('.xml', '.rels')):
            # Also detect UTF-16/32 markup before ElementTree expands entities.
            if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data.replace(b'\x00', b''), re.IGNORECASE):
                raise ValueError('DTD/entity declarations are not accepted in XLSX XML')
        return data

    def part_hash(self, name):
        digest = hashlib.sha256()
        with self.open(name) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
