"""``python -m tradebot`` entry point, identical to the ``tradebot`` console script."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
