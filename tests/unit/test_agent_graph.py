from __future__ import annotations

from datetime import date
from typing import Any, Mapping, cast

import pytest

from crestline_agent.graph import MediAssistAgent, _extract_slots
from crestline_agent.llm import IntentDecision
from crestline_retrieval.models import (
    AuthorizationContext,
    DocumentMatch,
    DocumentSearchResult,
    EvidenceResult,
    EvidenceStatus,
    Outcome,
    SubstitutionVerification,
)
from crestline_retrieval.service import RetrievalService


AS_OF = date(2026, 10, 22)
GUIDE_HASH = "5e04ec5b0f391d6486df049ba9da2cdb4de8703870f692eccf23078ff1e609e7"
CONTRACT_HASH = "2ca0255a0670aabf7c9e1b62960f3776511577e2d3cebed5e6e1af16943f96d7"
AMENDMENT_HASH = "037d8180a9ddb70123a41f1597eb7c76a2ddeda25ea9830f17c25de0b6a4fb4e"


class StaticClassifier:
    def __init__(self, decision: IntentDecision | None = None) -> None:
        self.decision = decision or IntentDecision("clarification")
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def classify(self, message: str, safe_context: Mapping[str, str]) -> IntentDecision:
        self.calls.append((message, safe_context))
        return self.decision


def _auth(principal: str = "rep-east") -> AuthorizationContext:
    return AuthorizationContext(
        principal,
        {"H-E101": "East", "H-E102": "East"},
        frozenset({"East"}),
    )


