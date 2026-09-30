"""Aizen invoice sender package.

Pure-path bootstrap for hosts without ``pip``. If a sibling ``vendor/``
directory is present, it is appended to :data:`sys.path` so the vendored
``requests``/``python-dotenv`` are importable. It is *appended*, so a real
virtualenv on ``sys.path`` always wins and a normal checkout behaves exactly
as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"


def _bootstrap_vendor() -> None:
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    if not vendor.is_dir():
        return
    entry = str(vendor)
    if entry not in sys.path:
        sys.path.append(entry)


_bootstrap_vendor()