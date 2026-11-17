# shopcalc (RepoBoss demo repository)

A tiny order-pricing library with a real test suite, used to try RepoBoss end to end.

- `src/shopcalc/settings.py` — tax rate, free-shipping threshold, bulk-discount rules.
- `src/shopcalc/catalog.py` — loads product prices from `src/shopcalc/data/catalog.json`.
- `src/shopcalc/pricing.py` — line totals, bulk discount, shipping, tax and the order total.

The three RepoBoss transformations each find a sensible site here: a configuration constant
(`settings.py`), a data-file reference (`catalog.py`) and a boundary condition (`pricing.py`).

Run the tests directly with `python -m pytest -q` from this folder.
