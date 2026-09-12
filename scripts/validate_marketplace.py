#!/usr/bin/env python3
"""Pre-submission QA validator for the Agent Skills marketplace.

Mirrors the official Agent Skills structural checks (SKILL.md frontmatter,
naming rules, directory-name parity) plus marketplace.json integrity:
exactly one entrypoint, all paths resolve, no duplicate skill ids.

Exit code 0 = all checks passed; 1 = at least one failure found.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / "marketplace.json"

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SKILL_NAME_LEN = 64
REQUIRED_FRONTMATTER = ("name", "description")
FORBIDDEN_REFS = (".venv", "__pycache__", ".pytest_cache", "output/")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-ish frontmatter (name/description lines) from SKILL.md."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    body = lines[1:end]
    rest = "\n".join(lines[end + 1:])
    fm: dict = {}
    for line in body:
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip('"\'')
    return fm, rest


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    if not MARKETPLACE.exists():
        failures.append("marketplace.json does not exist")
        return 1

    mp = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    required_keys = ("name", "version", "description", "skills")
    for key in required_keys:
        if key not in mp:
            failures.append(f"marketplace.json missing required key: {key}")

    skills = mp.get("skills", [])
    ids = [s.get("id") for s in skills]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        failures.append(f"duplicate skill id: {dup}")
    entrypoints = [s for s in skills if s.get("entrypoint") is True]
    if len(entrypoints) != 1:
        failures.append(f"expected exactly one entrypoint, found {len(entrypoints)}")

    declared = {s.get("id") for s in skills}
    dirs_seen: set[str] = set()
    for skill in skills:
        sid = skill.get("id")
        path = skill.get("path")
        if not sid or not path:
            failures.append(f"skill missing id/path: {skill}")
            continue
        if not SKILL_NAME_RE.match(sid) or len(sid) > MAX_SKILL_NAME_LEN:
            failures.append(f"skill name violates Agent Skills naming rules: {sid!r}")
        skill_dir = ROOT / path
        if not skill_dir.is_dir():
            failures.append(f"skill directory not found: {path}")
            continue
        if skill_dir.name != sid:
            failures.append(f"directory name {skill_dir.name!r} != skill name {sid!r}")
        if skill_dir.name in dirs_seen:
            failures.append(f"skill directory referenced twice: {skill_dir.name}")
        dirs_seen.add(skill_dir.name)
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            failures.append(f"{path}/SKILL.md missing")
            continue
        text = md.read_text(encoding="utf-8")
        fm, rest = parse_frontmatter(text)
        for key in REQUIRED_FRONTMATTER:
            if not fm.get(key):
                failures.append(f"{path}/SKILL.md missing frontmatter key: {key}")
        for forbid in FORBIDDEN_REFS:
            if forbid in text:
                warnings.append(f"{path}/SKILL.md references forbidden path/remnant: {forbid}")

        # Validate any backtick-relative paths under skills/ mentioned in the body.
        for tok in re.findall(r"`([^`]+)`", rest):
            if tok.startswith("skills/") and not (ROOT / tok).exists():
                failures.append(f"{path}/SKILL.md dead reference: {tok!r}")

        # Validate relative paths like paths to scripts (e.g. scripts/...).
        for tok in re.findall(r"`([^`]+)`", rest):
            if tok.startswith("scripts/") and not (ROOT / tok).exists():
                failures.append(f"{path}/SKILL.md dead reference: {tok!r}")

    for sid in declared:
        if sid not in dirs_seen:
            failures.append(f"declared skill has no matching SKILL.md directory: {sid}")

    exit_code = 1 if failures else 0
    print("=== Marketplace / Agent Skills Validation ===")
    for f in failures:
        print(f"  FAIL: {f}")
    for w in warnings:
        print(f"  WARN: {w}")
    if not failures:
        print("  All marketplace and Agent Skills structural checks PASSED.")
    print(f"Result: {'FAIL' if failures else 'PASS'} ({len(skills)} skills, "
          f"{len(failures)} failures, {len(warnings)} warnings)")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())