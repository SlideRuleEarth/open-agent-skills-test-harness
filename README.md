# sliderule-skills

A set of open-standard Agent Skills for working with [SlideRule Earth](https://slideruleearth.io). Each top-level folder is a self-contained skill that can be used by any platform that supports the open Agent Skills standard (Claude Code, the Claude Agent SDK, etc.).

## Skills in this repo

- [sliderule-api](sliderule-api/) — Query NASA ICESat-2 and GEDI data via the SlideRule HTTP API
- [sliderule-docsearch](sliderule-docsearch/) — Semantic search of the SlideRule documentation
- [sliderule-openapi](sliderule-openapi/) — OpenAPI specs for the SlideRule endpoints
- [sliderule-params](sliderule-params/) — Reference for SlideRule request parameters
- [sliderule-pipeline](sliderule-pipeline/) — Directives for orchestrating SlideRule analyses as single-script pipelines
- [sliderule-region-picker](sliderule-region-picker/) — Interactive map for defining geographic regions
- [nsidc-reference](nsidc-reference/) — Reference for NASA NSIDC and ORNL DAAC data products

## Installing the skills (one source of truth)

To make this repo the single source of truth, symlink each skill folder into the directories that Claude Code (`~/.claude/skills/`) and the Claude Agent SDK (`~/.agents/skills/`) scan at startup. Updating the repo then updates every consumer.

### 1. Create the target directories

```bash
mkdir -p ~/.claude/skills ~/.agents/skills
```

### 2. Symlink each skill

From inside this repo:

```bash
REPO="$(pwd)"
for skill in sliderule-api sliderule-docsearch sliderule-openapi sliderule-params sliderule-pipeline sliderule-region-picker nsidc-reference; do
  ln -sfn "$REPO/$skill" "$HOME/.claude/skills/$skill"
  ln -sfn "$REPO/$skill" "$HOME/.agents/skills/$skill"
done
```

`ln -sfn` replaces any existing symlink at the target path, so this is safe to re-run after adding new skills. It will refuse to overwrite a real directory — if you already have a non-symlink copy of a skill installed, remove or rename it first.

### 3. Verify

```bash
ls -l ~/.claude/skills ~/.agents/skills
```

Each entry should show an arrow pointing back to this repo, e.g. `sliderule-api -> /path/to/sliderule-skills/sliderule-api`.

### Installing a single skill

If you only want one skill:

```bash
ln -sfn "$(pwd)/sliderule-api" ~/.claude/skills/sliderule-api
ln -sfn "$(pwd)/sliderule-api" ~/.agents/skills/sliderule-api
```

### Uninstalling

Symlinks can be removed without affecting the repo:

```bash
rm ~/.claude/skills/sliderule-api ~/.agents/skills/sliderule-api
```
