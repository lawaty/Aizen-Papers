#!/usr/bin/env python3
"""Rebuild ``vendor/`` from the local virtualenv for no-``pip`` hosts.

Some shared hosts (cPanel) let you upload files but give you no shell to run
``pip``. Every runtime dependency here is pure Python, so we can copy the
already-installed copies out of the venv into ``vendor/`` and ship that.

Only Python source is copied: compiled ``.so`` extensions are removed because
they are built for the interpreter version and ABI of the machine that produced
them and would not import elsewhere. ``charset_normalizer`` ships a pure-Python
fallback for exactly this reason.

Usage:
    python tools/build_vendor.py            # uses ./.venv
    python tools/build_vendor.py /path/venv
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Runtime deps, with the vendored dir name differing from the import name.
PACKAGES = {
    "requests": "requests",
    "urllib3": "urllib3",
    "certifi": "certifi",
    "charset_normalizer": "charset_normalizer",
    "idna": "idna",
    "dotenv": "dotenv",
}

# Directories that are noise (and break pytest collection) inside vendor/.
DROP_DIRS = {"__pycache__", "tests", "test"}
DROP_SUFFIXES = (".so", ".pyd", ".pyc", ".pyo")


def site_packages(venv: Path) -> Path:
    candidates = sorted(venv.glob("lib/python*/site-packages"))
    if candidates:
        return candidates[0]
    # Windows layout: <venv>/Lib/site-packages
    windows = venv / "Lib" / "site-packages"
    if windows.is_dir():
        return windows
    raise SystemExit(f"no site-packages under {venv}")


def prune(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            if child.name in DROP_DIRS:
                shutil.rmtree(child, ignore_errors=True)
        elif child.suffix in DROP_SUFFIXES:
            child.unlink(missing_ok=True)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    venv = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else root / ".venv"
    site = site_packages(venv)
    target = root / "vendor"

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    missing = []
    for import_name, dir_name in PACKAGES.items():
        source = site / dir_name
        if not source.is_dir():
            missing.append(import_name)
            continue
        destination = target / dir_name
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*DROP_DIRS, "*.dist-info", "*.so", "*.pyd", "*.pyc"),
        )
        prune(destination)

    if missing:
        print(f"error: not found in {site}: {', '.join(missing)}", file=sys.stderr)
        print("install them first: pip install -r requirements.txt", file=sys.stderr)
        return 1

    total = sum(f.stat().st_size for f in target.rglob("*.py"))
    print(f"vendored {len(PACKAGES)} packages into {target} ({total / 1024:.0f} KiB of Python)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())