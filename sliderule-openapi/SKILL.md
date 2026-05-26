---
name: sliderule-openapi
description: Structured lookups against the SlideRule OpenAPI 3.1 specification, plus authoritative curated supplements for parameter couplings and elevation/datum routes the spec cannot express. Use for questions like "what does `cnf` do?", "what columns does atl06x return?", "which parameters must be paired with `atl08_class`?", "what routes exist for orthometric / MSL output?", "what does `use_abs_h` do?", "what fields can I request via `atl08_fields`?". Slices the bundled spec; also consults `references/parameter_couplings.md` and `references/elevation_datums.md` for content the spec cannot carry. Use `sliderule-docsearch` for narrative ("how do I...", "what is..."); `nsidc-reference` for ICESat-2/GEDI science theory and HDF5-variable meaning; `sliderule-params` for the planning sequence (study-type-to-endpoint mapping, phase-by-phase reasoning), not for parameter or coupling lookups.
---

# sliderule-openapi

Loads a bundled SlideRule OpenAPI 3.1 specification and slices it to the
fragment relevant to a single endpoint, parameter, or schema. Companion to
`sliderule-docsearch` (narrative search) and `nsidc-reference` (NASA science
docs).

## Requirements

Requires Python 3.8+ and the `requests` package. Loads the spec from
either an HTTPS URL or a local file path; both transports work
offline-first if the spec has been fetched once.

See `CHANGELOG.md` for version history.

## Architecture

Single bundled OpenAPI 3.1 spec. Each invocation is an independent
Python process that loads the spec (HTTPS GET or local file read),
normalizes it in memory, slices to the query target, and exits.
There is no on-disk or cross-invocation cache; consecutive calls each
do their own load. The helper handles three concerns:

1. **Loading.** From an HTTPS URL (production) or a local file (development),
   selected via `--spec-path` or `--base-url`.
2. **Normalization.** A small set of in-spec inconsistencies are smoothed
   over before slicing (see `scripts/README.md` for the catalog of
   transforms) so the model sees clean fragments.
3. **Slicing.** Most queries need only one endpoint plus its
   transitively-referenced component schemas. The full spec is ~150K
   tokens; a typical slice is 3-10K. The helper walks `$ref`s starting
   from the query target and returns a minimal coherent spec subset.

The skill ships no copy of the spec itself — every invocation loads it
fresh from the configured source.

## Invocation

```bash
python scripts/openapi.py                              # slim index
python scripts/openapi.py endpoint atl06x              # full slice for one endpoint
python scripts/openapi.py schema Atl06DataFrame        # one component schema
python scripts/openapi.py param cnf                    # find a parameter across all schemas
python scripts/openapi.py applies-to atl08x            # list all parameters accepted by an endpoint
```

Flags:

- `--spec-path PATH` — load from a local file. Used during development
  before the production endpoint is live.
- `--base-url URL` — load from an alternate HTTPS source. Default:
  `https://sliderule.slideruleearth.io/openapi/sliderule.json`.
- `--timeout SECONDS` — HTTP timeout (default 30). Local-file loads are
  unaffected.
- `--no-normalize` — skip the in-helper normalization (return the spec
  verbatim). Useful when debugging surprising slice content.
- `-v` / `--verbose` — print the source URL/path to stderr before loading.

The `SLIDERULE_OPENAPI_BASE` env var overrides the default base URL.
`SLIDERULE_OPENAPI_SPEC_PATH` overrides to a local file path. CLI flags
take precedence over env vars.

## URL / path layout

The spec is a single document. The default URL points at the bundled
form (`sliderule.json`) — everything is inline, all `$ref`s are
internal JSON-pointer references like `#/components/schemas/Atl06Parameters`.

There's also a modular form available at the same host under
`sliderule/openapi.json` (which `$ref`s out to per-endpoint and
per-component files). This skill does **not** use the modular form —
some path files are known to have JSON validation issues, and the
bundled form has everything inline at a single fetch cost.

## Output

