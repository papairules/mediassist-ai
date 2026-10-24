from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "crestline_synthetic_v1" / "source_csv"


def _rows(file_name: str) -> list[dict[str, str]]:
    with (SOURCE_DIR / file_name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fulfillment() -> dict[tuple[str, str], dict[str, int]]:
    shipments: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for shipment in _rows("shipments.csv"):
        shipments[(shipment["order_id"], shipment["line_id"])].append(shipment)

    result: dict[tuple[str, str], dict[str, int]] = {}
    for line in _rows("order_lines.csv"):
        key = (line["order_id"], line["line_id"])
        evidence = shipments[key]
        ordered = int(line["quantity_ordered"])
        shipped = sum(int(row["quantity_shipped"]) for row in evidence)
        delivered = sum(
            int(row["quantity_shipped"])
            for row in evidence
            if row["carrier_status"].lower() == "delivered" and row["delivered_at"]
        )
        result[key] = {
            "ordered": ordered,
            "shipped": shipped,
            "delivered": delivered,
            "unshipped": ordered - shipped,
        }
    return result


def test_fulfillment_verification_totals_and_scenarios() -> None:
    facts = _fulfillment()
    assert len(facts) == 11
    assert sum(row["ordered"] for row in facts.values()) == 342
    assert sum(row["shipped"] for row in facts.values()) == 192
    assert sum(row["unshipped"] for row in facts.values()) == 150

    assert facts[("ORD-1001", "1")] == {
        "ordered": 100,
        "shipped": 60,
        "delivered": 0,
        "unshipped": 40,
    }
    assert facts[("ORD-1004", "1")]["delivered"] == 25
    assert facts[("ORD-1010", "1")]["unshipped"] == 0
    assert facts[("ORD-1010", "2")]["unshipped"] == 10


def test_supplier_eta_current_record_and_history_verification() -> None:
    eta_by_line: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for eta in _rows("supplier_etas.csv"):
        eta_by_line[(eta["order_id"], eta["line_id"])].append(eta)

    current = {
        key: max(
            history,
            key=lambda row: (
                row["supplier_reported_at"],
                row["ingested_at"],
                row["source_record_id"],
            ),
        )
        for key, history in eta_by_line.items()
    }
    line_keys = {
        (line["order_id"], line["line_id"]) for line in _rows("order_lines.csv")
    }

    assert sum(len(history) for history in eta_by_line.values()) == 8
    assert sum(key in current for key in line_keys) == 6
    assert sum(not row["expected_supplier_restock_date"] for row in current.values()) == 3
    assert current[("ORD-1001", "1")]["source_record_id"] == "ETA-001-NEW"
    assert (
        current[("ORD-1001", "1")]["expected_supplier_restock_date"]
        == "2026-10-28"
    )
    assert current[("ORD-1002", "1")]["source_record_id"] == "ETA-002-NEW"
    assert (
        current[("ORD-1002", "1")]["expected_supplier_restock_date"]
        == "2026-10-29"
    )
    for key in (("ORD-1003", "1"), ("ORD-1008", "1"), ("ORD-1010", "2")):
        assert current[key]["expected_supplier_restock_date"] == ""


def test_inventory_latest_snapshot_and_missing_vs_zero_verification() -> None:
    latest: dict[tuple[str, str], dict[str, str]] = {}
    for row in _rows("inventory.csv"):
        key = (row["warehouse_id"], row["sku"])
        previous = latest.get(key)
        if previous is None or row["snapshot_at"] > previous["snapshot_at"]:
            latest[key] = row

    regional: dict[tuple[str, str], int] = defaultdict(int)
    for row in latest.values():
        regional[(row["region"], row["sku"])] += int(row["quantity_on_hand"]) - int(
            row["quantity_reserved"]
        )

    regions = {row["region"] for row in _rows("hospitals.csv")}
    products = {row["sku"] for row in _rows("products.csv")}
    assert len(regions) * len(products) == 30
    assert len(regional) == 14
    assert sum(regional.values()) == 359
    assert regional[("East", "BR-IV22-BLU-LF")] == 45
    assert regional[("West", "BR-IV22-BLU-LF")] == 0
    assert regional[("Central", "BR-SYR10-LF")] == 32
    assert ("East", "BR-SYR10-LF") not in regional
