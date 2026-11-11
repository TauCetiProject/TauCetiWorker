#!/usr/bin/env python3
"""The vendored price table carries every rate the cost model bills from.

Rates live in the review engine (TauCetiReview runner/prices.json) and are vendored here; CI
checks the copy byte for byte. This checks the half CI cannot: that the table the copy contains
is one this module's billing math can read without a KeyError on a real report.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_worker.cost_model import (
    AI_PRICES,
    AI_PRICES_SHA,
    PRICES_PATH,
    _api_cost,
    _model_family,
    load_price_windows,
)

# The table comes from the vendored file and nowhere else.
assert AI_PRICES == load_price_windows(PRICES_PATH)
assert AI_PRICES, "the vendored prices.json priced nothing"
assert len(AI_PRICES_SHA) == 12, AI_PRICES_SHA

for model, windows in AI_PRICES.items():
    assert windows, f"{model}: no rate windows"
    dates = [window["effective"] for window in windows]
    assert dates == sorted(dates), f"{model}: windows not in chronological order: {dates}"
    for window in windows:
        for field in ("input", "output"):
            assert isinstance(window.get(field), int | float), f"{model}@{window['effective']}.{field}"

# Every model this module can classify has to price every token kind it bills, in every window:
# _api_cost indexes the cache-write rates directly, so a window missing one fails on a report
# rather than showing up as a gap in the table. The long-tier shape exercises `long_context`.
MILLION = 1_000_000
STANDARD = {
    "input_tokens": MILLION,
    "cached_input_tokens": 1_000,
    "cache_write_input_tokens": 1_000,
    "output_tokens": 1_000,
}
LONG = {
    "long_input_tokens": MILLION,
    "long_cached_input_tokens": 1_000,
    "long_cache_write_input_tokens": 1_000,
    "long_output_tokens": 1_000,
}
CLAUDE = {
    "input_tokens": MILLION,
    "cache_creation_5m_input_tokens": 1_000,
    "cache_creation_1h_input_tokens": 1_000,
    "cache_read_input_tokens": 1_000,
    "output_tokens": 1_000,
}

priced = 0
for model, windows in AI_PRICES.items():
    for provider, tokens in (("codex", STANDARD), ("codex", STANDARD | LONG), ("claude", CLAUDE)):
        if _model_family(provider, model) is None:
            continue
        for window in windows:
            cost = _api_cost(provider, model, tokens, window["effective"])
            assert isinstance(cost, float) and cost > 0, f"{provider}/{model}@{window['effective']}: {cost}"
            priced += 1
assert priced, "no model in the vendored table was priced at all"

# A model the table has no row for is unpriced rather than mispriced: the worker reports coverage
# and never guesses a rate, unlike the review engine, which inherits one to keep a review running.
assert _api_cost("codex", "gpt-99-sol", STANDARD, "2026-10-15") is None

print("prices: PASS")
