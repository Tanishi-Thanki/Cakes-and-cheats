#!/usr/bin/env python3
"""Submission packaging script for Adobe University Hackathon Round 3.

Packs the project into a clean, portable zip archive excluding:
- version control (.git)
- virtual environments (.venv, venv)
- Python bytecode caches (__pycache__, *.pyc)
- test/tool caches (.pytest_cache, .mypy_cache)
- IDE configuration (.vscode, .idea)
- runtime scratch output (output/)
- large binary or model weight files

Verifies that the generated archive satisfies the <= 50 MB requirement.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import zipfile

MAX_ARCHIVE_BYTES: int = 50 * 1024 * 1024  # 50 MB

EXCLUDE_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".gemini",
    "output",
}

EXCLUDE_EXTENSIONS: set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".pt",
    ".pth",
    ".bin",
    ".onnx",
    ".safetensors",
    ".zip",
}


def should_include(rel_path: Path) -> bool:
    """Determine whether a path should be included in the submission."""
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS:
            return False
        if part.startswith(".git"):
            return False
    if rel_path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return False
    return True


def collect_submission_files(root: Path) -> list[Path]:
    """Collect all files eligible for submission, relative to root."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".git")]
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            rel_file = rel_dir / fname if str(rel_dir) != "." else Path(fname)
            if should_include(rel_file):
                files.append(rel_file)
    return sorted(files)


def build_package(
    root: Path,
    output_zip: Path,
    dry_run: bool = False,
) -> int:
    """Collect files and optionally build the submission zip archive."""
    eligible_files = collect_submission_files(root)
    total_uncompressed = sum((root / f).stat().st_size for f in eligible_files)

    print(f"=== Submission Packaging Audit ===")
    print(f"Root: {root.resolve()}")
    print(f"Eligible files: {len(eligible_files)}")
    print(f"Total uncompressed size: {total_uncompressed:,} bytes ({total_uncompressed / (1024 * 1024):.2f} MB)")

    if dry_run:
        print("[Dry Run] Archive generation skipped. All file checks passed.")
        return 0

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_file in eligible_files:
            abs_file = root / rel_file
            zf.write(abs_file, arcname=str(rel_file).replace("\\", "/"))

    compressed_size = output_zip.stat().st_size
    print(f"Generated archive: {output_zip}")
    print(f"Archive compressed size: {compressed_size:,} bytes ({compressed_size / (1024 * 1024):.2f} MB)")

    if compressed_size > MAX_ARCHIVE_BYTES:
        print(f"ERROR: Archive exceeds maximum allowed size of 50 MB ({compressed_size:,} bytes)!", file=sys.stderr)
        return 1

    print(f"SUCCESS: Submission archive is within the 50 MB limit.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Package submission archive.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("submission.zip"),
        help="Path for generated zip file (default: submission.zip)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit files and calculate sizes without writing the zip archive.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    return build_package(root=root, output_zip=args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
