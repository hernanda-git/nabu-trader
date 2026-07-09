"""Auto-bump the package version on every deploy.

Reads the current version from ``src/version.py``, increments the numeric
part (v55 -> v56 -> ...), writes it back, and prints the new version so a
caller (or CI) can consume it. This guarantees the Telegram ``/version``
command and startup banner reflect the actual deployed build instead of
being stuck on a hardcoded number.

Usage:
    python scripts/bump_version.py            # increments and writes src/version.py
    python scripts/bump_version.py --dry-run  # prints next version without writing
    python scripts/bump_version.py --set v99  # force a specific version
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "src" / "version.py"

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']v?(\d+)["\']')


def read_version() -> tuple[int, str]:
    """Return (number, raw_text) of the current version, or (0, 'v0')."""
    text = VERSION_FILE.read_text(encoding="utf-8") if VERSION_FILE.exists() else ""
    m = _VERSION_RE.search(text)
    if not m:
        return 0, "v0"
    return int(m.group(1)), text


def write_version(num: int) -> None:
    new_text = f'"""Package version — updated automatically during deploy."""\n\n__version__ = "v{num}"\n'
    VERSION_FILE.write_text(new_text, encoding="utf-8")


def next_version(force: str | None = None) -> int:
    cur, _ = read_version()
    if force:
        m = re.match(r"v?(\d+)", force)
        if not m:
            raise SystemExit(f"Invalid --set value: {force!r} (expected e.g. v99)")
        return int(m.group(1))
    return cur + 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Bump package version")
    ap.add_argument("--dry-run", action="store_true", help="print next version without writing")
    ap.add_argument("--set", dest="set_ver", default=None, help="force a specific version, e.g. v99")
    args = ap.parse_args()

    nxt = next_version(args.set_ver)
    if args.dry_run:
        print(f"v{nxt}")
        return

    write_version(nxt)
    print(f"v{nxt}")


if __name__ == "__main__":
    main()