def _doc(
    document_id: str,
    record_field: str,
    record_id: str,
    content_hash: str,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "document_version": "V2" if document_id.startswith("SUB_") else "2026-01",
        "document_status": "current",
        "effective_from": "2026-01-01",
        "page_number": 1,
        "source_file": f"{document_id}.pdf",
        "content_sha256": content_hash,
        record_field: record_id,
    }


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = []

    def get_order_visibility(
        self,
        order_id: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        self.calls.append(("order", (order_id, auth.principal_id), {}))
        hospital = "H-E101" if order_id == "ORD-1001" else "H-E102"
        quantity = 100 if order_id == "ORD-1001" else 30
        shipped = 60 if order_id == "ORD-1001" else 0
        return EvidenceResult(
            EvidenceStatus.FOUND,
            (
                {
                    "hospital_id": hospital,
                    "region": "East",
                    "order_id": order_id,
                    "line_id": "1",
                    "sku": "AV-IV22-BLU-LF",
                    "quantity_ordered": quantity,
                    "quantity_shipped": shipped,
                    "quantity_carrier_reported_delivered": 0,
                    "quantity_unshipped": quantity - shipped,
                    "line_fulfillment_state": "partially_shipped" if shipped else "not_shipped",
                    "shipment_evidence": [
                        {
                            "shipment_id": "SHIP-001",
                            "record_id": "shipment-record-001",
                            "tracking_id": "SYN-AT-5501",
                        }
                    ]
                    if shipped
                    else [],
                    "order_record_id": "order-record",
                    "order_line_record_id": "line-record",
                },
            ),
        )

    def get_backorder_evidence(
        self,
        order_id: str,
        auth: AuthorizationContext,
        *,
        line_id: str | None = None,
    ) -> EvidenceResult:
        self.calls.append(("backorder", (order_id, auth.principal_id), {"line_id": line_id}))
        unknown = order_id == "ORD-1003"
        return EvidenceResult(
            EvidenceStatus.FOUND,
            (
                {
                    "hospital_id": "H-W201" if unknown else "H-E101",
                    "region": "West" if unknown else "East",
                    "order_id": order_id,
                    "line_id": "1",
                    "sku": "AV-IV22-BLU-LF",
                    "quantity_unshipped": 40,
                    "current_supplier_restock_date": None if unknown else "2026-10-28",
                    "current_supplier_eta_record_id": (
                        "ETA-003-MISSING" if unknown else "ETA-001-NEW"
                    ),
                    "current_supplier_eta_bronze_record_id": "eta-bronze-record",
                    "current_supplier_eta_source_file_name": "supplier_etas.csv",
                },
            ),
        )

    def get_product_inventory(
        self,
        region: str,
        sku: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        self.calls.append(("inventory", (region, sku, auth.principal_id), {}))
        return EvidenceResult(
            EvidenceStatus.FOUND,
            (
                {
                    "region": region,
                    "sku": sku,
                    "has_inventory_observation": True,
                    "quantity_available": 45,
                    "latest_current_snapshot_at": "2026-10-22T22:30:00Z",
                    "warehouse_inventory_evidence": [
                        {
                            "warehouse_id": "WH-EAST",
                            "snapshot_at": "2026-10-22T22:30:00Z",
                            "record_id": "inventory-record",
                        }
                    ],
                    "product_record_id": "product-record",
                },
            ),
        )

    def search_document_evidence(
        self,
        query_text: str,
        auth: AuthorizationContext,
        *,
        num_results: int = 5,
    ) -> DocumentSearchResult:
        self.calls.append(("document", (query_text, auth.principal_id), {"num_results": num_results}))
        if "substitution guide candidates" in query_text:
            return DocumentSearchResult(
                EvidenceStatus.FOUND,
                (
                    DocumentMatch(
                        chunk_id="substitution:approved-current",
                        chunk_type="substitution_guide_row",
                        embedding_text=(
                            "Requested SKU: AV-IV22-BLU-LF\n"
                            "Candidate SKU: BR-IV22-BLU-LF\nStatus: APPROVED"
                        ),
                        discovery_score=0.95,
                        document_id="SUB_GUIDE_2026_V2_CURRENT",
                        document_version="V2",
                        document_status="current",
                        effective_from="2026-09-01",
                        page_number=1,
                        source_file="SUB_GUIDE_2026_V2_CURRENT.pdf",
                        content_sha256=GUIDE_HASH,
                        source_record_id="approved-row",
                        extraction_method="embedded_text_table",
                        extractor_name="crestline_deterministic_pdf",
                        extractor_version="1.0.0",
                        row_status="APPROVED",
                        requested_sku="AV-IV22-BLU-LF",
                        candidate_sku="BR-IV22-BLU-LF",
                    ),
                    DocumentMatch(
                        chunk_id="substitution:rejected-current",
                        chunk_type="substitution_guide_row",
                        embedding_text=(
                            "Requested SKU: AV-IV22-BLU-LF\n"
                            "Candidate SKU: BR-IV22-BLU-LX\nStatus: REJECTED"
                        ),
                        discovery_score=0.88,
                        document_id="SUB_GUIDE_2026_V2_CURRENT",
                        document_version="V2",
                        document_status="current",
                        effective_from="2026-09-01",
                        page_number=1,
                        source_file="SUB_GUIDE_2026_V2_CURRENT.pdf",
                        content_sha256=GUIDE_HASH,
                        source_record_id="rejected-row",
                        extraction_method="embedded_text_table",
                        extractor_name="crestline_deterministic_pdf",
                        extractor_version="1.0.0",
                        row_status="REJECTED",
                        requested_sku="AV-IV22-BLU-LF",
                        candidate_sku="BR-IV22-BLU-LX",
                    ),
                    DocumentMatch(
                        chunk_id="substitution:superseded",
                        chunk_type="substitution_guide_row",
                        embedding_text=(
                            "Requested SKU: AV-IV22-BLU-LF\n"
                            "Candidate SKU: NV-IV22-BLU-LF\nStatus: APPROVED"
                        ),
                        discovery_score=0.84,
                        document_id="SUB_GUIDE_2025_V1_SUPERSEDED",
                        document_version="V1",
                        document_status="superseded",
                        effective_from="2025-01-01",
                        page_number=1,
                        source_file="SUB_GUIDE_2025_V1_SUPERSEDED.pdf",
                        content_sha256="superseded-hash",
                        source_record_id="superseded-row",
                        extraction_method="embedded_text_table",
                        extractor_name="crestline_deterministic_pdf",
                        extractor_version="1.0.0",
                        row_status="APPROVED",
                        requested_sku="AV-IV22-BLU-LF",
                        candidate_sku="NV-IV22-BLU-LF",
                    ),
                ),
            )
        return DocumentSearchResult(
            EvidenceStatus.FOUND,
            (
                DocumentMatch(
                    chunk_id="substitution:row",
                    chunk_type="substitution_guide_row",
                    embedding_text="Current guide evidence for AV-IV22-BLU-LF.",
                    discovery_score=0.8,
                    document_id="SUB_GUIDE_2026_V2_CURRENT",
                    document_version="V2",
                    document_status="current",
                    effective_from="2026-09-01",
                    page_number=1,
                    source_file="SUB_GUIDE_2026_V2_CURRENT.pdf",
                    content_sha256=GUIDE_HASH,
                    source_record_id="guide-row",
                    extraction_method="embedded_text",
                    extractor_name="crestline_deterministic_pdf",
                    extractor_version="1.0.0",
                ),
            ),
        )

    def get_contract_rules(
        self,
        hospital_id: str,
        auth: AuthorizationContext,
        *,
        as_of: date,
    ) -> EvidenceResult:
        self.calls.append(("contract", (hospital_id, auth.principal_id), {"as_of": as_of}))
        if not auth.allows_hospital(hospital_id):
            return EvidenceResult(
                EvidenceStatus.ACCESS_DENIED,
                outcome=Outcome.ACCESS_DENIED,
            )
        row = _doc("CONTRACT_H-E101_BASE", "contract_clause_id", "brand-lock", CONTRACT_HASH)
        row.update(
            {
                "clause_type": "brand_restriction",
                "clause_text": "Aurevia brand-lock on all IV catheters.",
            }
        )
        return EvidenceResult(EvidenceStatus.FOUND, (row,))

    def verify_substitution(self, **kwargs: Any) -> SubstitutionVerification:
        self.calls.append(("substitution", (), kwargs))
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
            row = _doc("SUB_GUIDE_2026_V2_CURRENT", "substitution_row_id", "rejected-row", GUIDE_HASH)
            row["row_status"] = "REJECTED"
            return SubstitutionVerification(
                Outcome.BLOCKED,
                ("current_guide_status_blocks_candidate", "ifu_attributes_do_not_match"),
                guide_evidence=(row,),
                **common,
            )
        if hospital == "H-E101":
            row = _doc("CONTRACT_H-E101_BASE", "contract_clause_id", "brand-lock", CONTRACT_HASH)
            row["clause_type"] = "brand_restriction"
            return SubstitutionVerification(
                Outcome.BLOCKED,
                ("contract_brand_restriction_without_exact_override",),
                contract_evidence=(row,),
                **common,
            )
        amendment = _doc(
            "AMENDMENT_H-E102_2026_01",
            "contract_clause_id",
            "amendment-row",
            AMENDMENT_HASH,
        )
        amendment["clause_type"] = "brand_lock_override"
        amendment.update(
            {
                "is_override": True,
                "requested_sku": "AV-IV22-BLU-LF",
                "candidate_sku": "BR-IV22-BLU-LF",
            }
        )
        if kwargs.get("order_id") is None or kwargs.get("line_id") is None:
            return SubstitutionVerification(
                Outcome.INSUFFICIENT_EVIDENCE,
                ("conditional_amendment_requires_order_line_context",),
                contract_evidence=(amendment,),
                inventory_evidence=(
                    {
                        "has_inventory_observation": True,
                        "quantity_available": 45,
                        "product_record_id": "product-record",
                    },
                ),
                **common,
            )
        return SubstitutionVerification(
            Outcome.ELIGIBLE_FOR_HUMAN_REVIEW,
            ("all_deterministic_preconditions_met_human_clinical_review_required",),
            contract_evidence=(amendment,),
            inventory_evidence=(
                {
                    "has_inventory_observation": True,
                    "quantity_available": 45,
                    "product_record_id": "product-record",
                },
            ),
            backorder_evidence=(
                {
                    "order_id": kwargs.get("order_id"),
                    "line_id": kwargs.get("line_id"),
                    "quantity_unshipped": 30,
                    "current_supplier_eta_record_id": "ETA-002-NEW",
                },
            ),
            **common,
        )


def _invoke(
    agent: MediAssistAgent,
    service: FakeService,
    classifier: StaticClassifier,
    message: str,
    thread_id: str,
    auth: AuthorizationContext | None = None,
) -> Any:
    return agent.invoke(
        message,
        thread_id=thread_id,
        service=cast(RetrievalService, service),
        authorization=auth or _auth(),
        classifier=classifier,
        as_of=AS_OF,
    )


def test_order_route_executes_tool_and_returns_source_citations() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    reply = _invoke(agent, service, classifier, "Where is ORD-1001?", "order-thread")
    assert reply.intent == "order"
    assert "100 ordered, 60 shipped" in reply.text
    assert "shipment=SHIP-001" in reply.text
    assert "record=shipment-record-001" in reply.text
    assert service.calls[0][0] == "order"
    assert classifier.calls == []


@pytest.mark.parametrize("message", ["Hi", "Hello", "Hey", "Good morning", "Good afternoon", "Good evening"])
def test_greeting_is_static_and_does_not_call_backend(message: str) -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("inventory"))

    reply = _invoke(agent, service, classifier, message, "greeting-thread")

    assert reply.intent == "greeting"
    assert "I'm MediAssist AI" in reply.text
    assert "What would you like to check?" in reply.text
    assert classifier.calls == []
    assert service.calls == []


def test_combined_greeting_and_capability_question_is_static() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("order"))

    reply = _invoke(
        agent,
        service,
        classifier,
        "Hi, what can you help me with?",
        "capability-thread",
    )

    assert reply.intent == "capability"
    assert "Where is ORD-1001?" in reply.text
    assert "qualified human clinical review" in reply.text
    assert classifier.calls == []
    assert service.calls == []


