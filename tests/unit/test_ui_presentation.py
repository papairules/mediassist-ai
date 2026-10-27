from __future__ import annotations

from datetime import date
from typing import Any

import crestline_ui.app as ui_app
from crestline_agent.cli import build_demo_only_authorization
from crestline_agent.graph import MediAssistAgent
from crestline_eval.fixtures import GoldenFixtureBackend
from crestline_eval.runner import DeterministicClassifier
from crestline_retrieval.service import RetrievalService
from crestline_ui.presentation import parse_response


class _Column:
    def __enter__(self) -> "_Column":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _StreamlitCapture:
    def __init__(self) -> None:
        self.markdowns: list[str] = []
        self.captions: list[str] = []

    def markdown(self, value: str, **kwargs: Any) -> None:
        del kwargs
        self.markdowns.append(value)

    def caption(self, value: str, **kwargs: Any) -> None:
        del kwargs
        self.captions.append(value)

    def columns(self, count: int) -> list[_Column]:
        return [_Column() for _ in range(count)]

    def expander(self, label: str, **kwargs: Any) -> _Column:
        del label, kwargs
        return _Column()


def test_static_guidance_responses_render_as_user_visible_text() -> None:
    greeting = parse_response(
        "Hi! I'm MediAssist AI. I can help you track orders, check supplier restock "
        "estimates, find regional inventory, and review potential product substitutions.\n\n"
        "What would you like to check?"
    )
    capability = parse_response("I can help with order visibility.\n\nWhich order should I check?")
    reporting = parse_response(
        "I understand you want a portfolio summary. This prototype checks one authorized order at a time."
    )
    unsupported = parse_response("I understand that request is not available in this prototype.")
    clarification = parse_response("CLARIFICATION_REQUIRED: Which order ID should I check?")

    assert greeting.summary_text == ""
    assert greeting.brief_explanation == greeting.text
    assert "Hi! I'm MediAssist AI." in greeting.brief_explanation
    assert capability.brief_explanation == capability.text
    assert reporting.brief_explanation == reporting.text
    assert unsupported.brief_explanation == unsupported.text
    assert clarification.summary_text == "Please clarify."


def test_order_response_builds_structured_metrics_and_operational_sources() -> None:
    view = parse_response(
        "ORD-1001 line 1 (AV-IV22-BLU-LF): 100 ordered, 60 shipped, "
        "0 carrier-reported delivered, 40 unshipped; state=partially_shipped. "
        "[shipment=SHIP-001; record=shipment-record-001; tracking=SYN-AT-5501] "
        "[order_record=order-record; line_record=line-record]"
    )
    assert [(metric.label, metric.value) for metric in view.metrics] == [
        ("Ordered", "100"),
        ("Shipped", "60"),
        ("Unshipped", "40"),
        ("Fulfillment", "Partially Shipped"),
    ]
    assert len(view.citations) == 2
    assert view.citations[0].record_id == "shipment-record-001"


def test_supplier_unknown_date_remains_unknown() -> None:
    view = parse_response(
        "ORD-1003 line 1: 40 units unshipped; supplier restock date unknown; "
        "hospital delivery date unknown. [supplier_record=ETA-003-MISSING; "
        "bronze_record=bronze-id; source_file=supplier_etas.csv]"
    )
    assert [(metric.label, metric.value) for metric in view.metrics] == [
        ("Unshipped", "40"),
        ("Supplier restock estimate", "Unknown"),
    ]
    assert "hospital delivery date unknown" in view.display_text


def test_current_and_superseded_document_citations_are_distinct() -> None:
    view = parse_response(
        "Evidence. "
        "[SUB_GUIDE_2026_V2_CURRENT version=V2 status=current p.1; "
        "record=current-row; sha256=current-hash] "
        "[SUB_GUIDE_2025_V1_SUPERSEDED version=V1 status=superseded p.2; "
        "record=historical-row; sha256=historical-hash]"
    )
    assert view.citations[0].is_current
    assert not view.citations[0].is_historical
    assert view.citations[1].is_historical
    assert not view.citations[1].is_current


