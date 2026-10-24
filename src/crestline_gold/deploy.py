"""Render and publish the three version-controlled Gold SQL views."""

from __future__ import annotations

from importlib import resources
import logging
import re
from typing import Any


LOGGER = logging.getLogger(__name__)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPECTED_VIEWS = (
    "order_line_fulfillment",
    "backorder_eta_evidence",
    "product_region_inventory",
)


class GoldConfigurationError(ValueError):
    """Raised when a Gold SQL template or identifier is unsafe."""


def _identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise GoldConfigurationError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def load_gold_sql() -> str:
    """Load the SQL bundled in the installed Python wheel."""
    try:
        return (
            resources.files("crestline_gold")
            .joinpath("sql/gold_views.sql")
            .read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise GoldConfigurationError(f"Cannot load packaged Gold SQL: {exc}") from exc


def split_sql_statements(sql: str) -> tuple[str, ...]:
    """Split this controlled template and enforce its three-view contract."""
    statements = tuple(part.strip() for part in sql.split(";") if part.strip())
    if len(statements) != len(EXPECTED_VIEWS):
        raise GoldConfigurationError(
            f"Expected exactly {len(EXPECTED_VIEWS)} SQL statements; "
            f"found {len(statements)}"
        )
    return statements


def render_gold_sql(
    catalog: str = "workspace",
    silver_schema: str = "crestline_silver",
    gold_schema: str = "crestline_gold",
) -> tuple[str, ...]:
    """Safely render catalog/schema identifiers into the packaged SQL."""
    rendered = load_gold_sql()
    replacements = {
        "{{catalog}}": _identifier(catalog),
        "{{silver_schema}}": _identifier(silver_schema),
        "{{gold_schema}}": _identifier(gold_schema),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    if "{{" in rendered or "}}" in rendered:
        raise GoldConfigurationError("Gold SQL contains an unresolved placeholder")

    statements = split_sql_statements(rendered)
    for statement, view_name in zip(statements, EXPECTED_VIEWS):
        target = ".".join(
            (_identifier(catalog), _identifier(gold_schema), _identifier(view_name))
        )
        normalized = " ".join(statement.split()).upper()
        expected_prefix = f"CREATE OR REPLACE VIEW {target}".upper()
        if expected_prefix not in normalized:
            raise GoldConfigurationError(
                f"Gold statement does not create expected view {view_name}"
            )
    return statements


def deploy_gold_views(
    spark: Any,
    catalog: str = "workspace",
    silver_schema: str = "crestline_silver",
    gold_schema: str = "crestline_gold",
) -> None:
    """Create or replace the three views in dependency order."""
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    for view_name, statement in zip(
        EXPECTED_VIEWS,
        render_gold_sql(catalog, silver_schema, gold_schema),
    ):
        spark.sql(statement)
        LOGGER.info("published Gold view=%s.%s.%s", catalog, gold_schema, view_name)
