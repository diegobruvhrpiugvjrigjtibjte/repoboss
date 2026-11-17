"""Order total: line prices, bulk discount, shipping and tax."""

from dataclasses import dataclass

from shopcalc import settings
from shopcalc.catalog import price_of


@dataclass(frozen=True)
class Line:
    sku: str
    quantity: int


def line_total(line: Line) -> float:
    if line.quantity <= 0:
        raise ValueError("quantity must be positive")
    total = price_of(line.sku) * line.quantity
    if line.quantity >= settings.BULK_MIN_QUANTITY:
        total *= 1 - settings.BULK_DISCOUNT
    return round(total, 2)


def subtotal(lines: list[Line]) -> float:
    return round(sum(line_total(line) for line in lines), 2)


def shipping(amount: float) -> float:
    if amount == 0:
        return 0.0
    if amount >= settings.FREE_SHIPPING_THRESHOLD:
        return 0.0
    return settings.SHIPPING_FEE


def tax(amount: float) -> float:
    return round(amount * settings.TAX_RATE, 2)


def order_total(lines: list[Line]) -> float:
    net = subtotal(lines)
    return round(net + tax(net) + shipping(net), 2)
