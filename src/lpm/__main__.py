"""Module entry point for running ``python -m lpm``."""

from .app import main as _main


def main() -> int:
    """Execute :mod:`lpm`'s legacy CLI entry point."""

    return _main()


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    raise SystemExit(main())
