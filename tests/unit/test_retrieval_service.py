from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import pytest

from crestline_retrieval.client import RetrievalBackendError
from crestline_retrieval.models import (
    AuthorizationContext,
    EvidenceStatus,
    Outcome,
    SqlParameter,
)
from crestline_retrieval.service import RetrievalService, VECTOR_INDEX


AS_OF = date(2026, 10, 22)


@dataclass(frozen=True)
class QueryCall:
    statement: str
    parameters: tuple[SqlParameter, ...]


class FakeBackend:
    def __init__(
        self,
        responses: Mapping[str, list[Mapping[str, Any]]] | None = None,
        vector_rows: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.vector_rows = vector_rows or []
        self.query_calls: list[QueryCall] = []
        self.vector_calls: list[dict[str, Any]] = []

    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]:
        self.query_calls.append(QueryCall(statement, tuple(parameters)))
        for marker, rows in self.responses.items():
            if marker in statement:
                return rows
        return []

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
        self.vector_calls.append(
            {
                "index_name": index_name,
                "query_text": query_text,
                "columns": tuple(columns),
                "num_results": num_results,
                "query_type": query_type,
                "filters": filters,
            }
        )
        return self.vector_rows


@pytest.fixture
def east_auth() -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="trusted-rep-east",
        hospital_regions={"H-E101": "East", "H-E102": "East"},
        allowed_regions=frozenset({"East"}),
    )


def _doc(
    *,
    document_id: str,
    document_version: str,
    record_id_name: str,
    record_id: str,
    status: str = "current",
) -> dict[str, Any]:
    return {
        record_id_name: record_id,
        "document_id": document_id,
        "display_document_id": document_id,
        "document_version": document_version,
        "document_status": status,
        "effective_from": "2026-09-01",
        "page_number": 1,
        "source_file": f"{document_id}.pdf",
        "content_sha256": "a" * 64,
        "extraction_method": "embedded_text",
        "extractor_name": "crestline_deterministic_pdf",
        "extractor_version": "1.0.0",
        "extraction_status": "extracted",
        "review_reasons": [],
    }


def _guide(status: str, candidate: str = "BR-IV22-BLU-LF") -> dict[str, Any]:
    return {
        **_doc(
            document_id="SUB_GUIDE_2026_V2_CURRENT",
            document_version="V2",
            record_id_name="substitution_row_id",
            record_id=f"guide-{candidate}-{status}",
        ),
        "requested_sku": "AV-IV22-BLU-LF",
        "candidate_sku": candidate,
        "row_status": status,
        "required_attributes": "22G, blue, 25 mm, latex-free",
        "clinical_note": "Human clinical review and contract checks required.",
        "table_number": 1,
        "table_row_number": 1,
    }


def _ifu(sku: str, *, latex_free: bool = True) -> dict[str, Any]:
    return {
        **_doc(
            document_id=f"IFU_{sku}",
            document_version="2026-08",
            record_id_name="ifu_fact_id",
            record_id=f"ifu-fact-{sku}",
        ),
        "ifu_id": f"IFU-{sku}-2026",
        "revision": "2026-08",
        "product_sku": sku,
        "gauge": 22,
        "device_length_mm": 25,
        "latex_free": latex_free,
        "sterile": True,
        "single_use": True,
        "clinical_note": "This IFU does not establish interchangeability.",
        "establishes_interchangeability": False,
    }


def _contract(
    hospital_id: str,
    *,
    override: bool = False,
    candidate: str | None = None,
) -> dict[str, Any]:
    document_id = "AMENDMENT_H-E102_2026_01" if override else f"CONTRACT_{hospital_id}_BASE"
    return {
        **_doc(
            document_id=document_id,
            document_version="AMD-E102-2026-01" if override else f"CTR-{hospital_id[2:]}",
            record_id_name="contract_clause_id",
            record_id=f"contract-{hospital_id}-{'override' if override else 'lock'}",
        ),
        "hospital_id": hospital_id,
        "clause_type": "brand_lock_override" if override else "brand_restriction",
        "clause_text": (
            "Overrides the IV-catheter brand-lock only for this pair while a documented "
            "backorder remains open."
            if override
            else "Aurevia brand-lock on all IV catheters."
        ),
        "is_override": override,
        "overrides_document_id": f"CONTRACT_{hospital_id}_BASE" if override else None,
        "requested_sku": "AV-IV22-BLU-LF" if override else None,
        "candidate_sku": candidate if override else None,
        "conditions": ["Human clinical authorization still required."] if override else [],
    }


