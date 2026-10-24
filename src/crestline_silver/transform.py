"""Deterministic batch transformations from Bronze strings to Silver Delta."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Iterable

from crestline_bronze.config import ColumnConfig, Registry, SourceConfig
from crestline_silver.rules import (
    FOREIGN_KEYS,
    ForeignKeyRule,
    silver_table_name,
)


LOGGER = logging.getLogger(__name__)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROVENANCE_COLUMNS = (
    "_source_id",
    "_file_id",
    "_source_record_json",
    "_record_id",
    "_ingested_at",
    "_ingestion_run_id",
    "_source_file_path",
    "_source_file_name",
    "_source_file_size",
    "_source_file_modified_at",
)


class SilverValidationError(RuntimeError):
    """Raised before publication when Silver invariants do not hold."""


@dataclass(frozen=True)
class PreparedTable:
    source: SourceConfig
    silver_table: str
    normalized_view: str
    deduplicated_view: str
    bronze_count: int
    silver_count: int


def _identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise SilverValidationError(f"Unsafe identifier: {value!r}")
    return f"`{value}`"


def _qualified(*parts: str) -> str:
    return ".".join(_identifier(part) for part in parts)


def column_expression(column: ColumnConfig) -> str:
    """Return a safe SQL expression for one registry-declared source column."""
    name = _identifier(column.name)
    normalized = f"nullif(trim({name}), '')"
    if column.data_type == "string":
        value = normalized
    elif column.data_type == "integer":
        value = f"try_cast({normalized} AS BIGINT)"
    elif column.data_type == "boolean":
        value = f"try_cast(lower({normalized}) AS BOOLEAN)"
    elif column.data_type == "date":
        value = f"try_cast({normalized} AS DATE)"
    elif column.data_type == "timestamp":
        value = f"try_cast({normalized} AS TIMESTAMP)"
    else:
        raise SilverValidationError(
            f"Unsupported type for {column.name}: {column.data_type}"
        )
    return f"{value} AS {name}"


def build_normalized_sql(source: SourceConfig, bronze_table: str) -> str:
    selections = [column_expression(column) for column in source.columns]
    selections.extend(_identifier(column) for column in PROVENANCE_COLUMNS)
    projection = ",\n  ".join(selections)
    return f"SELECT\n  {projection}\nFROM {bronze_table}"


def build_type_check_sql(source: SourceConfig, bronze_table: str) -> str:
    """Count non-empty source values that cannot be converted as declared."""
    predicates: list[str] = []
    for column in source.columns:
        if column.data_type == "string":
            continue
        name = _identifier(column.name)
        normalized = f"nullif(trim({name}), '')"
        target_type = {
            "integer": "BIGINT",
            "boolean": "BOOLEAN",
            "date": "DATE",
            "timestamp": "TIMESTAMP",
        }[column.data_type]
        predicates.append(
            f"({normalized} IS NOT NULL AND "
            f"try_cast({normalized} AS {target_type}) IS NULL)"
        )
    predicate = " OR ".join(predicates) or "false"
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {bronze_table}\n"
        f"WHERE {predicate}"
    )


def build_deduplicated_sql(source: SourceConfig, normalized_view: str) -> str:
    selected = [source.column_names, PROVENANCE_COLUMNS]
    columns = tuple(column for group in selected for column in group)
    select_columns = ",\n  ".join(_identifier(column) for column in columns)
    keys = ", ".join(_identifier(key) for key in source.keys)
    return (
        "SELECT\n  "
        f"{select_columns}\n"
        "FROM (\n"
        "  SELECT *,\n"
        "    row_number() OVER (\n"
        f"      PARTITION BY {keys}\n"
        "      ORDER BY `_source_file_modified_at` DESC NULLS LAST,\n"
        "        `_ingested_at` DESC NULLS LAST, `_record_id` DESC\n"
        "    ) AS `_silver_row_number`\n"
        f"  FROM {_identifier(normalized_view)}\n"
        ") ranked\n"
        "WHERE `_silver_row_number` = 1"
    )


def build_required_check_sql(source: SourceConfig, normalized_view: str) -> str:
    required = [column.name for column in source.columns if column.required]
    predicates = " OR ".join(f"{_identifier(name)} IS NULL" for name in required)
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {_identifier(normalized_view)}\n"
        f"WHERE {predicates}"
    )


def build_ambiguous_duplicate_check_sql(
    source: SourceConfig, normalized_view: str
) -> str:
    keys = ", ".join(_identifier(key) for key in source.keys)
    normalized_record = ", ".join(
        f"'{column}', {_identifier(column)}" for column in source.column_names
    )
    return (
        "SELECT count(*) AS violation_count\n"
        "FROM (\n"
        f"  SELECT {keys}\n"
        f"  FROM {_identifier(normalized_view)}\n"
        f"  GROUP BY {keys}\n"
        "  HAVING count(*) > 1\n"
        "    AND count(DISTINCT to_json(named_struct("
        f"{normalized_record}))) > 1\n"
        ") ambiguous"
    )


def build_duplicate_count_sql(source: SourceConfig, normalized_view: str) -> str:
    keys = ", ".join(_identifier(key) for key in source.keys)
    return (
        "SELECT coalesce(sum(row_count - 1), 0) AS duplicate_count\n"
        "FROM (\n"
        f"  SELECT {keys}, count(*) AS row_count\n"
        f"  FROM {_identifier(normalized_view)}\n"
        f"  GROUP BY {keys}\n"
        "  HAVING count(*) > 1\n"
        ") duplicates"
    )


def build_foreign_key_check_sql(
    rule: ForeignKeyRule, child_view: str, parent_view: str
) -> str:
    join = " AND ".join(
        f"child.{_identifier(child)} = parent.{_identifier(parent)}"
        for child, parent in zip(rule.child_columns, rule.parent_columns)
    )
    populated = " AND ".join(
        f"child.{_identifier(column)} IS NOT NULL" for column in rule.child_columns
    )
    where = f"\nWHERE {populated}" if rule.nullable else ""
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {_identifier(child_view)} child\n"
        f"LEFT ANTI JOIN {_identifier(parent_view)} parent ON {join}"
        f"{where}"
    )


def _scalar(spark: Any, sql: str, field: str) -> int:
    row = spark.sql(sql).first()
    if row is None:
        raise SilverValidationError(f"Validation query returned no result: {sql}")
    try:
        value = row[field]
    except (KeyError, TypeError):
        value = row[0]
    return int(value)


def _raise_for_violations(spark: Any, name: str, sql: str) -> None:
    violations = _scalar(spark, sql, "violation_count")
    if violations:
        raise SilverValidationError(f"{name} failed with {violations} violation(s)")


def _validate_registry(registry: Registry) -> dict[str, SourceConfig]:
    sources = {source.source_id: source for source in registry.enabled_sources()}
    related_sources = {
        source_id
        for rule in FOREIGN_KEYS
        for source_id in (rule.child_source_id, rule.parent_source_id)
    }
    missing = related_sources - set(sources)
    if len(sources) != 9 or missing:
        raise SilverValidationError(
            "Silver requires the canonical nine-source registry; "
            f"found {sorted(sources)}, missing relationships for {sorted(missing)}"
        )
    return sources


def _prepare_tables(
    spark: Any,
    sources: dict[str, SourceConfig],
    catalog: str,
    bronze_schema: str,
) -> tuple[PreparedTable, ...]:
    prepared: list[PreparedTable] = []
    for source in sources.values():
        normalized_view = f"_mediassist_silver_normalized_{source.source_id}"
        deduplicated_view = f"_mediassist_silver_{source.source_id}"
        bronze_table = _qualified(catalog, bronze_schema, source.target_table)
        _raise_for_violations(
            spark,
            f"{source.source_id}.declared_types",
            build_type_check_sql(source, bronze_table),
        )
        spark.sql(
            f"CREATE OR REPLACE TEMP VIEW {_identifier(normalized_view)} AS\n"
            f"{build_normalized_sql(source, bronze_table)}"
        )
        _raise_for_violations(
            spark,
            f"{source.source_id}.required_values",
            build_required_check_sql(source, normalized_view),
        )
        _raise_for_violations(
            spark,
            f"{source.source_id}.ambiguous_duplicates",
            build_ambiguous_duplicate_check_sql(source, normalized_view),
        )
        duplicate_count = _scalar(
            spark,
            build_duplicate_count_sql(source, normalized_view),
            "duplicate_count",
        )
        spark.sql(
            f"CREATE OR REPLACE TEMP VIEW {_identifier(deduplicated_view)} AS\n"
            f"{build_deduplicated_sql(source, normalized_view)}"
        )
        bronze_count = int(spark.table(bronze_table).count())
        silver_count = int(spark.table(deduplicated_view).count())
        if bronze_count - duplicate_count != silver_count:
            raise SilverValidationError(
                f"{source.source_id} reconciliation failed: bronze={bronze_count} "
                f"duplicates={duplicate_count} silver={silver_count}"
            )
        LOGGER.info(
            "source=%s bronze_rows=%d duplicates_removed=%d silver_rows=%d",
            source.source_id,
            bronze_count,
            duplicate_count,
            silver_count,
        )
        prepared.append(
            PreparedTable(
                source,
                silver_table_name(source.source_id),
                normalized_view,
                deduplicated_view,
                bronze_count,
                silver_count,
            )
        )
    return tuple(prepared)


def _validate_relationships(
    spark: Any, prepared: Iterable[PreparedTable]
) -> None:
    views = {table.source.source_id: table.deduplicated_view for table in prepared}
    for rule in FOREIGN_KEYS:
        _raise_for_violations(
            spark,
            rule.name,
            build_foreign_key_check_sql(
                rule,
                views[rule.child_source_id],
                views[rule.parent_source_id],
            ),
        )


def run_silver(
    spark: Any,
    registry: Registry,
    catalog: str = "workspace",
    bronze_schema: str = "crestline_bronze",
    silver_schema: str = "crestline_silver",
) -> tuple[PreparedTable, ...]:
    """Validate all nine sources, then publish deterministic Silver tables."""
    sources = _validate_registry(registry)
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    prepared = _prepare_tables(spark, sources, catalog, bronze_schema)
    _validate_relationships(spark, prepared)

    for table in prepared:
        target = _qualified(catalog, silver_schema, table.silver_table)
        spark.sql(
            f"CREATE OR REPLACE TABLE {target}\n"
            "USING DELTA\n"
            f"AS SELECT * FROM {_identifier(table.deduplicated_view)}"
        )
        LOGGER.info("published source=%s target=%s", table.source.source_id, target)
    return prepared
