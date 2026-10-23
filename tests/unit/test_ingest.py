from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import crestline_bronze.ingest as ingestion
from crestline_bronze.config import load_registry


class FakeExpression:
    def __init__(self, branch: str = "expression") -> None:
        self.branch = branch

    def __eq__(self, _: object) -> Any:
        return FakeExpression("valid")

    def __gt__(self, _: object) -> FakeExpression:
        return FakeExpression("invalid")


class FakeDataFrame:
    def __init__(self, branch: str = "validated") -> None:
        self.branch = branch

    def where(self, condition: FakeExpression) -> FakeDataFrame:
        return FakeDataFrame(condition.branch)

    def drop(self, *_: str) -> FakeDataFrame:
        return self

    def withColumn(self, _: str, __: FakeExpression) -> FakeDataFrame:
        return self

    def select(self, *_: str) -> FakeDataFrame:
        return self


def test_microbatch_path_does_not_use_dataframe_caching() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ingestion._write_microbatch)))
    unsupported = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"cache", "persist", "unpersist"}
    }
    assert unsupported == set()


def test_microbatch_merges_valid_and_quarantine_branches_without_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    functions = SimpleNamespace(
        size=lambda *_: FakeExpression(),
        sha2=lambda *_: FakeExpression(),
        concat_ws=lambda *_: FakeExpression(),
        lit=lambda *_: FakeExpression(),
        col=lambda *_: FakeExpression(),
        to_json=lambda *_: FakeExpression(),
    )
    pyspark = ModuleType("pyspark")
    pyspark_sql = ModuleType("pyspark.sql")
    pyspark_sql.functions = functions  # type: ignore[attr-defined]
    pyspark.sql = pyspark_sql  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark", pyspark)
    monkeypatch.setitem(sys.modules, "pyspark.sql", pyspark_sql)

    source = load_registry().enabled_sources("orders")[0]
    validated = FakeDataFrame()
    merges: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        ingestion, "add_validation_reasons", lambda _batch, _source: validated
    )

    def record_merge(dataframe: Any, table: str, identity: str) -> None:
        merges.append((dataframe.branch, table, identity))

    monkeypatch.setattr(ingestion, "_merge_once", record_merge)

    ingestion._write_microbatch(
        batch=FakeDataFrame(),
        batch_id=7,
        source=source,
        target_table="workspace.crestline_bronze.orders_raw",
        quarantine_table="workspace.crestline_bronze.ingestion_quarantine",
    )

    assert merges == [
        ("valid", "workspace.crestline_bronze.orders_raw", "_record_id"),
        (
            "invalid",
            "workspace.crestline_bronze.ingestion_quarantine",
            "_quarantine_id",
        ),
    ]
