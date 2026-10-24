"""Validated, idempotent publication of document facts to Silver Delta tables."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .extract import ExtractionResult, extract_documents, write_datasets
from .ocr import OcrFunction


ARTIFACT_FORMAT_VERSION = 1
EXPECTED_COUNTS = {
    "document_pages": 13,
    "substitution_guide_rows": 11,
    "contract_clauses": 12,
    "ifu_product_facts": 4,
}
EXPECTED_DOCUMENT_IDS = {
    "AMENDMENT_H-E102_2026_01",
    "CONTRACT_H-C301_BASE",
    "CONTRACT_H-E101_BASE",
    "CONTRACT_H-E102_BASE",
    "CONTRACT_H-W201_BASE",
    "CONTRACT_H-W202_BASE",
    "IFU_AV-IV22-BLU-LF",
    "IFU_BR-IV20-PNK-LF",
    "IFU_BR-IV22-BLU-LF",
    "IFU_BR-IV22-BLU-LX",
    "SUB_GUIDE_2025_V1_SUPERSEDED",
    "SUB_GUIDE_2026_V2_CURRENT",
    "SUB_SUPPLEMENT_SCANNED_2026",
}
DATASET_ORDER = tuple(EXPECTED_COUNTS)
KEY_FIELDS = {
    "document_pages": "document_page_id",
    "substitution_guide_rows": "substitution_row_id",
    "contract_clauses": "contract_clause_id",
    "ifu_product_facts": "ifu_fact_id",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_ARTIFACT_SET_ID = re.compile(r"^[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PRICE_PATTERN = re.compile(r"\$\s*\d|confidential negotiated pricing|pricing:\s*confidential", re.I)


class PublicationValidationError(RuntimeError):
    """Raised before any table write when a publication invariant fails."""


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    nullable: bool = False


COMMON_FIELDS = (
    Field("document_id", "string"),
    Field("display_document_id", "string", True),
    Field("document_version", "string"),
    Field("document_status", "string"),
    Field("effective_from", "date"),
    Field("page_number", "bigint"),
    Field("source_file", "string"),
    Field("content_sha256", "string"),
    Field("extraction_method", "string"),
    Field("extractor_name", "string"),
    Field("extractor_version", "string"),
)
DATASET_SCHEMAS: dict[str, tuple[Field, ...]] = {
    "document_pages": COMMON_FIELDS
    + (
        Field("document_page_id", "string"),
        Field("document_type", "string"),
        Field("region", "string"),
        Field("hospital_id", "string", True),
        Field("sensitivity", "string"),
        Field("retrieval_text", "string"),
        Field("has_embedded_text", "boolean"),
        Field("table_count", "bigint"),
        Field("restricted_pricing_redacted", "boolean"),
        Field("extraction_status", "string"),
        Field("review_reasons", "array_string"),
    ),
    "substitution_guide_rows": COMMON_FIELDS
    + (
        Field("substitution_row_id", "string"),
        Field("table_number", "bigint", True),
        Field("table_row_number", "bigint"),
        Field("requested_sku", "string"),
        Field("candidate_sku", "string"),
        Field("row_status", "string"),
        Field("required_attributes", "string", True),
        Field("clinical_note", "string", True),
        Field("extraction_status", "string"),
        Field("review_reasons", "array_string"),
    ),
    "contract_clauses": COMMON_FIELDS
    + (
        Field("contract_clause_id", "string"),
        Field("hospital_id", "string"),
        Field("clause_type", "string"),
        Field("clause_text", "string"),
        Field("is_override", "boolean"),
        Field("overrides_document_id", "string", True),
        Field("requested_sku", "string", True),
        Field("candidate_sku", "string", True),
        Field("conditions", "array_string"),
        Field("extraction_status", "string"),
        Field("review_reasons", "array_string"),
    ),
    "ifu_product_facts": COMMON_FIELDS
    + (
        Field("ifu_fact_id", "string"),
        Field("ifu_id", "string"),
        Field("revision", "string"),
        Field("product_sku", "string"),
        Field("gauge", "bigint"),
        Field("device_length_mm", "bigint"),
        Field("latex_free", "boolean"),
        Field("sterile", "boolean"),
        Field("single_use", "boolean"),
        Field("clinical_note", "string"),
        Field("establishes_interchangeability", "boolean"),
        Field("extraction_status", "string"),
        Field("review_reasons", "array_string"),
    ),
}


@dataclass(frozen=True)
class ArtifactSet:
    artifact_set_id: str
    path: Path
    records: dict[str, list[dict[str, Any]]]
    source_documents: dict[str, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _result_records(result: ExtractionResult) -> dict[str, list[dict[str, Any]]]:
    return {
        "document_pages": result.document_pages,
        "substitution_guide_rows": result.substitution_guide_rows,
        "contract_clauses": result.contract_clauses,
        "ifu_product_facts": result.ifu_product_facts,
    }


def _validate_value(field: Field, value: Any, dataset: str, row_number: int) -> None:
    if value is None:
        if field.nullable:
            return
        raise PublicationValidationError(
            f"{dataset} row {row_number} has NULL required field {field.name}"
        )
    valid = False
    if field.kind in {"string", "date"}:
        valid = isinstance(value, str) and bool(value)
        if valid and field.kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                valid = False
    elif field.kind == "bigint":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif field.kind == "boolean":
        valid = isinstance(value, bool)
    elif field.kind == "array_string":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not valid:
        raise PublicationValidationError(
            f"{dataset} row {row_number} has invalid {field.kind} field {field.name}"
        )


def validate_records(
    records: Mapping[str, list[dict[str, Any]]],
    source_documents: Mapping[str, str],
) -> None:
    """Validate all four datasets as one fail-closed publication unit."""
    if set(records) != set(DATASET_ORDER):
        raise PublicationValidationError(
            f"Expected datasets {list(DATASET_ORDER)}; found {sorted(records)}"
        )
    if set(source_documents) != EXPECTED_DOCUMENT_IDS:
        raise PublicationValidationError("Source document identities are incomplete or conflicting")
    if any(not SHA256.fullmatch(value) for value in source_documents.values()):
        raise PublicationValidationError("Source document hashes must be lowercase SHA-256 values")

    for dataset in DATASET_ORDER:
        rows = records[dataset]
        if len(rows) != EXPECTED_COUNTS[dataset]:
            raise PublicationValidationError(
                f"{dataset} expected {EXPECTED_COUNTS[dataset]} rows; found {len(rows)}"
            )
        fields = DATASET_SCHEMAS[dataset]
        expected_fields = {field.name for field in fields}
        key_field = KEY_FIELDS[dataset]
        keys: set[str] = set()
        for row_number, row in enumerate(rows, start=1):
            if set(row) != expected_fields:
                missing = sorted(expected_fields - set(row))
                unexpected = sorted(set(row) - expected_fields)
                raise PublicationValidationError(
                    f"{dataset} row {row_number} schema mismatch: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            for field in fields:
                _validate_value(field, row[field.name], dataset, row_number)
            key = row[key_field]
            if key in keys:
                raise PublicationValidationError(f"{dataset} has duplicate key {key}")
            keys.add(key)
            document_id = row["document_id"]
            if document_id not in source_documents:
                raise PublicationValidationError(
                    f"{dataset} references unknown document {document_id}"
                )
            if row["content_sha256"] != source_documents[document_id]:
                raise PublicationValidationError(
                    f"{dataset} source hash conflicts for document {document_id}"
                )
            if row["page_number"] < 1:
                raise PublicationValidationError(f"{dataset} has an invalid page citation")
            if row["extraction_status"] != "extracted":
                raise PublicationValidationError(
                    f"{dataset} contains non-publishable extraction status"
                )

    pages = records["document_pages"]
    page_documents = {row["document_id"] for row in pages}
    if page_documents != EXPECTED_DOCUMENT_IDS or len(page_documents) != len(pages):
        raise PublicationValidationError("Document page coverage must be exactly one page per manifest document")
    for row in pages:
        if PRICE_PATTERN.search(row["retrieval_text"]):
            raise PublicationValidationError(
                f"Restricted pricing detected in page retrieval text for {row['document_id']}"
            )
        if row["sensitivity"] == "restricted_pricing" and not row["restricted_pricing_redacted"]:
            raise PublicationValidationError(
                f"Restricted page was not marked redacted: {row['document_id']}"
            )

    clauses = records["contract_clauses"]
    if any(PRICE_PATTERN.search(row["clause_text"]) for row in clauses):
        raise PublicationValidationError("Restricted pricing detected in contract clauses")

    scanned_rows = [
        row
        for row in records["substitution_guide_rows"]
        if row["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
    ]
    if len(scanned_rows) != 1:
        raise PublicationValidationError("Expected exactly one scanned supplement row")
    scanned = scanned_rows[0]
    if (
        scanned["row_status"] != "PENDING CLINICAL REVIEW"
        or scanned["extraction_method"] != "tesseract_ocr"
        or "clinical_review_required" not in scanned["review_reasons"]
    ):
        raise PublicationValidationError(
            "Scanned supplement must retain OCR provenance and pending clinical review"
        )


def _source_documents(records: Mapping[str, list[dict[str, Any]]]) -> dict[str, str]:
    documents: dict[str, str] = {}
    for row in records["document_pages"]:
        document_id = row["document_id"]
        content_hash = row["content_sha256"]
        if document_id in documents and documents[document_id] != content_hash:
            raise PublicationValidationError(f"Conflicting source hashes for {document_id}")
        documents[document_id] = content_hash
    return dict(sorted(documents.items()))


def _artifact_set_id(source_manifest_sha256: str, datasets: Mapping[str, Mapping[str, Any]]) -> str:
    identity = {
        "format_version": ARTIFACT_FORMAT_VERSION,
        "source_manifest_sha256": source_manifest_sha256,
        "datasets": datasets,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_artifact_set(
    manifest_path: Path,
    pdf_directory: Path,
    output_root: Path,
    ocr: OcrFunction | None,
) -> ArtifactSet:
    """Extract from PDFs, validate, and atomically create a content-addressed artifact set."""
    result = extract_documents(manifest_path, pdf_directory, ocr=ocr)
    records = _result_records(result)
    source_documents = _source_documents(records)
    validate_records(records, source_documents)
    output_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".document_artifacts_", dir=output_root) as temporary:
        temporary_path = Path(temporary)
        paths = write_datasets(result, temporary_path)
        datasets: dict[str, dict[str, Any]] = {}
        for name in DATASET_ORDER:
            path = paths[name]
            datasets[name] = {
                "file_name": path.name,
                "sha256": _sha256(path),
                "row_count": len(records[name]),
                "key_field": KEY_FIELDS[name],
            }
        source_manifest_sha256 = _sha256(manifest_path)
        artifact_set_id = _artifact_set_id(source_manifest_sha256, datasets)
        publication_manifest = {
            "format_version": ARTIFACT_FORMAT_VERSION,
            "artifact_set_id": artifact_set_id,
            "source_manifest_sha256": source_manifest_sha256,
            "source_documents": source_documents,
            "datasets": datasets,
        }
        (temporary_path / "publication_manifest.json").write_text(
            json.dumps(publication_manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        target = output_root / artifact_set_id
        if target.exists():
            existing = load_artifact_set(target, expected_artifact_set_id=artifact_set_id)
            if existing.records != records:
                raise PublicationValidationError(
                    f"Existing artifact set conflicts with deterministic output: {artifact_set_id}"
                )
            return existing
        temporary_path.rename(target)

    return load_artifact_set(target, expected_artifact_set_id=artifact_set_id)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicationValidationError(
                    f"Malformed JSON in {path.name} line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise PublicationValidationError(
                    f"Expected an object in {path.name} line {line_number}"
                )
            rows.append(value)
    return rows


def load_artifact_set(path: Path, expected_artifact_set_id: str) -> ArtifactSet:
    """Load and fully validate an immutable local or Unity Catalog Volume artifact set."""
    if not SAFE_ARTIFACT_SET_ID.fullmatch(expected_artifact_set_id):
        raise PublicationValidationError("Invalid artifact_set_id")
    if path.name != expected_artifact_set_id:
        raise PublicationValidationError("Artifact directory does not match artifact_set_id")
    manifest_path = path / "publication_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationValidationError("Cannot load publication_manifest.json") from exc
    expected_manifest_fields = {
        "format_version",
        "artifact_set_id",
        "source_manifest_sha256",
        "source_documents",
        "datasets",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_manifest_fields:
        raise PublicationValidationError("Publication manifest schema is invalid")
    if manifest["format_version"] != ARTIFACT_FORMAT_VERSION:
        raise PublicationValidationError("Unsupported publication artifact format")
    if manifest["artifact_set_id"] != expected_artifact_set_id:
        raise PublicationValidationError("Publication manifest artifact_set_id mismatch")
    if not isinstance(manifest["datasets"], dict) or set(manifest["datasets"]) != set(DATASET_ORDER):
        raise PublicationValidationError("Publication manifest dataset inventory is invalid")
    if not isinstance(manifest["source_documents"], dict):
        raise PublicationValidationError("Publication manifest source documents are invalid")

    computed_id = _artifact_set_id(
        manifest["source_manifest_sha256"], manifest["datasets"]
    )
    if computed_id != expected_artifact_set_id:
        raise PublicationValidationError("Publication manifest content identity mismatch")

    records: dict[str, list[dict[str, Any]]] = {}
    for name in DATASET_ORDER:
        metadata = manifest["datasets"][name]
        expected_metadata = {"file_name", "sha256", "row_count", "key_field"}
        if not isinstance(metadata, dict) or set(metadata) != expected_metadata:
            raise PublicationValidationError(f"Invalid artifact metadata for {name}")
        if metadata["file_name"] != f"{name}.jsonl" or metadata["key_field"] != KEY_FIELDS[name]:
            raise PublicationValidationError(f"Unexpected artifact identity for {name}")
        artifact_path = path / metadata["file_name"]
        if not artifact_path.is_file() or _sha256(artifact_path) != metadata["sha256"]:
            raise PublicationValidationError(f"Artifact hash mismatch for {name}")
        rows = _load_jsonl(artifact_path)
        if len(rows) != metadata["row_count"]:
            raise PublicationValidationError(f"Artifact count mismatch for {name}")
        records[name] = rows

    source_documents = {
        str(key): str(value) for key, value in manifest["source_documents"].items()
    }
    validate_records(records, source_documents)
    return ArtifactSet(expected_artifact_set_id, path, records, source_documents)


def _identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise PublicationValidationError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _schema_ddl(fields: Iterable[Field]) -> str:
    spark_types = {
        "string": "STRING",
        "date": "DATE",
        "bigint": "BIGINT",
        "boolean": "BOOLEAN",
        "array_string": "ARRAY<STRING>",
    }
    return ", ".join(f"`{field.name}` {spark_types[field.kind]}" for field in fields)


def _spark_records(rows: list[dict[str, Any]], fields: Iterable[Field]) -> list[dict[str, Any]]:
    date_fields = {field.name for field in fields if field.kind == "date"}
    return [
        {
            key: date.fromisoformat(value) if key in date_fields else value
            for key, value in row.items()
        }
        for row in rows
    ]


def publish_artifact_set(
    spark: Any,
    artifact_root: str,
    artifact_set_id: str,
    catalog: str = "workspace",
    silver_schema: str = "crestline_silver",
) -> ArtifactSet:
    """Validate all inputs, prepare all views, then replace exactly four Delta tables."""
    if not SAFE_ARTIFACT_SET_ID.fullmatch(artifact_set_id):
        raise PublicationValidationError("artifact_set_id must be a 64-character lowercase SHA-256")
    root = Path(artifact_root)
    artifact_set = load_artifact_set(
        root / artifact_set_id,
        expected_artifact_set_id=artifact_set_id,
    )
    spark.conf.set("spark.sql.session.timeZone", "UTC")

    views: dict[str, str] = {}
    for name in DATASET_ORDER:
        view = f"_mediassist_document_publish_{name}"
        fields = DATASET_SCHEMAS[name]
        frame = spark.createDataFrame(
            _spark_records(artifact_set.records[name], fields),
            schema=_schema_ddl(fields),
        )
        if frame.count() != EXPECTED_COUNTS[name]:
            raise PublicationValidationError(f"Spark row reconciliation failed for {name}")
        frame.createOrReplaceTempView(view)
        views[name] = view

    for name in DATASET_ORDER:
        target = ".".join(
            (_identifier(catalog), _identifier(silver_schema), _identifier(name))
        )
        spark.sql(
            f"CREATE OR REPLACE TABLE {target}\n"
            "USING DELTA\n"
            f"TBLPROPERTIES ('mediassist.artifact_set_id' = '{artifact_set_id}')\n"
            f"AS SELECT * FROM {_identifier(views[name])}"
        )
    return artifact_set
