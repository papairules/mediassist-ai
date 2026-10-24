"""Typed wrappers around the existing RetrievalService interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from crestline_retrieval.models import (
    AuthorizationContext,
    DocumentSearchResult,
    EvidenceResult,
    SubstitutionVerification,
)
from crestline_retrieval.service import RetrievalService


@dataclass(frozen=True)
class OrderVisibilityInput:
    order_id: str


@dataclass(frozen=True)
class BackorderEvidenceInput:
    order_id: str
    line_id: str | None = None


@dataclass(frozen=True)
class ProductInventoryInput:
    region: str
    sku: str


@dataclass(frozen=True)
class DocumentSearchInput:
    query: str
    num_results: int = 5


@dataclass(frozen=True)
class ContractRulesInput:
    hospital_id: str
    as_of: date


@dataclass(frozen=True)
class SubstitutionInput:
    requested_sku: str
    candidate_sku: str
    hospital_id: str
    region: str
    as_of: date
    order_id: str | None = None
    line_id: str | None = None
    request_text: str | None = None


class AgentTools:
    """Bind every call to fresh trusted authorization supplied for this turn."""

    def __init__(self, service: RetrievalService, auth: AuthorizationContext) -> None:
        self._service = service
        self._auth = auth

    def get_order_visibility(self, value: OrderVisibilityInput) -> EvidenceResult:
        return self._service.get_order_visibility(value.order_id, self._auth)

    def get_backorder_evidence(self, value: BackorderEvidenceInput) -> EvidenceResult:
        return self._service.get_backorder_evidence(
            value.order_id,
            self._auth,
            line_id=value.line_id,
        )

    def get_product_inventory(self, value: ProductInventoryInput) -> EvidenceResult:
        return self._service.get_product_inventory(value.region, value.sku, self._auth)

    def search_document_evidence(self, value: DocumentSearchInput) -> DocumentSearchResult:
        return self._service.search_document_evidence(
            value.query,
            self._auth,
            num_results=value.num_results,
        )

    def get_contract_rules(self, value: ContractRulesInput) -> EvidenceResult:
        return self._service.get_contract_rules(
            value.hospital_id,
            self._auth,
            as_of=value.as_of,
        )

    def verify_substitution(self, value: SubstitutionInput) -> SubstitutionVerification:
        return self._service.verify_substitution(
            requested_sku=value.requested_sku,
            candidate_sku=value.candidate_sku,
            hospital_id=value.hospital_id,
            region=value.region,
            auth=self._auth,
            as_of=value.as_of,
            order_id=value.order_id,
            line_id=value.line_id,
            request_text=value.request_text,
        )
