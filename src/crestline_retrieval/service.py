"""Deterministic evidence retrieval and substitution validation."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Mapping, Sequence

from .client import RetrievalBackend, RetrievalBackendError
from .models import (
    AuthorizationContext,
    DocumentMatch,
    DocumentSearchResult,
    EvidenceResult,
    EvidenceStatus,
    Outcome,
    SqlParameter,
    SubstitutionVerification,
)


CATALOG = "workspace"
GOLD_SCHEMA = "crestline_gold"
SILVER_SCHEMA = "crestline_silver"
VECTOR_INDEX = "workspace.crestline_silver.document_retrieval_chunks_index"
BLOCKING_GUIDE_STATUSES = frozenset(
    {"REJECTED", "REVOKED", "APPROVED (REVOKED)", "PENDING_REVIEW", "PENDING CLINICAL REVIEW"}
)
VECTOR_COLUMNS = (
    "chunk_id",
    "chunk_type",
    "embedding_text",
    "source_record_id",
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
    "row_status",
    "requested_sku",
    "candidate_sku",
    "product_sku",
)
SAFE_CHUNK_TYPES = frozenset({"substitution_guide_row", "ifu_product_fact"})
SENSITIVE_TEXT = re.compile(
    r"(?:confidential\s+(?:contract\s+)?pricing|unit\s+price|negotiated\s+price|\$\s*\d)",
    re.IGNORECASE,
)


def _param(name: str, value: str, type_name: str = "STRING") -> SqlParameter:
    return SqlParameter(name=name, value=value, type=type_name)


def _access_denied(message: str) -> EvidenceResult:
    return EvidenceResult(
        status=EvidenceStatus.ACCESS_DENIED,
        message=message,
        outcome=Outcome.ACCESS_DENIED,
    )


def _rows_result(rows: Sequence[Mapping[str, Any]], not_found: str) -> EvidenceResult:
    if not rows:
        return EvidenceResult(status=EvidenceStatus.NOT_FOUND, message=not_found)
    return EvidenceResult(status=EvidenceStatus.FOUND, records=tuple(rows))


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


class RetrievalService:
    """Read-only APIs whose signatures can be wrapped as tools without redesign."""

    def __init__(
        self,
        backend: RetrievalBackend,
        *,
        vector_index: str = VECTOR_INDEX,
    ) -> None:
        self._backend = backend
        self._vector_index = vector_index

    def _hospital_filter(
        self,
        auth: AuthorizationContext,
    ) -> tuple[str, list[SqlParameter]]:
        hospitals = sorted(auth.hospital_regions)
        placeholders = []
        parameters: list[SqlParameter] = []
        for index, hospital_id in enumerate(hospitals):
            name = f"authorized_hospital_{index}"
            placeholders.append(f":{name}")
            parameters.append(_param(name, hospital_id))
        return ", ".join(placeholders), parameters

    def get_order_visibility(
        self,
        order_id: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        hospital_filter, parameters = self._hospital_filter(auth)
        if not hospital_filter:
            return _access_denied("No hospital scope is authorized")
        sql = f"""SELECT
  hospital_id, hospital_name, region, contract_id, order_id, line_id,
  placed_at, erp_loaded_at, sku, brand, category, uom,
  quantity_ordered, quantity_shipped, quantity_carrier_reported_delivered,
  quantity_unshipped, shipment_count, latest_carrier_scan_at,
  shipment_evidence, line_fulfillment_state,
  order_record_id, order_line_record_id, product_record_id
FROM {CATALOG}.{GOLD_SCHEMA}.order_line_fulfillment
WHERE order_id = :order_id
  AND hospital_id IN ({hospital_filter})
