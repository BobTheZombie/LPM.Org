"""Small :mod:`tqdm` compatibility shim used in the test environment."""

from __future__ import annotations

from typing import Iterable, Iterator, Optional


class tqdm:
    """Minimal progress bar implementation compatible with the real API."""

    def __init__(
        self,
        iterable: Optional[Iterable] = None,
        *,
        total: Optional[int] = None,
        desc: Optional[str] = None,
        unit: str = "it",
        **_: object,
    ) -> None:
        self.iterable = iterable if iterable is not None else []
        self.total = total
        self.n = 0
        self.unit = unit
        self.desc = desc

    def __iter__(self) -> Iterator:
        for item in self.iterable:
            self.n += 1
            yield item

    def __enter__(self) -> "tqdm":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - cleanup is trivial
        return None

    def update(self, n: int = 1) -> None:
        self.n += n

    def set_description(self, desc: Optional[str] = None, **_: object) -> None:
        self.desc = desc

    def set_postfix(self, *_, **__):  # pragma: no cover - compatibility shim
        return None

    def close(self) -> None:  # pragma: no cover - compatibility shim
        return None

    def refresh(self) -> None:  # pragma: no cover - compatibility shim
        return None


__all__ = ["tqdm"]