def test_greeting_preserves_verified_order_context() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "greeting-memory-thread"

    _invoke(agent, service, classifier, "Where is ORD-1001?", thread_id)
    reply = _invoke(agent, service, classifier, "Hello", thread_id)

    assert reply.intent == "greeting"
    assert [call[0] for call in service.calls] == ["order"]
    snapshot = agent._graph.get_state({"configurable": {"thread_id": thread_id}})
    assert snapshot.values["last_order_id"] == "ORD-1001"
    assert snapshot.values["last_hospital_id"] == "H-E101"


def test_broad_reporting_acknowledges_limit_and_scopes_demo_examples() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("order"))
    demo_east = AuthorizationContext(
        "DEMO_ONLY_cli_east",
        {"H-E101": "East", "H-E102": "East"},
        frozenset({"East"}),
    )

    reply = _invoke(
        agent,
        service,
        classifier,
        "Show me all delayed orders",
        "report-thread",
        demo_east,
    )

    assert reply.intent == "reporting"
    assert "portfolio or daily order summary" in reply.text
    assert "one authorized order lookup at a time" in reply.text
    assert "ORD-1001" in reply.text and "ORD-1002" in reply.text
    assert "ORD-1003" not in reply.text
    assert "Which order would you like me to inspect?" in reply.text
    assert classifier.calls == []
    assert service.calls == []


