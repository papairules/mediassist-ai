from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import crestline_documents.extract as extraction_module
from crestline_documents.extract import (
    ExtractionError,
    ExtractionResult,
    extract_documents,
    load_and_reconcile_manifest,
    write_datasets,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "crestline_synthetic_v1/source_csv/document_manifest.csv"
PDF_DIRECTORY = ROOT / "crestline_synthetic_v1/source_pdfs"
SCANNED_TEXT = """SCANNED - Syringe Substitution Supplement
Document: SUB-SCAN-2026-01 | Effective: 2026-10-10

Original SKU: AV-SYR10-LF
Proposed substitute: BR-SYR10-LF
Approval status: PENDING CLINICAL REVIEW
Site policy and full manufacturer IFU review required.
Never auto-approve based on a scanned row or old ticket.
SYNTHETIC TRAINING DATA - NOT FOR CLINICAL USE"""


def _fake_ocr(path: Path, page_number: int) -> str:
    assert path.name == "SUB_SUPPLEMENT_SCANNED_2026.pdf"
    assert page_number == 1
    return SCANNED_TEXT


@pytest.fixture(scope="module")
def extraction() -> ExtractionResult:
    return extract_documents(MANIFEST, PDF_DIRECTORY, ocr=_fake_ocr)


def test_manifest_reconciles_all_thirteen_pdfs_and_sha256() -> None:
    documents = load_and_reconcile_manifest(MANIFEST, PDF_DIRECTORY)

    assert len(documents) == 13
    assert {item.source_file for item in documents} == {
        path.name for path in PDF_DIRECTORY.glob("*.pdf")
    }
    assert all(len(item.content_sha256) == 64 for item in documents)


def test_extracts_expected_dataset_counts(extraction: ExtractionResult) -> None:
    assert len(extraction.document_pages) == 13
    assert len(extraction.substitution_guide_rows) == 11
    assert len(extraction.contract_clauses) == 12
    assert len(extraction.ifu_product_facts) == 4
    assert extraction.review_records() == []


def test_every_fact_has_citation_and_extraction_provenance(extraction: ExtractionResult) -> None:
    required = {
        "document_id",
        "document_version",
        "document_status",
        "effective_from",
        "page_number",
        "source_file",
        "content_sha256",
        "extraction_method",
        "extractor_name",
        "extractor_version",
        "extraction_status",
    }
    for dataset in (
        extraction.document_pages,
        extraction.substitution_guide_rows,
        extraction.contract_clauses,
        extraction.ifu_product_facts,
    ):
        assert dataset
        for record in dataset:
            assert required <= record.keys()
            assert record["page_number"] == 1
            assert len(record["content_sha256"]) == 64


def test_guide_versions_statuses_and_multiline_notes_are_preserved(
    extraction: ExtractionResult,
) -> None:
    rows = extraction.substitution_guide_rows
    legacy = [row for row in rows if row["document_status"] == "superseded"]
    assert len(legacy) == 3
    revoked = next(row for row in legacy if row["candidate_sku"] == "BR-IV22-BLU-LX")
    assert revoked["row_status"] == "APPROVED (REVOKED)"
    assert revoked["clinical_note"] == "Replaced in 2026 due to latex attribute\nmismatch."

    current_latex = next(
        row
        for row in rows
        if row["document_id"] == "SUB_GUIDE_2026_V2_CURRENT"
        and row["candidate_sku"] == "BR-IV22-BLU-LX"
    )
    assert current_latex["row_status"] == "REJECTED"
    assert current_latex["clinical_note"] == "Candidate contains latex. Old V1 approval\nrevoked."


def test_h_e101_brand_lock_remains_contract_evidence(extraction: ExtractionResult) -> None:
    clause = next(
        row
        for row in extraction.contract_clauses
        if row["document_id"] == "CONTRACT_H-E101_BASE"
        and row["clause_type"] == "brand_restriction"
    )
    assert clause["hospital_id"] == "H-E101"
    assert "Aurevia brand-lock on all IV catheters" in clause["clause_text"]
    assert clause["is_override"] is False


def test_h_e102_amendment_is_a_narrow_override_not_a_replacement(
    extraction: ExtractionResult,
) -> None:
    amendment = next(
        row
        for row in extraction.contract_clauses
        if row["document_id"] == "AMENDMENT_H-E102_2026_01"
        and row["clause_type"] == "brand_lock_override"
    )
    assert amendment["requested_sku"] == "AV-IV22-BLU-LF"
    assert amendment["candidate_sku"] == "BR-IV22-BLU-LF"
    assert amendment["is_override"] is True
    assert amendment["overrides_document_id"] == "CONTRACT_H-E102_BASE"
    assert "documented backorder remains open" in amendment["clause_text"]
    assert amendment["review_reasons"] == ["clinical_authorization_still_required"]


def test_scanned_supplement_stays_pending_clinical_review(
    extraction: ExtractionResult,
) -> None:
    row = next(
        item
        for item in extraction.substitution_guide_rows
        if item["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
    )
    assert row["requested_sku"] == "AV-SYR10-LF"
    assert row["candidate_sku"] == "BR-SYR10-LF"
    assert row["row_status"] == "PENDING CLINICAL REVIEW"
    assert row["review_reasons"] == ["clinical_review_required"]
    assert row["extraction_method"] == "tesseract_ocr"


def test_absent_ocr_quarantines_evidence_instead_of_approving_it() -> None:
    result = extract_documents(MANIFEST, PDF_DIRECTORY, ocr=None)
    scanned_page = next(
        item
        for item in result.document_pages
        if item["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
    )

    assert scanned_page["extraction_status"] == "requires_review"
    assert scanned_page["review_reasons"] == ["ocr_engine_unavailable"]
    assert not any(
        item["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
        for item in result.substitution_guide_rows
    )


def test_ocr_failure_is_quarantined_without_leaking_exception_text() -> None:
    def failing_ocr(path: Path, page_number: int) -> str:
        raise RuntimeError("secret local detail")

    result = extract_documents(MANIFEST, PDF_DIRECTORY, ocr=failing_ocr)
    scanned_page = next(
        item
        for item in result.document_pages
        if item["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
    )
    assert scanned_page["extraction_status"] == "quarantined"
    assert scanned_page["review_reasons"] == ["ocr_failed:RuntimeError", "ocr_returned_no_text"]


def test_ambiguous_contract_clause_is_quarantined_not_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = extraction_module._base_contract_clauses

    def fail_one_contract(
        text: str,
        document: extraction_module.ManifestDocument,
        common: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if document.document_id == "CONTRACT_H-E101_BASE":
            raise ExtractionError("ambiguous content that must not leak")
        return original(text, document, common)

    monkeypatch.setattr(extraction_module, "_base_contract_clauses", fail_one_contract)
    result = extract_documents(MANIFEST, PDF_DIRECTORY, ocr=_fake_ocr)
    page = next(
        row for row in result.document_pages if row["document_id"] == "CONTRACT_H-E101_BASE"
    )

    assert page["extraction_status"] == "quarantined"
    assert page["review_reasons"] == ["deterministic_parse_failed:hospital_contract"]
    assert not any(
        row["document_id"] == "CONTRACT_H-E101_BASE" for row in result.contract_clauses
    )


def test_confidential_prices_are_not_written_to_retrieval_datasets(
    extraction: ExtractionResult,
) -> None:
    serialized = json.dumps(
        {
            "pages": extraction.document_pages,
            "clauses": extraction.contract_clauses,
            "ifu": extraction.ifu_product_facts,
            "guides": extraction.substitution_guide_rows,
        }
    )
    assert "$12.10" not in serialized
    assert "$17.20" not in serialized
    assert "$18.75" not in serialized
    assert "$18.05" not in serialized
    assert "$19.30" not in serialized
    assert "[REDACTED RESTRICTED PRICING]" in serialized


def test_ifu_facts_do_not_claim_interchangeability(extraction: ExtractionResult) -> None:
    facts = {row["product_sku"]: row for row in extraction.ifu_product_facts}
    assert facts["BR-IV22-BLU-LF"]["latex_free"] is True
    assert facts["BR-IV22-BLU-LX"]["latex_free"] is False
    assert all(row["establishes_interchangeability"] is False for row in facts.values())


def test_jsonl_output_is_repeatable(extraction: ExtractionResult, tmp_path: Path) -> None:
    first = write_datasets(extraction, tmp_path / "first")
    second = write_datasets(extraction, tmp_path / "second")

    assert set(first) == {
        "document_pages",
        "substitution_guide_rows",
        "contract_clauses",
        "ifu_product_facts",
    }
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes()


def test_manifest_rejects_unregistered_pdf_directory(tmp_path: Path) -> None:
    empty_directory = tmp_path / "source_pdfs"
    empty_directory.mkdir()
    with pytest.raises(ExtractionError, match="outside the configured PDF directory"):
        load_and_reconcile_manifest(MANIFEST, empty_directory)


def test_records_are_plain_json_compatible(extraction: ExtractionResult) -> None:
    records: list[dict[str, Any]] = extraction.document_pages + extraction.contract_clauses
    json.dumps(records)
