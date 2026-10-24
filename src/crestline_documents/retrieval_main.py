"""Databricks wheel entry point for the document retrieval Delta projection."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from .retrieval import run_retrieval_preparation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--silver_schema", "--silver-schema", default="crestline_silver")
    return parser


def run(
    catalog: str = "workspace",
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
    run_retrieval_preparation(spark, catalog=catalog, silver_schema=silver_schema)


def main() -> None:
    arguments = _parser().parse_args()
    run(catalog=arguments.catalog, silver_schema=arguments.silver_schema)
