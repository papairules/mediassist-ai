from __future__ import annotations

from io import StringIO
from typing import Any, Mapping, Sequence, cast

from crestline_retrieval.live_integration import (
    DEFAULT_PROFILE,
    DEFAULT_VECTOR_INDEX,
    DEFAULT_WAREHOUSE_ID,
    DEMO_ONLY_AUTHORIZATION,
    EXPECTED_HASHES,
    EXPECTED_RECORD_IDS,
    CachingBackend,
    run_checks,
)
from crestline_retrieval.models import (
    DocumentMatch,
    DocumentSearchResult,
    EvidenceResult,
    EvidenceStatus,
    Outcome,
    SqlParameter,
    SubstitutionVerification,
)
from crestline_retrieval.service import RetrievalService


def _document_row(
    document_id: str,
    record_field: str,
    record_id: str,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "document_version": "V2" if document_id.startswith("SUB_") else document_id,
        "document_status": "current",
        "effective_from": "2026-09-01",
        "page_number": 1,
        "source_file": f"{document_id}.pdf",
        "content_sha256": EXPECTED_HASHES[document_id],
        record_field: record_id,
    }


class PassingService:
    def get_order_visibility(self, order_id: str, auth: Any) -> EvidenceResult:
        assert order_id == "ORD-1001"
        assert auth is DEMO_ONLY_AUTHORIZATION
        return EvidenceResult(
            status=EvidenceStatus.FOUND,
            records=(
                {
                    "hospital_id": "H-E101",
                    "quantity_ordered": 100,
                    "quantity_shipped": 60,
                    "quantity_unshipped": 40,
                    "shipment_evidence": [
                        {"shipment_id": "SHIP-001", "record_id": "shipment-record"}
                    ],
                },
            ),
        )

    def get_backorder_evidence(
        self,
        order_id: str,
        auth: Any,
        *,
        line_id: str | None = None,
    ) -> EvidenceResult:
        assert auth is DEMO_ONLY_AUTHORIZATION
        if order_id == "ORD-1003":
            assert line_id == "1"
            return EvidenceResult(
                status=EvidenceStatus.FOUND,
                records=(
                    {
                        "hospital_id": "H-W201",
                        "current_supplier_eta_record_id": "ETA-003-MISSING",
                        "current_supplier_restock_date": None,
                        "supplier_eta_revision_history": [
                            {"expected_supplier_restock_date": None}
                        ],
                    },
                ),
            )
        raise AssertionError(f"Unexpected order lookup: {order_id}")

    def search_document_evidence(
        self,
        query_text: str,
        auth: Any,
        *,
        num_results: int = 5,
    ) -> DocumentSearchResult:
        assert "AV-IV22-BLU-LF" in query_text
        assert auth is DEMO_ONLY_AUTHORIZATION
        assert num_results == 15
        return DocumentSearchResult(
            status=EvidenceStatus.FOUND,
            matches=(
                self._match("SUB_GUIDE_2026_V2_CURRENT", "substitution_guide_row"),
                self._match("IFU_AV-IV22-BLU-LF", "ifu_product_fact"),
            ),
        )

    @staticmethod
    def _match(document_id: str, chunk_type: str) -> DocumentMatch:
        return DocumentMatch(
            chunk_id=f"chunk:{document_id}",
            chunk_type=chunk_type,
            embedding_text="citation-ready evidence",
            discovery_score=0.8,
            document_id=document_id,
            document_version="V2" if chunk_type == "substitution_guide_row" else "2026-08",
            document_status="current",
            effective_from="2026-09-01",
            page_number=1,
            source_file=f"{document_id}.pdf",
            content_sha256=EXPECTED_HASHES[document_id],
            source_record_id=f"record:{document_id}",
            extraction_method="embedded_text",
            extractor_name="crestline_deterministic_pdf",
            extractor_version="1.0.0",
        )

    def verify_substitution(self, **kwargs: Any) -> SubstitutionVerification:
        candidate = kwargs["candidate_sku"]
        hospital = kwargs["hospital_id"]
        common = {
            "requested_sku": kwargs["requested_sku"],
            "candidate_sku": candidate,
            "hospital_id": hospital,
            "region": kwargs["region"],
            "as_of_date": kwargs["as_of"].isoformat(),
        }
        if candidate == "BR-IV22-BLU-LX":
            row = _document_row(
                "SUB_GUIDE_2026_V2_CURRENT",
                "substitution_row_id",
                EXPECTED_RECORD_IDS["rejected_latex_guide"],
            )
            row["row_status"] = "REJECTED"
            return SubstitutionVerification(
                outcome=Outcome.BLOCKED,
                reason_codes=("current_guide_status_blocks_candidate",),
                guide_evidence=(row,),
                **common,
            )
        if hospital == "H-E101":
            row = _document_row(
                "CONTRACT_H-E101_BASE",
                "contract_clause_id",
                EXPECTED_RECORD_IDS["h_e101_brand_lock"],
            )
            return SubstitutionVerification(
                outcome=Outcome.BLOCKED,
                reason_codes=("contract_brand_restriction_without_exact_override",),
                contract_evidence=(row,),
                **common,
            )
        row = _document_row(
            "AMENDMENT_H-E102_2026_01",
            "contract_clause_id",
            EXPECTED_RECORD_IDS["h_e102_amendment"],
        )
        return SubstitutionVerification(
            outcome=Outcome.ELIGIBLE_FOR_HUMAN_REVIEW,
            reason_codes=("all_deterministic_preconditions_met_human_clinical_review_required",),
            contract_evidence=(row,),
            inventory_evidence=({"quantity_available": 45},),
            backorder_evidence=(
                {"order_id": "ORD-1002", "quantity_unshipped": 30},
            ),
            **common,
        )


