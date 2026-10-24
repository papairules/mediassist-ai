"""Local command for deterministic Crestline PDF extraction."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .extract import ExtractionError, extract_documents, write_datasets
from .ocr import OcrFunction, find_tesseract, tesseract_ocr


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("crestline_synthetic_v1/source_csv/document_manifest.csv"),
    )
    parser.add_argument(
        "--pdf-directory",
        type=Path,
        default=Path("crestline_synthetic_v1/source_pdfs"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("build/document_extraction"),
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("auto", "disabled"),
        default="auto",
        help="Use local Tesseract when found, or explicitly leave image-only pages for review.",
    )
    parser.add_argument("--tesseract-command", default="tesseract")
    parser.add_argument("--fail-on-review", action="store_true")
    return parser


def _ocr_function(mode: str, command: str) -> OcrFunction | None:
    if mode == "disabled" or find_tesseract(command) is None:
        return None

    def run(pdf_path: Path, page_number: int) -> str:
        return tesseract_ocr(pdf_path, page_number, command=command)

    return run


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = build_parser().parse_args(argv)
    try:
        result = extract_documents(
            arguments.manifest,
            arguments.pdf_directory,
            ocr=_ocr_function(arguments.ocr_mode, arguments.tesseract_command),
        )
        paths = write_datasets(result, arguments.output_directory)
    except (ExtractionError, OSError) as exc:
        LOGGER.error("Document extraction failed: %s", exc)
        return 1

    reviews = result.review_records()
    LOGGER.info(
        "Extraction complete pages=%d substitutions=%d clauses=%d ifu_facts=%d review=%d",
        len(result.document_pages),
        len(result.substitution_guide_rows),
        len(result.contract_clauses),
        len(result.ifu_product_facts),
        len(reviews),
    )
    for name, path in paths.items():
        LOGGER.info("Wrote %s to %s", name, path)
    return 2 if arguments.fail_on_review and reviews else 0


if __name__ == "__main__":
    raise SystemExit(main())
