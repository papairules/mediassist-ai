from crestline_bronze.identity import stable_sha256


def test_stable_sha256_is_deterministic_and_ordered() -> None:
    first = stable_sha256("orders", "/landing/orders/a.csv", 123, "2026-10-22")
    assert first == stable_sha256(
        "orders", "/landing/orders/a.csv", 123, "2026-10-22"
    )
    assert first != stable_sha256(
        "orders", "/landing/orders/b.csv", 123, "2026-10-22"
    )


def test_stable_sha256_distinguishes_empty_and_separator_safe_components() -> None:
    assert stable_sha256("a", None) == stable_sha256("a", "")
    assert stable_sha256("a", "b") != stable_sha256("ab")