ORDER BY line_id"""
        rows = self._backend.query(sql, [_param("order_id", order_id), *parameters])
        if not rows:
            return _access_denied("Order is unavailable within the trusted hospital scope")
        return _rows_result(rows, "Order not found")

    def get_backorder_evidence(
        self,
        order_id: str,
        auth: AuthorizationContext,
        *,
        line_id: str | None = None,
    ) -> EvidenceResult:
        hospital_filter, parameters = self._hospital_filter(auth)
        if not hospital_filter:
            return _access_denied("No hospital scope is authorized")
        line_filter = ""
        query_parameters = [_param("order_id", order_id), *parameters]
        if line_id is not None:
            line_filter = "\n  AND line_id = :line_id"
            query_parameters.append(_param("line_id", line_id))
        sql = f"""SELECT
  hospital_id, hospital_name, region, order_id, line_id, sku, uom,
  quantity_ordered, quantity_shipped, quantity_unshipped,
  has_unshipped_quantity, line_fulfillment_state, shipment_evidence,
  supplier_eta_record_count, current_supplier_eta_record_id,
  current_supplier_restock_date, current_supplier_reported_at,
  current_supplier_eta_ingested_at, current_date_classification,
  current_source_note, current_supplier_eta_source_id,
  current_supplier_eta_bronze_record_id, current_supplier_eta_file_id,
  current_supplier_eta_source_file_name,
  current_supplier_eta_source_file_modified_at,
  current_supplier_eta_bronze_ingested_at, supplier_eta_revision_history,
  order_record_id, order_line_record_id, product_record_id
FROM {CATALOG}.{GOLD_SCHEMA}.backorder_eta_evidence
WHERE order_id = :order_id
  AND hospital_id IN ({hospital_filter}){line_filter}
ORDER BY line_id"""
        rows = self._backend.query(sql, query_parameters)
        if not rows:
            return _access_denied("Backorder evidence is unavailable within the trusted scope")
        return _rows_result(rows, "Backorder evidence not found")

    def get_product_inventory(
        self,
        region: str,
        sku: str,
        auth: AuthorizationContext,
    ) -> EvidenceResult:
        if not auth.allows_region(region):
            return _access_denied("Region is outside the trusted authorization scope")
        sql = f"""SELECT
  region, sku, brand, category, gauge, color, length_mm, latex_free,
  sterile, manufacturer_status, has_inventory_observation,
  warehouse_count, quantity_on_hand, quantity_reserved, quantity_available,
  earliest_current_snapshot_at, latest_current_snapshot_at,
  warehouse_inventory_evidence, product_source_id, product_record_id,
  product_file_id, product_source_file_name,
  product_source_file_modified_at, product_ingested_at
