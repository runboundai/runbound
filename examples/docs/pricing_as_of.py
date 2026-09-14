"""How to read the date the built-in price table was last checked. Shown on: Budgets and pricing."""

import re

import runbound

# docs: pricing-as-of
pricing_as_of = runbound.pricing.as_of()
print(pricing_as_of)
# /docs

assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pricing_as_of), pricing_as_of
assert pricing_as_of == runbound.pricing.PRICES_AS_OF