class FailingOrderService(PassingService):
    def get_order_visibility(self, order_id: str, auth: Any) -> EvidenceResult:
        result = super().get_order_visibility(order_id, auth)
        row = dict(result.records[0])
        row["quantity_shipped"] = 100
        return EvidenceResult(status=EvidenceStatus.FOUND, records=(row,))


def test_live_check_report_passes_all_synthetic_assertions() -> None:
    output = StringIO()
    code = run_checks(cast(RetrievalService, PassingService()), output)
    report = output.getvalue()
    assert code == 0
    assert report.count("PASS ") == 6
    assert "SUMMARY passed=6 failed=0 total=6" in report
    assert "DEMO_ONLY" not in report


def test_live_check_reports_failure_and_returns_nonzero() -> None:
    output = StringIO()
    code = run_checks(cast(RetrievalService, FailingOrderService()), output)
    report = output.getvalue()
    assert code == 1
    assert "FAIL authorized_order_visibility" in report
    assert "SUMMARY passed=5 failed=1 total=6" in report


class CountingBackend:
    def __init__(self) -> None:
        self.sql_calls = 0
        self.vector_calls = 0

    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]:
        self.sql_calls += 1
        return [{"statement": statement, "parameter_count": len(parameters)}]

    def vector_search(
        self,
        *,
        index_name: str,
        query_text: str,
        columns: Sequence[str],
        num_results: int,
        query_type: str,
        filters: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        self.vector_calls += 1
        return [{"index": index_name, "query": query_text}]


def test_live_backend_cache_avoids_identical_repeated_queries() -> None:
    underlying = CountingBackend()
    cached = CachingBackend(underlying)
    parameters = [SqlParameter("id", "ORD-1001")]
    assert cached.query("SELECT 1", parameters) == cached.query("SELECT 1", parameters)
    first = cached.vector_search(
        index_name=DEFAULT_VECTOR_INDEX,
        query_text="AV-IV22-BLU-LF",
        columns=["chunk_id"],
        num_results=5,
        query_type="HYBRID",
        filters={"chunk_type": ["substitution_guide_row"]},
    )
    second = cached.vector_search(
        index_name=DEFAULT_VECTOR_INDEX,
        query_text="AV-IV22-BLU-LF",
        columns=["chunk_id"],
        num_results=5,
        query_type="HYBRID",
        filters={"chunk_type": ["substitution_guide_row"]},
    )
    assert first == second
    assert underlying.sql_calls == 1
    assert underlying.vector_calls == 1


def test_live_defaults_use_declared_existing_resources() -> None:
    assert DEFAULT_PROFILE == "abinroy90@gmail.com"
    assert DEFAULT_WAREHOUSE_ID == "d249a9e5325ec021"
    assert DEFAULT_VECTOR_INDEX == "workspace.crestline_silver.document_retrieval_chunks_index"
    assert DEMO_ONLY_AUTHORIZATION.principal_id.startswith("DEMO_ONLY")
    assert "H-E101" in DEMO_ONLY_AUTHORIZATION.hospital_regions
    assert "H-E102" in DEMO_ONLY_AUTHORIZATION.hospital_regions
    assert "H-W201" in DEMO_ONLY_AUTHORIZATION.hospital_regions
