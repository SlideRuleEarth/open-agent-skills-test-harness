# Skill Upload Guide

This export contains the **example skills** bundled with the open-agent-skills-test-harness repo. They currently teach the agent how to work with [SlideRule Earth](https://slideruleearth.io), a NASA-funded service for on-demand processing of ICESat-2 and GEDI satellite data — but the export tooling works for any skill kept in the repo.

## What's in each zip

Each `.zip` file is one self-contained skill:

| File | What it does |
|---|---|
| `sliderule-pipeline.zip` | Directives for orchestrating multi-step analyses as single scripts |
| `sliderule-region-picker.zip` | Interactive map for defining geographic regions |

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

**For most users**, upload both. They're independent, standalone tools: `sliderule-pipeline` guides consolidating an analysis into a single reproducible script, and `sliderule-region-picker` provides an interactive map for defining the geographic region.

**If you only need one**, both skills are standalone:

- `sliderule-pipeline` and `sliderule-region-picker` are independent — neither depends on the other.

## Regenerating the exports

If you have the source repo, regenerate all exports with:

```bash
python export.py
```

Or export specific skills:

```bash
python export.py sliderule-pipeline sliderule-region-picker
```

Output goes to `exports/` by default (override with `-o <dir>`).

## Notes

- Both skills are prose-only — the `SKILL.md` file in each zip is the complete instruction set, with no Python scripts to execute. They work anywhere the agent can read the skill text.
