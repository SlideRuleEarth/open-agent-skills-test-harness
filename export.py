#!/usr/bin/env python3
"""Export each skill directory into a zip file for upload as project knowledge.

Each skill directory (identified by containing a SKILL.md) is zipped into
its own archive preserving the directory structure.

Usage:
    python export.py                  # writes to exports/
    python export.py -o dist/skills   # writes to dist/skills/
    python export.py sliderule-pipeline-direct_request    # export only one skill
"""

import argparse
import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SKILLS_SUBDIR = "skills_examples"
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


def discover_skills(repo: Path) -> list[Path]:
    root = repo / SKILLS_SUBDIR
    skills = []
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                skills.append(entry)
    return skills


def zip_skill(skill_dir: Path, out_dir: Path) -> Path:
    out_file = out_dir / f"{skill_dir.name}.zip"
    with zipfile.ZipFile(out_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, filenames in os.walk(skill_dir):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fname in sorted(filenames):
                fpath = Path(root) / fname
                arcname = fpath.relative_to(skill_dir.parent)
                zf.write(fpath, arcname)
    return out_file


def main():
    parser = argparse.ArgumentParser(description="Export skills as zip files for project knowledge upload")
    parser.add_argument(
        "-o", "--output-dir",
        default=str(REPO_ROOT / "exports"),
        help="Output directory (default: exports/)",
    )
    parser.add_argument(
        "skills",
        nargs="*",
        help="Specific skill names to export (default: all)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_skills = discover_skills(REPO_ROOT)
    if args.skills:
        selected = []
        for name in args.skills:
            matches = [s for s in all_skills if s.name == name]
            if not matches:
                print(f"error: no skill directory named '{name}'", file=sys.stderr)
                sys.exit(1)
            selected.extend(matches)
        all_skills = selected

    if not all_skills:
        print("No skills found.", file=sys.stderr)
        sys.exit(1)

    print(f"Exporting {len(all_skills)} skill(s) into {out_dir}/\n")

    for skill_dir in all_skills:
        out_file = zip_skill(skill_dir, out_dir)
        size_kb = out_file.stat().st_size / 1024
        print(f"  {out_file.name:40s} {size_kb:6.1f} KB")

    print(f"\nDone. Upload the zip files from {out_dir}/ to your project knowledge.")


if __name__ == "__main__":
    main()
