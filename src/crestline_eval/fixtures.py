"""Schema-accurate, deterministic local backend for golden evaluations."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from crestline_retrieval.models import SqlParameter


GUIDE_HASH = "5e04ec5b0f391d6486df049ba9da2cdb4de8703870f692eccf23078ff1e609e7"
GUIDE_V1_HASH = "72079cd352e914da2f9957d492e8f4a38c5c5e8be9e2bb9e1e1193bd176a48cc"


def _parameters(values: Sequence[SqlParameter]) -> dict[str, str]:
    return {item.name: item.value for item in values}


def _document(
    document_id: str,
    version: str,
    status: str,
    effective_from: str,
    content_hash: str,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "display_document_id": document_id,
        "document_version": version,
        "document_status": status,
        "effective_from": effective_from,
        "page_number": 1,
        "source_file": f"{document_id}.pdf",
        "content_sha256": content_hash,
        "extraction_method": "embedded_text",
        "extractor_name": "crestline_deterministic_pdf",
        "extractor_version": "1.0.0",
        "extraction_status": "extracted",
        "review_reasons": [],
    }


def _guide(
    candidate: str,
    status: str,
    record_id: str,
    *,
    current: bool = True,
) -> dict[str, Any]:
    row = _document(
        "SUB_GUIDE_2026_V2_CURRENT" if current else "SUB_GUIDE_2025_V1_SUPERSEDED",
        "V2" if current else "V1",
        "current" if current else "superseded",
        "2026-09-01" if current else "2025-01-01",
        GUIDE_HASH if current else GUIDE_V1_HASH,
    )
    row.update(
        {
            "substitution_row_id": record_id,
            "requested_sku": "AV-IV22-BLU-LF",
            "candidate_sku": candidate,
            "row_status": status,
            "required_attributes": "22G, blue, 25 mm, latex-free",
            "clinical_note": "Current guide and contract evidence require human review.",
            "table_number": 1,
            "table_row_number": 1,
        }
    )
    return row


GUIDES = [
    _guide("BR-IV22-BLU-LF", "APPROVED", "02cfd97a00c13899803ecfc2d41eca9014fef1a2ae02c5bec4c9717b8c8b34da"),
    _guide("NV-IV22-BLU-LF", "APPROVED", "0eedef24e5ec3e5f9dec8f7ef74b5b90d60e5a2ef97d28dc152577afa176ca33"),
    _guide("BR-IV22-BLU-LX", "REJECTED", "21557e199755510d9f417c56a01b712c253a9b5de5cbfc81fe83152802d03660"),
    _guide("BR-IV20-PNK-LF", "REJECTED", "7d76ba5fb757ef89b682eaa3367904d4f225ab4a545f783ff5ee6307a2ff51ad"),
    _guide("BR-IV22-BLU-LF", "APPROVED", "c9559437a3913ddfbdc8a13ce390926c3b48c6a63edb7b4c1fdc03e5eb9498aa", current=False),
    _guide("BR-IV22-BLU-LX", "APPROVED (REVOKED)", "64db102e61338bf1ee205a8ee1cee03b97993a76d96d203ca6460076f5ebb1d3", current=False),
]


def _ifu(sku: str, record_id: str, content_hash: str, *, gauge: int, length: int, latex_free: bool) -> dict[str, Any]:
    row = _document(f"IFU_{sku}", "2026-08", "current", "2026-08-01", content_hash)
    row.update(
        {
            "ifu_fact_id": record_id,
            "ifu_id": f"IFU-{sku}-2026",
            "revision": "2026-08",
            "product_sku": sku,
            "gauge": gauge,
            "device_length_mm": length,
            "latex_free": latex_free,
            "sterile": True,
            "single_use": True,
            "clinical_note": "This IFU does not establish interchangeability.",
            "establishes_interchangeability": False,
        }
    )
    return row


IFUS = [
    _ifu("AV-IV22-BLU-LF", "645945598ba45f9bcde4ce07d1cbe8ef3899c57a05f05687b41ee8417f1f919f", "fd29324cbbf4e8b7563207f1be85132cec03f21eba763cc61c4525d627215ed9", gauge=22, length=25, latex_free=True),
    _ifu("BR-IV22-BLU-LF", "f6364ec077534a3688249bc44bb2f3510da9fb81442691c6a916682d0b472ae3", "f7959293dec4c710dd4bb9ace685e7503d52e26a93270b317c6b40ab403b005f", gauge=22, length=25, latex_free=True),
    _ifu("BR-IV22-BLU-LX", "c3cb6202b8769a724cc0c8f67ef118d2041028d9a4f953897908daddf68eab50", "1306083880bae102277ad8519cf6aee98873ffa89c3cba415e85f787f4f2656a", gauge=22, length=25, latex_free=False),
    _ifu("BR-IV20-PNK-LF", "cd1fe666d92eaf38b64b0c21d58236699069957ba49566525617857f0dd1994c", "0a8282774103567e8231b39303098227d32c9308871660ac239a2f94b27af79c", gauge=20, length=32, latex_free=True),
]


def _contract(hospital: str, document_id: str, content_hash: str, record_id: str, text: str) -> dict[str, Any]:
    row = _document(document_id, document_id.replace("CONTRACT_H-", "CTR-"), "current", "2026-01-01", content_hash)
    row.update(
        {
            "contract_clause_id": record_id,
            "hospital_id": hospital,
            "clause_type": "brand_restriction",
            "clause_text": text,
            "is_override": False,
            "overrides_document_id": None,
            "requested_sku": None,
            "candidate_sku": None,
            "conditions": [],
        }
    )
    return row


CONTRACTS = {
    "H-E101": [
        _contract("H-E101", "CONTRACT_H-E101_BASE", "2ca0255a0670aabf7c9e1b62960f3776511577e2d3cebed5e6e1af16943f96d7", "9fcee0d97e2f93e4fa655053b2de818cacc3cdb7c4277ff26f896fd23c3bcfcb", "Aurevia brand-lock on all IV catheters; only signed amendments can permit other brands.")
    ],
    "H-E102": [
        _contract("H-E102", "CONTRACT_H-E102_BASE", "47464847f0b71b4df22d0e3b47796c412336e47343d80340174c505b5bc6a8fa", "23c0e23f843234b4b702e542978a19815ba4c909f07ae40b8dacfb6e908eeea5", "Aurevia brand-lock on all IV catheters unless a signed amendment overrides it."),
    ],
    "H-W201": [
        _contract("H-W201", "CONTRACT_H-W201_BASE", "3375dbdbe853a54cd9bc5a0ff7a15dec8bb684c17405bbfdc8e9a7387f824f6d", "f8f9ab3467f3f82532db2379cde7dcaf90f394f328d93b27c98a897f2b1d20ec", "Aurevia brand-lock on all IV catheters. No listed exceptions.")
    ],
}
AMENDMENT = _document("AMENDMENT_H-E102_2026_01", "AMD-E102-2026-01", "current", "2026-10-01", "037d8180a9ddb70123a41f1597eb7c76a2ddeda25ea9830f17c25de0b6a4fb4e")
AMENDMENT.update(
    {
        "contract_clause_id": "bd1b6dd89a8602751dd44e29ec3a04dfa6c576d97259459cb553948d9ce3d0b2",
        "hospital_id": "H-E102",
        "clause_type": "brand_lock_override",
        "clause_text": "Overrides the IV-catheter brand-lock only for this pair while a documented backorder remains open.",
        "is_override": True,
        "overrides_document_id": "CONTRACT_H-E102_BASE",
        "requested_sku": "AV-IV22-BLU-LF",
        "candidate_sku": "BR-IV22-BLU-LF",
        "conditions": ["Qualified clinical review is still required."],
    }
)
CONTRACTS["H-E102"].append(AMENDMENT)


def _order(order_id: str, hospital: str, quantity: int, shipped: int) -> dict[str, Any]:
    evidence = []
    if order_id == "ORD-1001":
        evidence = [
            {
                "shipment_id": "SHIP-001",
                "record_id": "SHIP-REC-001",
                "tracking_id": "SYN-AT-5501",
                "quantity_shipped": 60,
                "carrier": "Atlas Parcel",
                "carrier_estimated_arrival": "2026-10-24",
            }
        ]
    return {
        "hospital_id": hospital,
        "hospital_name": {
            "H-E101": "Crestwood East Hospital",
            "H-E102": "Maple Shore Medical Center",
            "H-W201": "Sierra Valley Hospital",
            "H-W202": "Canyon Ridge Surgical Center",
        }[hospital],
        "region": "West" if hospital.startswith("H-W") else "East",
        "contract_id": f"CTR-{hospital[2:]}",
        "order_id": order_id,
        "line_id": "1",
        "placed_at": "2026-10-20T00:00:00Z",
        "erp_loaded_at": "2026-10-22T22:30:00Z",
        "sku": "AV-IV22-BLU-LF",
        "brand": "Aurevia",
        "category": "IV catheter",
        "uom": "EA",
        "quantity_ordered": quantity,
        "quantity_shipped": shipped,
        "quantity_carrier_reported_delivered": 0,
        "quantity_unshipped": quantity - shipped,
        "shipment_count": len(evidence),
        "latest_carrier_scan_at": "2026-10-22T14:20:00Z" if evidence else None,
        "shipment_evidence": evidence,
        "line_fulfillment_state": "partially_shipped" if shipped else "not_shipped",
        "order_record_id": f"ORD-REC-{order_id[4:]}",
        "order_line_record_id": f"LINE-REC-{order_id[4:]}-1",
        "product_record_id": "PROD-REC-AV-IV22-BLU-LF",
    }


ORDERS = {
    "ORD-1001": _order("ORD-1001", "H-E101", 100, 60),
    "ORD-1002": _order("ORD-1002", "H-E102", 30, 0),
    "ORD-1003": _order("ORD-1003", "H-W201", 40, 0),
    "ORD-1008": _order("ORD-1008", "H-W202", 10, 0),
}


def _backorder(order_id: str, eta_id: str, eta: str | None) -> dict[str, Any]:
    row = dict(ORDERS[order_id])
    row.update(
        {
            "has_unshipped_quantity": True,
            "supplier_eta_record_count": 2 if order_id != "ORD-1003" else 1,
            "current_supplier_eta_record_id": eta_id,
            "current_supplier_restock_date": eta,
            "current_supplier_reported_at": "2026-10-22T05:00:00Z",
            "current_supplier_eta_ingested_at": "2026-10-22T06:00:00Z",
            "current_date_classification": "unverified_supplier_restock_estimate",
            "current_source_note": "Supplier estimate, not a hospital delivery commitment",
            "current_supplier_eta_source_id": "supplier_etas",
            "current_supplier_eta_bronze_record_id": f"BRONZE-{eta_id}",
            "current_supplier_eta_file_id": "supplier-etas-file",
            "current_supplier_eta_source_file_name": "supplier_etas.csv",
            "current_supplier_eta_source_file_modified_at": "2026-10-22T22:30:00Z",
            "current_supplier_eta_bronze_ingested_at": "2026-10-22T22:35:00Z",
            "supplier_eta_revision_history": [
                {
                    "source_record_id": eta_id,
                    "expected_supplier_restock_date": eta,
                }
            ],
        }
    )
    return row


BACKORDERS = {
    "ORD-1001": _backorder("ORD-1001", "ETA-001-NEW", "2026-10-28"),
    "ORD-1002": _backorder("ORD-1002", "ETA-002-NEW", "2026-10-29"),
    "ORD-1003": _backorder("ORD-1003", "ETA-003-MISSING", None),
    "ORD-1008": _backorder("ORD-1008", "ETA-008-MISSING", None),
}


INVENTORY = {
    ("East", "AV-IV22-BLU-LF"): 0,
    ("East", "BR-IV22-BLU-LF"): 45,
    ("East", "BR-IV22-BLU-LX"): 190,
    ("East", "BR-IV20-PNK-LF"): 20,
    ("East", "NV-IV22-BLU-LF"): 17,
    ("West", "BR-IV22-BLU-LF"): 0,
}


def _inventory(region: str, sku: str, available: int) -> dict[str, Any]:
    return {
        "region": region,
        "sku": sku,
        "brand": sku.split("-")[0],
        "category": "IV catheter",
        "gauge": 20 if "IV20" in sku else 22,
        "color": "pink" if "PNK" in sku else "blue",
        "length_mm": 32 if "IV20" in sku else 25,
        "latex_free": not sku.endswith("LX"),
        "sterile": True,
        "manufacturer_status": "active",
        "has_inventory_observation": True,
        "warehouse_count": 1,
        "quantity_on_hand": available + 5,
        "quantity_reserved": 5,
        "quantity_available": available,
        "earliest_current_snapshot_at": "2026-10-22T22:30:00Z",
        "latest_current_snapshot_at": "2026-10-22T22:30:00Z",
        "warehouse_inventory_evidence": [{"warehouse_id": f"WH-{region.upper()}", "record_id": f"INV-{region}-{sku}", "snapshot_at": "2026-10-22T22:30:00Z"}],
        "product_source_id": "products",
        "product_record_id": f"PROD-{sku}",
        "product_file_id": "products-file",
        "product_source_file_name": "products.csv",
        "product_source_file_modified_at": "2026-10-22T22:30:00Z",
        "product_ingested_at": "2026-10-22T22:35:00Z",
    }


def _vector_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": f"substitution:{row['substitution_row_id']}",
        "chunk_type": "substitution_guide_row",
        "embedding_text": (
            f"Requested SKU: {row['requested_sku']}\nCandidate SKU: {row['candidate_sku']}\n"
            f"Status: {row['row_status']}\nRequired attributes: {row['required_attributes']}\n"
            f"Clinical note: {row['clinical_note']}"
        ),
        "document_id": row["document_id"],
        "document_version": row["document_version"],
        "document_status": row["document_status"],
        "effective_from": row["effective_from"],
        "page_number": row["page_number"],
        "source_file": row["source_file"],
        "content_sha256": row["content_sha256"],
        "source_record_id": row["substitution_row_id"],
        "extraction_method": row["extraction_method"],
        "extractor_name": row["extractor_name"],
        "extractor_version": row["extractor_version"],
        "row_status": row["row_status"],
        "requested_sku": row["requested_sku"],
        "candidate_sku": row["candidate_sku"],
        "product_sku": None,
        "score": 0.9,
    }


class GoldenFixtureBackend:
    """Read-only backend matching RetrievalService's deployed SQL projections."""

    def query(
        self,
        statement: str,
        parameters: Sequence[SqlParameter] = (),
    ) -> list[Mapping[str, Any]]:
        values = _parameters(parameters)
        authorized = {
            value for name, value in values.items() if name.startswith("authorized_hospital_")
        }
        if ".order_line_fulfillment" in statement:
            if "order_id" in values:
                row = ORDERS.get(values.get("order_id", ""))
                return [dict(row)] if row and row["hospital_id"] in authorized else []
            if "GROUP BY" in statement:
                summary_rows: list[Mapping[str, Any]] = []
                for order in ORDERS.values():
                    if order["hospital_id"] not in authorized:
                        continue
                    summary_rows.append(
                        {
                            "hospital_id": order["hospital_id"],
                            "hospital_name": order["hospital_name"],
                            "region": order["region"],
                            "order_id": order["order_id"],
                            "line_count": 1,
                            "quantity_ordered": order["quantity_ordered"],
                            "quantity_shipped": order["quantity_shipped"],
                            "quantity_carrier_reported_delivered": order[
                                "quantity_carrier_reported_delivered"
                            ],
                            "quantity_unshipped": order["quantity_unshipped"],
                            "outstanding_line_count": int(order["quantity_unshipped"] > 0),
                            "order_record_id": order["order_record_id"],
                        }
                    )
                return summary_rows
            line_rows: list[Mapping[str, Any]] = [
                dict(row)
                for row in ORDERS.values()
                if row["hospital_id"] in authorized
                and (
                    "quantity_unshipped > 0" not in statement
                    or row["quantity_unshipped"] > 0
                )
                and (
                    "line_fulfillment_state = 'partially_shipped'" not in statement
                    or row["line_fulfillment_state"] == "partially_shipped"
                )
                and (
                    "line_fulfillment_state = 'not_shipped'" not in statement
                    or row["line_fulfillment_state"] == "not_shipped"
                )
            ]
            return line_rows
        if ".backorder_eta_evidence" in statement:
            if "current_supplier_restock_date IS NULL" in statement:
                return [
                    dict(row)
                    for row in BACKORDERS.values()
                    if row["hospital_id"] in authorized
                    and row["quantity_unshipped"] > 0
                    and row["current_supplier_restock_date"] is None
                ]
            row = BACKORDERS.get(values.get("order_id", ""))
            if row and row["hospital_id"] in authorized and (
                "line_id" not in values or values["line_id"] == row["line_id"]
            ):
                return [dict(row)]
            return []
        if ".product_region_inventory" in statement:
            if any(name.startswith("authorized_region_") for name in values):
                regions = {
                    value for name, value in values.items() if name.startswith("authorized_region_")
                }
                requested_region = values.get("requested_region")
                if requested_region:
                    regions = {requested_region} & regions
                requested_skus = {
                    value for name, value in values.items() if name.startswith("requested_sku_")
                }
                inventory_rows: list[Mapping[str, Any]] = []
                for (region, sku), available_quantity in INVENTORY.items():
                    if region not in regions or (requested_skus and sku not in requested_skus):
                        continue
                    inventory_rows.append(_inventory(region, sku, available_quantity))
                return inventory_rows
            key = (values.get("region", ""), values.get("sku", ""))
            available_for_key = INVENTORY.get(key)
            return (
                [_inventory(*key, available_for_key)]
                if available_for_key is not None
                else []
            )
        if ".substitution_guide_rows" in statement:
            return [
                dict(row)
                for row in GUIDES
                if row["requested_sku"] == values.get("requested_sku")
                and row["candidate_sku"] == values.get("candidate_sku")
            ]
        if ".ifu_product_facts" in statement:
            skus = {values.get("requested_sku"), values.get("candidate_sku")}
            return [dict(row) for row in IFUS if row["product_sku"] in skus]
        if ".contract_clauses" in statement:
            return [dict(row) for row in CONTRACTS.get(values.get("hospital_id", ""), [])]
        raise AssertionError(f"Unexpected evaluation SQL: {statement}")

    def vector_search(
        self,
        *,
        index_name: str,
        query_text: str,
        columns: Sequence[str],
        num_results: int,
        query_type: str,
        filters: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        del index_name, columns, filters
        assert query_type == "HYBRID"
        requested = "AV-IV22-BLU-LF" if "AV-IV22-BLU-LF" in query_text else ""
        rows = [row for row in GUIDES if row["requested_sku"] == requested]
        return [_vector_row(row) for row in rows[:num_results]]
