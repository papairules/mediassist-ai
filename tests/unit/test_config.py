from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from crestline_bronze.config import ConfigurationError, load_registry


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "src" / "crestline_bronze" / "sources.yml"
SOURCE_DIR = ROOT / "crestline_synthetic_v1" / "source_csv"
FILES_BY_SOURCE = {
    "hospitals": "hospitals.csv",
    "products": "products.csv",
    "orders": "orders.csv",
    "order_lines": "order_lines.csv",
    "shipments": "shipments.csv",
    "inventory": "inventory.csv",
    "supplier_etas": "supplier_etas.csv",
    "document_manifest": "document_manifest.csv",
    "rep_access_demo_only": "rep_access_DEMO_ONLY.csv",
}


def test_registry_contains_all_nine_sources() -> None:
    registry = load_registry(REGISTRY_PATH)
    assert len(registry.sources) == 9
    assert {source.source_id for source in registry.sources} == set(FILES_BY_SOURCE)


def test_packaged_registry_is_the_canonical_nine_source_registry() -> None:
    registry = load_registry()
    assert len(registry.sources) == 9
    assert {source.source_id for source in registry.sources} == set(FILES_BY_SOURCE)


@pytest.mark.parametrize("source_id,file_name", FILES_BY_SOURCE.items())
def test_registry_columns_match_synthetic_csv_headers(
    source_id: str, file_name: str
) -> None:
    registry = load_registry(REGISTRY_PATH)
    source = next(item for item in registry.sources if item.source_id == source_id)
    with (SOURCE_DIR / file_name).open(newline="", encoding="utf-8") as handle:
        header = tuple(next(csv.reader(handle)))
    assert source.column_names == header


def test_registry_rejects_unknown_key(tmp_path: Path) -> None:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["sources"][0]["keys"] = ["not_a_column"]
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="keys not found"):
        load_registry(path)


def test_registry_rejects_duplicate_target(tmp_path: Path) -> None:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["sources"][1]["target_table"] = raw["sources"][0]["target_table"]
    path = tmp_path / "invalid.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Duplicate target_table"):
        load_registry(path)


def test_source_selection_is_explicit() -> None:
    registry = load_registry(REGISTRY_PATH)
    assert registry.enabled_sources("orders")[0].target_table == "orders_raw"
    with pytest.raises(ConfigurationError, match="Unknown or disabled"):
        registry.enabled_sources("missing")
