"""Per-model price table and cost estimate (USD per 1M tokens).

Only Claude models are priced; the numbers come from the claude-api skill's
model table (first-party API list prices). Other models (Gemini, GPT) have no
reliable numbers here, so ``estimate_cost`` returns None for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_watch.model import Agent, effort_from_model


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    input: float
    cached: float
    output: float


def _p(inp: float, out: float, cached: float | None = None) -> Price:
    """Build a Price; cache reads default to 0.1x input when the table lists no explicit rate."""
    return Price(inp, round(inp * 0.1, 4) if cached is None else cached, out)


PRICES: dict[str, Price] = {
    "claude-fable-5-2": _p(8.00, 40.00, 0.20),
    "claude-mythos-5-2": _p(8.00, 40.00, 0.20),
    "claude-fable-5-1": _p(10.00, 50.00, 0.25),
    "claude-mythos-5-1": _p(10.00, 50.00, 0.25),
    "claude-fable-5": _p(10.00, 50.00, 1.00),
    "claude-mythos-5": _p(10.00, 50.00, 1.00),
    "claude-opus-5-5": _p(4.00, 20.00, 0.20),
    "claude-opus-5": _p(5.00, 25.00),
    "claude-opus-4-8": _p(5.00, 25.00),
    "claude-opus-4-7": _p(5.00, 25.00),
    "claude-opus-4-6": _p(5.00, 25.00),
    "claude-sonnet-5-5": _p(2.00, 10.00, 0.10),
    "claude-sonnet-5": _p(2.00, 10.00),
    "claude-sonnet-4-6": _p(3.00, 15.00),
    "claude-haiku-5-5": _p(0.10, 0.50),
    "claude-haiku-4-5": _p(1.00, 5.00),
}

_DATE_RE = re.compile(r"-\d{8}$")
_FAMILY_RE = re.compile(r"^(?:opus|sonnet|haiku|fable|mythos)-\d")


def normalize_model(model: str | None) -> str:
    """Normalize a model id to a PRICES key: case, provider prefix, [1m], effort and date suffixes."""
    if not model:
        return ""
    m = model.strip().lower().split("[", 1)[0]
    if "claude-" in m:
        m = "claude-" + m.split("claude-", 1)[1]
    elif _FAMILY_RE.match(m.replace(".", "-")):
        m = "claude-" + m
    m = m.replace(".", "-")
    if effort := effort_from_model(m):
        m = m.removesuffix(f"-{effort}")
    return _DATE_RE.sub("", m)


def price_for(model: str | None) -> Price | None:
    """Return the Price for a model id (any common variant), or None when unknown."""
    return PRICES.get(normalize_model(model))


def estimate_cost(agent: Agent) -> float | None:
    """Estimate an agent's cost in USD, or None when its model has no known price.

    in_tokens includes cached tokens; cache writes are billed as plain input here
    (they cost 1.25x in reality, so this slightly underestimates).
    """
    price = price_for(agent.model)
    if price is None:
        return None
    cached = min(max(agent.cached_tokens, 0), max(agent.in_tokens, 0))
    fresh = max(agent.in_tokens, 0) - cached
    return (fresh * price.input + cached * price.cached + max(agent.out_tokens, 0) * price.output) / 1_000_000


def fmt_cost(cost: float) -> str:
    """Format dollars as '≈ $1.23' ('≈ <$0.01' for tiny non-zero amounts)."""
    if 0 < cost < 0.01:
        return "≈ <$0.01"
    return f"≈ ${cost:,.2f}"
