from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from crestline_bronze.config import load_registry
from crestline_silver.rules import FOREIGN_KEYS, silver_table_name


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "crestline_synthetic_v1" / "source_csv"
EXPECTED_COUNTS = {
    "hospitals": 5,
    "products": 10,
    "orders": 10,
    "order_lines": 11,
    "shipments": 7,
    "inventory": 14,
    "supplier_etas": 8,
    "document_manifest": 13,
    "rep_access_demo_only": 7,
}


def _rows(source_id: str) -> list[dict[str, str]]:
    source = load_registry().enabled_sources(source_id)[0]
    with (SOURCE_DIR / source.source_file).open(
        newline="", encoding="utf-8"
    ) as handle:
        return list(csv.DictReader(handle))


def test_silver_maps_all_canonical_sources_one_to_one() -> None:
    registry = load_registry()
    mappings = {
        source.source_id: silver_table_name(source.source_id)
        for source in registry.sources
    }
    assert set(mappings.values()) == {
        "hospitals",
        "products",
        "orders",
        "order_lines",
        "shipments",
        "inventory_snapshots",
        "supplier_etas",
        "document_manifest",
        "rep_access_demo_only",
    }
    assert len(FOREIGN_KEYS) == 8


def test_fixture_contains_85_rows_with_unique_business_keys() -> None:
    registry = load_registry()
    observed: dict[str, int] = {}
    for source in registry.sources:
        rows = _rows(source.source_id)
        observed[source.source_id] = len(rows)
        keys = [tuple(row[key] for key in source.keys) for row in rows]
        assert len(keys) == len(set(keys)), source.source_id

    assert observed == EXPECTED_COUNTS
    assert sum(observed.values()) == 85


def test_fixture_satisfies_declared_referential_relationships() -> None:
    rows_by_source = {
        source.source_id: _rows(source.source_id) for source in load_registry().sources
    }
    for rule in FOREIGN_KEYS:
        parent_keys = {
            tuple(row[column] for column in rule.parent_columns)
            for row in rows_by_source[rule.parent_source_id]
        }
        for row in rows_by_source[rule.child_source_id]:
            child_key = tuple(row[column] for column in rule.child_columns)
            if rule.nullable and any(not value for value in child_key):
                continue
            assert child_key in parent_keys, rule.name


def test_historical_and_nullable_records_are_intentionally_preserved() -> None:
    etas = _rows("supplier_etas")
    revisions = Counter((row["order_id"], row["line_id"]) for row in etas)
    assert revisions[("ORD-1001", "1")] == 2
    assert revisions[("ORD-1002", "1")] == 2
    assert any(not row["expected_supplier_restock_date"] for row in etas)

    inventory = _rows("inventory")
    inventory_source = load_registry().enabled_sources("inventory")[0]
    assert inventory_source.keys == ("warehouse_id", "sku", "snapshot_at")
    assert len(inventory) == EXPECTED_COUNTS["inventory"]

    shipments = _rows("shipments")
    shipment_source = load_registry().enabled_sources("shipments")[0]
    assert shipment_source.keys == ("shipment_id",)
    partial = next(row for row in shipments if row["shipment_id"] == "SHIP-001")
    ordered = next(
        row
        for row in _rows("order_lines")
        if (row["order_id"], row["line_id"])
        == (partial["order_id"], partial["line_id"])
    )
    assert int(partial["quantity_shipped"]) < int(ordered["quantity_ordered"])

    documents = _rows("document_manifest")
    assert {row["status"] for row in documents} >= {"current", "superseded"}
    assert any(not row["hospital_id"] for row in documents)

    products = _rows("products")
    assert any(not row["gauge"] for row in products)
    assert any(not row["length_mm"] for row in products)