def _inventory(*, observed: bool = True, available: int | None = 45, candidate: str = "BR-IV22-BLU-LF") -> dict[str, Any]:
    return {
        "region": "East",
        "sku": candidate,
        "brand": "Borealis",
        "category": "IV catheter",
        "gauge": 22,
        "color": "blue",
        "length_mm": 25,
        "latex_free": candidate != "BR-IV22-BLU-LX",
        "sterile": True,
        "manufacturer_status": "active",
        "has_inventory_observation": observed,
        "warehouse_count": 1 if observed else None,
        "quantity_on_hand": available,
        "quantity_reserved": 0 if observed else None,
        "quantity_available": available,
        "latest_current_snapshot_at": "2026-10-22T22:30:00Z",
        "warehouse_inventory_evidence": [{"record_id": "inventory-record"}] if observed else None,
        "product_record_id": "product-record",
    }


def _backorder() -> dict[str, Any]:
    return {
        "hospital_id": "H-E102",
        "region": "East",
        "order_id": "ORD-1002",
        "line_id": "1",
        "sku": "AV-IV22-BLU-LF",
        "quantity_ordered": 30,
        "quantity_shipped": 0,
        "quantity_unshipped": 30,
        "has_unshipped_quantity": True,
        "current_supplier_eta_record_id": "ETA-002-NEW",
        "current_supplier_restock_date": "2026-10-29",
        "supplier_eta_revision_history": [{"source_record_id": "ETA-002-OLD"}],
    }


def _verification_backend(
    *,
    guide: Mapping[str, Any],
    ifus: Sequence[Mapping[str, Any]],
    contracts: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    backorder: Mapping[str, Any] | None = None,
) -> FakeBackend:
    return FakeBackend(
        {
            ".substitution_guide_rows": [guide],
            ".ifu_product_facts": list(ifus),
            ".contract_clauses": list(contracts),
            ".product_region_inventory": [inventory],
            ".backorder_eta_evidence": [backorder] if backorder else [],
        }
    )


def test_order_visibility_is_parameterized_and_authorized(
    east_auth: AuthorizationContext,
) -> None:
    backend = FakeBackend(
        {".order_line_fulfillment": [{"hospital_id": "H-E101", "order_id": "ORD-1001", "line_id": "1"}]}
    )
    service = RetrievalService(backend)
    hostile_value = "ORD-1001' OR 1=1 --"
    result = service.get_order_visibility(hostile_value, east_auth)
    assert result.status == EvidenceStatus.FOUND
    call = backend.query_calls[0]
    assert hostile_value not in call.statement
    assert {parameter.name: parameter.value for parameter in call.parameters}["order_id"] == hostile_value
    assert "hospital_id IN (:authorized_hospital_0, :authorized_hospital_1)" in call.statement


def test_unauthorized_hospital_access_fails_before_any_query() -> None:
    backend = FakeBackend()
    service = RetrievalService(backend)
    auth = AuthorizationContext("rep-west", {"H-W201": "West"}, frozenset({"West"}))
    contract = service.get_contract_rules("H-E101", auth, as_of=AS_OF)
    verification = service.verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E101",
        region="East",
        auth=auth,
        as_of=AS_OF,
    )
    assert contract.outcome == Outcome.ACCESS_DENIED
    assert verification.outcome == Outcome.ACCESS_DENIED
    assert backend.query_calls == []


