#!/usr/bin/env python3
"""Render reimbursement PDFs to provenance-tracked PNGs with PyMuPDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
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


def render_one(pdf_path: Path, source_root: Path, png_root: Path) -> dict[str, Any]:
    relative_pdf = pdf_path.relative_to(source_root)
    output_parent = png_root / relative_pdf.parent
    output_parent.mkdir(parents=True, exist_ok=True)

    source_record: dict[str, Any] = {
        "source_relative_path": relative_pdf.as_posix(),
        "source_type": "pdf",
        "source_sha256": sha256(pdf_path),
        "source_bytes": pdf_path.stat().st_size,
        "pages": [],
    }

    try:
        with pymupdf.open(pdf_path) as doc:
            source_record["page_count"] = doc.page_count
            if doc.page_count < 1:
                raise RuntimeError("PDF has no pages")

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve(strict=True)
    rework_output = args.rework_output.resolve(strict=False)
    png_root = rework_output / "PDF转PNG"
    manifest_path = args.manifest or (rework_output / "pdf-render-manifest.json")

    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    if source_root == rework_output or is_relative_to(rework_output, source_root):
        raise ValueError("Rework output must be outside the original source directory")
    if manifest_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing manifest: {manifest_path}")

    pdf_paths = sorted(
        (path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found under {source_root}")

    rework_output.mkdir(parents=True, exist_ok=True)
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
            manifest["documents"].append(render_one(pdf_path, source_root, png_root))
    except Exception:
        # A partial run is intentionally left visible for audit, but no success manifest is written.
        raise

    manifest["summary"] = {
        "pdf_count": len(manifest["documents"]),
        "pdf_page_count": sum(int(item["page_count"]) for item in manifest["documents"]),
        "rendered_png_count": sum(len(item["pages"]) for item in manifest["documents"]),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": os.fspath(manifest_path), **manifest["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
