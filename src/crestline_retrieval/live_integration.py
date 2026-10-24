"""Read-only live smoke test for deployed MediAssist retrieval resources."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
import sys
from typing import Any, Callable, Mapping, Sequence, TextIO

from .client import DatabricksBackend, RetrievalBackend
from .models import AuthorizationContext, EvidenceStatus, Outcome, SqlParameter
from .service import RetrievalService


DEFAULT_PROFILE = "abinroy90@gmail.com"
DEFAULT_WAREHOUSE_ID = "d249a9e5325ec021"
DEFAULT_VECTOR_INDEX = "workspace.crestline_silver.document_retrieval_chunks_index"
SYNTHETIC_AS_OF = date(2026, 10, 22)

# DEMO_ONLY: fixed test scope for synthetic fixtures. This is not production
# authorization and is deliberately not loaded from rep_access_demo_only.
DEMO_ONLY_AUTHORIZATION = AuthorizationContext(
    principal_id="DEMO_ONLY_live_integration_test",
    hospital_regions={
        "H-E101": "East",
        "H-E102": "East",
        "H-W201": "West",
    },
    allowed_regions=frozenset({"East", "West"}),
    may_search_public_documents=True,
)

EXPECTED_HASHES = {
    "SUB_GUIDE_2026_V2_CURRENT": "5e04ec5b0f391d6486df049ba9da2cdb4de8703870f692eccf23078ff1e609e7",
    "IFU_AV-IV22-BLU-LF": "fd29324cbbf4e8b7563207f1be85132cec03f21eba763cc61c4525d627215ed9",
    "CONTRACT_H-E101_BASE": "2ca0255a0670aabf7c9e1b62960f3776511577e2d3cebed5e6e1af16943f96d7",
    "AMENDMENT_H-E102_2026_01": "037d8180a9ddb70123a41f1597eb7c76a2ddeda25ea9830f17c25de0b6a4fb4e",
}
EXPECTED_RECORD_IDS = {
    "rejected_latex_guide": "21557e199755510d9f417c56a01b712c253a9b5de5cbfc81fe83152802d03660",
    "h_e101_brand_lock": "9fcee0d97e2f93e4fa655053b2de818cacc3cdb7c4277ff26f896fd23c3bcfcb",
    "h_e102_amendment": "bd1b6dd89a8602751dd44e29ec3a04dfa6c576d97259459cb553948d9ce3d0b2",
}


class LiveAssertionError(AssertionError):
    """A deployed-resource assertion did not match the synthetic scenario."""


class CachingBackend:
    """Cache identical read-only calls for the duration of one smoke-test run."""

    def __init__(self, backend: RetrievalBackend) -> None:
        self._backend = backend
        self._sql_cache: dict[
            tuple[str, tuple[SqlParameter, ...]], list[Mapping[str, Any]]
        ] = {}
        self._vector_cache: dict[
            tuple[str, str, tuple[str, ...], int, str, str],
            list[Mapping[str, Any]],
        ] = {}

    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]:
        key = (statement, tuple(parameters))
        if key not in self._sql_cache:
            self._sql_cache[key] = self._backend.query(statement, parameters)
        return self._sql_cache[key]

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
        serialized_filters = json.dumps(filters or {}, sort_keys=True)
        key = (
            index_name,
            query_text,
            tuple(columns),
            num_results,
            query_type,
            serialized_filters,
        )
        if key not in self._vector_cache:
            self._vector_cache[key] = self._backend.vector_search(
                index_name=index_name,
                query_text=query_text,
                columns=columns,
                num_results=num_results,
                query_type=query_type,
                filters=filters,
            )
        return self._vector_cache[key]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveAssertionError(message)


def _as_int(value: Any) -> int:
    if value is None:
        raise LiveAssertionError("Expected an integer but found NULL")
    return int(value)


def _require_document_citation(
    row: Mapping[str, Any],
    *,
    document_id: str,
    record_id_field: str,
    record_id: str | None = None,
) -> None:
    _require(row.get("document_id") == document_id, f"Expected document {document_id}")
    _require(bool(row.get("document_version")), f"{document_id} has no version")
    _require(_as_int(row.get("page_number")) >= 1, f"{document_id} has no page citation")
    _require(bool(row.get("source_file")), f"{document_id} has no source file")
    expected_hash = EXPECTED_HASHES.get(document_id)
    if expected_hash is not None:
        _require(row.get("content_sha256") == expected_hash, f"{document_id} hash mismatch")
    _require(bool(row.get(record_id_field)), f"{document_id} has no supporting record ID")
    if record_id is not None:
        _require(row.get(record_id_field) == record_id, f"{document_id} record ID mismatch")


def _check_order_visibility(service: RetrievalService) -> str:
    result = service.get_order_visibility("ORD-1001", DEMO_ONLY_AUTHORIZATION)
    _require(result.status == EvidenceStatus.FOUND, "ORD-1001 was not found in authorized scope")
    _require(len(result.records) == 1, "ORD-1001 should contain exactly one line")
    row = result.records[0]
    _require(row.get("hospital_id") == "H-E101", "ORD-1001 hospital mismatch")
    _require(_as_int(row.get("quantity_ordered")) == 100, "ORD-1001 ordered quantity mismatch")
    _require(_as_int(row.get("quantity_shipped")) == 60, "ORD-1001 shipped quantity mismatch")
    _require(_as_int(row.get("quantity_unshipped")) == 40, "ORD-1001 unshipped quantity mismatch")
    shipments = row.get("shipment_evidence")
    if not isinstance(shipments, list) or len(shipments) != 1:
        raise LiveAssertionError("ORD-1001 shipment evidence mismatch")
    shipment = shipments[0]
    if not isinstance(shipment, dict):
        raise LiveAssertionError("ORD-1001 shipment evidence is malformed")
    _require(shipment.get("shipment_id") == "SHIP-001", "ORD-1001 shipment ID mismatch")
    _require(bool(shipment.get("record_id")), "SHIP-001 is missing its source record ID")
    return "ORD-1001: 100 ordered, 60 shipped, 40 unshipped; cited SHIP-001"


def _check_unknown_eta(service: RetrievalService) -> str:
    result = service.get_backorder_evidence(
        "ORD-1003",
        DEMO_ONLY_AUTHORIZATION,
        line_id="1",
    )
    _require(result.status == EvidenceStatus.FOUND, "ORD-1003 backorder evidence was not found")
    _require(len(result.records) == 1, "ORD-1003 should contain exactly one line")
    row = result.records[0]
    _require(row.get("hospital_id") == "H-W201", "ORD-1003 hospital mismatch")
    _require(
        row.get("current_supplier_eta_record_id") == "ETA-003-MISSING",
        "ORD-1003 current supplier record mismatch",
    )
    _require(row.get("current_supplier_restock_date") is None, "ORD-1003 ETA must remain NULL")
    history = row.get("supplier_eta_revision_history")
    if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], dict):
        raise LiveAssertionError("ORD-1003 ETA history mismatch")
    _require(history[0].get("expected_supplier_restock_date") is None, "ETA history invented a date")
    return "ORD-1003: supplier record ETA-003-MISSING preserves NULL restock date"


def _check_vector_search(service: RetrievalService) -> str:
    result = service.search_document_evidence(
        "AV-IV22-BLU-LF current substitution guide and manufacturer IFU evidence",
        DEMO_ONLY_AUTHORIZATION,
        num_results=15,
    )
    _require(result.status == EvidenceStatus.FOUND, "Hybrid Vector Search returned no evidence")
    _require(result.semantic_discovery_only, "Vector Search was not labeled discovery-only")
    by_document: dict[str, list[Any]] = {}
    for match in result.matches:
        by_document.setdefault(match.document_id, []).append(match)
        _require(match.page_number >= 1, f"{match.document_id} has no page citation")
        _require(len(match.content_sha256) == 64, f"{match.document_id} has an invalid hash")
        _require(bool(match.source_record_id), f"{match.document_id} has no source record ID")
        _require(
            not match.document_id.startswith(("CONTRACT", "AMENDMENT")),
            "Contract content crossed into public Vector Search",
        )
    _require("SUB_GUIDE_2026_V2_CURRENT" in by_document, "Current V2 guide was not discovered")
    _require("IFU_AV-IV22-BLU-LF" in by_document, "Requested SKU IFU was not discovered")
    guide = by_document["SUB_GUIDE_2026_V2_CURRENT"][0]
    ifu = by_document["IFU_AV-IV22-BLU-LF"][0]
    _require(guide.content_sha256 == EXPECTED_HASHES[guide.document_id], "V2 guide hash mismatch")
    _require(ifu.content_sha256 == EXPECTED_HASHES[ifu.document_id], "IFU hash mismatch")
    return f"HYBRID discovery: {len(result.matches)} citation-ready matches including V2 guide and IFU"


def _check_latex_candidate_blocked(service: RetrievalService) -> str:
    result = service.verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LX",
        hospital_id="H-E102",
        region="East",
        auth=DEMO_ONLY_AUTHORIZATION,
        as_of=SYNTHETIC_AS_OF,
        request_text="Emergency exception; patient has no known latex allergy",
    )
    _require(result.outcome == Outcome.BLOCKED, "BR-IV22-BLU-LX must be BLOCKED")
    _require(
        "current_guide_status_blocks_candidate" in result.reason_codes,
        "Current rejected guide status did not block BR-IV22-BLU-LX",
    )
    current_rejected = [
        row
        for row in result.guide_evidence
        if row.get("document_status") == "current" and row.get("row_status") == "REJECTED"
    ]
    _require(len(current_rejected) == 1, "Expected one current REJECTED guide row")
    _require_document_citation(
        current_rejected[0],
        document_id="SUB_GUIDE_2026_V2_CURRENT",
        record_id_field="substitution_row_id",
        record_id=EXPECTED_RECORD_IDS["rejected_latex_guide"],
    )
    return "BR-IV22-BLU-LX: BLOCKED by current V2 REJECTED evidence"


def _check_h_e101_brand_lock(service: RetrievalService) -> str:
    result = service.verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E101",
        region="East",
        auth=DEMO_ONLY_AUTHORIZATION,
        as_of=SYNTHETIC_AS_OF,
    )
    _require(result.outcome == Outcome.BLOCKED, "H-E101 brand lock must block the candidate")
    _require(
        "contract_brand_restriction_without_exact_override" in result.reason_codes,
        "H-E101 brand restriction was not enforced",
    )
    brand_lock = [
        row
        for row in result.contract_evidence
        if row.get("contract_clause_id") == EXPECTED_RECORD_IDS["h_e101_brand_lock"]
    ]
    _require(len(brand_lock) == 1, "H-E101 brand-lock clause was not cited")
    _require_document_citation(
        brand_lock[0],
        document_id="CONTRACT_H-E101_BASE",
        record_id_field="contract_clause_id",
        record_id=EXPECTED_RECORD_IDS["h_e101_brand_lock"],
    )
    return "H-E101: BLOCKED by current contract brand lock with no exact amendment"


def _check_h_e102_amendment(service: RetrievalService) -> str:
    result = service.verify_substitution(
        requested_sku="AV-IV22-BLU-LF",
        candidate_sku="BR-IV22-BLU-LF",
        hospital_id="H-E102",
        region="East",
        auth=DEMO_ONLY_AUTHORIZATION,
        as_of=SYNTHETIC_AS_OF,
        order_id="ORD-1002",
        line_id="1",
    )
    _require(
        result.outcome == Outcome.ELIGIBLE_FOR_HUMAN_REVIEW,
        f"H-E102 narrow amendment outcome was {result.outcome.value}",
    )
    amendment = [
        row
        for row in result.contract_evidence
        if row.get("contract_clause_id") == EXPECTED_RECORD_IDS["h_e102_amendment"]
    ]
    _require(len(amendment) == 1, "H-E102 exact amendment was not cited")
    _require_document_citation(
        amendment[0],
        document_id="AMENDMENT_H-E102_2026_01",
        record_id_field="contract_clause_id",
        record_id=EXPECTED_RECORD_IDS["h_e102_amendment"],
    )
    inventory = list(result.inventory_evidence)
    _require(len(inventory) == 1, "H-E102 candidate inventory evidence is incomplete")
    _require(_as_int(inventory[0].get("quantity_available")) == 45, "East availability must be 45")
    backorder = list(result.backorder_evidence)
    _require(len(backorder) == 1, "H-E102 amendment requires exact open backorder evidence")
    _require(backorder[0].get("order_id") == "ORD-1002", "Amendment used the wrong order")
    _require(_as_int(backorder[0].get("quantity_unshipped")) > 0, "ORD-1002 backorder is not open")
    return "H-E102: exact amendment evaluated; candidate eligible only for human review"


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[RetrievalService], str]


CHECKS = (
    Check("authorized_order_visibility", _check_order_visibility),
    Check("unknown_supplier_eta", _check_unknown_eta),
    Check("hybrid_vector_discovery", _check_vector_search),
    Check("latex_candidate_blocked", _check_latex_candidate_blocked),
    Check("h_e101_brand_lock", _check_h_e101_brand_lock),
    Check("h_e102_narrow_amendment", _check_h_e102_amendment),
)


def run_checks(service: RetrievalService, output: TextIO = sys.stdout) -> int:
    failures = 0
    for check in CHECKS:
        try:
            detail = check.run(service)
        except Exception as exc:  # keep the report complete, then fail closed
            failures += 1
            print(f"FAIL {check.name}: {type(exc).__name__}: {exc}", file=output)
        else:
            print(f"PASS {check.name}: {detail}", file=output)
    print(
        f"SUMMARY passed={len(CHECKS) - failures} failed={failures} total={len(CHECKS)}",
        file=output,
    )
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE_ID)
    parser.add_argument("--vector-index", default=DEFAULT_VECTOR_INDEX)
    return parser


def main() -> None:
    args = _parser().parse_args()
    from databricks.sdk import WorkspaceClient

    workspace = WorkspaceClient(
        profile=args.profile,
        product="mediassist-live-integration",
        product_version="0.1.0",
    )
    backend = CachingBackend(DatabricksBackend(workspace, args.warehouse_id))
    service = RetrievalService(backend, vector_index=args.vector_index)
    raise SystemExit(run_checks(service))


if __name__ == "__main__":
    main()