FROM {CATALOG}.{GOLD_SCHEMA}.product_region_inventory
WHERE region = :region AND sku = :sku"""
        rows = self._backend.query(
            sql,
            [_param("region", region), _param("sku", sku)],
        )
        return _rows_result(rows, "Product or region was not found")

    def search_document_evidence(
        self,
        query_text: str,
        auth: AuthorizationContext,
        *,
        num_results: int = 5,
    ) -> DocumentSearchResult:
        if not auth.may_search_public_documents:
            return DocumentSearchResult(
                status=EvidenceStatus.ACCESS_DENIED,
                message="Public document search is not authorized",
                outcome=Outcome.ACCESS_DENIED,
            )
        if not query_text.strip():
            return DocumentSearchResult(
                status=EvidenceStatus.NOT_FOUND,
                message="A non-empty search query is required",
            )
        bounded_results = max(1, min(num_results, 20))
        rows = self._backend.vector_search(
            index_name=self._vector_index,
            query_text=query_text,
            columns=VECTOR_COLUMNS,
            num_results=bounded_results,
            query_type="HYBRID",
            filters={"chunk_type": list(sorted(SAFE_CHUNK_TYPES))},
        )
        matches: list[DocumentMatch] = []
        for row in rows:
            chunk_type = str(row.get("chunk_type", ""))
            text = str(row.get("embedding_text", ""))
            document_id = str(row.get("document_id", ""))
            if (
                chunk_type not in SAFE_CHUNK_TYPES
                or document_id.startswith(("CONTRACT", "AMENDMENT"))
                or SENSITIVE_TEXT.search(text)
            ):
                raise RetrievalBackendError("Vector Search returned prohibited document content")
            required = (
                "chunk_id",
                "source_record_id",
                "document_version",
                "document_status",
                "effective_from",
                "page_number",
                "source_file",
                "content_sha256",
                "extraction_method",
                "extractor_name",
                "extractor_version",
            )
            if any(row.get(field) is None for field in required) or not text:
                raise RetrievalBackendError("Vector Search result lacks citation provenance")
            raw_score = row.get("score", row.get("_score"))
            matches.append(
                DocumentMatch(
                    chunk_id=str(row["chunk_id"]),
                    chunk_type=chunk_type,
                    embedding_text=text,
                    discovery_score=(float(raw_score) if raw_score is not None else None),
                    document_id=document_id,
                    document_version=str(row["document_version"]),
                    document_status=str(row["document_status"]),
                    effective_from=str(row["effective_from"]),
                    page_number=int(row["page_number"]),
                    source_file=str(row["source_file"]),
                    content_sha256=str(row["content_sha256"]),
                    source_record_id=str(row["source_record_id"]),
                    extraction_method=str(row["extraction_method"]),
                    extractor_name=str(row["extractor_name"]),
                    extractor_version=str(row["extractor_version"]),
                    row_status=(str(row["row_status"]) if row.get("row_status") is not None else None),
                    requested_sku=(str(row["requested_sku"]) if row.get("requested_sku") is not None else None),
                    candidate_sku=(str(row["candidate_sku"]) if row.get("candidate_sku") is not None else None),
                    product_sku=(str(row["product_sku"]) if row.get("product_sku") is not None else None),
                )
            )
        if not matches:
            return DocumentSearchResult(
                status=EvidenceStatus.NOT_FOUND,
                message="No document evidence matched",
            )
        return DocumentSearchResult(status=EvidenceStatus.FOUND, matches=tuple(matches))

    def get_contract_rules(
        self,
        hospital_id: str,
        auth: AuthorizationContext,
        *,
        as_of: date,
    ) -> EvidenceResult:
        if not auth.allows_hospital(hospital_id):
            return _access_denied("Hospital contract access is not authorized")
        sql = f"""SELECT
  contract_clause_id, hospital_id, clause_type, clause_text,
  is_override, overrides_document_id, requested_sku, candidate_sku,
  conditions, document_id, display_document_id, document_version,
  document_status, effective_from, page_number, source_file, content_sha256,
  extraction_method, extractor_name, extractor_version,
  extraction_status, review_reasons
FROM {CATALOG}.{SILVER_SCHEMA}.contract_clauses
WHERE hospital_id = :hospital_id
  AND document_status = 'current'
  AND effective_from <= :as_of
ORDER BY effective_from, document_id, contract_clause_id"""
        rows = self._backend.query(
            sql,
            [_param("hospital_id", hospital_id), _param("as_of", as_of.isoformat(), "DATE")],
        )
        if any(SENSITIVE_TEXT.search(str(row.get("clause_text", ""))) for row in rows):
            raise RetrievalBackendError("Contract retrieval returned prohibited pricing content")
        return _rows_result(rows, "No effective contract evidence was found")

    def _guide_rows(
        self,
        requested_sku: str,
        candidate_sku: str,
        as_of: date,
    ) -> list[Mapping[str, Any]]:
        sql = f"""SELECT
  substitution_row_id, requested_sku, candidate_sku, row_status,
  required_attributes, clinical_note, table_number, table_row_number,
  document_id, display_document_id, document_version, document_status,
  effective_from, page_number, source_file, content_sha256,
  extraction_method, extractor_name, extractor_version,
  extraction_status, review_reasons
FROM {CATALOG}.{SILVER_SCHEMA}.substitution_guide_rows
WHERE requested_sku = :requested_sku
  AND candidate_sku = :candidate_sku
  AND effective_from <= :as_of
