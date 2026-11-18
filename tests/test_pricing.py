"""Tests for the price table, model-id normalization and cost math."""

import pytest

from agent_watch.model import Agent, Source
from agent_watch.pricing import PRICES, Price, estimate_cost, fmt_cost, normalize_model, price_for


def _agent(model: str, in_tokens: int = 0, out_tokens: int = 0, cached: int = 0) -> Agent:
    return Agent(
        key="k", source=Source.CLAUDE, session_key="s", model=model,
        in_tokens=in_tokens, out_tokens=out_tokens, cached_tokens=cached,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "claude-opus-5-5",
        "claude-opus-5-5-high",
        "claude-opus-5-5-low",
        "CLAUDE-OPUS-5-5",
        "claude-opus-5.5",
        "claude-opus-5-5[1m]",
        "anthropic.claude-opus-5-5",
        "us.anthropic.claude-opus-5-5",
        "claude-opus-5-5-20261101",
        "opus-5-5",
    ],
)
def test_opus_5_5_variants_normalize(raw: str) -> None:
    assert normalize_model(raw) == "claude-opus-5-5"
    assert price_for(raw) == Price(4.0, 0.2, 20.0)


def test_exact_table_values() -> None:
    assert PRICES["claude-opus-5-5"] == Price(4.0, 0.2, 20.0)
    assert PRICES["claude-opus-5"] == Price(5.0, 0.5, 25.0)
    assert PRICES["claude-sonnet-5-5"] == Price(2.0, 0.1, 10.0)
    assert PRICES["claude-sonnet-4-6"] == Price(3.0, 0.3, 15.0)
    assert PRICES["claude-haiku-5-5"] == Price(0.1, 0.01, 0.5)
    assert PRICES["claude-haiku-4-5"] == Price(1.0, 0.1, 5.0)
    assert PRICES["claude-fable-5-2"] == Price(8.0, 0.2, 40.0)
    assert PRICES["claude-fable-5-1"].cached == 0.25
    assert PRICES["claude-fable-5"].cached == 1.0


def test_dated_and_effort_variants_of_other_models() -> None:
    assert price_for("claude-haiku-4-5-20251001") == PRICES["claude-haiku-4-5"]
    assert price_for("claude-sonnet-5-5-high") == PRICES["claude-sonnet-5-5"]
    assert price_for("claude-opus-5") == PRICES["claude-opus-5"]  # not confused with 5-5


@pytest.mark.parametrize(
    "model",
    ["", None, "sonnet", "opus", "gemini-4-hydrogen-high", "gemini-4-flash", "gpt-5.6-terra", "claude-opus-4-5", "claude-sonnet-4-5-20250929"],
)
def test_unknown_models_have_no_price(model) -> None:
    assert price_for(model) is None
    assert estimate_cost(_agent(model or "", 1000, 1000)) is None


def test_cost_math_with_cache() -> None:
    # opus 5.5: 600k fresh * $4 + 400k cached * $0.20 + 100k out * $20 per 1M
    cost = estimate_cost(_agent("claude-opus-5-5", in_tokens=1_000_000, out_tokens=100_000, cached=400_000))
    assert cost == pytest.approx(0.6 * 4.0 + 0.4 * 0.2 + 0.1 * 20.0)


def test_cost_math_no_cache_and_zero() -> None:
    assert estimate_cost(_agent("claude-sonnet-5-5", 45_000, 7_000)) == pytest.approx(0.09 + 0.07)
    assert estimate_cost(_agent("claude-sonnet-5-5")) == 0.0


def test_cached_larger_than_input_is_clamped() -> None:
    cost = estimate_cost(_agent("claude-opus-5-5", in_tokens=1000, out_tokens=0, cached=5000))
    assert cost == pytest.approx(1000 * 0.2 / 1_000_000)


def test_fmt_cost() -> None:
    assert fmt_cost(1.234) == "≈ $1.23"
    assert fmt_cost(0.41) == "≈ $0.41"
    assert fmt_cost(0.001) == "≈ <$0.01"
    assert fmt_cost(0.0) == "≈ $0.00"
    assert fmt_cost(1234.5) == "≈ $1,234.50"
