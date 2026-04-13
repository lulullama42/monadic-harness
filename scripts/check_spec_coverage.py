#!/usr/bin/env python3
"""Spec reconciliation: check which spec IDs have test coverage.

Scans docs/specs.md for all spec IDs (e.g., A1, C3, D18, P9).
Scans tests/ for "Verifies: <ID>" references.
Reports covered, uncovered, and orphaned (referenced but not in spec) IDs.

Usage:
    python scripts/check_spec_coverage.py
    python scripts/check_spec_coverage.py --verbose
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS_PATH = ROOT / "docs" / "specs.md"
TESTS_DIR = ROOT / "tests"

# Match spec IDs like A1, C18, D19, P10, I8, R5, G5, K14, F10
SPEC_ID_RE = re.compile(r"\|\s*([A-Z]\d{1,2})\s*\|")
# Match "Verifies: X1, X2, ..." in test files
VERIFIES_RE = re.compile(r"Verifies:\s*((?:[A-Z]\d{1,2}(?:,\s*)?)+)")


def extract_spec_ids(specs_path: Path) -> dict[str, str]:
    """Extract all spec IDs and their one-line descriptions from specs.md."""
    ids: dict[str, str] = {}
    text = specs_path.read_text()

    for line in text.splitlines():
        m = SPEC_ID_RE.match(line.strip())
        if m:
            spec_id = m.group(1)
            # Extract description: second column of the table
            cols = [c.strip() for c in line.split("|") if c.strip()]
            desc = cols[1] if len(cols) > 1 else ""
            # Strip markdown bold
            desc = desc.replace("**", "")
            ids[spec_id] = desc
    return ids


def extract_test_coverage(tests_dir: Path) -> dict[str, list[str]]:
    """Extract spec IDs referenced in test files via 'Verifies:' comments."""
    coverage: dict[str, list[str]] = {}

    for test_file in sorted(tests_dir.glob("test_*.py")):
        text = test_file.read_text()
        for m in VERIFIES_RE.finditer(text):
            ids_str = m.group(1)
            for spec_id in re.findall(r"[A-Z]\d{1,2}", ids_str):
                coverage.setdefault(spec_id, []).append(test_file.name)
    return coverage


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    if not SPECS_PATH.exists():
        print(f"ERROR: {SPECS_PATH} not found")
        return 1

    spec_ids = extract_spec_ids(SPECS_PATH)
    test_coverage = extract_test_coverage(TESTS_DIR)

    covered = {sid for sid in spec_ids if sid in test_coverage}
    uncovered = {sid for sid in spec_ids if sid not in test_coverage}
    orphaned = {sid for sid in test_coverage if sid not in spec_ids}

    total = len(spec_ids)
    pct = len(covered) / total * 100 if total else 0

    print(f"Spec coverage: {len(covered)}/{total} ({pct:.0f}%)")
    print()

    if uncovered:
        print(f"UNCOVERED ({len(uncovered)}):")
        for sid in sorted(uncovered):
            print(f"  {sid}: {spec_ids[sid]}")
        print()

    if orphaned:
        print(f"ORPHANED (referenced in tests but not in specs) ({len(orphaned)}):")
        for sid in sorted(orphaned):
            files = ", ".join(test_coverage[sid])
            print(f"  {sid}: referenced in {files}")
        print()

    if verbose and covered:
        print(f"COVERED ({len(covered)}):")
        for sid in sorted(covered):
            files = ", ".join(test_coverage[sid])
            print(f"  {sid}: {spec_ids[sid]}  [{files}]")
        print()

    return 1 if uncovered else 0


if __name__ == "__main__":
    sys.exit(main())