def test_substitution_outcome_and_inventory_are_presentation_only() -> None:
    view = parse_response(
        "INSUFFICIENT_EVIDENCE. Do not infer a substitution.\n"
        "Evaluated pair: requested=AV-IV22-BLU-LF; candidate=BR-IV22-BLU-LF.\n"
        "Inventory: observed=True, available=45; product_record=product-record.\n"
        "Contract evidence reviewed: clause=brand_lock_override "
        "[AMENDMENT_H-E102_2026_01 version=2026-01 status=current p.1; "
        "record=amendment-row; sha256=amendment-hash]"
    )
    assert view.outcome == "INSUFFICIENT_EVIDENCE"
    assert (view.metrics[0].label, view.metrics[0].value) == ("Candidate inventory", "45")
    assert view.citations[0].document_id == "AMENDMENT_H-E102_2026_01"
    assert "Contract evidence reviewed" in view.display_text


def test_discovered_candidate_outcomes_remain_independent() -> None:
    view = parse_response(
        "Candidate discovery used semantic guide search only.\n"
        "Candidate BR-IV22-BLU-LF:\nBLOCKED. Contract restriction.\n"
        "Candidate BR-IV22-BLU-LX:\nINSUFFICIENT_EVIDENCE. IFU missing.\n"
        "Candidate NV-IV22-BLU-LF:\nELIGIBLE_FOR_HUMAN_REVIEW—not approved."
    )
    assert [(candidate.sku, candidate.outcome) for candidate in view.candidate_outcomes] == [
        ("BR-IV22-BLU-LF", "BLOCKED"),
        ("BR-IV22-BLU-LX", "INSUFFICIENT_EVIDENCE"),
        ("NV-IV22-BLU-LF", "ELIGIBLE_FOR_HUMAN_REVIEW"),
    ]
    assert view.candidate_summary == (
        "3 alternatives evaluated: 1 requires clinical review, 1 blocked, "
        "1 has insufficient evidence"
    )
    assert view.candidate_headline == "Alternatives evaluated"


def test_all_blocked_candidates_have_concise_summary_and_scoped_inventory() -> None:
    view = parse_response(
        "Candidate discovery used semantic guide search only.\n"
        "Candidate BR-IV22-BLU-LF:\n"
        "BLOCKED. The candidate must not be presented as eligible.\n"
        "Reasons: contract_brand_restriction_without_exact_override.\n"
        "Inventory: observed=True, available=45; product_record=product-br.\n"
        "Candidate BR-IV20-PNK-LF:\n"
        "BLOCKED. The candidate must not be presented as eligible.\n"
        "Reasons: ifu_attributes_do_not_match, candidate_has_no_observed_available_inventory.\n"
        "Inventory: observed=True, available=17; product_record=product-pnk."
    )
    assert view.candidate_summary == "No eligible alternatives currently identified"
    assert view.candidate_headline == "No — no eligible alternatives currently identified."
    assert view.metrics == ()
    assert [candidate.inventory_available for candidate in view.candidate_outcomes] == ["45", "17"]
    assert [candidate.inventory_observed for candidate in view.candidate_outcomes] == [True, True]
    assert view.candidate_outcomes[0].human_reasons == (
        "A brand restriction has no applicable exact override.",
    )
    assert view.candidate_outcomes[1].human_reasons == (
        "Required IFU product attributes do not match.",
        "No observed available inventory exists.",
    )
    assert view.candidate_outcomes[0].citations[-1].details == (
        ("product_record", "product-br"),
    )
    assert view.candidate_outcomes[1].citations[-1].details == (
        ("product_record", "product-pnk"),
    )


def test_revoked_guide_row_is_historical_even_in_current_document() -> None:
    view = parse_response(
        "Candidate NV-IV22-BLU-LF:\n"
        "BLOCKED. The candidate must not be presented as eligible.\n"
        "Reasons: current_guide_status_blocks_candidate.\n"
        "Guide: status=APPROVED (REVOKED) "
        "[SUB_GUIDE_2026_V2_CURRENT version=V2 status=current p.2; "
        "record=revoked-row; sha256=revoked-hash]"
    )
    citation = view.candidate_outcomes[0].citations[0]
    assert citation.row_status == "APPROVED (REVOKED)"
    assert citation.is_historical
    assert not citation.is_current


