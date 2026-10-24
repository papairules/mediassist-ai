from __future__ import annotations

import re
import sys
from typing import Any

import pytest

import crestline_gold.main as entry_point
from crestline_bronze.config import load_registry
from crestline_gold.deploy import (
    EXPECTED_VIEWS,
    GoldConfigurationError,
    deploy_gold_views,
    load_gold_sql,
    render_gold_sql,
    split_sql_statements,
)
from crestline_silver.rules import silver_table_name


class _Conf:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeSpark:
    def __init__(self) -> None:
        self.conf = _Conf()
        self.queries: list[str] = []

    def sql(self, query: str) -> None:
        self.queries.append(query)


def _split_projection(projection: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for index, character in enumerate(projection):
        if character == "'":
            quoted = not quoted
        elif not quoted and character == "(":
            depth += 1
        elif not quoted and character == ")":
            depth -= 1
        elif not quoted and character == "," and depth == 0:
            items.append(projection[start:index].strip())
            start = index + 1
    items.append(projection[start:].strip())
    return items


def _final_projection(sql: str) -> list[str]:
    select_matches = list(re.finditer(r"(?mi)^\s*SELECT\s*$", sql))
    assert select_matches
    projection_at = select_matches[-1].end()
    from_match = re.search(r"(?mi)^\s*FROM\s+", sql[projection_at:])
    assert from_match is not None
    from_at = projection_at + from_match.start()
    return _split_projection(sql[projection_at:from_at])


def _cte_sql(sql: str, cte_name: str) -> str:
    marker = f"{cte_name} AS ("
    content_at = sql.index(marker) + len(marker)
    depth = 1
    quoted = False
    for index in range(content_at, len(sql)):
        character = sql[index]
        if character == "'":
            quoted = not quoted
        elif not quoted and character == "(":
            depth += 1
        elif not quoted and character == ")":
            depth -= 1
            if depth == 0:
                return sql[content_at:index]
    raise AssertionError(f"Unclosed CTE: {cte_name}")


def _output_name(expression: str) -> str:
    alias = re.search(r"\bAS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*$", expression, re.I)
    if alias:
        return alias.group(1).lower()
    column = re.search(r"(?:^|\.)`?([A-Za-z_][A-Za-z0-9_]*)`?\s*$", expression)
    if column:
        return column.group(1).lower()
    raise AssertionError(f"Cannot resolve output name: {expression}")


def _resolved_output_names(statement: str) -> list[str]:
    names: list[str] = []
    for expression in _final_projection(statement):
        if expression == "line_facts.*":
            names.extend(
                _output_name(item)
                for item in _final_projection(_cte_sql(statement, "line_facts"))
            )
        else:
            names.append(_output_name(expression))
    return names


def test_packaged_sql_defines_exactly_three_views_and_no_tables() -> None:
    sql = load_gold_sql()
    statements = split_sql_statements(sql)
    assert len(statements) == 3
    assert sql.upper().count("CREATE OR REPLACE VIEW") == 3
    assert "CREATE TABLE" not in sql.upper()
    assert "CREATE OR REPLACE TABLE" not in sql.upper()
    assert tuple(
        name for name in EXPECTED_VIEWS if f"`{name}`" in sql
    ) == EXPECTED_VIEWS


def test_every_gold_view_has_unique_final_output_column_names() -> None:
    for view_name, statement in zip(EXPECTED_VIEWS, render_gold_sql()):
        names = _resolved_output_names(statement)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert duplicates == [], f"{view_name} has duplicate columns: {duplicates}"


def test_gold_sql_renders_only_safe_identifiers() -> None:
    statements = render_gold_sql("workspace", "crestline_silver", "crestline_gold")
    assert all("{{" not in statement for statement in statements)
    assert "`workspace`.`crestline_silver`.`shipments`" in statements[0]
    assert "`workspace`.`crestline_gold`.`order_line_fulfillment`" in statements[0]
    with pytest.raises(GoldConfigurationError, match="Unsafe SQL identifier"):
        render_gold_sql("workspace; DROP SCHEMA x", "silver", "gold")


def test_gold_sql_has_required_preaggregation_and_evidence_semantics() -> None:
    fulfillment, eta, inventory = render_gold_sql()

    assert "shipment_rollup AS" in fulfillment
    assert "GROUP BY s.order_id, s.line_id" in fulfillment
    assert fulfillment.index("shipment_rollup AS") < fulfillment.index(
        "LEFT JOIN shipment_rollup"
    )
    assert "sort_array(" in fulfillment
    assert "'tracking_id', s.tracking_id" in fulfillment
    assert "'carrier_estimated_arrival', s.carrier_estimated_arrival" in fulfillment
    assert "quantity_shipped > quantity_ordered" in fulfillment
    assert "over_shipped_data_issue" in fulfillment

    assert "PARTITION BY e.order_id, e.line_id" in eta
    assert "e.supplier_reported_at DESC" in eta
    assert "e.ingested_at DESC" in eta
    assert "WHERE current_record_rank = 1" in eta
    assert (
        "c.expected_supplier_restock_date AS current_supplier_restock_date" in eta
    )
    assert "supplier_eta_revision_history" in eta
    assert "hospital_delivery" not in eta.lower()

    assert "PARTITION BY i.warehouse_id, i.sku" in inventory
    assert "WHERE current_snapshot_rank = 1" in inventory
    assert inventory.index("WHERE current_snapshot_rank = 1") < inventory.index(
        "sum(i.quantity_on_hand)"
    )
    assert "i.sku IS NOT NULL AS has_inventory_observation" in inventory
    assert "coalesce(i.quantity_available" not in inventory.lower()


def test_gold_sql_references_actual_silver_tables_and_columns() -> None:
    sql = load_gold_sql()
    referenced_tables = {
        "hospitals",
        "products",
        "orders",
        "order_lines",
        "shipments",
        "inventory_snapshots",
        "supplier_etas",
    }
    actual_tables = {
        silver_table_name(source.source_id) for source in load_registry().sources
    }
    assert referenced_tables <= actual_tables
    for table in referenced_tables:
        assert f"{{{{silver_schema}}}}.`{table}`" in sql

    source_columns = {
        source.source_id: set(source.column_names) for source in load_registry().sources
    }
    required_columns = {
        "order_lines": {"order_id", "line_id", "sku", "quantity_ordered"},
        "shipments": {
            "shipment_id",
            "quantity_shipped",
            "carrier_estimated_arrival",
            "delivered_at",
        },
        "supplier_etas": {
            "source_record_id",
            "expected_supplier_restock_date",
            "supplier_reported_at",
            "ingested_at",
        },
        "inventory": {
            "warehouse_id",
            "snapshot_at",
            "quantity_on_hand",
            "quantity_reserved",
        },
    }
    for source_id, columns in required_columns.items():
        assert columns <= source_columns[source_id]
        assert all(column in sql for column in columns)

    for provenance_column in (
        "_record_id",
        "_source_file_name",
        "_source_file_modified_at",
        "_ingested_at",
    ):
        assert provenance_column in sql

    assert "rep_access_demo_only" not in sql
    assert "document_manifest" not in sql
    assert "pricing" not in sql.lower()


def test_deployer_executes_three_statements_in_dependency_order() -> None:
    spark = FakeSpark()
    deploy_gold_views(spark)
    assert spark.conf.values == {"spark.sql.session.timeZone": "UTC"}
    assert len(spark.queries) == 3
    for query, view_name in zip(spark.queries, EXPECTED_VIEWS):
        assert f"`{view_name}`" in query


def test_gold_wheel_entry_point_is_zero_argument(
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
            "run_gold_views",
            "--catalog",
            "workspace",
            "--silver_schema",
            "crestline_silver",
            "--gold_schema",
            "crestline_gold",
        ],
    )
    entry_point.main()
    assert calls == [
        {
            "catalog": "workspace",
            "silver_schema": "crestline_silver",
            "gold_schema": "crestline_gold",
        }
    ]
