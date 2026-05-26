---
name: sliderule-docsearch
description: Narrative search over the SlideRule Earth documentation — concepts, workflows, "how do I..." / "what is..." / "why use X" questions about SlideRule and its ICESat-2 / GEDI endpoints (atl03p, atl06p, atl08p, atl13p, atl24p, gedil4ap, atl03x, atl06x, etc.). Use `sliderule-openapi` instead for structured lookups (parameter signatures, default values, output column schemas, valid values, ancillary field-name enumeration); use `nsidc-reference` instead for ICESat-2/GEDI science theory, ATBDs, photon-classification algorithms, or HDF5 structure.
---

# sliderule-docsearch

Narrative semantic search over the SlideRule Earth documentation at
[docs.slideruleearth.io](https://docs.slideruleearth.io/). Companion to
[`sliderule-openapi`](../sliderule-openapi) (structured parameter / output
schemas) and [`nsidc-reference`](../nsidc-reference) (NASA / ORNL science
theory).

## Search or skip?

This skill is the right tool for **SlideRule-specific narrative knowledge**:
how parameters interact, why you'd use one endpoint over another, conceptual
workflows, version history, examples. Search when the question hinges on
*how SlideRule does something* and the answer needs synthesis, not lookup.

**Skip docsearch and route elsewhere when:**

- **Structured lookup** (parameter signature, default, type, valid values,
  output columns, ancillary field-name enumeration) → `sliderule-openapi`.
  Default-value questions look retrievable here because release-note chunks
  mention historical defaults — but those chunks document *changes*, not
  current state. The OpenAPI spec is authoritative.
- **ICESat-2 / GEDI science theory** (photon classification, ATBD content,
  geophysical corrections, beam geometry, HDF5 product structure, physical
  meaning of confidence values or quality flags) → `nsidc-reference`.
- **General concept** that merely shows up in SlideRule's orbit (file
  formats like GeoParquet/HDF5/Arrow, geospatial primitives like WGS84/UTM,
  Python tooling, CS fundamentals) → answer from training, or web search
  if recency matters. A docsearch query on these returns chunks that
  mention the term in passing without defining it.

