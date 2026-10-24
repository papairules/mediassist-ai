from __future__ import annotations

from crestline_ui.presentation import parse_response


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
