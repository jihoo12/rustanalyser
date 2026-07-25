#!/usr/bin/env python3
"""Bump version across pyproject.toml and __init__.py, rebuild, and optionally publish."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "rust_analyzer" / "__init__.py"


def get_version() -> str:
    text = PYPROJECT.read_text()
    m = re.search(r'^version\s*=\s*"(.+?)"', text, re.MULTILINE)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return m.group(1)


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    sys.exit(f"Unknown bump part: {part}")


def set_version(new: str) -> None:
    # pyproject.toml
    text = PYPROJECT.read_text()
    PYPROJECT.write_text(re.sub(
        r'^(version\s*=\s*)".+?"',
        f'\\1"{new}"',
        text,
        count=1,
        flags=re.MULTILINE,
    ))
    # __init__.py
    INIT.write_text(f'__version__ = "{new}"\n')
    print(f"  {PYPROJECT.name}: {new}")
    print(f"  {INIT.name}: {new}")


def main() -> None:
    p = argparse.ArgumentParser(description="Bump version, rebuild, optionally publish.")
    p.add_argument("part", choices=["major", "minor", "patch"], help="Version part to bump")
    p.add_argument("--publish", action="store_true", help="Build and publish to PyPI after bump")
    p.add_argument("--token", default=None, help="PyPI token (or set UV_PUBLISH_TOKEN)")
    args = p.parse_args()

    old = get_version()
    new = bump(old, args.part)

    print(f"Bumping {old} -> {new} ({args.part})")
    set_version(new)

    print("Building...", flush=True)
    subprocess.run(["uv", "build"], cwd=ROOT, check=True)

    if args.publish:
        cmd = ["uv", "publish"]
        if args.token:
            cmd += ["--token", args.token]
        print("Publishing to PyPI...")
        subprocess.run(cmd, cwd=ROOT, check=True)
        print("Published!")
    else:
        print("Done. Run 'uv publish --token <TOKEN>' to publish.")


if __name__ == "__main__":
    main()
