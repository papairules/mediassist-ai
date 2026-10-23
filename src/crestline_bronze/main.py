"""Databricks Python wheel entry point for registered Bronze sources."""

from __future__ import annotations

import argparse
import logging
import re
from typing import Any

from crestline_bronze.config import ConfigurationError, load_registry
from crestline_bronze.ingest import ingest_source


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest registered CSV sources into Bronze Delta tables."
    )
    parser.add_argument("--config")
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="crestline_bronze")
    parser.add_argument("--volume", default="mediassist_ingestion")
    parser.add_argument("--source_id", "--source-id", default="")
    return parser


def _validate_runtime_identifier(value: str, name: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{name} must be a safe identifier: {value!r}")
    return value


def run(
    config: str | None = None,
    catalog: str = "workspace",
    schema: str = "crestline_bronze",
    volume: str = "mediassist_ingestion",
    source_id: str = "",
    spark: Any | None = None,
) -> None:
    """Run all enabled sources, or one source selected by job parameter."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    catalog = _validate_runtime_identifier(catalog, "catalog")
    schema = _validate_runtime_identifier(schema, "schema")
    volume = _validate_runtime_identifier(volume, "volume")
    registry = load_registry(config)
    if spark is None:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    selected = registry.enabled_sources(source_id.strip())
    logging.getLogger(__name__).info("Processing %d registered sources", len(selected))
    for source in selected:
        ingest_source(spark, source, catalog, schema, volume)


def main() -> None:
    """Parse Databricks wheel task parameters and invoke ingestion."""
    args = _parser().parse_args()
    run(
        config=args.config,
        catalog=args.catalog,
        schema=args.schema,
        volume=args.volume,
        source_id=args.source_id,
    )