def test_broad_reporting_does_not_offer_synthetic_examples_outside_demo_scope() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    west = AuthorizationContext("trusted-west", {"H-W201": "West"}, frozenset({"West"}))

    reply = _invoke(agent, service, classifier, "Summarize today's orders", "west-report", west)

    assert reply.intent == "reporting"
    assert "Please provide an order ID" in reply.text
    assert "ORD-1001" not in reply.text
    assert "ORD-1002" not in reply.text
    assert service.calls == []


def test_unsupported_request_gets_closest_supported_guidance() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("order"))

    reply = _invoke(agent, service, classifier, "Can you create an order?", "unsupported-thread")

    assert reply.intent == "unsupported"
    assert "not available in this prototype" in reply.text
    assert "one authorized order" in reply.text
    assert classifier.calls == []
    assert service.calls == []


def test_ambiguous_order_status_request_asks_for_order_id() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()

    reply = _invoke(agent, service, classifier, "I need an order status", "ambiguous-order")

    assert reply.intent == "clarification"
    assert "Which order ID should I check?" in reply.text


def test_followup_uses_thread_memory_but_other_thread_is_isolated() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    _invoke(agent, service, classifier, "Where is ORD-1001?", "session-a")
    followup = _invoke(agent, service, classifier, "What about the remaining units?", "session-a")
    isolated = _invoke(agent, service, classifier, "What about the remaining units?", "session-b")
    assert followup.intent == "backorder"
    assert "ETA-001-NEW" in followup.text
    assert service.calls[1][0] == "backorder"
    assert service.calls[1][1][0] == "ORD-1001"
    assert isolated.intent == "clarification"
    assert "CLARIFICATION_REQUIRED" in isolated.text
    assert [call[0] for call in service.calls].count("backorder") == 1


