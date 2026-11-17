import pytest

from shopcalc import settings
from shopcalc.catalog import load_catalog, price_of
from shopcalc.pricing import Line, line_total, order_total, shipping, subtotal, tax


def test_catalog_loads_all_products():
    assert load_catalog() == {"PEN": 1.5, "NOTEBOOK": 4.0, "BACKPACK": 35.0, "LAMP": 20.0}


def test_unknown_product_is_rejected():
    with pytest.raises(KeyError):
        price_of("UMBRELLA")


def test_line_total_without_discount():
    assert line_total(Line("NOTEBOOK", 3)) == 12.0


def test_bulk_discount_starts_exactly_at_the_threshold():
    assert line_total(Line("PEN", 9)) == 13.5
    assert line_total(Line("PEN", 10)) == 13.5  # 15.0 minus 10 %


def test_bulk_discount_rate():
    assert line_total(Line("NOTEBOOK", 20)) == 72.0


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        line_total(Line("PEN", 0))


def test_shipping_is_charged_below_threshold():
    assert shipping(49.99) == 4.9


def test_shipping_is_free_at_and_above_threshold():
    assert shipping(50) == 0.0
    assert shipping(120) == 0.0


def test_empty_order_has_no_shipping():
    assert shipping(0) == 0.0


def test_tax_rate():
    assert settings.TAX_RATE == 0.22
    assert tax(100) == 22.0


def test_small_order_total():
    # 2 notebooks = 8.00, tax 1.76, shipping 4.90
    assert order_total([Line("NOTEBOOK", 2)]) == 14.66


def test_large_order_total():
    # backpack 35 + lamp 20 = 55, tax 12.10, free shipping
    assert order_total([Line("BACKPACK", 1), Line("LAMP", 1)]) == 67.1


def test_subtotal_mixed_lines():
    assert subtotal([Line("PEN", 10), Line("LAMP", 2)]) == 53.5
