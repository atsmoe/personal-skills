#!/usr/bin/env python3
"""Render reimbursement PDFs to provenance-tracked PNGs with PyMuPDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf


RENDER_PARAMETERS = {
    "dpi": 300,
    "colorspace": "RGB",
    "alpha": False,
    "annots": True,
    "format": "PNG",
}


@dataclass(frozen=True)
class RenderLimits:
    pdf_bytes: int = 100 * 1024 * 1024
    documents: int = 200
    pages: int = 500
    page_pixels: int = 40_000_000
    total_pages: int = 1000
    total_pixels: int = 250_000_000
    total_source_bytes: int = 512 * 1024 * 1024


def check_pdf_limits(pdf_path: Path, limits: RenderLimits) -> tuple[int, int]:
    if pdf_path.stat().st_size > limits.pdf_bytes:
        raise ValueError('PDF exceeds source byte limit')
    with pymupdf.open(pdf_path) as doc:
        if doc.needs_pass or not 1 <= doc.page_count <= limits.pages:
            raise ValueError('Encrypted PDF or page count exceeds limit')
        pixels = 0
        for page in doc:
            width, height = page.rect.width, page.rect.height
            if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                raise ValueError('Invalid PDF page dimensions')
            # Add a pixel per side to conservatively cover raster rounding.
            count = (math.ceil(width * 300 / 72) + 1) * (math.ceil(height * 300 / 72) + 1)
            if count > limits.page_pixels:
                raise ValueError('PDF page exceeds render pixel limit')
            pixels += count
            if pixels > limits.total_pixels:
                raise ValueError('PDF exceeds total render pixel limit')
        return doc.page_count, pixels


def preflight_pdfs(paths: list[Path], limits: RenderLimits) -> None:
    if len(paths) > limits.documents or sum(p.stat().st_size for p in paths) > limits.total_source_bytes:
        raise ValueError('PDF batch exceeds document or source byte limit')
    pages, pixels = 0, 0
    for path in paths:
        count, size = check_pdf_limits(path, limits)
        pages += count
        pixels += size
        if pages > limits.total_pages or pixels > limits.total_pixels:
            raise ValueError('PDF batch exceeds total page or render pixel limit')


def add_limit_arguments(parser):
    parser.add_argument('--max-pages', type=int, default=500, help='Maximum pages per PDF')
    parser.add_argument('--max-page-pixels', type=int, default=40_000_000)
    parser.add_argument('--max-total-pages', type=int, default=1000)
    parser.add_argument('--max-total-pixels', type=int, default=250_000_000)


def limits_from_args(args):
    values = (args.max_pages, args.max_page_pixels, args.max_total_pages, args.max_total_pixels)
    if any(v <= 0 for v in values):
        raise ValueError('Render resource limits must be positive')
    return RenderLimits(pages=args.max_pages, page_pixels=args.max_page_pixels,
                        total_pages=args.max_total_pages, total_pixels=args.max_total_pixels)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def output_name(pdf_path: Path, page_no: int, page_count: int) -> str:
    if page_count == 1:
        return f"{pdf_path.stem}.png"
    return f"{pdf_path.stem}-第{page_no:03d}页.png"


def version_info() -> dict[str, Any]:
    return {
        "pymupdf": getattr(pymupdf, "__version__", None),
        "binding": getattr(pymupdf, "VersionBind", None),
        "mupdf_backend": getattr(pymupdf, "VersionFitz", None),
        "release_date": getattr(pymupdf, "VersionDate", None),
    }


def render_one(pdf_path: Path, source_root: Path, png_root: Path, limits: RenderLimits | None = None) -> dict[str, Any]:
    limits = limits or RenderLimits()
    pdf_path, source_root, png_root = pdf_path.resolve(strict=True), source_root.resolve(strict=True), png_root.resolve()
    if not pdf_path.is_relative_to(source_root) or png_root.is_relative_to(source_root) or source_root.is_relative_to(png_root):
        raise ValueError('Source PDF must stay inside source root; PNG output must not overlap originals')
    relative_pdf = pdf_path.relative_to(source_root)
    output_parent = png_root / relative_pdf.parent
    if not output_parent.resolve().is_relative_to(png_root):
        raise ValueError('PNG output link escapes output root')
    if pdf_path.stat().st_size > limits.pdf_bytes:
        raise RuntimeError('PDF exceeds source byte limit')

    source_record: dict[str, Any] = {
        "source_relative_path": relative_pdf.as_posix(),
        "source_type": "pdf",
        "source_sha256": sha256(pdf_path),
        "source_bytes": pdf_path.stat().st_size,
        "pages": [],
    }

    try:
        check_pdf_limits(pdf_path, limits)
        with pymupdf.open(pdf_path) as doc:
            source_record["page_count"] = doc.page_count
            if doc.page_count < 1:
                raise RuntimeError("PDF has no pages")

            targets = [output_parent / output_name(pdf_path, n + 1, doc.page_count) for n in range(doc.page_count)]
            if any(t.exists() or t.is_symlink() or not t.resolve().is_relative_to(png_root) for t in targets):
                raise FileExistsError('Existing or escaped rendered output path')
            output_parent.mkdir(parents=True, exist_ok=True)

            for page_index, page in enumerate(doc):
                page_no = page_index + 1
                target = output_parent / output_name(pdf_path, page_no, doc.page_count)
                if target.exists():
                    raise FileExistsError(f"Refusing to overwrite existing rendered evidence: {target}")

                pix = page.get_pixmap(
                    dpi=300,
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                    annots=True,
                )
                pix.save(target)
                source_record["pages"].append(
                    {
                        "page_number": page_no,
                        "rendered_relative_path": target.relative_to(png_root.parent).as_posix(),
                        "png_sha256": sha256(target),
                        "png_bytes": target.stat().st_size,
                        "width": pix.width,
                        "height": pix.height,
                        "render_parameters": RENDER_PARAMETERS,
                    }
                )
    except Exception as exc:
        source_record["status"] = "failed"
        source_record["error"] = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"Failed closed while rendering {relative_pdf}: {exc}") from exc

    source_record["status"] = "rendered"
    return source_record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path, help="Read-only root containing original PDF evidence")
    parser.add_argument("rework_output", type=Path, help="New candidate/rework output directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Manifest path (default: <rework_output>/pdf-render-manifest.json)",
    )
    add_limit_arguments(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    limits = limits_from_args(args)
    source_root = args.source_root.resolve(strict=True)
    rework_output = args.rework_output.resolve(strict=False)
    png_root = rework_output / "PDF转PNG"
    manifest_path = (args.manifest or (rework_output / "pdf-render-manifest.json")).resolve()

    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    if is_relative_to(rework_output, source_root) or is_relative_to(source_root, rework_output):
        raise ValueError("Rework output must not overlap the original source directory")
    if rework_output.exists():
        raise FileExistsError('Rework output must be a NEW directory')
    if not manifest_path.is_relative_to(rework_output) or manifest_path.suffix.lower() != '.json':
        raise ValueError('Manifest must be an auxiliary JSON inside rework output')
    if manifest_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing manifest: {manifest_path}")

    pdf_paths = sorted(
        (path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found under {source_root}")
    if any(not p.resolve().is_relative_to(source_root) for p in pdf_paths):
        raise ValueError('PDF source links must not escape original source root')
    preflight_pdfs(pdf_paths, limits)

    rework_output.mkdir(parents=True, exist_ok=False)
    png_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema": "reimbursement-pdf-render-provenance/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": os.fspath(source_root),
        "rework_output": os.fspath(rework_output),
        "png_root": os.fspath(png_root),
        "tool": {"library": "PyMuPDF / MuPDF", "import": "pymupdf", **version_info()},
        "render_parameters": RENDER_PARAMETERS,
        "documents": [],
    }

    try:
        for pdf_path in pdf_paths:
            manifest["documents"].append(render_one(pdf_path, source_root, png_root, limits))
    except Exception:
        # A partial run is intentionally left visible for audit, but no success manifest is written.
        raise

    manifest["summary"] = {
        "pdf_count": len(manifest["documents"]),
        "pdf_page_count": sum(int(item["page_count"]) for item in manifest["documents"]),
        "rendered_png_count": sum(len(item["pages"]) for item in manifest["documents"]),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"manifest": os.fspath(manifest_path), **manifest["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