def test_candidate_evidence_stays_attached_to_its_candidate() -> None:
    view = parse_response(
        "Candidate BR-IV22-BLU-LF:\n"
        "ELIGIBLE_FOR_HUMAN_REVIEW—not approved.\n"
        "Reasons: all_deterministic_preconditions_met_human_clinical_review_required.\n"
        "Inventory: observed=True, available=45; product_record=product-br.\n"
        "Guide: status=APPROVED "
        "[SUB_GUIDE_2026_V2_CURRENT version=V2 status=current p.1; "
        "record=approved-row; sha256=current-hash]\n"
        "Candidate BR-IV22-BLU-LX:\n"
        "BLOCKED.\n"
        "Reasons: current_guide_status_blocks_candidate.\n"
        "Inventory: observed=True, available=0; product_record=product-lx.\n"
        "Guide: status=REJECTED "
        "[SUB_GUIDE_2026_V2_CURRENT version=V2 status=current p.1; "
        "record=rejected-row; sha256=current-hash]"
    )
    eligible, blocked = view.candidate_outcomes
    assert eligible.inventory_available == "45"
    assert eligible.citations[0].record_id == "approved-row"
    assert blocked.inventory_available == "0"
    assert blocked.citations[0].record_id == "rejected-row"


def test_concise_blocked_substitution_keeps_full_evidence_available() -> None:
    view = parse_response(
        "BLOCKED. The candidate must not be presented as eligible.\n"
        "Evaluated pair: requested=AV-IV22-BLU-LF; candidate=BR-IV22-BLU-LX.\n"
        "Reasons: current_guide_status_blocks_candidate, ifu_attributes_do_not_match.\n"
        "Guide: status=REJECTED [SUB_GUIDE_2026_V2_CURRENT version=V2 status=current p.1; "
        "record=guide-row; sha256=guide-hash]\n"
        "IFU: product=BR-IV22-BLU-LX [IFU_BR_IV22_LX version=2026 status=current p.1; "
        "record=ifu-row; sha256=ifu-hash]\n"
        "Inventory: observed=True, available=0; product_record=product-row."
    )
    assert view.summary_text == "No — this substitution is blocked."
    assert "Requested AV-IV22-BLU-LF; candidate BR-IV22-BLU-LX." in view.brief_explanation
    assert "Current guide status blocks this candidate." in view.brief_explanation
    assert "Guide: status=REJECTED" in view.supporting_text
    assert "IFU: product=BR-IV22-BLU-LX" in view.supporting_text
    assert "Inventory: observed=True, available=0" in view.supporting_text
    assert view.citations[0].document_id == "SUB_GUIDE_2026_V2_CURRENT"


def test_conditional_eligibility_is_never_rendered_as_approval() -> None:
    view = parse_response(
        "ELIGIBLE_FOR_HUMAN_REVIEW—not approved. All deterministic preconditions passed.\n"
        "Evaluated pair: requested=AV-IV22-BLU-LF; candidate=BR-IV22-BLU-LF.\n"
        "Reasons: all_deterministic_preconditions_met_human_clinical_review_required."
    )
    assert view.summary_text == "Conditionally eligible — human review required."
    assert "Not approved." in view.brief_explanation
    assert "clinical review" in view.brief_explanation


def test_operational_summaries_keep_eta_semantics_and_metrics() -> None:
    order = parse_response(
        "ORD-1001 line 1 (AV-IV22-BLU-LF): 100 ordered, 60 shipped, "
        "0 carrier-reported delivered, 40 unshipped; state=partially_shipped. "
        "[order_record=order-record; line_record=line-record]"
    )
    assert order.summary_text == "ORD-1001 line 1: Partially Shipped."
    assert "40 remain unshipped" in order.brief_explanation
    assert [metric.label for metric in order.metrics[:3]] == ["Ordered", "Shipped", "Unshipped"]

    eta = parse_response(
        "ORD-1002 line 1: 40 units unshipped; latest supplier restock estimate=2026-10-28; "
        "hospital delivery date unknown. [supplier_record=ETA-002]"
    )
    assert eta.summary_text == "Supplier restock estimate: 2026-10-28."
    assert "not a confirmed hospital delivery date" in eta.brief_explanation
    assert "hospital delivery date unknown" not in eta.summary_text
    assert "supplier_record=ETA-002" in eta.supporting_text or eta.citations


