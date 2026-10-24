from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

import crestline_documents.retrieval_main as entry_point
from crestline_documents.publication import DATASET_SCHEMAS
from crestline_documents.retrieval import (
    EXPECTED_CHUNKS,
    EXPECTED_GUIDE_ROWS,
    EXPECTED_IFU_FACTS,
    OUTPUT_COLUMNS,
    RetrievalValidationError,
    build_ifu_semantics_check_sql,
    build_projection_sql,
    build_retrieval_integrity_check_sql,
    build_source_distribution_check_sql,
    run_retrieval_preparation,
)


class FakeRow:
    def __init__(self, value: int, field: str) -> None:
        self.value = value
        self.field = field

    def __getitem__(self, key: object) -> int:
        if key not in (0, self.field):
            raise KeyError(key)
        return self.value


class FakeResult:
    def __init__(self, row: FakeRow | None = None) -> None:
        self.row = row

    def first(self) -> FakeRow | None:
        return self.row


class FakeConf:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeSpark:
    def __init__(self, *, fail_validation: bool = False, guide_count: int = 11) -> None:
        self.conf = FakeConf()
        self.queries: list[str] = []
        self.fail_validation = fail_validation
        self.guide_count = guide_count

    def sql(self, query: str) -> FakeResult:
        self.queries.append(query)
        if "AS source_count" in query:
            value = self.guide_count if "substitution_guide_rows" in query else 4
            return FakeResult(FakeRow(value, "source_count"))
        if "AS retrieval_count" in query:
            return FakeResult(FakeRow(15, "retrieval_count"))
        if "violation_count" in query:
            value = 1 if self.fail_validation else 0
            return FakeResult(FakeRow(value, "violation_count"))
        return FakeResult()


def test_projection_uses_actual_published_source_columns() -> None:
    guide_fields = {field.name for field in DATASET_SCHEMAS["substitution_guide_rows"]}
    ifu_fields = {field.name for field in DATASET_SCHEMAS["ifu_product_facts"]}
    assert {
        "substitution_row_id",
        "requested_sku",
        "candidate_sku",
        "row_status",
        "required_attributes",
        "clinical_note",
    } <= guide_fields
    assert {
        "ifu_fact_id",
        "product_sku",
        "gauge",
        "device_length_mm",
        "latex_free",
        "sterile",
        "single_use",
        "establishes_interchangeability",
    } <= ifu_fields


def test_projection_keeps_complete_structure_aware_facts_and_stable_ids() -> None:
    sql = build_projection_sql("guide_source", "ifu_source")
    assert "concat('substitution:', substitution_row_id) AS chunk_id" in sql
    assert "concat('ifu:', ifu_fact_id) AS chunk_id" in sql
    assert "Requested SKU: " in sql and "Candidate SKU: " in sql
    assert "Row status: " in sql and "Required attributes: " in sql
    assert "Clinical note: " in sql
    assert "Establishes interchangeability: " in sql
    assert "UNION ALL" in sql
    assert "contract_clauses" not in sql
    assert "document_pages" not in sql
    assert len(OUTPUT_COLUMNS) == len(set(OUTPUT_COLUMNS))


def test_validations_require_exact_counts_provenance_and_safe_content() -> None:
    guide_check = build_source_distribution_check_sql("guide_source", "guide")
    ifu_check = build_source_distribution_check_sql("ifu_source", "ifu")
    safety_check = build_retrieval_integrity_check_sql("chunks")
    assert "= 3" in guide_check and "= 7" in guide_check and "= 1" in guide_check
    assert all(document_id in ifu_check for document_id in (
        "IFU_AV-IV22-BLU-LF",
        "IFU_BR-IV20-PNK-LF",
        "IFU_BR-IV22-BLU-LF",
        "IFU_BR-IV22-BLU-LX",
    ))
    assert "CONTRACT|AMENDMENT" in safety_check
    assert "confidential negotiated pricing" in safety_check
    assert "establishes_interchangeability <> false" in build_ifu_semantics_check_sql(
        "ifu_source"
    )


def test_publication_is_exact_idempotent_and_cdf_enabled() -> None:
    spark = FakeSpark()
    first = run_retrieval_preparation(spark)
    second = run_retrieval_preparation(spark)
    writes = [query for query in spark.queries if "CREATE OR REPLACE TABLE" in query]
    assert first == second
    assert first.guide_rows == EXPECTED_GUIDE_ROWS
    assert first.ifu_facts == EXPECTED_IFU_FACTS
    assert first.total_chunks == EXPECTED_CHUNKS
    assert len(writes) == 2
    assert all("'delta.enableChangeDataFeed' = 'true'" in query for query in writes)
    assert all("AS SELECT" in query for query in writes)
    assert not any("INSERT INTO" in query or " APPEND " in query.upper() for query in writes)


def test_incomplete_source_fails_before_view_or_table_write() -> None:
    spark = FakeSpark(guide_count=10)
    with pytest.raises(RetrievalValidationError, match="exactly 11 guide rows"):
        run_retrieval_preparation(spark)
    assert not any("CREATE OR REPLACE" in query for query in spark.queries)


def test_conflicting_source_fails_before_view_or_table_write() -> None:
    spark = FakeSpark(fail_validation=True)
    with pytest.raises(RetrievalValidationError, match="violation"):
        run_retrieval_preparation(spark)
    assert not any("CREATE OR REPLACE" in query for query in spark.queries)


def test_wheel_entry_point_is_zero_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entry_point, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_document_retrieval",
            "--catalog",
            "workspace",
            "--silver_schema",
            "crestline_silver",
        ],
    )
    entry_point.main()
    assert calls == [{"catalog": "workspace", "silver_schema": "crestline_silver"}]


def test_bundle_job_reuses_serverless_wheel_pattern() -> None:
    with open("resources/document_retrieval_job.yml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    job = config["resources"]["jobs"]["document_retrieval_chunks"]
    assert len(job["tasks"]) == 1
    task = job["tasks"][0]
    assert task["python_wheel_task"]["entry_point"] == "prepare_document_retrieval"
    assert task["environment_key"] == "default"
    assert job["environments"][0]["spec"]["dependencies"] == [
        "../dist/mediassist_bronze-*.whl"
    ]


def test_verification_sql_covers_counts_keys_provenance_safety_and_cdf() -> None:
    sql = Path("sql/document_retrieval_verification.sql").read_text(encoding="utf-8")
    assert "count(DISTINCT chunk_id)" in sql
    assert "HAVING count(*) > 1" in sql
    assert "content_sha256 NOT RLIKE '^[0-9a-f]{64}$'" in sql
    assert "CONTRACT|AMENDMENT" in sql
    assert "establishes_interchangeability <> false" in sql
    assert "PENDING CLINICAL REVIEW" in sql
    assert "delta.enableChangeDataFeed" in sql
