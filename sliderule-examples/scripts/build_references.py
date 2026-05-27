"""Build static reference files from SlideRule Jupyter notebook examples.

Parses each .ipynb in the sliderule repo's examples directory and produces:
  - references/{stem}.py  — code cells verbatim, markdown cells as comments
  - references/index.md   — topic/API/keyword index for the agent

Run from the sliderule-skills repo root with the sliderule repo adjacent:

    python sliderule-examples/scripts/build_references.py

Or specify the repo explicitly:

    python sliderule-examples/scripts/build_references.py --repo /path/to/sliderule
"""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REFERENCES_DIR = SKILL_DIR / "references"

DEFAULT_REPO = SKILL_DIR.parent.parent / "sliderule"
EXAMPLES_SUBPATH = Path("clients/python/examples")

GITHUB_BASE = "https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples"

TOPIC_RULES: list[tuple[str, str]] = [
    (r"first_request|getting.started", "getting-started"),
    (r"atl24|bathy|coastal", "bathymetry"),
    (r"phoreal|canopy|boulder.watershed", "canopy"),
    (r"atl06|glims|glacier|grandmesa(?!.*classif)", "land-ice"),
    (r"atl13|inland.water|lake", "inland-water"),
    (r"gedi|biomass", "gedi"),
    (r"arcticdem|user_url_raster", "raster"),
    (r"geoparquet|(?:las.output)", "output-format"),
    (r"earthdata|cmr|query", "data-discovery"),
    (r"ancillary|field", "ancillary"),
    (r"atl03|photon|classif", "photon-data"),
    (r"atl09|atmo", "atmosphere"),
    (r"job_runner|blanket", "advanced"),
]

API_PATTERN = re.compile(
    r"(?:icesat2|gedi|sliderule|arcticdem)\."
    r"(atl\d+\w*|gedil\d+\w*|init|toregion|raster|mosaic|strip)"
    r"\s*\("
)


def classify_topic(stem: str) -> str:
    for pattern, topic in TOPIC_RULES:
        if re.search(pattern, stem, re.IGNORECASE):
            return topic
    return "general"


def extract_title(cells: list[dict]) -> str:
    for cell in cells:
        if cell.get("cell_type") == "markdown":
            source = "".join(cell.get("source", []))
            for line in source.splitlines():
                line = line.strip()
                if line.startswith("# "):
                    return line.lstrip("# ").strip()
    return ""


def extract_apis(cells: list[dict]) -> list[str]:
    apis: set[str] = set()
    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        for match in API_PATTERN.finditer(source):
            apis.add(match.group(1))
    return sorted(apis)


def markdown_to_comments(source: str) -> str:
    lines = source.strip().splitlines()
    if not lines:
        return ""

    heading = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            break

    result_lines: list[str] = []

    if heading:
        result_lines.append(f"# --- {heading} ---")
    else:
        result_lines.append("# ---")

    for line in lines:
        stripped = line.strip()
        if heading and stripped.startswith("#") and stripped.lstrip("#").strip() == heading:
            continue
        if stripped:
            for wrapped in textwrap.wrap(stripped, width=76):
                result_lines.append(f"# {wrapped}")
        else:
            result_lines.append("#")

    return "\n".join(result_lines)


def notebook_to_python(cells: list[dict]) -> str:
    parts: list[str] = []

    for cell in cells:
        cell_type = cell.get("cell_type", "")
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue

        if cell_type == "markdown":
            parts.append(markdown_to_comments(source))
        elif cell_type == "code":
            parts.append(source.rstrip())

    return "\n\n".join(parts) + "\n"


def build_index(entries: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# SlideRule Example Index")
    lines.append("")
    lines.append("## Examples")
    lines.append("")
    lines.append("| File | Topic | Title | APIs | Notebook |")
    lines.append("|---|---|---|---|---|")

    for e in sorted(entries, key=lambda x: x["stem"]):
        apis = ", ".join(e["apis"]) if e["apis"] else "—"
        link = f"[notebook]({GITHUB_BASE}/{e['stem']}.ipynb)"
        lines.append(f"| {e['stem']}.py | {e['topic']} | {e['title']} | {apis} | {link} |")

    by_topic: dict[str, list[str]] = {}
    for e in entries:
        by_topic.setdefault(e["topic"], []).append(f"{e['stem']}.py")

    lines.append("")
    lines.append("## By Topic")
    lines.append("")
    for topic in sorted(by_topic):
        files = ", ".join(sorted(by_topic[topic]))
        lines.append(f"- **{topic}**: {files}")

    by_api: dict[str, list[str]] = {}
    for e in entries:
        for api in e["apis"]:
            by_api.setdefault(api, []).append(f"{e['stem']}.py")

    lines.append("")
    lines.append("## By API")
    lines.append("")
    for api in sorted(by_api):
        files = ", ".join(sorted(set(by_api[api])))
        lines.append(f"- **{api}**: {files}")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help=f"Path to the sliderule repo (default: {DEFAULT_REPO})")
    args = parser.parse_args()

    examples_dir = args.repo / EXAMPLES_SUBPATH
    if not examples_dir.is_dir():
        print(f"ERROR: examples directory not found: {examples_dir}")
        print(f"  Expected at: <repo>/{EXAMPLES_SUBPATH}")
        print(f"  Repo path: {args.repo}")
        return 1

    notebooks = sorted(examples_dir.glob("*.ipynb"))
    if not notebooks:
        print(f"ERROR: no .ipynb files found in {examples_dir}")
        return 1

    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    count = 0

    for nb_path in notebooks:
        stem = nb_path.stem

        with open(nb_path, "r", encoding="utf-8") as f:
            nb = json.load(f)

        cells = nb.get("cells", [])
        if not cells:
            print(f"  SKIP {nb_path.name} (no cells)")
            continue

        py_content = notebook_to_python(cells)
        py_path = REFERENCES_DIR / f"{stem}.py"
        py_path.write_text(py_content, encoding="utf-8")

        title = extract_title(cells)
        topic = classify_topic(stem)
        apis = extract_apis(cells)

        entries.append({
            "stem": stem,
            "title": title,
            "topic": topic,
            "apis": apis,
        })

        count += 1
        print(f"  {stem}.py  ({topic})")

    index_content = build_index(entries)
    index_path = REFERENCES_DIR / "index.md"
    index_path.write_text(index_content, encoding="utf-8")

    print(f"\nBuilt {count} reference files + index.md in {REFERENCES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
