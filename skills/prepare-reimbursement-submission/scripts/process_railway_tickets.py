#!/usr/bin/env python3
"""Copy verified railway PDFs, render provenance-tracked pages, and package attachments."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from render_pdf_evidence import RENDER_PARAMETERS, render_one, sha256, version_info


def clean_part(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', str(value)).strip().rstrip('. ')


def normalize_amount(value: str) -> str:
    try:
        amount = Decimal(str(value).strip().replace('￥', '').replace('¥', '').replace(',', ''))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01')):
            raise ValueError('金额必须是非负、最多两位小数的有限数值')
        return f'{amount:.2f}'
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'无效票价：{value!r}') from exc


def build_stem(item: dict) -> str:
    fields = [clean_part(item[key]) for key in ('name', 'origin', 'destination')]
    if not all(fields):
        raise ValueError('完成状态的姓名、出发地和目的地不能为空')
    return f'{fields[0]}-{normalize_amount(item["amount"])}-{fields[1]}-{fields[2]}'


def unique_stem(stem: str, used: set[str]) -> str:
    candidate, number = stem, 1
    while candidate.casefold() in used:
        number += 1
        candidate = f'{stem}（{number}）'
    used.add(candidate.casefold())
    return candidate


def preflight(input_dir: Path, metadata: Path, output_dir: Path, zip_path: Path | None) -> list[dict]:
    if not input_dir.is_dir():
        raise NotADirectoryError(input_dir)
    if output_dir.exists():
        raise FileExistsError(f'输出目录必须是新路径，不覆盖已有内容：{output_dir}')
    if output_dir.is_relative_to(input_dir) or input_dir.is_relative_to(output_dir):
        raise ValueError('源目录与输出目录不能交叠')
    if metadata.is_relative_to(output_dir):
        raise ValueError('元数据不能位于待创建的输出目录')
    if zip_path:
        if zip_path.exists():
            raise FileExistsError(f'不覆盖已有 ZIP：{zip_path}')
        if zip_path.is_relative_to(input_dir) or zip_path.is_relative_to(output_dir):
            raise ValueError('ZIP 必须位于源目录和候选输出目录之外')

    items = json.loads(metadata.read_text(encoding='utf-8-sig'))
    if not isinstance(items, list) or not items:
        raise ValueError('metadata 必须是非空数组')
    required = {'source', 'name', 'amount', 'origin', 'destination', 'status'}
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError('元数据缺少必要字段')
        if any(not isinstance(item[key], str) for key in required) or item.get('status') not in ('完成', '待确认'):
            raise ValueError('元数据字段必须是字符串，状态为完成或待确认')
        if not isinstance(item.get('note', ''), str):
            raise ValueError('备注必须是字符串')
        relative = Path(item['source'])
        source = (input_dir / relative).resolve()
        if relative.is_absolute() or not source.is_relative_to(input_dir):
            raise ValueError('源文件必须位于源目录内')
        if not source.is_file() or source.suffix.lower() != '.pdf':
            raise ValueError(f'源 PDF 不存在：{item["source"]}')
        with source.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise ValueError(f'源文件不是 PDF：{item["source"]}')
        if source in seen:
            raise ValueError(f'同一源文件重复列出：{item["source"]}')
        seen.add(source)
        if item['status'] == '完成':
            build_stem(item)
    actual = {p.resolve() for p in input_dir.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'}
    if seen != actual:
        raise ValueError('metadata 必须逐份覆盖源目录中的全部 PDF，不遗漏或重复')
    return items


def process(input_dir: Path, metadata: Path, output_dir: Path, zip_path: Path | None = None) -> dict:
    input_dir, metadata, output_dir = input_dir.resolve(), metadata.resolve(), output_dir.resolve()
    zip_path = zip_path.resolve() if zip_path else None
    items = preflight(input_dir, metadata, output_dir, zip_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    pdf_dir, png_dir = output_dir / 'PDF', output_dir / 'PDF转PNG'
    pdf_dir.mkdir()
    png_dir.mkdir()
    records, used = [], set()
    for number, item in enumerate(items, 1):
        source = (input_dir / item['source']).resolve()
        stem = build_stem(item) if item['status'] == '完成' else f'待确认-{clean_part(source.stem)}'
        stem = unique_stem(stem, used)
        target_pdf = pdf_dir / f'{stem}.pdf'
        source_hash = sha256(source)
        shutil.copy2(source, target_pdf)
        if sha256(target_pdf) != source_hash:
            raise RuntimeError('PDF 副本与原件字节不一致')
        record = {
            'number': number, 'source': source.relative_to(input_dir).as_posix(),
            'source_sha256': source_hash, 'source_bytes': source.stat().st_size,
            'metadata': dict(item), 'pdf': target_pdf.relative_to(output_dir).as_posix(),
            'pdf_sha256': source_hash, 'status': item['status'], 'note': item.get('note', ''),
            'render': None,
        }
        try:
            record['render'] = render_one(target_pdf, pdf_dir, png_dir)
        except RuntimeError as exc:
            record['status'] = '待确认'
            record['note'] = '; '.join(filter(None, [record['note'], str(exc)]))
        if sha256(source) != source_hash:
            raise RuntimeError('处理期间原件发生变化，需重新核对')
        records.append(record)

    report = output_dir / '处理报告.csv'
    with report.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['序号', '原文件名', '姓名', '金额', '出发地', '目的地', '新PDF文件名', '图片文件名', '状态', '备注'])
        for record in records:
            item = record['metadata']
            pages = (record['render'] or {}).get('pages', [])
            amount = normalize_amount(item['amount']) if item['status'] == '完成' else item['amount']
            writer.writerow([record['number'], record['source'], item['name'], amount, item['origin'],
                             item['destination'], record['pdf'], ' | '.join(p['rendered_relative_path'] for p in pages),
                             record['status'], record['note']])
    summary = {
        'pdf_count': len(records), 'png_count': len(list(png_dir.glob('*.png'))),
        'completed': sum(r['status'] == '完成' for r in records),
        'pending': sum(r['status'] != '完成' for r in records),
        'render_failures': sum(r['render'] is None for r in records), 'output': str(output_dir),
    }
    manifest = {
        'schema': 'reimbursement-railway-attachments/v1', 'source_root': str(input_dir),
        'tool': {'library': 'PyMuPDF / MuPDF', **version_info()},
        'render_parameters': RENDER_PARAMETERS, 'documents': records, 'summary': summary,
        'visual_verification': 'not performed by script',
    }
    (output_dir / '来源清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if zip_path and not summary['render_failures']:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'x', zipfile.ZIP_DEFLATED) as archive:
            for folder in (pdf_dir, png_dir):
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(output_dir.parent))
        summary['zip'] = str(zip_path)
    elif zip_path:
        summary['zip_not_created'] = '存在渲染失败，保留候选结果并先解决异常'
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description='依据已核实的铁路客票元数据整理附件；不做 OCR，不修改原件')
    parser.add_argument('--input-dir', required=True, type=Path)
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--zip', dest='zip_path', type=Path)
    args = parser.parse_args()
    try:
        summary = process(args.input_dir, args.metadata, args.output_dir, args.zip_path)
        print(json.dumps(summary, ensure_ascii=False))
        return 2 if summary['pending'] else 0
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