def test_backorder_preserves_unknown_supplier_date_and_history(
) -> None:
    west_auth = AuthorizationContext(
        "trusted-rep-west",
        {"H-W201": "West"},
        frozenset({"West"}),
    )
    row = {
        **_backorder(),
        "hospital_id": "H-W201",
        "region": "West",
        "order_id": "ORD-1003",
        "current_supplier_eta_record_id": "ETA-003-MISSING",
        "current_supplier_restock_date": None,
        "supplier_eta_revision_history": [
            {
                "source_record_id": "ETA-003-MISSING",
                "expected_supplier_restock_date": None,
            }
        ],
    }
    result = RetrievalService(FakeBackend({".backorder_eta_evidence": [row]})).get_backorder_evidence(
        "ORD-1003", west_auth
    )
    assert result.records[0]["current_supplier_restock_date"] is None
    assert result.records[0]["supplier_eta_revision_history"][0]["expected_supplier_restock_date"] is None


@pytest.mark.parametrize(
    ("inventory", "observed", "quantity"),
    [(_inventory(observed=True, available=0), True, 0), (_inventory(observed=False, available=None), False, None)],
)
def test_inventory_distinguishes_observed_zero_from_missing(
    east_auth: AuthorizationContext,
    inventory: Mapping[str, Any],
    observed: bool,
    quantity: int | None,
) -> None:
    result = RetrievalService(FakeBackend({".product_region_inventory": [inventory]})).get_product_inventory(
        "East", "BR-IV22-BLU-LF", east_auth
    )
    assert result.records[0]["has_inventory_observation"] is observed
    assert result.records[0]["quantity_available"] == quantity


def test_vector_search_is_hybrid_discovery_with_complete_provenance(
    east_auth: AuthorizationContext,
) -> None:
    row = {
        "chunk_id": "substitution:guide-row",
        "chunk_type": "substitution_guide_row",
        "embedding_text": "Requested SKU: AV-IV22-BLU-LF\nCandidate SKU: BR-IV22-BLU-LF",
        "source_record_id": "guide-row",
        "document_id": "SUB_GUIDE_2026_V2_CURRENT",
        "document_version": "V2",
        "document_status": "current",
        "effective_from": "2026-09-01",
        "page_number": 1,
        "source_file": "SUB_GUIDE_2026_V2_CURRENT.pdf",
        "content_sha256": "a" * 64,
        "extraction_method": "embedded_text",
        "extractor_name": "crestline_deterministic_pdf",
        "extractor_version": "1.0.0",
        "row_status": "APPROVED",
        "requested_sku": "AV-IV22-BLU-LF",
        "candidate_sku": "BR-IV22-BLU-LF",
        "product_sku": None,
        "score": 0.82,
    }
    backend = FakeBackend(vector_rows=[row])
    result = RetrievalService(backend).search_document_evidence("latex-free alternative", east_auth)
    assert result.semantic_discovery_only is True
    assert result.matches[0].discovery_score == 0.82
    assert result.matches[0].source_record_id == "guide-row"
    assert backend.vector_calls == [
        {
            "index_name": VECTOR_INDEX,
            "query_text": "latex-free alternative",
            "columns": backend.vector_calls[0]["columns"],
            "num_results": 5,
            "query_type": "HYBRID",
            "filters": {"chunk_type": ["ifu_product_fact", "substitution_guide_row"]},
        }
    ]


def test_vector_search_rejects_contract_or_pricing_content(
    east_auth: AuthorizationContext,
) -> None:
    unsafe = {
        "chunk_type": "substitution_guide_row",
        "document_id": "CONTRACT_H-E101_BASE",
        "embedding_text": "Confidential contract pricing $18.75",
    }
    service = RetrievalService(FakeBackend(vector_rows=[unsafe]))
    with pytest.raises(RetrievalBackendError, match="prohibited"):
        service.search_document_evidence("contract", east_auth)


def test_h_e102_narrow_amendment_is_eligible_only_for_human_review(
    east_auth: AuthorizationContext,
) -> None:
    backend = _verification_backend(
        guide=_guide("APPROVED"),
        ifus=[_ifu("AV-IV22-BLU-LF"), _ifu("BR-IV22-BLU-LF")],
        contracts=[_contract("H-E102"), _contract("H-E102", override=True, candidate="BR-IV22-BLU-LF")],
        inventory=_inventory(),
        backorder=_backorder(),
    )
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E102",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
        order_id="ORD-1002",
        line_id="1",
    )
    assert result.outcome == Outcome.ELIGIBLE_FOR_HUMAN_REVIEW
    assert result.requires_human_clinical_review is True
    assert result.semantic_similarity_used_as_approval is False
    assert result.guide_evidence[0]["substitution_row_id"]
    assert result.contract_evidence[1]["contract_clause_id"]
    assert result.ifu_evidence[0]["content_sha256"] == "a" * 64
    assert all(call.statement.lstrip().startswith("SELECT") for call in backend.query_calls)
    assert backend.vector_calls == []


