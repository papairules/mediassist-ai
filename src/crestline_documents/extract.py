"""Citation-ready, deterministic extraction for the Crestline PDF fixture."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from .ocr import OcrFunction


EXTRACTOR_NAME = "crestline_deterministic_pdf"
EXTRACTOR_VERSION = "1.0.0"
EXPECTED_DOCUMENT_TYPES = {
    "hospital_amendment",
    "hospital_contract",
    "manufacturer_ifu",
    "substitution_guide",
    "substitution_supplement_scanned",
}
GUIDE_STATUSES = {
    "APPROVED",
    "APPROVED (REVOKED)",
    "PENDING_REVIEW",
    "REJECTED",
}
SCANNED_STATUS = "PENDING CLINICAL REVIEW"


class ExtractionError(RuntimeError):
    """Raised when source integrity or deterministic parsing cannot be trusted."""


@dataclass(frozen=True)
class ManifestDocument:
    document_id: str
    source_path: Path
    source_file: str
    document_type: str
    status: str
    effective_from: str
    region: str
    hospital_id: str | None
    sensitivity: str
    content_sha256: str


@dataclass(frozen=True)
class ExtractionResult:
    document_pages: list[dict[str, Any]]
    substitution_guide_rows: list[dict[str, Any]]
    contract_clauses: list[dict[str, Any]]
    ifu_product_facts: list[dict[str, Any]]

    def review_records(self) -> list[dict[str, Any]]:
        return [
            row
            for dataset in (
                self.document_pages,
                self.substitution_guide_rows,
                self.contract_clauses,
                self.ifu_product_facts,
            )
            for row in dataset
            if row.get("extraction_status") != "extracted"
        ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_source_path(dataset_root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ExtractionError(f"Unsafe manifest relative_path: {relative_path!r}")
    root = dataset_root.resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents:
        raise ExtractionError(f"Manifest path escapes the dataset root: {relative_path}")
    return candidate


def load_and_reconcile_manifest(manifest_path: Path, pdf_directory: Path) -> list[ManifestDocument]:
    """Validate the manifest, PDFs, and declared SHA-256 values."""
    dataset_root = manifest_path.resolve().parent.parent
    pdf_root = pdf_directory.resolve()
    documents: list[ManifestDocument] = []
    document_ids: set[str] = set()
    declared_files: set[str] = set()

    with manifest_path.open(newline="", encoding="utf-8-sig") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            required = {
                "document_id",
                "relative_path",
                "document_type",
                "status",
                "effective_from",
                "region",
                "hospital_id",
                "sensitivity",
                "sha256",
            }
            if set(row) != required:
                raise ExtractionError("Document manifest has unexpected columns")
            document_id = row["document_id"].strip()
            relative_path = row["relative_path"].strip()
            if not document_id or document_id in document_ids:
                raise ExtractionError(f"Invalid or duplicate document_id at row {row_number}")
            source_path = _safe_source_path(dataset_root, relative_path)
            if source_path.parent != pdf_root or source_path.suffix.lower() != ".pdf":
                raise ExtractionError(f"Manifest PDF is outside the configured PDF directory: {relative_path}")
            if not source_path.is_file():
                raise ExtractionError(f"Manifest PDF is missing: {relative_path}")
            document_type = row["document_type"].strip()
            if document_type not in EXPECTED_DOCUMENT_TYPES:
                raise ExtractionError(f"Unsupported document_type: {document_type}")
            expected_hash = row["sha256"].strip().lower()
            actual_hash = _sha256(source_path)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or actual_hash != expected_hash:
                raise ExtractionError(f"SHA-256 mismatch for {document_id}")
            document_ids.add(document_id)
            declared_files.add(source_path.name)
            documents.append(
                ManifestDocument(
                    document_id=document_id,
                    source_path=source_path,
                    source_file=source_path.name,
                    document_type=document_type,
                    status=row["status"].strip(),
                    effective_from=row["effective_from"].strip(),
                    region=row["region"].strip(),
                    hospital_id=row["hospital_id"].strip() or None,
                    sensitivity=row["sensitivity"].strip(),
                    content_sha256=actual_hash,
                )
            )

    actual_files = {path.name for path in pdf_root.glob("*.pdf") if path.is_file()}
    missing_from_manifest = sorted(actual_files - declared_files)
    missing_from_directory = sorted(declared_files - actual_files)
    if missing_from_manifest or missing_from_directory:
        raise ExtractionError(
            "Manifest/PDF inventory mismatch: "
            f"unregistered={missing_from_manifest}, missing={missing_from_directory}"
        )
    return sorted(documents, key=lambda item: item.document_id)


def _normalize_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


def _normalize_inline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _redact_restricted_pricing(text: str) -> tuple[str, bool]:
    patterns = (
        r"(?im)^Confidential negotiated pricing:.*$",
        r"(?im)^Pricing: Confidential,.*$",
    )
    redacted = text
    changed = False
    for pattern in patterns:
        redacted, count = re.subn(pattern, "[REDACTED RESTRICTED PRICING]", redacted)
        changed = changed or count > 0
    return redacted, changed


def _document_identity(text: str, document: ManifestDocument) -> tuple[str | None, str]:
    identity = re.search(
        r"(?:(?:Document|IFU|Amendment|Contract) ID|Document):\s*([^|\n.]+)",
        text,
    )
    display_document_id = identity.group(1).strip() if identity else None
    revision = re.search(r"Revision:\s*([^|\n.]+)", text)
    version = re.search(r"VERSION\s+(\d+)", text)
    if revision:
        document_version = revision.group(1).strip()
    elif version:
        document_version = f"V{version.group(1)}"
    else:
        document_version = display_document_id or document.document_id
    return display_document_id, document_version


def _provenance(
    document: ManifestDocument,
    page_number: int,
    extraction_method: str,
    display_document_id: str | None,
    document_version: str,
) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "display_document_id": display_document_id,
        "document_version": document_version,
        "document_status": document.status,
        "effective_from": document.effective_from,
        "page_number": page_number,
        "source_file": document.source_file,
        "content_sha256": document.content_sha256,
        "extraction_method": extraction_method,
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
    }


def _fact_id(prefix: str, document_id: str, page_number: int, ordinal: int) -> str:
    value = f"{prefix}|{document_id}|{page_number}|{ordinal}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_guide_rows(
    document: ManifestDocument,
    page_number: int,
    tables: list[list[list[str | None]]],
    common: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    row_ordinal = 0
    for table_number, table in enumerate(tables, start=1):
        if not table:
            continue
        headers = [_normalize_inline(value or "") for value in table[0]]
        current_headers = ["Requested SKU", "Candidate SKU", "Status", "Required attributes", "Clinical note"]
        legacy_headers = ["Requested product", "Old substitute", "Old approval", "Legacy note"]
        if headers not in (current_headers, legacy_headers):
            continue
        for table_row_number, values in enumerate(table[1:], start=1):
            if not values or not any(values):
                continue
            row_ordinal += 1
            cells = [_normalize_text(value or "") for value in values]
            if headers == current_headers:
                requested_sku, candidate_sku, status, attributes, note = cells
            else:
                requested_sku, candidate_sku, status, note = cells
                attributes = ""
            extraction_status = "extracted"
            review_reasons: list[str] = []
            if not requested_sku or not candidate_sku or status not in GUIDE_STATUSES:
                extraction_status = "requires_review"
                review_reasons.append("unexpected_or_incomplete_substitution_row")
            output.append(
                {
                    **common,
                    "substitution_row_id": _fact_id("substitution", document.document_id, page_number, row_ordinal),
                    "table_number": table_number,
                    "table_row_number": table_row_number,
                    "requested_sku": requested_sku,
                    "candidate_sku": candidate_sku,
                    "row_status": status,
                    "required_attributes": attributes or None,
                    "clinical_note": note,
                    "extraction_status": extraction_status,
                    "review_reasons": review_reasons,
                }
            )
    if not output:
        raise ExtractionError(f"No substitution table rows found in {document.document_id} page {page_number}")
    return output


def _extract_scanned_row(text: str, document: ManifestDocument, common: dict[str, Any]) -> dict[str, Any] | None:
    requested = re.search(r"Original(?: SKU)?:\s*(\S+)", text, flags=re.IGNORECASE)
    candidate = re.search(r"Proposed(?: substitute)?:\s*(\S+)", text, flags=re.IGNORECASE)
    status = re.search(r"Approval status:\s*([^\n]+)", text, flags=re.IGNORECASE)
    note = re.search(
        r"Site policy[^\n]*(?:\n[^\n]*)?",
        text,
        flags=re.IGNORECASE,
    )
    if not requested or not candidate or not status:
        return None
    row_status = _normalize_inline(status.group(1)).upper()
    if row_status != SCANNED_STATUS:
        return None
    return {
        **common,
        "substitution_row_id": _fact_id("substitution", document.document_id, 1, 1),
        "table_number": None,
        "table_row_number": 1,
        "requested_sku": requested.group(1).rstrip("."),
        "candidate_sku": candidate.group(1).rstrip("."),
        "row_status": row_status,
        "required_attributes": None,
        "clinical_note": _normalize_text(note.group(0)) if note else None,
        "extraction_status": "extracted",
        "review_reasons": ["clinical_review_required"],
    }


def _labeled_segment(text: str, label: str, following_label: str) -> str:
    pattern = rf"{re.escape(label)}:\s*(.*?)\s*{re.escape(following_label)}:"
    match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        raise ExtractionError(f"Required contract clause is missing: {label}")
    return _normalize_inline(match.group(1))


def _base_contract_clauses(
    text: str, document: ManifestDocument, common: dict[str, Any]
) -> list[dict[str, Any]]:
    brand = _labeled_segment(text, "Brand restrictions", "Additional terms")
    additional = _labeled_segment(text, "Additional terms", "Confidential negotiated pricing")
    values = (("brand_restriction", brand), ("additional_terms", additional))
    return [
        {
            **common,
            "contract_clause_id": _fact_id("contract", document.document_id, 1, ordinal),
            "hospital_id": document.hospital_id,
            "clause_type": clause_type,
            "clause_text": clause_text,
            "is_override": False,
            "overrides_document_id": None,
            "requested_sku": None,
            "candidate_sku": None,
            "conditions": [],
            "extraction_status": "extracted",
            "review_reasons": [],
        }
        for ordinal, (clause_type, clause_text) in enumerate(values, start=1)
    ]


def _amendment_clauses(
    text: str,
    document: ManifestDocument,
    common: dict[str, Any],
    base_document_id: str,
) -> list[dict[str, Any]]:
    supersession = _labeled_segment(text, "Supersession", "Allowed requested SKU")
    pair = re.search(
        r"Allowed requested SKU:\s*([A-Z0-9-]+)\.\s*Permitted alternate SKU:\s*([A-Z0-9-]+)\.",
        text,
    )
    conditions = re.search(
        r"This is a conditional contractual exception,(.*?)Not permitted:",
        text,
        flags=re.DOTALL,
    )
    remaining = _labeled_segment(text, "Not permitted", "Pricing")
    if not pair or not conditions:
        raise ExtractionError(f"Amendment scope is incomplete: {document.document_id}")
    condition_text = _normalize_inline("This is a conditional contractual exception," + conditions.group(1))
    return [
        {
            **common,
            "contract_clause_id": _fact_id("contract", document.document_id, 1, 1),
            "hospital_id": document.hospital_id,
            "clause_type": "brand_lock_override",
            "clause_text": supersession,
            "is_override": True,
            "overrides_document_id": base_document_id,
            "requested_sku": pair.group(1),
            "candidate_sku": pair.group(2),
            "conditions": [condition_text],
            "extraction_status": "extracted",
            "review_reasons": ["clinical_authorization_still_required"],
        },
        {
            **common,
            "contract_clause_id": _fact_id("contract", document.document_id, 1, 2),
            "hospital_id": document.hospital_id,
            "clause_type": "remaining_restrictions",
            "clause_text": remaining,
            "is_override": False,
            "overrides_document_id": None,
            "requested_sku": None,
            "candidate_sku": None,
            "conditions": [],
            "extraction_status": "extracted",
            "review_reasons": [],
        },
    ]


def _yes_no(value: str) -> bool:
    normalized = value.strip().upper()
    if normalized == "YES":
        return True
    if normalized == "NO":
        return False
    raise ExtractionError(f"Unexpected IFU boolean: {value}")


def _ifu_fact(text: str, document: ManifestDocument, common: dict[str, Any]) -> dict[str, Any]:
    identity = re.search(
        r"IFU ID:\s*([^|]+)\|\s*Revision:\s*([^|]+)\|\s*Product code:\s*([^\.\s]+)\.",
        text,
    )
    specification = re.search(
        r"Gauge:\s*(\d+)G;\s*Device length:\s*(\d+) mm;\s*Latex-free:\s*(YES|NO);\s*"
        r"Sterile:\s*(YES|NO);\s*Single use:\s*(YES|NO)\.",
        text,
    )
    clinical_note = re.search(r"Clinical note:\s*(.*?)\s*Document control:", text, flags=re.DOTALL)
    if not identity or not specification or not clinical_note:
        raise ExtractionError(f"Incomplete IFU fields: {document.document_id}")
    return {
        **common,
        "ifu_fact_id": _fact_id("ifu", document.document_id, 1, 1),
        "ifu_id": identity.group(1).strip(),
        "revision": identity.group(2).strip(),
        "product_sku": identity.group(3).strip(),
        "gauge": int(specification.group(1)),
        "device_length_mm": int(specification.group(2)),
        "latex_free": _yes_no(specification.group(3)),
        "sterile": _yes_no(specification.group(4)),
        "single_use": _yes_no(specification.group(5)),
        "clinical_note": _normalize_inline(clinical_note.group(1)),
        "establishes_interchangeability": False,
        "extraction_status": "extracted",
        "review_reasons": ["qualified_clinical_judgment_required"],
    }


def _base_document_for_amendment(
    amendment: ManifestDocument, documents: Iterable[ManifestDocument]
) -> str:
    candidates = [
        item.document_id
        for item in documents
        if item.document_type == "hospital_contract" and item.hospital_id == amendment.hospital_id
    ]
    if len(candidates) != 1:
        raise ExtractionError(
            f"Expected exactly one base contract for amendment {amendment.document_id}; found {candidates}"
        )
    return candidates[0]


def extract_documents(
    manifest_path: Path,
    pdf_directory: Path,
    ocr: OcrFunction | None = None,
) -> ExtractionResult:
    """Extract registered documents; unavailable OCR yields review evidence, never facts."""
    documents = load_and_reconcile_manifest(manifest_path, pdf_directory)
    pages: list[dict[str, Any]] = []
    guide_rows: list[dict[str, Any]] = []
    contract_clauses: list[dict[str, Any]] = []
    ifu_facts: list[dict[str, Any]] = []

    for document in documents:
        with pdfplumber.open(document.source_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                embedded_text = _normalize_text(page.extract_text() or "")
                extraction_method = "embedded_text"
                extraction_status = "extracted"
                review_reasons: list[str] = []
                extracted_text = embedded_text
                tables = page.extract_tables() if embedded_text else []

                if not embedded_text:
                    if document.document_type != "substitution_supplement_scanned":
                        extraction_status = "quarantined"
                        review_reasons.append("unexpected_empty_embedded_text")
                    elif ocr is None:
                        extraction_method = "ocr_unavailable"
                        extraction_status = "requires_review"
                        review_reasons.append("ocr_engine_unavailable")
                    else:
                        extraction_method = "tesseract_ocr"
                        try:
                            extracted_text = _normalize_text(ocr(document.source_path, page_number))
                        except Exception as exc:
                            extraction_status = "quarantined"
                            review_reasons.append(f"ocr_failed:{type(exc).__name__}")
                        if not extracted_text:
                            extraction_status = "quarantined"
                            review_reasons.append("ocr_returned_no_text")

                display_document_id, document_version = _document_identity(extracted_text, document)
                common = _provenance(
                    document,
                    page_number,
                    extraction_method,
                    display_document_id,
                    document_version,
                )
                retrieval_text, pricing_redacted = _redact_restricted_pricing(extracted_text)
                page_record = {
                    **common,
                    "document_page_id": _fact_id("page", document.document_id, page_number, 1),
                    "document_type": document.document_type,
                    "region": document.region,
                    "hospital_id": document.hospital_id,
                    "sensitivity": document.sensitivity,
                    "retrieval_text": retrieval_text,
                    "has_embedded_text": bool(embedded_text),
                    "table_count": len(tables),
                    "restricted_pricing_redacted": pricing_redacted,
                    "extraction_status": extraction_status,
                    "review_reasons": review_reasons,
                }
                pages.append(page_record)

                if extraction_status != "extracted":
                    continue
                try:
                    if document.document_type == "substitution_guide":
                        guide_rows.extend(
                            _extract_guide_rows(document, page_number, tables, common)
                        )
                    elif document.document_type == "substitution_supplement_scanned":
                        scanned_row = _extract_scanned_row(extracted_text, document, common)
                        if scanned_row is None:
                            page_record["extraction_status"] = "requires_review"
                            page_record["review_reasons"].append(
                                "ambiguous_scanned_substitution_fields"
                            )
                        else:
                            guide_rows.append(scanned_row)
                    elif document.document_type == "hospital_contract":
                        contract_clauses.extend(
                            _base_contract_clauses(extracted_text, document, common)
                        )
                    elif document.document_type == "hospital_amendment":
                        base_document_id = _base_document_for_amendment(document, documents)
                        contract_clauses.extend(
                            _amendment_clauses(
                                extracted_text, document, common, base_document_id
                            )
                        )
                    elif document.document_type == "manufacturer_ifu":
                        ifu_facts.append(_ifu_fact(extracted_text, document, common))
                except ExtractionError:
                    page_record["extraction_status"] = "quarantined"
                    page_record["review_reasons"].append(
                        f"deterministic_parse_failed:{document.document_type}"
                    )

    return ExtractionResult(
        document_pages=pages,
        substitution_guide_rows=guide_rows,
        contract_clauses=contract_clauses,
        ifu_product_facts=ifu_facts,
    )


def write_datasets(result: ExtractionResult, output_directory: Path) -> dict[str, Path]:
    """Write the four logical datasets as deterministic, UTF-8 JSON Lines."""
    output_directory.mkdir(parents=True, exist_ok=True)
    datasets = {
        "document_pages": result.document_pages,
        "substitution_guide_rows": result.substitution_guide_rows,
        "contract_clauses": result.contract_clauses,
        "ifu_product_facts": result.ifu_product_facts,
    }
    paths: dict[str, Path] = {}
    for name, records in datasets.items():
        path = output_directory / f"{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as target:
            for record in records:
                target.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        paths[name] = path
    return paths
