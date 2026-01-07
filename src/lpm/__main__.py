from __future__ import annotations

import sys
from pathlib import Path


def _load_main():
    if __package__ in {None, ""}:
        src_root = Path(__file__).resolve().parents[1]
        if str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
        from lpm.app import main as app_main
    else:
        from .app import main as app_main
    return app_main


def main() -> int:
    return _load_main()()


if __name__ == "__main__":
    raise SystemExit(main())