def test_current_guide_wins_over_superseded_revoked_history(
    east_auth: AuthorizationContext,
) -> None:
    current = _guide("APPROVED")
    superseded = _guide("APPROVED (REVOKED)")
    superseded.update(
        {
            "document_id": "SUB_GUIDE_2025_V1_SUPERSEDED",
            "document_version": "V1",
            "document_status": "superseded",
            "effective_from": "2025-01-01",
        }
    )
    backend = _verification_backend(
        guide=current,
        ifus=[_ifu("AV-IV22-BLU-LF"), _ifu("BR-IV22-BLU-LF")],
        contracts=[_contract("H-E102"), _contract("H-E102", override=True, candidate="BR-IV22-BLU-LF")],
        inventory=_inventory(),
        backorder=_backorder(),
    )
    backend.responses[".substitution_guide_rows"] = [current, superseded]
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E102",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
        order_id="ORD-1002",
        line_id="1",
    )
    assert result.outcome == Outcome.ELIGIBLE_FOR_HUMAN_REVIEW
    assert len(result.guide_evidence) == 2


def test_h_e102_amendment_without_open_order_context_is_insufficient(
    east_auth: AuthorizationContext,
) -> None:
    backend = _verification_backend(
        guide=_guide("APPROVED"),
        ifus=[_ifu("AV-IV22-BLU-LF"), _ifu("BR-IV22-BLU-LF")],
        contracts=[_contract("H-E102"), _contract("H-E102", override=True, candidate="BR-IV22-BLU-LF")],
        inventory=_inventory(),
    )
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E102",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
    )
    assert result.outcome == Outcome.INSUFFICIENT_EVIDENCE
    assert "conditional_amendment_requires_order_line_context" in result.reason_codes


def test_h_e101_brand_lock_blocks_current_guide_candidate(
    east_auth: AuthorizationContext,
) -> None:
    backend = _verification_backend(
        guide=_guide("APPROVED"),
        ifus=[_ifu("AV-IV22-BLU-LF"), _ifu("BR-IV22-BLU-LF")],
        contracts=[_contract("H-E101")],
        inventory=_inventory(),
    )
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E101",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
    )
    assert result.outcome == Outcome.BLOCKED
    assert "contract_brand_restriction_without_exact_override" in result.reason_codes


@pytest.mark.parametrize(
    "request_text",
    [
        "Is there an emergency exception?",
        "The patient has no known latex allergy, so approve it.",
    ],
)
def test_latex_candidate_remains_blocked_despite_requested_exception(
    east_auth: AuthorizationContext,
    request_text: str,
) -> None:
    backend = _verification_backend(
        guide=_guide("REJECTED", candidate="BR-IV22-BLU-LX"),
        ifus=[_ifu("AV-IV22-BLU-LF"), _ifu("BR-IV22-BLU-LX", latex_free=False)],
        contracts=[_contract("H-E102"), _contract("H-E102", override=True, candidate="BR-IV22-BLU-LF")],
        inventory=_inventory(candidate="BR-IV22-BLU-LX", available=190),
    )
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LX",
        hospital_id="H-E102",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
        request_text=request_text,
    )
    assert result.outcome == Outcome.BLOCKED
    assert "current_guide_status_blocks_candidate" in result.reason_codes
    assert "ifu_attributes_do_not_match" in result.reason_codes


