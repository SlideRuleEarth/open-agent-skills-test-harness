---
name: sliderule-examples
description: >
  Find and present Jupyter notebook examples from the SlideRule Python
  client repository. Covers ICESat-2 (ATL03, ATL06, ATL08, ATL13, ATL24),
  GEDI, ArcticDEM, and general SlideRule workflows. Use when the user asks
  for example code, wants to see how a specific API or feature is used in
  practice, needs a starting point for their analysis, or asks "show me an
  example of X", "is there an example of...", "how is atl06p used", "what
  notebooks exist". Also trigger when building a request and an example
  would help illustrate the parameter configuration pattern.
---

# SlideRule Examples

## Requirements

No runtime dependencies. The reference files are pre-built Python files
extracted from the SlideRule repo's Jupyter notebook examples.

To rebuild references after upstream notebook changes:

```bash
python scripts/build_references.py
```

This assumes the `sliderule` repo is checked out adjacent to this repo
(`../sliderule`). Override with `--repo /path/to/sliderule`.

## Architecture

Static reference files. Each notebook is converted to a `.py` file —
code cells verbatim, markdown cells as `# ` comment blocks — preserving
the notebook's narrative flow as readable Python. An `index.md` maps
topics and APIs to filenames.

No network access, no caching, no runtime scripts. The agent reads
files directly.

## Use or skip?

This skill is the right tool for **worked example code**: "show me how
to use atl06p", "is there an example of GeoParquet output", "how does
the PhoREAL notebook set up parameters", "what notebooks exist". Use
when the user wants to see real code patterns, not documentation prose
or parameter definitions.

**Skip sliderule-examples and route elsewhere when:**

- **Narrative docs** ("how does atl06x work", "what is YAPC") →
  `sliderule-docsearch`
- **Parameter lookup** ("what is the default for cnf", "what columns
  does atl06x return") → `sliderule-openapi`
- **Request planning** ("help me set up a request for Lake Tahoe") →
  `sliderule-params`
- **Science theory** ("how does photon classification work") →
  `nsidc-reference`
- **Executing a request** → `sliderule-api` + `sliderule-pipeline`

### The boundary, by example

| Question | Route |
| --- | --- |
| Show me an example of ATL06 processing | sliderule-examples |
| What examples are available? | sliderule-examples |
| How do I configure ATL06 parameters? | sliderule-params |
| What is the default value of `cnf`? | sliderule-openapi |
| How does the ATL06 algorithm work? | nsidc-reference |
| How does SlideRule's atl06x differ from atl06p? | sliderule-docsearch |
| Run an ATL06 request for Greenland | sliderule-params + sliderule-api |

## Which reference to open

| Open | When |
|---|---|
| `references/index.md` | Always start here. The user asks what examples exist, needs to find which example covers a topic or API, or you need to locate the right file. |
| `references/{name}.py` | The user asks about a specific example, wants to see how an API or feature is used, or you've identified the relevant file from the index. Open one at a time. |

Do NOT read example files speculatively. Read the index, identify the
right file, then read that one file.

## Agent instructions

1. **Read `references/index.md` first.** Find the matching example by
   topic (the "By Topic" section) or by API (the "By API" section).
   The table at the top has titles and GitHub notebook links.

2. **Read one `references/{name}.py` at a time.** These are the actual
   example code — code cells from the notebook with markdown narrative
   as comment blocks. Present the relevant parts to the user, not the
   entire file (unless they ask for it all).

3. **Don't bulk-read examples.** If the user's question spans multiple
   topics, read the index to identify candidates, present the list,
   and let the user choose which to drill into.

4. **Example code is illustrative, not authoritative for parameter
   defaults.** A notebook might set `cnf=4` for a specific demo, but
   the authoritative default lives in the OpenAPI spec
   (`sliderule-openapi`). When the user cares about what a parameter's
   default is, verify against the spec.

5. **The GitHub notebook link has rendered output.** The `.py` reference
   files don't include cell outputs (plots, tables). If the user wants
   to see the visual results, point them to the notebook link in the
   index.

## Not covered

- **Running notebook code.** This skill reads and presents examples;
  it does not execute them. For running SlideRule requests, use
  `sliderule-api` and `sliderule-pipeline`.
- **Parameter documentation.** Notebooks show parameter values in
  context but don't document them. Use `sliderule-openapi` for
  parameter schemas and `sliderule-params` for planning.
- **ICESat-2/GEDI science.** Notebooks may reference science concepts
  but don't explain them. Use `nsidc-reference` for algorithm theory.
- **SlideRule narrative docs.** For "how do I..." questions that need
  prose answers rather than code examples, use `sliderule-docsearch`.
