#!/usr/bin/env python3
"""Export ordered Comic Sol page PNGs as one deterministic raster PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from comic_sol import (
    PAGE_HEIGHT,
    PAGE_WIDTH,
    canonical_artifact_bytes,
    read_json,
    sha256_file,
)
from pdf_quality import PdfQualityError, verify_pdf_payload
from project_io import ProjectTransaction, durable_atomic_write
from validate_project import validate_manifest, require_valid_project


PAGE_PATTERN = re.compile(r"^page-([0-9]{3})\.png$")


class PdfExportError(ValueError):
    """Raised when composed pages cannot be safely exported."""


def _validated_manifest(project_dir: Path) -> dict[str, object]:
    manifest_path = project_dir / "project.json"
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PdfExportError(f"invalid project manifest: {error}") from error
    issues = validate_manifest(manifest)
    if issues:
        first = issues[0]
        raise PdfExportError(
            f"invalid project manifest at {first.field}: {first.message}"
        )
    return manifest


def discover_pages(project_dir: Path) -> list[Path]:
    """Discover the exact contiguous page sequence required by the manifest."""
    project_dir = Path(project_dir)
    manifest = _validated_manifest(project_dir)
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        raise PdfExportError("invalid project manifest settings")
    page_count = settings.get("page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise PdfExportError("manifest page_count must be a positive integer")

    pages_dir = project_dir / "pages"
    if not pages_dir.is_dir():
        raise PdfExportError("no composed page PNGs exist: pages directory is missing")
    numbered: list[tuple[int, Path]] = []
    for path in pages_dir.iterdir():
        match = PAGE_PATTERN.fullmatch(path.name)
        if match is not None and path.is_file():
            numbered.append((int(match.group(1)), path))
    numbered.sort(key=lambda item: item[0])
    if not numbered:
        raise PdfExportError("no composed page PNGs exist")
    actual = [number for number, _ in numbered]
    expected = list(range(1, page_count + 1))
    if actual != expected:
        missing = [f"page-{number:03d}.png" for number in expected if number not in actual]
        detail = f"; missing {', '.join(missing)}" if missing else ""
        raise PdfExportError(
            f"page filenames must be contiguous from page-001.png through "
            f"page-{page_count:03d}.png{detail}"
        )
    return [path for _, path in numbered]


def _load_pages(paths: list[Path]) -> list[Image.Image]:
    pages: list[Image.Image] = []
    try:
        for path in paths:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    if image.format != "PNG":
                        raise PdfExportError(f"{path.name} must contain PNG data")
                    if image.size != (PAGE_WIDTH, PAGE_HEIGHT):
                        raise PdfExportError(
                            f"{path.name} must be exactly {PAGE_WIDTH}x{PAGE_HEIGHT}"
                        )
                    converted = image.convert("RGB")
                    converted.load()
                    pages.append(converted)
            except PdfExportError:
                raise
            except (OSError, SyntaxError) as error:
                raise PdfExportError(f"{path.name} is not a readable PNG") from error
    except Exception:
        for page in pages:
            page.close()
        raise
    return pages


def _render_verified_payload(
    directory: Path,
    filename: str,
    pages: list[Image.Image],
) -> tuple[bytes, dict[str, object]]:
    """Render once, fsync, then verify every decoded pixel before publication."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=f".{filename}.",
            suffix=".tmp.pdf",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        pages[0].save(
            temporary_path,
            format="PDF",
            resolution=150.0,
            save_all=True,
            append_images=pages[1:],
            title=False,
            producer="Comic Sol 1.0",
            creationDate=False,
            modDate=False,
        )
        # Windows rejects fsync on a read-only descriptor.
        with temporary_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        payload = temporary_path.read_bytes()
        try:
            verification = verify_pdf_payload(payload, pages)
        except PdfQualityError as error:
            raise PdfExportError(str(error)) from error
        return payload, verification
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def export_pdf(project_dir: Path, output_path: Path | None = None) -> Path:
    """Validate, fully verify, and atomically publish an ordered raster PDF."""
    project_dir = Path(project_dir)
    manifest = _validated_manifest(project_dir)
    page_paths = discover_pages(project_dir)
    pages = _load_pages(page_paths)
    try:
        if output_path is None:
            project_id = manifest.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise PdfExportError("manifest project_id is invalid")
            destination = project_dir / f"exports/{project_id}.pdf"
        else:
            destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload, _ = _render_verified_payload(
            destination.parent, destination.name, pages
        )
        durable_atomic_write(destination, payload)
        return destination
    except PdfExportError:
        raise
    except (OSError, ValueError) as error:
        raise PdfExportError(f"PDF export failed: {error}") from error
    finally:
        for page in pages:
            page.close()


def guarded_export(project_dir: Path, output_path: Path | None = None) -> Path:
    """Verify and transactionally publish PDF, provenance, and descriptors."""
    project_dir = Path(project_dir)
    require_valid_project(project_dir, "export-ready")
    manifest = read_json(project_dir / "project.json")
    project_id = manifest.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise PdfExportError("manifest project_id is invalid")
    destination = (
        Path(output_path)
        if output_path is not None
        else project_dir / f"exports/{project_id}.pdf"
    )
    try:
        pdf_relative = destination.relative_to(project_dir).as_posix()
    except ValueError as error:
        raise PdfExportError(
            "guarded export destination must remain inside the project"
        ) from error

    page_paths = discover_pages(project_dir)
    pages = _load_pages(page_paths)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload, metrics = _render_verified_payload(
            destination.parent, destination.name, pages
        )
    finally:
        for page in pages:
            page.close()

    pdf_sha256 = hashlib.sha256(payload).hexdigest()
    source_pages: list[dict[str, object]] = []
    for page_number, page_path in enumerate(page_paths, 1):
        qa_relative = f"qa/pages/page-{page_number:03d}.json"
        qa_path = project_dir / qa_relative
        source_pages.append({
            "dimensions": [PAGE_WIDTH, PAGE_HEIGHT],
            "page_qa_path": qa_relative,
            "page_qa_sha256": sha256_file(qa_path),
            "path": page_path.relative_to(project_dir).as_posix(),
            "sha256": sha256_file(page_path),
        })
    verification = {
        **metrics,
        "kind": "pdf-verification",
        "pdf_path": pdf_relative,
        "pdf_sha256": pdf_sha256,
        "schema_version": "1.0",
        "source_pages": source_pages,
        "verified_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    verification_payload = canonical_artifact_bytes(verification)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    artifacts["pdf"] = {"path": pdf_relative, "sha256": pdf_sha256}
    artifacts["pdf_verification"] = {
        "path": "exports/pdf-verification.json",
        "sha256": hashlib.sha256(verification_payload).hexdigest(),
    }
    manifest["artifacts"] = artifacts
    with ProjectTransaction(project_dir, "pdf-export") as transaction:
        transaction.stage_bytes(pdf_relative, payload)
        transaction.stage_bytes(
            "exports/pdf-verification.json", verification_payload
        )
        transaction.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="export_pdf.py")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        # The canonical destination records the pdf descriptor that final
        # validation requires; an explicit --output is an ad-hoc copy.
        if arguments.output is None:
            print(guarded_export(arguments.project_dir))
        else:
            print(export_pdf(arguments.project_dir, arguments.output))
        return 0
    except (OSError, TypeError, ValueError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
