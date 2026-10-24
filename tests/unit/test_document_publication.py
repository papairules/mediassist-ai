from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

import crestline_documents.publish_main as entry_point
from crestline_documents.extract import extract_documents
from crestline_documents.publication import (
    DATASET_ORDER,
    EXPECTED_COUNTS,
    ArtifactSet,
    PublicationValidationError,
    load_artifact_set,
    prepare_artifact_set,
    publish_artifact_set,
    validate_records,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "crestline_synthetic_v1/source_csv/document_manifest.csv"
PDF_DIRECTORY = ROOT / "crestline_synthetic_v1/source_pdfs"
SCANNED_TEXT = """SCANNED - Syringe Substitution Supplement
Document: SUB-SCAN-2026-01 | Effective: 2026-10-10
Original SKU: AV-SYR10-LF
Proposed substitute: BR-SYR10-LF
Approval status: PENDING CLINICAL REVIEW
Site policy and full manufacturer IFU review required.
Never auto-approve based on a scanned row or old ticket.
SYNTHETIC TRAINING DATA - NOT FOR CLINICAL USE"""


def _fake_ocr(path: Path, page_number: int) -> str:
    assert path.name == "SUB_SUPPLEMENT_SCANNED_2026.pdf"
    assert page_number == 1
    return SCANNED_TEXT


@pytest.fixture(scope="module")
def artifact_set(tmp_path_factory: pytest.TempPathFactory) -> ArtifactSet:
    root = tmp_path_factory.mktemp("document_publication")
    return prepare_artifact_set(MANIFEST, PDF_DIRECTORY, root, ocr=_fake_ocr)


def test_prepared_set_has_exact_schema_counts_and_manifest_identity(
    artifact_set: ArtifactSet,
) -> None:
    assert len(artifact_set.artifact_set_id) == 64
    assert set(artifact_set.records) == set(DATASET_ORDER)
    assert {
        name: len(records) for name, records in artifact_set.records.items()
    } == EXPECTED_COUNTS
    assert len(artifact_set.source_documents) == 13
    assert (artifact_set.path / "publication_manifest.json").is_file()
    validate_records(artifact_set.records, artifact_set.source_documents)


def test_preparation_is_content_addressed_and_repeatable(
    artifact_set: ArtifactSet,
) -> None:
    repeated = prepare_artifact_set(
        MANIFEST,
        PDF_DIRECTORY,
        artifact_set.path.parent,
        ocr=_fake_ocr,
    )
    assert repeated.artifact_set_id == artifact_set.artifact_set_id
    assert repeated.path == artifact_set.path
    assert repeated.records == artifact_set.records


def test_schema_validation_rejects_missing_and_unexpected_fields(
    artifact_set: ArtifactSet,
) -> None:
    records = copy.deepcopy(artifact_set.records)
    del records["document_pages"][0]["page_number"]
    records["document_pages"][0]["raw_contract_text"] = "must never publish"
    with pytest.raises(PublicationValidationError, match="schema mismatch"):
        validate_records(records, artifact_set.source_documents)


def test_completeness_validation_rejects_missing_rows(artifact_set: ArtifactSet) -> None:
    records = copy.deepcopy(artifact_set.records)
    records["ifu_product_facts"].pop()
    with pytest.raises(PublicationValidationError, match="expected 4 rows; found 3"):
        validate_records(records, artifact_set.source_documents)


def test_unique_fact_keys_are_required(artifact_set: ArtifactSet) -> None:
    records = copy.deepcopy(artifact_set.records)
    records["contract_clauses"][1]["contract_clause_id"] = records["contract_clauses"][0][
        "contract_clause_id"
    ]
    with pytest.raises(PublicationValidationError, match="duplicate key"):
        validate_records(records, artifact_set.source_documents)


def test_restricted_pricing_cannot_cross_publication_boundary(
    artifact_set: ArtifactSet,
) -> None:
    records = copy.deepcopy(artifact_set.records)
    contract_page = next(
        row for row in records["document_pages"] if row["sensitivity"] == "restricted_pricing"
    )
    contract_page["retrieval_text"] += "\nConfidential negotiated pricing: $18.75"
    with pytest.raises(PublicationValidationError, match="Restricted pricing detected"):
        validate_records(records, artifact_set.source_documents)


def test_scanned_row_cannot_be_promoted_to_approved(artifact_set: ArtifactSet) -> None:
    records = copy.deepcopy(artifact_set.records)
    scanned = next(
        row
        for row in records["substitution_guide_rows"]
        if row["document_id"] == "SUB_SUPPLEMENT_SCANNED_2026"
    )
    scanned["row_status"] = "APPROVED"
    with pytest.raises(PublicationValidationError, match="pending clinical review"):
        validate_records(records, artifact_set.source_documents)


def test_mixed_or_tampered_artifact_set_fails_before_spark_writes(
    artifact_set: ArtifactSet,
    tmp_path: Path,
) -> None:
    copied = tmp_path / artifact_set.artifact_set_id
    shutil.copytree(artifact_set.path, copied)
    page_file = copied / "document_pages.jsonl"
    page_file.write_text(page_file.read_text() + "{}\n")
    spark = FakeSpark()

    with pytest.raises(PublicationValidationError, match="Artifact hash mismatch"):
        publish_artifact_set(
            spark,
            str(tmp_path),
            artifact_set.artifact_set_id,
        )
    assert spark.queries == []
    assert spark.frames == []


class FakeConf:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeFrame:
    def __init__(self, rows: list[dict[str, Any]], views: list[str]) -> None:
        self.rows = rows
        self.views = views

    def count(self) -> int:
        return len(self.rows)

    def createOrReplaceTempView(self, name: str) -> None:
        self.views.append(name)


class FakeSpark:
    def __init__(self) -> None:
        self.conf = FakeConf()
        self.frames: list[tuple[list[dict[str, Any]], str]] = []
        self.views: list[str] = []
        self.queries: list[str] = []

    def createDataFrame(self, rows: list[dict[str, Any]], schema: str) -> FakeFrame:
        self.frames.append((rows, schema))
        return FakeFrame(rows, self.views)

    def sql(self, query: str) -> None:
        self.queries.append(query)


def test_publication_is_idempotent_create_or_replace(artifact_set: ArtifactSet) -> None:
    spark = FakeSpark()
    for _ in range(2):
        publish_artifact_set(
            spark,
            str(artifact_set.path.parent),
            artifact_set.artifact_set_id,
        )

    assert len(spark.queries) == 8
    assert len(spark.frames) == 8
    assert spark.conf.values == {"spark.sql.session.timeZone": "UTC"}
    for dataset in DATASET_ORDER:
        matching = [query for query in spark.queries if f".`{dataset}`" in query]
        assert len(matching) == 2
        assert all("CREATE OR REPLACE TABLE" in query for query in matching)
        assert all("USING DELTA" in query for query in matching)
        assert all(artifact_set.artifact_set_id in query for query in matching)
    assert not any("INSERT INTO" in query or "APPEND" in query.upper() for query in spark.queries)


def test_all_frames_are_prepared_before_first_table_write(artifact_set: ArtifactSet) -> None:
    events: list[str] = []

    class OrderedFrame(FakeFrame):
        def createOrReplaceTempView(self, name: str) -> None:
            events.append(f"view:{name}")

    class OrderedSpark(FakeSpark):
        def createDataFrame(self, rows: list[dict[str, Any]], schema: str) -> FakeFrame:
            return OrderedFrame(rows, self.views)

        def sql(self, query: str) -> None:
            events.append("write")

    publish_artifact_set(
        OrderedSpark(),
        str(artifact_set.path.parent),
        artifact_set.artifact_set_id,
    )
    assert events[:4] == [
        f"view:_mediassist_document_publish_{name}" for name in DATASET_ORDER
    ]
    assert events[4:] == ["write"] * 4


def test_document_publication_wheel_entry_point_is_zero_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(entry_point, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish_document_tables",
            "--artifact_root",
            "/Volumes/workspace/crestline_bronze/mediassist_ingestion/document_artifacts",
            "--artifact_set_id",
            "a" * 64,
            "--catalog",
            "workspace",
            "--silver_schema",
            "crestline_silver",
        ],
    )
    entry_point.main()
    assert calls == [
        {
            "artifact_root": "/Volumes/workspace/crestline_bronze/mediassist_ingestion/document_artifacts",
            "artifact_set_id": "a" * 64,
            "catalog": "workspace",
            "silver_schema": "crestline_silver",
        }
    ]


