import { test } from "node:test";
import assert from "node:assert/strict";

import { loadCatalog, priceOf } from "../src/catalog.js";
import { lineTotal, orderTotal, shipping, subtotal, tax } from "../src/pricing.js";
import { TAX_RATE } from "../src/settings.js";

test("catalog loads all products", () => {
  assert.deepEqual(Object.fromEntries(loadCatalog()), { PEN: 1.5, NOTEBOOK: 4, BACKPACK: 35, LAMP: 20 });
});

test("unknown product is rejected", () => {
  assert.throws(() => priceOf("UMBRELLA"));
});

test("line total without discount", () => {
  assert.equal(lineTotal("NOTEBOOK", 3), 12);
});

test("bulk discount starts exactly at the threshold", () => {
  assert.equal(lineTotal("PEN", 9), 13.5);
  assert.equal(lineTotal("PEN", 10), 13.5);
});

test("bulk discount rate", () => {
  assert.equal(lineTotal("NOTEBOOK", 20), 72);
});

test("quantity must be positive", () => {
  assert.throws(() => lineTotal("PEN", 0), RangeError);
});

test("shipping is charged below threshold", () => {
  assert.equal(shipping(49.99), 4.9);
});

test("shipping is free at and above threshold", () => {
  assert.equal(shipping(50), 0);
  assert.equal(shipping(120), 0);
});

test("empty order has no shipping", () => {
  assert.equal(shipping(0), 0);
});

test("tax rate", () => {
  assert.equal(TAX_RATE, 0.22);
  assert.equal(tax(100), 22);
});

test("small order total", () => {
  assert.equal(orderTotal([["NOTEBOOK", 2]]), 14.66);
});

test("large order total", () => {
  assert.equal(orderTotal([["BACKPACK", 1], ["LAMP", 1]]), 67.1);
});

test("subtotal of mixed lines", () => {
  assert.equal(subtotal([["PEN", 10], ["LAMP", 2]]), 53.5);
});