For SlideRule-adjacent how-to questions where training plus a web fetch of
the live docs would be comparable to docsearch (e.g., "how do I install the
SlideRule Python client"), either path works. Default to docsearch for
authoritative versioned content; fall back to web fetch only if docsearch
returns weak results.

### The boundary, by example

| Question | Route |
| --- | --- |
| What is GeoParquet? | training (general concept) |
| Does SlideRule's GeoParquet output include a CRS column? | docsearch |
| What does WGS84 mean? | training |
| What geoid model does SlideRule apply for `atl03_corr_fields: ["geoid"]`? | docsearch |
| What's the default value of `cnf`? | sliderule-openapi |
| What does `cnf=4` mean physically? | nsidc-reference |
| Why would I set `cnf=2` over `cnf=4`? | docsearch |
| How does the ATL06 surface-fit algorithm work? | nsidc-reference |
| How does the SlideRule atl06p endpoint use ATL06? | docsearch |
| Show me an example of YAPC photon classification | docsearch |
| What columns does `atl06x` return? | sliderule-openapi |
| Why use `atl06x` over `atl06p`? | docsearch |

When genuinely unsure between docsearch and another tool, prefer docsearch
— a wasted retrieval costs ~1 sec; a wrong answer from skipping costs more.
But don't substitute docsearch for `sliderule-openapi` on structured
lookups; the latter is definitive and the former is best-effort prose.

## Architecture

Hosted semantic + lexical search. One HTTPS POST per query to a
Lambda behind CloudFront; there's no offline mode. All retrieval
(embedding with `all-MiniLM-L6-v2`, cosine scoring, IDF-weighted
lexical overlap, reciprocal rank fusion) runs server-side. The
skill client is a thin transport wrapper.

## Invocation

```bash
python scripts/search.py "<query>" [--top-k 5] [--disable-lexical] [--categories ...]
```

Flags:

- `--top-k N` — number of results to return (default 5, max 50).
- `--disable-lexical` — ask the server to skip lexical rank-fusion; results become pure cosine similarity. Mainly for A/B comparison — leave off for normal use.
- `--categories LIST` — comma-separated category allowlist (e.g. `user_guide,api_reference`). Filter is applied server-side **before** ranking, so `top_k` reflects the filtered universe.
- `--search-url URL` — full override of the search endpoint (for staging/dev).
- `--timeout SECONDS` — HTTP timeout (default 60). The server is a Lambda; a cold start adds ~3–5 s to the first request after a period of idleness, so the timeout is set higher than a typical HTTP default.

`SLIDERULE_SEARCH_BASE` env var picks a different base URL — the skill
appends `/docsearch/search`.

## Output

The skill prints JSON to stdout, byte-for-byte the server response:

```json
{
  "query": "what is the X-Series API",
  "results": [
    {
      "score": 0.742,
      "category": "user_guide",
      "url": "https://docs.slideruleearth.io/...",
      "title": "X-Series APIs",
      "section": "Overview",
      "text": "...",
      "matched_tokens": ["atl03x"]
    }
  ],
  "corpus_meta": {
    "chunk_count": 754,
    "embedder": "sentence-transformers/all-MiniLM-L6-v2",
    "embedding_dim": 384,
    "corpus_sha256": "1163f9a9d3c550fe3ea84336bcac285e897bddd0485b97190d3403f1606694c3",
    "built_at": "2026-04-28T19:55:12Z",
    "pages_crawled": 92,
    "source_host": "docs.testsliderule.org"
  }
}
```

## Agent instructions

1. Invoke `python scripts/search.py "<user's question>"` with a concise,
   information-rich query. If the user asked a verbose question, trim it
   to the key nouns and concepts — this typically lifts retrieval
   quality.

   **Example of trimming a verbose user question:**
   - User: *"how do I set up confidence filtering for photons when
     I'm using the atl03x endpoint, I can't figure out the parameter
     name"*
   - Good search query: `"atl03x confidence filtering parameters"`
   - The retrieval is cosine + IDF; concise technical nouns beat
     natural language.

   **If the search call itself fails** (HTTP 5xx, timeout, connection
   error) — distinct from returning weak results — the server is a
   Lambda and occasionally hits transient errors under load, memory
   pressure, or cold-start. Retry once after ~15–30 seconds. If the
   second attempt also fails, tell the user the docs search is
   temporarily unavailable and either answer from general knowledge
   (if the "Search or skip?" gate would have allowed it anyway) or
   ask them to retry later. Don't fabricate doc content to fill the
   gap, and don't cite URLs you haven't seen returned by a successful
   search.

2. Parse the JSON response. Each `results[i]` has a `score` (cosine
   similarity, higher is better), `url`, `title`, `section`, `category`,
   and `text`. When present, `matched_tokens` lists which of the user's
   query tokens appeared literally in the chunk. For identifier-heavy
   queries this is the **primary disambiguator** — verify the exact
   identifier appears in `matched_tokens` on the top result. If only
   a related variant matched (e.g. user asked about `atl03x` but the
   top hit only matched `atl03`), either re-run with a more specific
   query or flag the mismatch to the user rather than citing a
   near-miss as an answer. Note: ranking reflects RRF-fused semantic
   + lexical scores, so `score` (which is cosine only) doesn't always
   monotonically decrease down the list.

3. **Use `category` to weigh content types against the user's intent.**
   Each chunk is tagged by URL-derived content type:

   | category          | what it is                               | prioritize when the user's question is... |
   | ----------------- | ---------------------------------------- | ------------------------------------------ |
   | `user_guide`      | curated concept + usage pages            | conceptual, "how do I...", "what is..."    |
   | `api_reference`   | function signatures + parameter tables   | API/parameter lookups                       |
   | `background`      | theory, data product descriptions        | background science, data interpretation     |
   | `getting_started` | quickstarts, first-request examples      | onboarding questions                        |
   | `tutorial`        | rendered notebooks with worked examples  | "show me an example of X"                   |
   | `developer_guide` | architecture, internals, build process   | contributing, internals                     |
   | `release_notes`   | per-version changelogs + known issues    | **only** version-keyed: "when was X added", "did Y change", "known bug in v?" |

   `release_notes` chunks often score high on conceptual queries
   because they name-drop the concept while reporting historical bugs.
   For a conceptual question, treat `release_notes` hits as caveats or
   ignore them — prefer `user_guide` / `api_reference` / `background`
   hits as the primary answer. You can also narrow with
   `--categories user_guide,api_reference` when you're confident about
   the target content type; the filter applies before ranking so
   `top_k` is exact.

4. Synthesize an answer from the top results. Cite the specific URLs
   you used — users rely on those links to go read the authoritative
   docs themselves.

   **When results disagree, resolve by authority and recency, not by
   averaging.** Semantic search will happily return contradictory
   chunks when the docs are in transition or when different categories
   describe the same concept at different levels of truth.

   - For questions about *current behavior* (defaults, signatures,
     valid values, what a parameter does), `api_reference` and
     `user_guide` outrank `tutorial` and `release_notes`. Tutorials
     pick a specific value to demonstrate something — they aren't
     documenting the default. Release notes describe *changes*, which
     makes them authoritative for "when did X change" but not for
     "what is X now".
   - If two chunks from authoritative categories genuinely contradict
     (e.g., two `user_guide` pages with different defaults for the
     same parameter), surface the conflict to the user rather than
     silently picking one. That pattern usually means the docs are
     mid-migration and the user needs to know before they rely on the
     answer.
   - Exact identifiers beat higher scores. If the user asked about
     `atl03x` and the top hit only carries `atl03` in `matched_tokens`,
     the answer belongs to whichever chunk actually matched `atl03x`,
     even if it ranks lower (see step 2).
5. If the top `score` is low (e.g., below ~0.3) or the results look
   off-topic, try one reformulation before giving up. Productive
   reformulations usually drop soft words and keep the technical
   nouns, add an endpoint or parameter name the user referenced but
   the first query omitted, split a compound question and search the
   more specific half first, or switch to a canonical identifier form
   (e.g., `atl03_corr_fields` rather than a paraphrase of it).

   Cap retries at one. If the second attempt also returns weak or
   off-topic results, tell the user the docs don't seem to cover
   their question and suggest rephrasing or pointing them at a
   specific doc section. Two failed searches look the same whether
   the corpus is missing the content or the query is hopeless —
   either way, more attempts won't resolve it.

## Not covered

For authoritative ICESat-2 / GEDI science (photon classification,
algorithm details, HDF5 structure, quality flags), use the
`nsidc-reference` skill instead — that's sourced from NSIDC + ORNL
DAAC user guides and ATBDs (ATL03, ATL06, ATL08, ATL13, ATL24, GEDI
L4A) rather than SlideRule's own docs.
