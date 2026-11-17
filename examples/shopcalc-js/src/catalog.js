// Product catalog backed by a JSON data file shipped with the package.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DATA_DIR = join(dirname(fileURLToPath(import.meta.url)), "data");
let cache = null;

export function loadCatalog() {
  if (cache === null) {
    const items = JSON.parse(readFileSync(join(DATA_DIR, "catalog.json"), "utf-8"));
    cache = new Map(items.map((item) => [item.sku, Number(item.price)]));
  }
  return cache;
}

export function priceOf(sku) {
  const catalog = loadCatalog();
  if (!catalog.has(sku)) {
    throw new Error(`unknown product: ${sku}`);
  }
  return catalog.get(sku);
}
