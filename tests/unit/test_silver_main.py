from __future__ import annotations

import sys
from typing import Any

import pytest

import crestline_silver.main as entry_point


def test_silver_wheel_entry_point_is_zero_argument(
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
            "run_silver_transform",
            "--catalog",
            "workspace",
            "--bronze_schema",
            "crestline_bronze",
            "--silver_schema",
            "crestline_silver",
        ],
    )

    entry_point.main()

    assert calls == [
        {
            "config": None,
            "catalog": "workspace",
            "bronze_schema": "crestline_bronze",
            "silver_schema": "crestline_silver",
        }
    ]
