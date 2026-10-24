"""Databricks wheel entry point for validated document Delta publication."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from .publication import publish_artifact_set


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact_root", "--artifact-root", required=True)
    parser.add_argument("--artifact_set_id", "--artifact-set-id", required=True)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--silver_schema", "--silver-schema", default="crestline_silver")
    return parser


def run(
    artifact_root: str,
    artifact_set_id: str,
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
    publish_artifact_set(
        spark,
        artifact_root=artifact_root,
        artifact_set_id=artifact_set_id,
        catalog=catalog,
        silver_schema=silver_schema,
    )


def main() -> None:
    arguments = _parser().parse_args()
    run(
        artifact_root=arguments.artifact_root,
        artifact_set_id=arguments.artifact_set_id,
        catalog=arguments.catalog,
        silver_schema=arguments.silver_schema,
    )