def test_bundle_defines_one_serverless_document_publication_task() -> None:
    resource = yaml.safe_load((ROOT / "resources/document_publication_job.yml").read_text())
    job = resource["resources"]["jobs"]["document_publication"]
    assert job["max_concurrent_runs"] == 1
    assert len(job["tasks"]) == 1
    task = job["tasks"][0]
    assert task["environment_key"] == "default"
    assert task["python_wheel_task"]["entry_point"] == "publish_document_tables"
    assert task["python_wheel_task"]["named_parameters"]["silver_schema"] == "${var.silver_schema}"
    assert job["environments"][0]["spec"]["dependencies"] == [
        "../dist/mediassist_bronze-*.whl"
    ]


def test_publication_manifest_is_plain_json(artifact_set: ArtifactSet) -> None:
    manifest = json.loads((artifact_set.path / "publication_manifest.json").read_text())
    assert manifest["artifact_set_id"] == artifact_set.artifact_set_id
    assert manifest["datasets"]["document_pages"]["row_count"] == 13


def test_extraction_output_itself_passes_publication_contract() -> None:
    result = extract_documents(MANIFEST, PDF_DIRECTORY, ocr=_fake_ocr)
    records = {
        "document_pages": result.document_pages,
        "substitution_guide_rows": result.substitution_guide_rows,
        "contract_clauses": result.contract_clauses,
        "ifu_product_facts": result.ifu_product_facts,
    }
    source_documents = {
        row["document_id"]: row["content_sha256"] for row in result.document_pages
    }
    validate_records(records, source_documents)
