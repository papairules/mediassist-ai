"""Auto Loader source construction. PySpark imports remain runtime-only."""

from __future__ import annotations

from typing import Any

from crestline_bronze.config import SourceConfig


def build_autoloader_stream(spark: Any, source: SourceConfig, landing_path: str) -> Any:
    """Return a streaming DataFrame with raw strings and source file metadata."""
    from pyspark.sql import functions as functions
    from pyspark.sql.types import StringType, StructField, StructType

    schema = StructType(
        [StructField(name, StringType(), True) for name in source.column_names]
        + [
            StructField("_corrupt_record", StringType(), True),
            StructField("_rescued_data", StringType(), True),
        ]
    )
    raw = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaEvolutionMode", "rescue")
        .option("rescuedDataColumn", "_rescued_data")
        .option("columnNameOfCorruptRecord", "_corrupt_record")
        .option("mode", "PERMISSIVE")
        .option("header", "true")
        .option("enforceSchema", "false")
        .option("encoding", "UTF-8")
        .schema(schema)
        .load(landing_path)
    )
    return raw.select(
        "*",
        functions.col("_metadata.file_path").alias("_source_file_path"),
        functions.col("_metadata.file_name").alias("_source_file_name"),
        functions.col("_metadata.file_size").alias("_source_file_size"),
        functions.col("_metadata.file_modification_time").alias(
            "_source_file_modified_at"
        ),
    )
