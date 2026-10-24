from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import crestline_silver.transform as silver
from crestline_bronze.config import ColumnConfig, load_registry


class _Result:
    def __init__(self, row: dict[str, int] | None = None) -> None:
        self._row = row

    def first(self) -> dict[str, int] | None:
        return self._row


class _Countable:
    def count(self) -> int:
        return 1


class _Conf:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeSpark:
    def __init__(self, violation_fragment: str | None = None) -> None:
        self.violation_fragment = violation_fragment
        self.queries: list[str] = []
        self.conf = _Conf()

    def sql(self, query: str) -> _Result:
        self.queries.append(query)
        if " AS duplicate_count" in query:
            return _Result({"duplicate_count": 0})
        if " AS violation_count" in query:
            count = int(
                self.violation_fragment is not None
                and self.violation_fragment in query
            )
            return _Result({"violation_count": count})
        return _Result()

    def table(self, _: str) -> _Countable:
        return _Countable()


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (ColumnConfig("sku", "string", True), "nullif(trim(`sku`), '')"),
        (ColumnConfig("quantity", "integer", True), "AS BIGINT"),
        (ColumnConfig("active", "boolean", True), "AS BOOLEAN"),
        (ColumnConfig("effective", "date", True), "AS DATE"),
        (ColumnConfig("loaded", "timestamp", True), "AS TIMESTAMP"),
    ],
)
def test_column_expressions_trim_null_and_cast(
    column: ColumnConfig, expected: str
) -> None:
    assert expected in silver.column_expression(column)


def test_type_validation_rejects_nonempty_failed_casts() -> None:
    products = load_registry().enabled_sources("products")[0]
    query = silver.build_type_check_sql(products, "`catalog`.`bronze`.`products_raw`")
    assert "try_cast(nullif(trim(`gauge`), '') AS BIGINT) IS NULL" in query
    assert "try_cast(nullif(trim(`latex_free`), '') AS BOOLEAN) IS NULL" in query


def test_deduplication_uses_business_key_and_deterministic_precedence() -> None:
    etas = load_registry().enabled_sources("supplier_etas")[0]
    query = silver.build_deduplicated_sql(etas, "etas_normalized")
    assert "PARTITION BY `source_record_id`" in query
    assert "`_source_file_modified_at` DESC NULLS LAST" in query
    assert "`_ingested_at` DESC NULLS LAST" in query
    assert "`_record_id` DESC" in query
    assert "PARTITION BY `order_id`, `line_id`" not in query


def test_materially_conflicting_duplicates_are_not_silently_dropped() -> None:
    orders = load_registry().enabled_sources("orders")[0]
    query = silver.build_ambiguous_duplicate_check_sql(orders, "orders_normalized")
    assert "GROUP BY `order_id`" in query
    assert "count(DISTINCT to_json(named_struct(" in query
    assert "'hospital_id', `hospital_id`" in query
    assert "_source_file_modified_at" not in query


def test_required_field_validation_includes_business_keys() -> None:
    lines = load_registry().enabled_sources("order_lines")[0]
    query = silver.build_required_check_sql(lines, "lines_normalized")
    assert "`order_id` IS NULL" in query
    assert "`line_id` IS NULL" in query
    assert "`quantity_ordered` IS NULL" in query


def test_foreign_key_validation_detects_broken_relationships() -> None:
    rule = next(item for item in silver.FOREIGN_KEYS if item.child_source_id == "orders")
    query = silver.build_foreign_key_check_sql(rule, "orders", "hospitals")
    assert "LEFT ANTI JOIN" in query
    assert "child.`hospital_id` = parent.`hospital_id`" in query


def test_nullable_document_relationship_only_checks_scoped_documents() -> None:
    rule = next(
        item
        for item in silver.FOREIGN_KEYS
        if item.child_source_id == "document_manifest"
    )
    query = silver.build_foreign_key_check_sql(rule, "documents", "hospitals")
    assert "child.`hospital_id` IS NOT NULL" in query
    assert "child.`region` IS NOT NULL" in query


@pytest.mark.parametrize(
    "violation_fragment",
    [
        "FROM `_mediassist_silver_normalized_hospitals`\nWHERE `hospital_id` IS NULL",
        "LEFT ANTI JOIN `_mediassist_silver_hospitals`",
    ],
)
def test_validation_failure_occurs_before_any_table_publication(
    violation_fragment: str,
) -> None:
    spark = FakeSpark(violation_fragment)
    with pytest.raises(silver.SilverValidationError):
        silver.run_silver(spark, load_registry())
    assert not any("CREATE OR REPLACE TABLE" in query for query in spark.queries)


def test_serverless_path_avoids_cache_and_rdd_apis() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(silver)))
    unsupported = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"cache", "persist", "unpersist", "rdd"}
    }
    assert unsupported == set()
