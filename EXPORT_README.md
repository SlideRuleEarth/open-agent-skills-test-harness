# SlideRule Skills — Upload Guide

This export contains a set of skills that teach the agent how to work with [SlideRule Earth](https://slideruleearth.io), a NASA-funded service for on-demand processing of ICESat-2 and GEDI satellite data.

## What's in each zip

Each `.zip` file is one self-contained skill:

| File | What it does |
|---|---|
| `sliderule-api.zip` | HTTP mechanics for calling the SlideRule processing API |
| `sliderule-params.zip` | Systematic planning of request parameters before making API calls |
| `sliderule-openapi.zip` | Structured lookups against the SlideRule OpenAPI spec (parameters, output columns, field enumerations) |
| `sliderule-docsearch.zip` | Semantic search over SlideRule documentation |
| `sliderule-examples.zip` | Worked Python examples from the SlideRule client notebooks |
| `sliderule-pipeline.zip` | Directives for orchestrating multi-step analyses as single scripts |
| `sliderule-region-picker.zip` | Interactive map for defining geographic regions |
| `nsidc-reference.zip` | Search NASA NSIDC/ORNL DAAC reference docs for ICESat-2 and GEDI science |

## How to upload

Upload the `.zip` files to your AI platform's project knowledge or context system. Each zip becomes a separate knowledge entry that the agent can reference.

For example, on claude.ai:

**Adding a new skill:**

1. Go to **Customize** → **Skills**.
2. Click **+** → **+ Create skill**.
3. Upload the `.zip` file.

**Replacing an existing skill:**

1. Go to **Customize** → **Skills**.
2. Find the skill entry and click the **⋮** menu.
3. Choose **Replace**, then upload the new zip.

## Which skills to upload

**For most users**, upload all of them. The skills are designed to work together — when the agent is planning a SlideRule request, it uses `sliderule-params` for parameter planning, `sliderule-openapi` for schema lookups, and `sliderule-api` for the HTTP call.

**If you only need a subset**, here are the dependency relationships:

- `sliderule-params` references `sliderule-openapi` for schema lookups
- `sliderule-api` references `sliderule-openapi` for schema details and `sliderule-params` for parameter planning
- `sliderule-docsearch` and `nsidc-reference` are independent search tools
- `sliderule-examples` is standalone reference material
- `sliderule-pipeline` and `sliderule-region-picker` are standalone

## Regenerating the exports

If you have the source repo, regenerate all exports with:

```bash
python export.py
```

Or export specific skills:

```bash
python export.py sliderule-api sliderule-params
```

Output goes to `exports/` by default (override with `-o <dir>`).

## Notes

- Several skills include Python scripts that the agent runs wherever it has a code sandbox with network access — that includes local runtimes (Claude Code, Cursor, Windsurf) **and** hosted ones like claude.ai, which executes skills in a container. The scripts become read-only reference only when a session has no code sandbox, no network grant, or a sandbox whose egress allowlist excludes the target host (e.g. the search endpoint or `docs.slideruleearth.io`); in that case the agent can read the script text but not run it. The scripts:
  - `nsidc-reference` — `scripts/search.py` (semantic search over NSIDC/ORNL DAAC docs)
  - `sliderule-docsearch` — `scripts/search.py` (semantic search over SlideRule docs), `scripts/fetch_doc.py` (fetch a full docs page as fallback)
  - `sliderule-openapi` — `scripts/openapi.py` (loads and slices the OpenAPI spec by endpoint, parameter, or schema)
- The `SKILL.md` file in each zip is the main instruction set. The `references/` and `scripts/` directories contain supplementary material that the instructions reference.