ORDER BY effective_from DESC, document_id, substitution_row_id"""
        return self._backend.query(
            sql,
            [
                _param("requested_sku", requested_sku),
                _param("candidate_sku", candidate_sku),
                _param("as_of", as_of.isoformat(), "DATE"),
            ],
        )

    def _ifu_rows(
        self,
        requested_sku: str,
        candidate_sku: str,
        as_of: date,
    ) -> list[Mapping[str, Any]]:
        sql = f"""SELECT
  ifu_fact_id, ifu_id, revision, product_sku, gauge, device_length_mm,
  latex_free, sterile, single_use, clinical_note,
  establishes_interchangeability, document_id, display_document_id,
  document_version, document_status, effective_from, page_number,
  source_file, content_sha256, extraction_method, extractor_name,
  extractor_version, extraction_status, review_reasons
FROM {CATALOG}.{SILVER_SCHEMA}.ifu_product_facts
WHERE product_sku IN (:requested_sku, :candidate_sku)
  AND document_status = 'current'
  AND effective_from <= :as_of
ORDER BY product_sku, effective_from DESC, ifu_fact_id"""
        return self._backend.query(
            sql,
            [
                _param("requested_sku", requested_sku),
                _param("candidate_sku", candidate_sku),
                _param("as_of", as_of.isoformat(), "DATE"),
            ],
        )

    def verify_substitution(
        self,
        *,
        requested_sku: str,
        candidate_sku: str,
        hospital_id: str,
        region: str,
        auth: AuthorizationContext,
        as_of: date,
        order_id: str | None = None,
        line_id: str | None = None,
        request_text: str | None = None,
    ) -> SubstitutionVerification:
        """Compose exact evidence; request_text can never alter deterministic policy."""
        del request_text
        trusted_region = auth.hospital_region(hospital_id)
        if trusted_region is None or trusted_region != region or not auth.allows_region(region):
            return self._verification(
                Outcome.ACCESS_DENIED,
                ("hospital_or_region_not_authorized",),
                requested_sku,
                candidate_sku,
                hospital_id,
                region,
                as_of,
            )

        guide_rows = self._guide_rows(requested_sku, candidate_sku, as_of)
        ifu_rows = self._ifu_rows(requested_sku, candidate_sku, as_of)
        contract = self.get_contract_rules(hospital_id, auth, as_of=as_of)
        inventory = self.get_product_inventory(region, candidate_sku, auth)
        backorder = EvidenceResult(status=EvidenceStatus.NOT_FOUND)
        if order_id is not None:
            backorder = self.get_backorder_evidence(
                order_id,
                auth,
                line_id=line_id,
            )

        reasons: list[str] = []
        current_guide = [
            row for row in guide_rows if str(row.get("document_status", "")).lower() == "current"
        ]
        statuses = {str(row.get("row_status", "")).upper() for row in current_guide}
        if statuses & BLOCKING_GUIDE_STATUSES:
            reasons.append("current_guide_status_blocks_candidate")
        elif len(current_guide) != 1 or statuses != {"APPROVED"}:
            reasons.append("current_approved_guide_row_missing_or_conflicting")

        ifu_by_sku: dict[str, list[Mapping[str, Any]]] = {}
        for row in ifu_rows:
            ifu_by_sku.setdefault(str(row.get("product_sku")), []).append(row)
        requested_ifu = ifu_by_sku.get(requested_sku, [])
        candidate_ifu = ifu_by_sku.get(candidate_sku, [])
        if len(requested_ifu) != 1 or len(candidate_ifu) != 1:
            reasons.append("complete_current_ifu_pair_missing_or_conflicting")
        else:
            comparable = ("gauge", "device_length_mm", "latex_free", "sterile", "single_use")
            if any(requested_ifu[0].get(field) != candidate_ifu[0].get(field) for field in comparable):
                reasons.append("ifu_attributes_do_not_match")
            if any(_bool(row.get("establishes_interchangeability")) for row in ifu_rows):
                reasons.append("invalid_ifu_interchangeability_claim")

        contract_rows = list(contract.records)
        if contract.status != EvidenceStatus.FOUND:
            reasons.append("effective_contract_evidence_missing")
        restrictive_contract = any(self._is_restrictive_clause(row) for row in contract_rows)
        base_document_ids = {
            str(row.get("document_id"))
            for row in contract_rows
            if not _bool(row.get("is_override"))
            and str(row.get("document_id", "")).startswith("CONTRACT_")
        }
        exact_overrides = [
            row
            for row in contract_rows
            if _bool(row.get("is_override"))
            and row.get("requested_sku") == requested_sku
            and row.get("candidate_sku") == candidate_sku
            and row.get("overrides_document_id") in base_document_ids
        ]
        if restrictive_contract and not exact_overrides:
            reasons.append("contract_brand_restriction_without_exact_override")
        if exact_overrides:
            if order_id is None or line_id is None:
                reasons.append("conditional_amendment_requires_order_line_context")
            elif backorder.status != EvidenceStatus.FOUND:
                reasons.append("conditional_amendment_backorder_evidence_missing")
            else:
                open_rows = [
                    row
                    for row in backorder.records
                    if row.get("hospital_id") == hospital_id
                    and row.get("sku") == requested_sku
                    and (_int(row.get("quantity_unshipped")) or 0) > 0
                ]
                if not open_rows:
                    reasons.append("conditional_amendment_has_no_open_backorder")

        inventory_rows = list(inventory.records)
        if inventory.status != EvidenceStatus.FOUND or len(inventory_rows) != 1:
            reasons.append("regional_inventory_evidence_missing_or_conflicting")
        elif not _bool(inventory_rows[0].get("has_inventory_observation")):
            reasons.append("regional_inventory_observation_missing")
        elif (_int(inventory_rows[0].get("quantity_available")) or 0) <= 0:
            reasons.append("candidate_has_no_observed_available_inventory")

        blocking_reasons = {
            "current_guide_status_blocks_candidate",
            "ifu_attributes_do_not_match",
            "invalid_ifu_interchangeability_claim",
            "contract_brand_restriction_without_exact_override",
            "conditional_amendment_has_no_open_backorder",
            "candidate_has_no_observed_available_inventory",
        }
        if any(reason in blocking_reasons for reason in reasons):
            outcome = Outcome.BLOCKED
        elif reasons:
            outcome = Outcome.INSUFFICIENT_EVIDENCE
        else:
            outcome = Outcome.ELIGIBLE_FOR_HUMAN_REVIEW
            reasons.append("all_deterministic_preconditions_met_human_clinical_review_required")
        return self._verification(
            outcome,
            tuple(reasons),
            requested_sku,
            candidate_sku,
            hospital_id,
            region,
            as_of,
            guide_rows=guide_rows,
            ifu_rows=ifu_rows,
            contract_rows=contract_rows,
            inventory_rows=inventory_rows,
            backorder_rows=list(backorder.records),
        )

    @staticmethod
    def _is_restrictive_clause(row: Mapping[str, Any]) -> bool:
        text = str(row.get("clause_text", "")).lower()
        if "no brand exclusivity" in text or "no exclusive brand-lock" in text:
            return False
        return "brand-lock" in text or "prohibited under this contract" in text

    @staticmethod
    def _verification(
        outcome: Outcome,
        reasons: tuple[str, ...],
        requested_sku: str,
        candidate_sku: str,
        hospital_id: str,
        region: str,
        as_of: date,
        *,
        guide_rows: Sequence[Mapping[str, Any]] = (),
        ifu_rows: Sequence[Mapping[str, Any]] = (),
        contract_rows: Sequence[Mapping[str, Any]] = (),
        inventory_rows: Sequence[Mapping[str, Any]] = (),
        backorder_rows: Sequence[Mapping[str, Any]] = (),
    ) -> SubstitutionVerification:
        return SubstitutionVerification(
            outcome=outcome,
            reason_codes=reasons,
            requested_sku=requested_sku,
            candidate_sku=candidate_sku,
            hospital_id=hospital_id,
            region=region,
            as_of_date=as_of.isoformat(),
            guide_evidence=tuple(guide_rows),
            ifu_evidence=tuple(ifu_rows),
            contract_evidence=tuple(contract_rows),
            inventory_evidence=tuple(inventory_rows),
            backorder_evidence=tuple(backorder_rows),
        )
