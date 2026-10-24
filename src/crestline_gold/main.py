"""Databricks Python wheel entry point for Gold view publication."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from crestline_gold.deploy import deploy_gold_views


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish the three MediAssist Gold views.")
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--silver_schema", "--silver-schema", default="crestline_silver")
    parser.add_argument("--gold_schema", "--gold-schema", default="crestline_gold")
    return parser


def run(
    catalog: str = "workspace",
    silver_schema: str = "crestline_silver",
    gold_schema: str = "crestline_gold",
    spark: Any | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if spark is None:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
    deploy_gold_views(spark, catalog, silver_schema, gold_schema)


def main() -> None:
    args = _parser().parse_args()
    run(
        catalog=args.catalog,
        silver_schema=args.silver_schema,
        gold_schema=args.gold_schema,
    )