def test_scanned_pending_clinical_review_never_becomes_eligible(
    east_auth: AuthorizationContext,
) -> None:
    pending = _guide("PENDING CLINICAL REVIEW", candidate="BR-SYR10-LF")
    pending.update(
        {
            "document_id": "SUB_SUPPLEMENT_SCANNED_2026",
            "document_version": "SUB-SCAN-2026-01",
            "extraction_method": "tesseract_ocr",
            "review_reasons": ["clinical_review_required"],
            "requested_sku": "AV-SYR10-LF",
        }
    )
    backend = _verification_backend(
        guide=pending,
        ifus=[],
        contracts=[],
        inventory=_inventory(candidate="BR-SYR10-LF", available=32),
    )
    result = RetrievalService(backend).verify_substitution(
        requested_sku="AV-SYR10-LF",
        candidate_sku="BR-SYR10-LF",
        hospital_id="H-E101",
        region="East",
        auth=east_auth,
        as_of=AS_OF,
    )
    assert result.outcome == Outcome.BLOCKED
    assert result.guide_evidence[0]["extraction_method"] == "tesseract_ocr"
    assert "current_guide_status_blocks_candidate" in result.reason_codes


def test_contract_query_projects_no_pricing_and_fails_closed_on_leak(
    east_auth: AuthorizationContext,
) -> None:
    safe_backend = FakeBackend({".contract_clauses": [_contract("H-E101")]})
    result = RetrievalService(safe_backend).get_contract_rules("H-E101", east_auth, as_of=AS_OF)
    assert result.status == EvidenceStatus.FOUND
    statement = safe_backend.query_calls[0].statement.lower()
    assert "price" not in statement and "document_pages" not in statement

    unsafe = _contract("H-E101")
    unsafe["clause_text"] = "Confidential negotiated price: $18.75"
    with pytest.raises(RetrievalBackendError, match="pricing"):
        RetrievalService(FakeBackend({".contract_clauses": [unsafe]})).get_contract_rules(
            "H-E101", east_auth, as_of=AS_OF
        )


def test_bounded_order_analytics_use_authorized_fixed_queries(
    east_auth: AuthorizationContext,
) -> None:
    backend = FakeBackend(
        {
            ".order_line_fulfillment": [
                {
                    "order_id": "ORD-1001",
                    "quantity_unshipped": 40,
                    "order_record_id": "order-record",
                }
            ],
            ".backorder_eta_evidence": [
                {
                    "order_id": "ORD-1008",
                    "quantity_unshipped": 10,
                    "current_supplier_restock_date": None,
                }
            ],
        }
    )
    service = RetrievalService(backend)

    lines = service.list_order_lines(east_auth, view="outstanding", limit=10)
    summary = service.summarize_orders(east_auth, limit=10)
    missing = service.list_missing_eta_orders(east_auth, limit=10)

    assert lines.status == EvidenceStatus.FOUND
    assert summary.status == EvidenceStatus.FOUND
    assert missing.status == EvidenceStatus.FOUND
    for call in backend.query_calls:
        assert "authorized_hospital_0" in call.statement
        assert "LIMIT 10" in call.statement
        assert all(parameter.name.startswith("authorized_hospital_") for parameter in call.parameters)
    assert "quantity_unshipped > 0" in backend.query_calls[0].statement
    assert "current_supplier_restock_date IS NULL" in backend.query_calls[2].statement


def test_region_inventory_analytics_preserve_scope_and_bound_inputs(
    east_auth: AuthorizationContext,
) -> None:
    backend = FakeBackend({".product_region_inventory": [{"sku": "BR-IV22-BLU-LF"}]})
    service = RetrievalService(backend)

    result = service.list_region_inventory(
        east_auth,
        region="East",
        skus=("BR-IV22-BLU-LF", "NV-IV22-BLU-LF"),
        limit=5,
    )

    assert result.status == EvidenceStatus.FOUND
    statement = backend.query_calls[0].statement
    assert "region IN" in statement
    assert "requested_region" in statement
    assert "requested_sku_0" in statement
    assert "LIMIT 5" in statement
    with pytest.raises(ValueError):
        service.list_region_inventory(east_auth, skus=tuple("SKU" for _ in range(11)))
    assert service.list_region_inventory(AuthorizationContext("west", {"H-W201": "West"}, frozenset({"West"})), region="East").status == EvidenceStatus.ACCESS_DENIED