def test_resolution_plan_preserves_operational_summary_and_metrics() -> None:
    view = parse_response(
        "Resolution review for ORD-1002 using authorized evidence:\n"
        "ORD-1002 line 1: 30 ordered, 0 shipped, 30 unshipped; state=not_shipped.\n"
        "Supplier restock estimate=2026-10-29; this is not a confirmed hospital delivery date. [supplier_record=ETA-002]\n"
        "Requested SKU regional inventory: observed=True, available=0.\n"
        "Candidate BR-IV22-BLU-LF:\nELIGIBLE_FOR_HUMAN_REVIEW—not approved.\n"
        "Next action: request a supplier confirmation; no hospital delivery date is known."
    )
    assert view.resolution_order_id == "ORD-1002"
    assert "30 ordered, 0 shipped, 30 unshipped" in view.resolution_summary
    assert "Next action:" in view.resolution_summary
    assert "Supplier restock estimate=2026-10-29" in view.supporting_text
    assert "Requested SKU regional inventory: observed=True, available=0" in view.supporting_text
    assert [(metric.label, metric.value) for metric in view.metrics] == [
        ("Ordered", "30"),
        ("Shipped", "0"),
        ("Unshipped", "30"),
        ("Fulfillment", "Not Shipped"),
        ("Supplier restock estimate", "2026-10-29"),
        ("Requested SKU inventory", "0"),
    ]


def test_streamlit_renders_resolution_plan_and_partial_listing(monkeypatch: Any) -> None:
    capture = _StreamlitCapture()
    monkeypatch.setattr(ui_app, "st", capture)
    service = RetrievalService(GoldenFixtureBackend())
    auth = build_demo_only_authorization("east")
    agent = MediAssistAgent()
    planner = agent.invoke(
        "How can we resolve ORD-1002?",
        thread_id="ui-resolution-test",
        service=service,
        authorization=auth,
        classifier=DeterministicClassifier(),
        as_of=date(2026, 10, 22),
    )
    ui_app._assistant_message(planner.text)
    assert any("Resolution plan — ORD-1002" in value for value in capture.markdowns)
    assert any("30 ordered, 0 shipped, 30 unshipped" in value for value in capture.markdowns)
    assert any("Substitution assessment" in value for value in capture.markdowns)
    assert any("Conditionally eligible" in value for value in capture.markdowns)

    capture.markdowns.clear()
    listing = agent.invoke(
        "Which orders are partially shipped?",
        thread_id="ui-report-test",
        service=service,
        authorization=auth,
        classifier=DeterministicClassifier(),
        as_of=date(2026, 10, 22),
    )
    ui_app._assistant_message(listing.text)
    rendered = "\n".join(capture.markdowns)
    assert "ORD-1001" in rendered
    assert "40 unshipped" in rendered

    capture.markdowns.clear()
    empty = agent.invoke(
        "Which outstanding orders have no supplier ETA?",
        thread_id="ui-empty-report-test",
        service=service,
        authorization=auth,
        classifier=DeterministicClassifier(),
        as_of=date(2026, 10, 22),
    )
    ui_app._assistant_message(empty.text)
    assert any("no authorized records matched" in value for value in capture.markdowns)

    capture.markdowns.clear()
    shorthand = agent.invoke(
        "What about 1001?",
        thread_id="ui-shorthand-test",
        service=service,
        authorization=auth,
        classifier=DeterministicClassifier(),
        as_of=date(2026, 10, 22),
    )
    ui_app._assistant_message(shorthand.text)
    assert any("ORD-1001" in value and "40 unshipped" in value for value in capture.markdowns)


def test_analytics_response_preserves_order_and_inventory_provenance() -> None:
    view = parse_response(
        "Outstanding order lines.\n"
        "ORD-1001 line 1 (AV-IV22-BLU-LF): 40 unshipped; "
        "[order_record=order-1001; product_record=product-av].\n"
        "East / BR-IV22-BLU-LF: observed=True, available=45, "
        "snapshot=2026-10-22T22:30:00Z. [product_record=product-br]"
    )
    assert view.brief_explanation
    record_values = {
        value
        for citation in view.citations
        for key, value in citation.details
        if key.endswith("record")
    }
    assert {"order-1001", "product-av", "product-br"} <= record_values