def test_context_free_remaining_units_does_not_accept_inventory_classifier() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("inventory"))

    reply = _invoke(agent, service, classifier, "What about the remaining units?", "fresh-thread")

    assert reply.intent == "clarification"
    assert "Which order" in reply.text
    assert classifier.calls == []
    assert service.calls == []


def test_remaining_units_with_verified_order_context_stays_backorder() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier(IntentDecision("inventory"))

    _invoke(agent, service, classifier, "Where is ORD-1001?", "order-thread")
    reply = _invoke(agent, service, classifier, "What about the remaining units?", "order-thread")

    assert reply.intent == "backorder"
    assert "ETA-001-NEW" in reply.text
    assert classifier.calls == []
    assert [call[0] for call in service.calls] == ["order", "backorder"]


def test_three_turn_order_eta_and_candidate_discovery_uses_verified_order_context() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "three-turn-discovery"

    order = _invoke(agent, service, classifier, "Where is ORD-1001?", thread_id)
    eta = _invoke(agent, service, classifier, "when it will arrive", thread_id)
    alternatives = _invoke(agent, service, classifier, "alternative i should use", thread_id)

    assert order.intent == "order" and "100 ordered, 60 shipped" in order.text
    assert eta.intent == "backorder" and "2026-10-28" in eta.text
    assert alternatives.intent == "substitution"
    assert "CLARIFICATION_REQUIRED" not in alternatives.text
    assert "semantic guide search only" in alternatives.text
    assert "Candidate BR-IV22-BLU-LF:\nBLOCKED" in alternatives.text
    assert "Candidate BR-IV22-BLU-LX:\nBLOCKED" in alternatives.text
    assert "NV-IV22-BLU-LF" not in alternatives.text
    substitution_calls = [call for call in service.calls if call[0] == "substitution"]
    assert len(substitution_calls) == 2
    assert {call[2]["candidate_sku"] for call in substitution_calls} == {
        "BR-IV22-BLU-LF",
        "BR-IV22-BLU-LX",
    }
    for call in substitution_calls:
        assert call[2]["requested_sku"] == "AV-IV22-BLU-LF"
        assert call[2]["order_id"] == "ORD-1001"
        assert call[2]["line_id"] == "1"
        assert call[2]["hospital_id"] == "H-E101"
        assert call[2]["region"] == "East"
    snapshot = agent._graph.get_state({"configurable": {"thread_id": thread_id}})
    persisted = repr(snapshot.values)
    assert "alternative i should use" not in persisted
    assert "when it will arrive" not in persisted
    assert snapshot.values["slots"] == {}


def test_candidate_discovery_does_not_reuse_order_from_another_hospital() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "hospital-isolation"
    _invoke(agent, service, classifier, "Where is ORD-1001?", thread_id)
    calls_before = len(service.calls)

    reply = _invoke(
        agent,
        service,
        classifier,
        "What alternative should I use for hospital H-E102?",
        thread_id,
    )

    assert reply.text.startswith("CLARIFICATION_REQUIRED")
    assert "Which authorized order" in reply.text
    new_calls = service.calls[calls_before:]
    assert new_calls == []
    assert not any(call[0] in {"document", "substitution"} for call in new_calls)
    snapshot = agent._graph.get_state({"configurable": {"thread_id": thread_id}})
    assert snapshot.values["last_order_id"] == ""
    assert snapshot.values["last_line_id"] == ""


def test_explicit_hospital_clears_incompatible_inherited_order_context() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "stale-order-context"
    _invoke(agent, service, classifier, "Where is ORD-1001?", thread_id)
    calls_before = len(service.calls)

    reply = _invoke(
        agent,
        service,
        classifier,
        "Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102?",
        thread_id,
    )

    assert reply.text.startswith("INSUFFICIENT_EVIDENCE")
    assert "conditional_amendment_requires_order_line_context" in reply.text
    new_calls = service.calls[calls_before:]
    assert [call[0] for call in new_calls] == ["substitution"]
    assert new_calls[0][2]["order_id"] is None
    assert new_calls[0][2]["line_id"] is None
    assert new_calls[0][2]["hospital_id"] == "H-E102"
    snapshot = agent._graph.get_state({"configurable": {"thread_id": thread_id}})
    assert snapshot.values["last_order_id"] == ""
    assert snapshot.values["last_line_id"] == ""
    assert snapshot.values["last_hospital_id"] == "H-E102"


