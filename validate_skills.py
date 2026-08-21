#!/usr/bin/env python3
"""Validate every skill's SKILL.md frontmatter against the Skills API limits.

`name` and `description` must resolve from the frontmatter and fit within their
length limits, or POST /v1/skills rejects the upload. The API reports this as a
bare 400 that names neither the offending skill nor the overage, and only at
upload time — long after the PR that introduced it merged. This catches it in
review instead.

Skills are discovered the same way the Makefile discovers them, so a new skill
is covered the moment it exists.

Usage: make validate   (or: python3 validate_skills.py [--skills-dir DIR])
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# Limits enforced by the Skills API on SKILL.md frontmatter.
LIMITS = {"name": 64, "description": 1024}

# Flag skills that are valid but nearly out of room, so a one-sentence edit in
# a later PR does not silently reintroduce the failure. Proportional, not a flat
# count: 20 characters left is alarming for a 1024-char description and entirely
# normal for a 64-char name.
HEADROOM_WARN_FRACTION = 0.05


def check(skill_md: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one SKILL.md."""
    name = skill_md.parent.name
    match = re.match(r"^---\n(.*?)\n---\n", skill_md.read_text(encoding="utf-8"), re.S)
    if not match:
        return ([f"{name}: SKILL.md has no YAML frontmatter block."], [])
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        return ([f"{name}: SKILL.md frontmatter is not valid YAML ({exc})."], [])

    errors: list[str] = []
    warnings: list[str] = []
    for field, limit in LIMITS.items():
        value = (frontmatter.get(field) or "").strip()
        if not value:
            errors.append(f"{name}: frontmatter has no `{field}`.")
            continue
        length = len(value)
        if length > limit:
            errors.append(
                f"{name}: `{field}` is {length} chars, "
                f"{length - limit} over the {limit} limit."
            )
        elif limit - length <= limit * HEADROOM_WARN_FRACTION:
            warnings.append(
                f"{name}: `{field}` is {length} chars, only "
                f"{limit - length} under the {limit} limit."
            )
    return (errors, warnings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-dir",
        default="skills_examples",
        help="Directory holding <skill-name>/SKILL.md (default: skills_examples)",
    )
    args = parser.parse_args()

    root = Path(args.skills_dir)
    skill_files = sorted(root.glob("*/SKILL.md"))
    if not skill_files:
        print(f"No skills found under {root}/", file=sys.stderr)
        return 1

    errors: list[str] = []
    warnings: list[str] = []
    for skill_md in skill_files:
        skill_errors, skill_warnings = check(skill_md)
        errors.extend(skill_errors)
        warnings.extend(skill_warnings)
        status = "FAIL" if skill_errors else ("warn" if skill_warnings else "ok")
        print(f"  [{status:4}] {skill_md.parent.name}")

    for warning in warnings:
        print(f"\n  warning: {warning}")
    if errors:
        print(f"\n{len(errors)} problem(s) — these will be rejected on upload:\n")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"\n{len(skill_files)} skill(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
