from crestline_bronze.config import ColumnConfig
from crestline_bronze.quality import _invalid_type_expression


def test_integer_validation_uses_try_cast() -> None:
    expression = _invalid_type_expression(ColumnConfig("quantity", "integer", True))
    assert expression is not None
    assert "try_cast(`quantity` AS BIGINT) IS NULL" in expression


def test_boolean_validation_is_strict_and_case_insensitive() -> None:
    expression = _invalid_type_expression(ColumnConfig("sterile", "boolean", True))
    assert expression is not None
    assert "lower(trim(`sterile`)) NOT IN ('true', 'false')" in expression


def test_string_has_no_type_coercion_rule() -> None:
    assert _invalid_type_expression(ColumnConfig("sku", "string", True)) is None

