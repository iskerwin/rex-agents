"""Public API list prices, to value tokens the way an API key would be billed.

Subscriptions are not billed this way, so the result is an estimate of API
cost, never a bill. A model without a published price stays unpriced rather
than borrowing a neighbour's. The standard tier is assumed, with no long-context
or fast-mode surcharge.

Sources, checked 2026-11-06:
https://platform.claude.com/docs/en/about-claude/pricing
https://developers.openai.com/api/docs/pricing
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class Price(NamedTuple):
    """USD per million tokens."""

    fresh: float
    cache_write: float  # shortest retention
    cache_write_long: float  # Claude's 1 hour retention
    cache_read: float
    output: float


PRICES = {
    "claude-fable-5-1": Price(10, 12.5, 20, 0.25, 50),
    "claude-opus-5-5": Price(4, 5, 8, 0.2, 20),
    "claude-opus-5": Price(5, 6.25, 10, 0.5, 25),
    "claude-sonnet-5-5": Price(2, 2.5, 4, 0.1, 10),
    "claude-sonnet-5": Price(2, 2.5, 4, 0.2, 10),
    "claude-haiku-5-5": Price(0.1, 0.125, 0.2, 0.01, 0.5),
    "gpt-6.1-sol": Price(2, 2.5, 2.5, 0.1, 10),
    "gpt-6.1-terra": Price(1, 1.25, 1.25, 0.05, 5),
}


def cost_usd(model: str, fresh: int, cache_read: int, cache_write: int, output: int,
             cache_write_long: int = 0) -> Optional[float]:
    """``cache_write_long`` is the part of ``cache_write`` kept for longer."""
    price = PRICES.get(model)
    if price is None:
        return None
    short = cache_write - cache_write_long
    return (fresh * price.fresh + short * price.cache_write + cache_write_long * price.cache_write_long
            + cache_read * price.cache_read + output * price.output) / 1e6
