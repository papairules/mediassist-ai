"""Create a validated, content-addressed document publication artifact set."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .extract import ExtractionError
from .ocr import find_tesseract, tesseract_ocr
from .publication import PublicationValidationError, prepare_artifact_set


LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
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
        "--output-root",
        type=Path,
        default=Path("build/document_publication"),
    )
    parser.add_argument("--tesseract-command", default="tesseract")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = _parser().parse_args(argv)
    if find_tesseract(arguments.tesseract_command) is None:
        LOGGER.error("Tesseract is required to prepare the complete publication artifact set")
        return 1

    def ocr(pdf_path: Path, page_number: int) -> str:
        return tesseract_ocr(
            pdf_path,
            page_number,
            command=arguments.tesseract_command,
        )

    try:
        artifact_set = prepare_artifact_set(
            arguments.manifest,
            arguments.pdf_directory,
            arguments.output_root,
            ocr=ocr,
        )
    except (ExtractionError, OSError, PublicationValidationError) as exc:
        LOGGER.error("Publication preparation failed: %s", exc)
        return 1
    LOGGER.info("artifact_set_id=%s", artifact_set.artifact_set_id)
    LOGGER.info("artifact_path=%s", artifact_set.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
