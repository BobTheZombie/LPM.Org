"""Console entry-point bootstrap helpers for :mod:`lpm`."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys
from typing import Iterable, Callable


CLI_MAIN_IMPORT = "lpm.app"


def _strip_script_directory() -> None:
    """Remove the console script's directory from :data:`sys.path`.

    When ``lpm`` is invoked via the ``console_scripts`` entry point, Python adds
    the directory containing the launcher (for example ``/usr/bin``) to the
    beginning of :data:`sys.path`.  Some environments also ship a helper module
    named :mod:`lpm` alongside the launcher, which shadows the real package and
    causes ``import lpm.app`` to fail with ``'lpm' is not a package``.  Stripping
    this leading entry ensures that import resolution falls back to the actual
    installation in ``site-packages``.
    """

    if not sys.path:
        return

    try:
        script_dir = Path(sys.argv[0]).resolve().parent
        leading = Path(sys.path[0]).resolve()
    except Exception:
        return

    if leading == script_dir:
        sys.path.pop(0)


def _load_cli_main() -> Callable[[Iterable[str] | None], int | None]:
    module = import_module(CLI_MAIN_IMPORT)
    return module.main


def main(argv: Iterable[str] | None = None) -> int:
    """Execute :mod:`lpm`'s command-line interface."""

    _strip_script_directory()
    cli_main = _load_cli_main()
    if argv is None:
        return cli_main()
    return cli_main(list(argv))


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    raise SystemExit(main())