The helper prints the slice as 2-space-indented JSON to stdout.
Errors (load failure, missing endpoint, etc.) go to stderr with
exit code 2.

**Execution-harness note.** The script always emits plain JSON to stdout — its
output never changes shape. If you call it through an agent that wraps tool calls
in a `{"returncode": <int>, "stdout": <str>, "stderr": <str>}` envelope, that
envelope is the harness capturing the *stdout channel*, not something the script
produces. So where the slice lands depends on where its stdout went:

- **Captured** (`python scripts/openapi.py endpoint atl06x` as the
  call) — the slice is the envelope's `stdout`; parse the envelope,
  then `json.loads(envelope["stdout"])`.
- **Redirected to a file** (`... > slice.json`, then read the file
  back) — the file holds plain JSON with no envelope; parse it
  directly (`json.loads(open("slice.json").read())`). Looking for
  `["stdout"]` here raises `KeyError` — the envelope never wrapped the
  file.

With no wrapping harness there's no envelope either way, so `python
scripts/openapi.py endpoint atl06x | jq` works as documented.

## Agent instructions

1. **Pick the right subcommand for the user's question.**

   | User asks about… | Use… |
   | --- | --- |
   | a parameter's meaning, type, default | `param <name>` |
   | what columns an endpoint returns | `endpoint <name>` (the 200 response refs the DataFrame schema) |
   | which parameters an endpoint accepts | `applies-to <name>` |
   | one specific component schema by name | `schema <name>` |
   | discoverability ("what endpoints exist?", "what's the API surface?") | run with no args |

2. **For parameter questions, watch for duplication across endpoint schemas.**
   The same parameter (e.g., `poly`, `asset`, `output`) is declared in
   every relevant `*Parameters` schema with the same definition. `param
   <name>` returns all occurrences; if descriptions differ across
   schemas, surface the difference rather than picking one silently.

3. **For output-column questions, follow the response schema chain
   AND consult `references/conditional_columns.md`.**

   *Schema chain:* an endpoint's 200 response refs either a
   `*DataFrame` (x-series) or an `allOf[*rec + *rec.<sub>]`
   composition (p/s/v-series). Each referenced schema has a
   `properties` map where each property is one parquet column with a
   description and item type.

   *Then conditional_columns.md*, every time. The schema lists the
   *logical* column set; the *delivered* column set can differ in
   two ways, and missing either is the failure mode users hit most
   often:
   - **Cross-cutting transforms** (top of the file) — apply on every
     endpoint that emits GeoParquet (the default `output.format`).
     The biggest one: `latitude` and `longitude` properties in any of
     eleven affected `*DataFrame` schemas collapse into a single
     `geometry` column in the response. Surface this whenever your
     answer would otherwise list `latitude` and/or `longitude` as
     response columns.
   - **Per-endpoint conditionals** (rest of the file) — individual
     columns gated on specific request flags
     (`te_quality_filter` / `can_quality_filter` on /atl08x,
     `atl24.compact` on /atl24x). If the spec eventually adds
     `x-condition` annotations these entries retire, but until
     then conditional_columns.md is the source of truth.

   *Binary-container questions* (parquet footer, embedded
   `meta`/`sliderule`/`recordinfo` metadata blocks, parsing the
   response) are handled directly when you read the Parquet response —
   a separate concern from which columns the response contains, and out
   of scope for this lookup skill.

4. **`/atl03x`'s `oneOf` response discriminator.** /atl03x uses a
   `oneOf` at the response level discriminated by request
   parameters. The discriminator is in **prose** in the `description`
   field of the `oneOf`, not a formal OpenAPI `discriminator` block.
   Parse the prose: clauses follow the literal pattern *"if 'phoreal'
   is supplied, returns PhoRealDataFrame; if 'fit' is supplied,
   returns SurfaceFitterDataFrame; …"* — extract the
   parameter→schema mapping and apply based on which parameters
   appear in the user's request. The `oneOf` only selects which
   DataFrame; the cross-cutting transforms in conditional_columns.md
   (rule #3) still apply on top of whichever DataFrame is selected.

5. **For algorithm questions** (e.g., "what algorithms does atl03x
   support?", "what modes are available?"), consult
   `references/algorithms.md`. The OpenAPI spec lists algorithms as
   object-typed properties of `Atl03Parameters` (`phoreal`, `fit`,
   `als`, `yapc`, `atl24`), but the curated file pairs each algorithm
   with the response schema it produces (if any) and the endpoints
   that accept it.

6. **For the `samples` parameter shape**, the spec currently has
   `properties: {}` (empty). Consult `references/samples_shape.md`
   for the actual asset-name → sampling-config dict structure. This
   reference file goes away when the spec fills in the schema.

7. **For parameter couplings** (`depends_on`, `required_pairings`,
   `interaction_detail`), consult `references/parameter_couplings.md`.
   These relationships aren't expressed in the OpenAPI spec — only the
   parameter signatures are. When the user is constructing a request,
   surface relevant couplings as required context, not optional trivia.

8. **For datum / orthometric / MSL / tide-correction questions**,
   consult `references/elevation_datums.md`. Three independent routes
   exist (`atl03_corr_fields`, `datum`, top-level `use_abs_h` on
   `/atl08x`) with different endpoint compatibility and an
   "accepts vs applies" caveat for `datum` under algorithm blocks
   (`phoreal`, `fit`, `atl24`). The OpenAPI spec records endpoint-level
   acceptance only; this reference fills the gap.

9. **For HDF5 ancillary field-name enumeration** ("what values are valid
   for `atl08_fields`?"), consult the JSON file for the matching
   selector in `references/field_selectors/` (e.g.,
   `fields_atl08.json` for `atl08_fields`). Each file lists every
   valid field name with HDF5 path, type, units, and description. The
   OpenAPI spec doesn't carry this enumeration. For ATL24 and GEDI
   ancillary parameters, no enumeration is currently available — note
   the gap and answer from what you can.

10. **For "show me an example request" / "how do I call X"** questions,
    route to `sliderule-docsearch` with category filter
    `--categories tutorial,user_guide`. The OpenAPI spec carries no
    `examples` blocks.

11. **Cite source paths in answers.** Tell the user which schema or
    endpoint the answer came from (e.g., "from `Atl06Parameters.cnf`")
    so they can verify against the live spec. If you used a reference
    file as a supplement, name that too.

## Relationship to other sliderule skills

This skill is the structured-lookup layer for facts about the SlideRule
API surface. The boundaries:

- **`sliderule-params`** uses this skill during request planning to
  look up parameter definitions, known couplings, and datum routes.
  All facts about what a parameter means or how it interacts with
  others come from here (couplings via
  `references/parameter_couplings.md`; orthometric / datum routes via
  `references/elevation_datums.md`).
- **`sliderule-api`** points here for any schema question — that skill
  covers HTTP mechanics (POST envelope, content-types, polygon format,
  failure modes), never schema details. When a response is parsed,
  column meanings are resolved against the DataFrame schema via this
  skill.
- **`sliderule-docsearch`** is the routing target for "how do I" /
  "what is" / examples / conceptual questions. Schema lookups don't
  belong there; narrative does.
- **`nsidc-reference`** is the routing target for ICESat-2/GEDI
  science theory, HDF5 product structure, ATBD content, and the
  *scientific meaning* of a specific HDF5 variable (not its
  enumeration — that's this skill).

## Not covered

- Parquet response file format / metadata blocks → handled directly when
  reading the response (no dedicated skill)
- "How do I" / workflow questions → `sliderule-docsearch`
- Example request bodies → `sliderule-docsearch`
- Photon classification science, beam geometry, quality flags → `nsidc-reference`
- AMS, Authenticator, ILB, Provisioner, Runner APIs — the bundled
  spec covers only the SlideRule processing API; the other services
  have their own specs at the same host.
- ATL24 and GEDI ancillary field-name enumeration — pipeline pending
  extension (see `references/field_selectors/MAINTENANCE.md`).
