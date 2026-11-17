"""Product catalog backed by a JSON data file shipped with the package."""

import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, float]:
    with open(DATA_DIR / "catalog.json", encoding="utf-8") as handle:
        items = json.load(handle)
    return {item["sku"]: float(item["price"]) for item in items}


def price_of(sku: str) -> float:
    catalog = load_catalog()
    if sku not in catalog:
        raise KeyError(f"unknown product: {sku}")
    return catalog[sku]
