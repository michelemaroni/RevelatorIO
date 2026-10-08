#!/usr/bin/env python3
"""Synchronize the explicit public allowlist into a clean Git checkout."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]
MANIFEST = SOURCE / "PUBLIC_FILES.txt"


def run_git(checkout, *args):
    result = subprocess.run(
        ("git", "-C", str(checkout), *args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "git command failed")
    return result.stdout


def public_paths():
    paths = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value == ".git":
            raise SystemExit("unsafe public path: %s" % value)
        source = SOURCE / path
        if not source.is_file():
            raise SystemExit("public source is missing: %s" % value)
        paths.append(value)
    if len(paths) != len(set(paths)):
        raise SystemExit("PUBLIC_FILES.txt contains duplicate paths")
    return tuple(paths)


def exact_checkout(path):
    checkout = path.expanduser().resolve()
    top = Path(run_git(checkout, "rev-parse", "--show-toplevel").strip())
    if top.resolve() != checkout:
        raise SystemExit("destination must be the checkout root: %s" % checkout)
    return checkout


def check(checkout, allowed):
    tracked = set(run_git(checkout, "ls-files").splitlines())
    wanted = set(allowed)
    missing = sorted(wanted - tracked)
    extra = sorted(tracked - wanted)
    if missing or extra:
        if missing:
            print("missing tracked public files:", *missing, sep="\n  ")
        if extra:
            print("tracked files outside allowlist:", *extra, sep="\n  ")
        return 1
    print("public checkout matches %d-file allowlist" % len(allowed))
    return 0


def synchronize(checkout, allowed):
    if run_git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise SystemExit("destination checkout must be clean before export")

    wanted = set(allowed)
    tracked = set(run_git(checkout, "ls-files").splitlines())
    for value in sorted(tracked - wanted):
        target = checkout / value
        if target.is_file() or target.is_symlink():
            target.unlink()

    for value in allowed:
        source = SOURCE / value
        target = checkout / value
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    print("synchronized %d allowlisted files into %s" %
          (len(allowed), checkout))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path,
                        help="clean public Git checkout to inspect or update")
    parser.add_argument("--check", action="store_true",
                        help="compare tracked files with the allowlist")
    args = parser.parse_args(argv)
    allowed = public_paths()
    checkout = exact_checkout(args.checkout)
    if args.check:
        return check(checkout, allowed)
    return synchronize(checkout, allowed)


if __name__ == "__main__":
    sys.exit(main())
