"""Structural Bronze validation expressed as deterministic Spark predicates."""

from __future__ import annotations

from typing import Any

from crestline_bronze.config import ColumnConfig, SourceConfig


def _quoted(name: str) -> str:
    return f"`{name}`"


def _invalid_type_expression(column: ColumnConfig) -> str | None:
    name = _quoted(column.name)
    populated = f"{name} IS NOT NULL AND trim({name}) <> ''"
    if column.data_type == "integer":
        return f"{populated} AND try_cast({name} AS BIGINT) IS NULL"
    if column.data_type == "boolean":
        return f"{populated} AND lower(trim({name})) NOT IN ('true', 'false')"
    if column.data_type == "date":
        return f"{populated} AND try_cast({name} AS DATE) IS NULL"
    if column.data_type == "timestamp":
        return f"{populated} AND try_cast({name} AS TIMESTAMP) IS NULL"
    return None


def add_validation_reasons(dataframe: Any, source: SourceConfig) -> Any:
    """Add an array of structural errors; no business values are transformed."""
    from pyspark.sql import Window
    from pyspark.sql import functions as functions

    reasons = [
        functions.when(
            functions.col("_corrupt_record").isNotNull(),
            functions.lit("MALFORMED_CSV"),
        ),
        functions.when(
            functions.col("_rescued_data").isNotNull(),
            functions.lit("UNEXPECTED_SCHEMA"),
        ),
    ]
    for column in source.columns:
        if column.required:
            reasons.append(
                functions.when(
                    functions.col(column.name).isNull()
                    | (functions.trim(functions.col(column.name)) == ""),
                    functions.lit(f"REQUIRED:{column.name}"),
                )
            )
        invalid_type = _invalid_type_expression(column)
        if invalid_type:
            reasons.append(
                functions.when(
                    functions.expr(invalid_type),
                    functions.lit(f"INVALID_{column.data_type.upper()}:{column.name}"),
                )
            )
    with_reasons = dataframe.withColumn(
        "_quarantine_reasons",
        functions.filter(functions.array(*reasons), lambda reason: reason.isNotNull()),
    )
    key_window = Window.partitionBy("_file_id", *source.keys)
    return with_reasons.withColumn(
        "_quarantine_reasons",
        functions.when(
            functions.count(functions.lit(1)).over(key_window) > 1,
            functions.array_union(
                functions.col("_quarantine_reasons"),
                functions.array(functions.lit("DUPLICATE_KEY_IN_FILE")),
            ),
        ).otherwise(functions.col("_quarantine_reasons")),
    )

