"""Databricks Python wheel entry point for the Silver batch workflow."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from crestline_bronze.config import load_registry
from crestline_silver.transform import run_silver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build typed and validated Silver Delta tables."
    )
    parser.add_argument("--config")
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--bronze_schema", "--bronze-schema", default="crestline_bronze")
    parser.add_argument("--silver_schema", "--silver-schema", default="crestline_silver")
    return parser


def run(
    config: str | None = None,
    catalog: str = "workspace",
    bronze_schema: str = "crestline_bronze",
    silver_schema: str = "crestline_silver",
    spark: Any | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if spark is None:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
    run_silver(
        spark=spark,
        registry=load_registry(config),
        catalog=catalog,
        bronze_schema=bronze_schema,
        silver_schema=silver_schema,
    )


def main() -> None:
    args = _parser().parse_args()
    run(
        config=args.config,
        catalog=args.catalog,
        bronze_schema=args.bronze_schema,
        silver_schema=args.silver_schema,
    )
