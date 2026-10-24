"""Read-only, deterministic retrieval services for MediAssist AI."""

from .client import DatabricksBackend, RetrievalBackend, RetrievalBackendError
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
from .service import RetrievalService

__all__ = [
    "AuthorizationContext",
    "DatabricksBackend",
    "DocumentMatch",
    "DocumentSearchResult",
    "EvidenceResult",
    "EvidenceStatus",
    "Outcome",
    "RetrievalBackend",
    "RetrievalBackendError",
    "RetrievalService",
    "SqlParameter",
    "SubstitutionVerification",
]
