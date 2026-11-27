"""Module entry point for `python -m wx_observer`."""

from __future__ import annotations

import sys

from wx_observer.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
