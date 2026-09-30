"""Entry point: `python -m black_number` or the `bn` / `blacknumber` command."""
from __future__ import annotations

import sys

from .ui.cli import run


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
