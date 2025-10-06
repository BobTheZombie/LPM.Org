"""Minimal core functionality emulating a sliver of Hypothesis."""

from __future__ import annotations

import functools
import inspect
import random
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


@dataclass
class Settings:
    """Container for configuration used by :func:`given`.

    Only the ``max_examples`` option is supported, mirroring the usage in the
    test-suite.  Additional keyword arguments are accepted for API
    compatibility but are otherwise ignored.
    """

    max_examples: int = 100

    def __init__(self, max_examples: int = 100, **_: Any) -> None:
        self.max_examples = max_examples


def settings(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator used to attach :class:`Settings` to a test function."""

    cfg = Settings(**kwargs)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "_hypothesis_settings", cfg)
        return func

    return decorator


class Strategy:
    """Simple strategy object used for data generation."""

    def __init__(self, sampler: Callable[[random.Random], Any]):
        self._sampler = sampler

    def sample(self, rng: random.Random) -> Any:
        return self._sampler(rng)

    def map(self, transform: Callable[[Any], Any]) -> "Strategy":
        return Strategy(lambda rng: transform(self.sample(rng)))

    def flatmap(self, builder: Callable[[Any], "Strategy"]) -> "Strategy":
        def sampler(rng: random.Random) -> Any:
            inner = builder(self.sample(rng))
            if not isinstance(inner, Strategy):
                raise TypeError("flatmap builder must return a Strategy")
            return inner.sample(rng)

        return Strategy(sampler)


def _ensure_strategy(value: Any) -> Strategy:
    if isinstance(value, Strategy):
        return value
    raise TypeError("expected a Strategy instance")


def integers(*, min_value: int, max_value: int) -> Strategy:
    if min_value > max_value:
        raise ValueError("min_value must be <= max_value")

    return Strategy(lambda rng: rng.randint(min_value, max_value))


def booleans() -> Strategy:
    return Strategy(lambda rng: bool(rng.getrandbits(1)))


def builds(func: Callable[..., Any], *strategies: Strategy) -> Strategy:
    strategies = tuple(_ensure_strategy(s) for s in strategies)

    def sampler(rng: random.Random) -> Any:
        samples = [strategy.sample(rng) for strategy in strategies]
        return func(*samples)

    return Strategy(sampler)


def lists(
    strategy: Strategy,
    *,
    min_size: int = 0,
    max_size: Optional[int] = None,
) -> Strategy:
    strategy = _ensure_strategy(strategy)
    if max_size is None:
        raise ValueError("max_size must be provided")
    if min_size > max_size:
        raise ValueError("min_size must be <= max_size")

    def sampler(rng: random.Random) -> List[Any]:
        length = rng.randint(min_size, max_size)
        return [strategy.sample(rng) for _ in range(length)]

    return Strategy(sampler)


def given(*strategies: Strategy) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    strategies = tuple(_ensure_strategy(s) for s in strategies)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cfg: Settings = getattr(wrapper, "_hypothesis_settings", Settings())
            rng = random.Random(0xC0FFEE)
            for _ in range(cfg.max_examples):
                samples = [strategy.sample(rng) for strategy in strategies]
                func(*args, *samples, **kwargs)

        wrapper.__signature__ = inspect.Signature(parameters=[])
        return wrapper

    return decorator


# Re-export strategy constructors for ``import hypothesis.strategies as st``.
class _StrategiesModule:
    integers = staticmethod(integers)
    booleans = staticmethod(booleans)
    builds = staticmethod(builds)
    lists = staticmethod(lists)

    @staticmethod
    def from_callable(func: Callable[..., Any]) -> Strategy:
        return Strategy(lambda rng: func())


strategies = _StrategiesModule()
