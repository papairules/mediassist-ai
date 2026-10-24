"""Small Silver-specific table mappings and relationship rules.

Column types, required flags, and business keys remain owned by the packaged
Bronze source registry. This module only names Silver tables and relationships
that cannot be inferred from a single source definition.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ForeignKeyRule:
    child_source_id: str
    child_columns: tuple[str, ...]
    parent_source_id: str
    parent_columns: tuple[str, ...]
    nullable: bool = False

    @property
    def name(self) -> str:
        child = "_".join(self.child_columns)
        parent = "_".join(self.parent_columns)
        return (
            f"{self.child_source_id}_{child}_to_"
            f"{self.parent_source_id}_{parent}"
        )


FOREIGN_KEYS = (
    ForeignKeyRule("orders", ("hospital_id",), "hospitals", ("hospital_id",)),
    ForeignKeyRule("order_lines", ("order_id",), "orders", ("order_id",)),
    ForeignKeyRule("order_lines", ("sku",), "products", ("sku",)),
    ForeignKeyRule(
        "shipments",
        ("order_id", "line_id"),
        "order_lines",
        ("order_id", "line_id"),
    ),
    ForeignKeyRule("inventory", ("sku",), "products", ("sku",)),
    ForeignKeyRule(
        "supplier_etas",
        ("order_id", "line_id"),
        "order_lines",
        ("order_id", "line_id"),
    ),
    ForeignKeyRule(
        "document_manifest",
        ("hospital_id", "region"),
        "hospitals",
        ("hospital_id", "region"),
        nullable=True,
    ),
    ForeignKeyRule(
        "rep_access_demo_only",
        ("hospital_id", "region"),
        "hospitals",
        ("hospital_id", "region"),
    ),
)


def silver_table_name(source_id: str) -> str:
    """Derive the Silver name, retaining the business-friendly snapshot name."""
    return "inventory_snapshots" if source_id == "inventory" else source_id
