// Order total: line prices, bulk discount, shipping and tax.
import * as settings from "./settings.js";
import { priceOf } from "./catalog.js";

const round2 = (value) => Math.round(value * 100) / 100;

export function lineTotal(sku, quantity) {
  if (quantity <= 0) {
    throw new RangeError("quantity must be positive");
  }
  let total = priceOf(sku) * quantity;
  if (quantity >= settings.BULK_MIN_QUANTITY) {
    total *= 1 - settings.BULK_DISCOUNT;
  }
  return round2(total);
}

export function subtotal(lines) {
  return round2(lines.reduce((sum, [sku, quantity]) => sum + lineTotal(sku, quantity), 0));
}

export function shipping(amount) {
  if (amount === 0) {
    return 0;
  }
  if (amount >= settings.FREE_SHIPPING_THRESHOLD) {
    return 0;
  }
  return settings.SHIPPING_FEE;
}

export function tax(amount) {
  return round2(amount * settings.TAX_RATE);
}

export function orderTotal(lines) {
  const net = subtotal(lines);
  return round2(net + tax(net) + shipping(net));
}
