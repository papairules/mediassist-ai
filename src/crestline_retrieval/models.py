"""Stable models suitable for later exposure as LangGraph tool schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class Outcome(str, Enum):
    ELIGIBLE_FOR_HUMAN_REVIEW = "ELIGIBLE_FOR_HUMAN_REVIEW"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ACCESS_DENIED = "ACCESS_DENIED"


class EvidenceStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    ACCESS_DENIED = "ACCESS_DENIED"


@dataclass(frozen=True)
class SqlParameter:
    name: str
    value: str
    type: str = "STRING"


@dataclass(frozen=True)
class AuthorizationContext:
    """Trusted scope created by server-side identity and policy code only."""

    principal_id: str
    hospital_regions: Mapping[str, str]
    allowed_regions: frozenset[str]
    may_search_public_documents: bool = True

    def __post_init__(self) -> None:
        if not self.principal_id:
            raise ValueError("principal_id is required")
        object.__setattr__(
            self,
            "hospital_regions",
            MappingProxyType(dict(self.hospital_regions)),
        )
        object.__setattr__(self, "allowed_regions", frozenset(self.allowed_regions))

    def hospital_region(self, hospital_id: str) -> str | None:
        return self.hospital_regions.get(hospital_id)

    def allows_hospital(self, hospital_id: str) -> bool:
        return hospital_id in self.hospital_regions

    def allows_region(self, region: str) -> bool:
        return region in self.allowed_regions


@dataclass(frozen=True)
class EvidenceResult:
    status: EvidenceStatus
    records: tuple[Mapping[str, Any], ...] = ()
    message: str | None = None
    outcome: Outcome | None = None


@dataclass(frozen=True)
class DocumentMatch:
    chunk_id: str
    chunk_type: str
    embedding_text: str
    discovery_score: float | None
    document_id: str
    document_version: str
    document_status: str
    effective_from: str
    page_number: int
    source_file: str
    content_sha256: str
    source_record_id: str
    extraction_method: str
    extractor_name: str
    extractor_version: str
    row_status: str | None = None
    requested_sku: str | None = None
    candidate_sku: str | None = None
    product_sku: str | None = None


@dataclass(frozen=True)
class DocumentSearchResult:
    status: EvidenceStatus
    matches: tuple[DocumentMatch, ...] = ()
    message: str | None = None
    outcome: Outcome | None = None
    semantic_discovery_only: bool = True


@dataclass(frozen=True)
class SubstitutionVerification:
    outcome: Outcome
    reason_codes: tuple[str, ...]
    requested_sku: str
    candidate_sku: str
    hospital_id: str
    region: str
    as_of_date: str
    guide_evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    ifu_evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    contract_evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    inventory_evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    backorder_evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    requires_human_clinical_review: bool = True
    semantic_similarity_used_as_approval: bool = False