def test_compatible_explicit_hospital_can_reuse_verified_order_context() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "compatible-order-context"
    _invoke(agent, service, classifier, "Where is ORD-1002?", thread_id)
    calls_before = len(service.calls)

    reply = _invoke(
        agent,
        service,
        classifier,
        "Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102?",
        thread_id,
    )

    assert reply.text.startswith("ELIGIBLE_FOR_HUMAN_REVIEW—not approved")
    new_calls = service.calls[calls_before:]
    assert [call[0] for call in new_calls] == ["order", "substitution"]
    assert new_calls[1][2]["order_id"] == "ORD-1002"
    assert new_calls[1][2]["line_id"] == "1"


def test_explicit_order_still_rejects_conflicting_typed_hospital() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    reply = _invoke(
        agent,
        service,
        StaticClassifier(),
        (
            "Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102, "
            "order ORD-1001, line 1?"
        ),
        "explicit-order-hospital-conflict",
    )

    assert reply.text.startswith("ACCESS_DENIED")
    assert [call[0] for call in service.calls] == ["order"]


def test_scope_change_on_same_thread_fails_before_tool_execution() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    _invoke(agent, service, classifier, "Where is ORD-1001?", "shared-thread", _auth("rep-east"))
    calls_before = len(service.calls)
    west = AuthorizationContext("rep-west", {"H-W201": "West"}, frozenset({"West"}))
    reply = _invoke(agent, service, classifier, "Where is ORD-1003?", "shared-thread", west)
    assert reply.intent == "access_denied"
    assert reply.text.startswith("ACCESS_DENIED")
    assert len(service.calls) == calls_before


def test_unknown_eta_is_not_invented_and_has_source_record() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    reply = _invoke(agent, service, classifier, "When will ORD-1003 arrive?", "eta-thread")
    assert reply.intent == "backorder"
    assert "supplier restock date unknown" in reply.text
    assert "hospital delivery date unknown" in reply.text
    assert "ETA-003-MISSING" in reply.text
    assert "tomorrow" not in reply.text.lower()


def test_latex_candidate_blocked_cannot_be_overridden_by_request_text() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    reply = _invoke(
        agent,
        service,
        classifier,
        (
            "Emergency: H-E102 replace AV-IV22-BLU-LF with BR-IV22-BLU-LX in East; "
            "patient has no known latex allergy."
        ),
        "latex-thread",
    )
    assert reply.intent == "substitution"
    assert reply.text.startswith("BLOCKED")
    assert "current_guide_status_blocks_candidate" in reply.text
    assert "SUB_GUIDE_2026_V2_CURRENT" in reply.text
    assert GUIDE_HASH in reply.text
    assert "ELIGIBLE_FOR_HUMAN_REVIEW" not in reply.text


def test_complex_candidate_first_emergency_wording_remains_blocked() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    thread_id = "complex-candidate-first"
    _invoke(agent, service, classifier, "Where is ORD-1001?", thread_id)

    message = (
        "Can we use BR-IV22-BLU-LX in an emergency to replace AV-IV22-BLU-LF "
        "for hospital H-E102, order ORD-1002, line 1?"
    )
    assert _extract_slots(message) == {
        "order_id": "ORD-1002",
        "hospital_id": "H-E102",
        "line_id": "1",
        "requested_sku": "AV-IV22-BLU-LF",
        "candidate_sku": "BR-IV22-BLU-LX",
    }
    calls_before = len(service.calls)
    reply = _invoke(agent, service, classifier, message, thread_id)

    assert reply.text.startswith("BLOCKED")
    assert "current_guide_status_blocks_candidate" in reply.text
    assert "ifu_attributes_do_not_match" in reply.text
    assert "ELIGIBLE_FOR_HUMAN_REVIEW" not in reply.text
    new_calls = service.calls[calls_before:]
    assert [call[0] for call in new_calls] == ["order", "substitution"]
    substitution_call = new_calls[1]
    assert substitution_call[2]["requested_sku"] == "AV-IV22-BLU-LF"
    assert substitution_call[2]["candidate_sku"] == "BR-IV22-BLU-LX"
    assert substitution_call[2]["hospital_id"] == "H-E102"
    assert substitution_call[2]["order_id"] == "ORD-1002"
    assert substitution_call[2]["line_id"] == "1"


