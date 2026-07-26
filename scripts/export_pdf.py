#!/usr/bin/env python3
"""Export ordered Comic Sol page PNGs as one deterministic raster PDF."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from comic_sol import PAGE_HEIGHT, PAGE_WIDTH, read_json, atomic_write_json, sha256_file
from project_io import durable_atomic_write
from validate_project import validate_manifest, require_valid_project


PAGE_PATTERN = re.compile(r"^page-([0-9]{3})\.png$")
PDF_STREAM_PATTERN = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


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


def _embedded_pdf_frames(payload: bytes) -> list[Image.Image]:
    frames: list[Image.Image] = []
    for match in PDF_STREAM_PATTERN.finditer(payload):
        stream = match.group(1)
        if not stream.startswith(b"\xff\xd8"):
            continue
        try:
            with Image.open(io.BytesIO(stream)) as image:
                converted = image.convert("RGB")
                converted.load()
                frames.append(converted)
        except (OSError, SyntaxError):
            continue
    return frames


def _verify_written_pdf(path: Path, source_pages: list[Image.Image]) -> None:
    frames: list[Image.Image] = []
    opened_pdf: Image.Image | None = None
    try:
        try:
            opened_pdf = Image.open(path)
            for index in range(opened_pdf.n_frames):
                opened_pdf.seek(index)
                frame = opened_pdf.convert("RGB")
                frame.load()
                frames.append(frame)
        except UnidentifiedImageError:
            frames = _embedded_pdf_frames(path.read_bytes())
        if len(frames) != len(source_pages):
            raise PdfExportError("written PDF page count does not match source pages")
        sample_points = (
            (0, 0),
            (PAGE_WIDTH - 1, 0),
            (0, PAGE_HEIGHT - 1),
            (PAGE_WIDTH - 1, PAGE_HEIGHT - 1),
        )
        for index, (frame, source) in enumerate(zip(frames, source_pages), 1):
            if frame.mode != "RGB" or frame.size != (PAGE_WIDTH, PAGE_HEIGHT):
                raise PdfExportError(f"written PDF page {index} has invalid mode or size")
            for point in sample_points:
                expected = source.getpixel(point)
                actual = frame.getpixel(point)
                if any(abs(left - right) > 4 for left, right in zip(expected, actual)):
                    raise PdfExportError(f"written PDF page order/content mismatch at page {index}")
    finally:
        if opened_pdf is not None:
            opened_pdf.close()
        for frame in frames:
            frame.close()


def export_pdf(project_dir: Path, output_path: Path | None = None) -> Path:
    """Validate, export, reopen, and atomically publish an ordered raster PDF."""
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
    except BaseException:
        for page in pages:
            page.close()
        raise

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
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
        _verify_written_pdf(temporary_path, pages)
        durable_atomic_write(destination, temporary_path.read_bytes())
        return destination
    except PdfExportError:
        raise
    except (OSError, ValueError) as error:
        raise PdfExportError(f"PDF export failed: {error}") from error
    finally:
        for page in pages:
            page.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def guarded_export(project_dir: Path, output_path: Path | None = None) -> Path:
    """Require export-ready validation before exporting, then record descriptor."""
    project_dir = Path(project_dir)
    require_valid_project(project_dir, "export-ready")
    destination = export_pdf(project_dir, output_path)
    manifest = read_json(project_dir / "project.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    artifacts["pdf"] = {
        "path": destination.relative_to(project_dir).as_posix(),
        "sha256": sha256_file(destination),
    }
    manifest["artifacts"] = artifacts
    atomic_write_json(project_dir / "project.json", manifest)
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
