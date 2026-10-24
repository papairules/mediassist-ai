"""Narrow OCR adapter for image-only synthetic PDF pages."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


OcrFunction = Callable[[Path, int], str]


def find_tesseract(command: str = "tesseract") -> str | None:
    """Return a usable Tesseract executable without assuming one is installed."""
    return shutil.which(command)


def tesseract_ocr(pdf_path: Path, page_number: int, command: str = "tesseract") -> str:
    """Render one PDF page and OCR it with an explicitly available Tesseract binary."""
    executable = find_tesseract(command)
    if executable is None:
        raise RuntimeError(f"OCR executable is unavailable: {command}")

    import pypdfium2 as pdfium

    with tempfile.TemporaryDirectory(prefix="crestline_ocr_") as temporary_directory:
        temporary_path = Path(temporary_directory)
        image_name = "page.png"
        document = pdfium.PdfDocument(pdf_path)
        try:
            page = document[page_number - 1]
            try:
                bitmap = page.render(scale=3.0)
                try:
                    bitmap.to_pil().save(temporary_path / image_name)
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()

        process = subprocess.run(
            [executable, image_name, "stdout", "--psm", "6"],
            cwd=temporary_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            message = process.stderr.strip() or "Tesseract returned a non-zero status"
            raise RuntimeError(message)
        return process.stdout.strip()