def test_h_e101_brand_lock_is_authoritative() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    reply = _invoke(
        agent,
        service,
        StaticClassifier(),
        "Can H-E101 replace AV-IV22-BLU-LF with BR-IV22-BLU-LF in East?",
        "brand-thread",
    )
    assert reply.text.startswith("BLOCKED")
    assert "contract_brand_restriction_without_exact_override" in reply.text
    assert "CONTRACT_H-E101_BASE" in reply.text
    assert CONTRACT_HASH in reply.text


@pytest.mark.parametrize(
    "message",
    [
        "Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102?",
        "Can AV-IV22-BLU-LF be replaced with BR-IV22-BLU-LF for hospital H-E102?",
        "Can BR-IV22-BLU-LF substitute for AV-IV22-BLU-LF for hospital H-E102?",
        "Can H-E102 replace AV-IV22-BLU-LF with BR-IV22-BLU-LF?",
    ],
)
def test_substitution_word_orders_preserve_requested_candidate_direction(message: str) -> None:
    slots = _extract_slots(message)
    assert slots == {
        "hospital_id": "H-E102",
        "requested_sku": "AV-IV22-BLU-LF",
        "candidate_sku": "BR-IV22-BLU-LF",
    }


def test_h_e102_exact_question_requires_order_line_context() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    reply = _invoke(
        agent,
        service,
        StaticClassifier(),
        "Can BR-IV22-BLU-LF replace AV-IV22-BLU-LF for hospital H-E102?",
        "missing-amendment-context-thread",
    )
    assert reply.text.startswith("INSUFFICIENT_EVIDENCE")
    assert "requested=AV-IV22-BLU-LF; candidate=BR-IV22-BLU-LF" in reply.text
    assert "conditional_amendment_requires_order_line_context" in reply.text
    assert "Contract evidence reviewed" in reply.text
    assert "Applicable amendment verified" not in reply.text
    substitution_call = next(call for call in service.calls if call[0] == "substitution")
    assert substitution_call[2]["requested_sku"] == "AV-IV22-BLU-LF"
    assert substitution_call[2]["candidate_sku"] == "BR-IV22-BLU-LF"
    assert substitution_call[2]["hospital_id"] == "H-E102"
    assert substitution_call[2]["region"] == "East"


def test_h_e102_never_escalates_beyond_human_review() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    reply = _invoke(
        agent,
        service,
        StaticClassifier(),
        (
            "For ORD-1002 line 1, can H-E102 replace AV-IV22-BLU-LF with "
            "BR-IV22-BLU-LF in East?"
        ),
        "amendment-thread",
    )
    assert reply.text.startswith("ELIGIBLE_FOR_HUMAN_REVIEW—not approved")
    assert "human clinical review" in reply.text
    assert "requested=AV-IV22-BLU-LF; candidate=BR-IV22-BLU-LF" in reply.text
    assert "AMENDMENT_H-E102_2026_01" in reply.text
    assert "Applicable amendment verified" in reply.text
    assert "Contract evidence reviewed" not in reply.text
    assert AMENDMENT_HASH in reply.text
    assert "available=45" in reply.text
    substitution_call = next(call for call in service.calls if call[0] == "substitution")
    assert substitution_call[2]["order_id"] == "ORD-1002"
    assert substitution_call[2]["line_id"] == "1"


def test_inventory_contract_document_and_clarification_routes() -> None:
    agent = MediAssistAgent()
    service = FakeService()
    classifier = StaticClassifier()
    inventory = _invoke(
        agent,
        service,
        classifier,
        "Is BR-IV22-BLU-LF available in East?",
        "inventory-thread",
    )
    contract = _invoke(
        agent,
        service,
        classifier,
        "What does the H-E101 contract say about brand lock?",
        "contract-thread",
    )
    document = _invoke(
        agent,
        service,
        classifier,
        "What does the current IFU say about AV-IV22-BLU-LF?",
        "document-thread",
    )
    clarification = _invoke(agent, service, classifier, "Please help me", "clarify-thread")
    assert inventory.intent == "inventory" and "available quantity=45" in inventory.text
    assert contract.intent == "contract" and "CONTRACT_H-E101_BASE" in contract.text
    assert document.intent == "document" and "Semantic discovery only" in document.text
    assert clarification.intent == "clarification"
    assert classifier.calls[-1][0] == "Please help me"
