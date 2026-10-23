from __future__ import annotations

import sys
from typing import Any

import pytest

import crestline_bronze.main as entry_point


def test_wheel_entry_point_defaults_to_packaged_registry(
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
            "run_bronze_ingestion",
            "--catalog",
            "workspace",
            "--schema",
            "crestline_bronze",
            "--volume",
            "mediassist_ingestion",
            "--source_id",
            "orders",
        ],
    )

    entry_point.main()

    assert calls == [
        {
            "config": None,
            "catalog": "workspace",
            "schema": "crestline_bronze",
            "volume": "mediassist_ingestion",
            "source_id": "orders",
        }
    ]


@pytest.mark.parametrize("source_id", ["", "orders"])
def test_wheel_entry_point_accepts_zero_arguments(
    monkeypatch: pytest.MonkeyPatch, source_id: str
) -> None:
    config_path = "/tmp/override-sources.yml"
    calls: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(entry_point, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_bronze_ingestion",
            "--config",
            config_path,
            "--catalog",
            "workspace",
            "--schema",
            "crestline_bronze",
            "--volume",
            "mediassist_ingestion",
            "--source_id",
            source_id,
        ],
    )

    entry_point.main()

    assert calls == [
        {
            "config": config_path,
            "catalog": "workspace",
            "schema": "crestline_bronze",
            "volume": "mediassist_ingestion",
            "source_id": source_id,
        }
    ]
