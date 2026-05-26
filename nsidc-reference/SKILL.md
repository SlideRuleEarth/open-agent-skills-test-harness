---
name: nsidc-reference
description: Search NSIDC + ORNL DAAC reference documents for ICESat-2 and GEDI science. Use for questions about photon classification, ATLAS instrument details, signal-finding algorithms, geophysical corrections, strong vs weak beams, quality flags, HDF5 file structure, data product variables, how algorithms like ATL06/ATL08/ATL13/ATL24 work, or what a specific dataset field means. Covers User Guides (format, structure, naming) and ATBDs (algorithm theory, signal processing, error models) for ATL03, ATL06, ATL08, ATL13, ATL24, and GEDI L4A. Use `sliderule-docsearch` instead for SlideRule's Python client, API usage, configuration, endpoints, or "how do I use SlideRule..." questions; use `sliderule-openapi` for SlideRule API parameter signatures, default values, output column schemas, valid values, or ancillary field-name enumeration.
---

# nsidc-reference

Semantic search over NSIDC and ORNL DAAC reference documents —
User Guides and ATBDs for the ICESat-2 and GEDI data products
that the SlideRule service ingests. Companion to
[`sliderule-docsearch`](../sliderule-docsearch) (SlideRule narrative
docs) and [`sliderule-openapi`](../sliderule-openapi) (SlideRule
structured parameter / output schemas).

## Search or skip?

This skill is the right tool for **NSIDC / ORNL DAAC science knowledge**:
how the algorithms work, what variables physically mean, how the
instrument operates, how products are structured, what flags and
geophysical corrections actually do. Search when the question hinges on
the *underlying science* of the ICESat-2 / GEDI data products.

**Skip nsidc-reference and route elsewhere when:**

- **SlideRule-specific narrative** (API usage, parameter behavior in the
  SlideRule context, Python client workflows, configuration, examples,
  version history) → `sliderule-docsearch`. NSIDC docs describe the
  underlying science; SlideRule's docsearch describes how the client
  exposes that science.
- **Structured lookup about SlideRule's API surface** (parameter
  signatures, default values, output column schemas, valid values,
  ancillary field-name enumeration) → `sliderule-openapi`.
- **General concept** that merely shows up in NSIDC's orbit but isn't
  NSIDC-specific (HDF5/NetCDF/GeoTIFF/COG internals, geospatial
  primitives like WGS84/EGM2008/UTM, Python tooling like h5py/xarray,
  signal-processing fundamentals) → answer from training, or web search
  if recency matters. NSIDC docs reference these in passing without
  defining them.

### The boundary, by example

| Question | Route |
| --- | --- |
| What does photon confidence level 4 physically mean? | nsidc-reference |
| What's the default value of `cnf` in SlideRule? | sliderule-openapi |
| How do I set `cnf` in a SlideRule atl03x call? | sliderule-docsearch |
| How does the ATL06 land-ice fitting algorithm work? | nsidc-reference |
| Why use atl06p over atl06x in SlideRule? | sliderule-docsearch |
| What columns does `atl06x` return from SlideRule? | sliderule-openapi |
| What does the `signal_conf_ph` flag mean in ATL03? | nsidc-reference |
| What are weak vs strong beams in ICESat-2? | nsidc-reference |
| How does ATLAS measure photon timing? | nsidc-reference |
| What is HDF5? | training (general concept) |
| What's the difference between WGS84 and EGM2008? | training |

When genuinely unsure between nsidc-reference and another tool, prefer
nsidc-reference — a wasted retrieval costs ~1 sec; a wrong answer from
skipping costs more. But don't substitute nsidc-reference for
`sliderule-openapi` on structured lookups; the latter is definitive and
the former is best-effort prose over PDFs.

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
- `--categories LIST` — comma-separated category allowlist (`user_guide,atbd`). Filter is applied server-side **before** ranking, so `top_k` reflects the filtered universe.
- `--search-url URL` — full override of the search endpoint (for staging/dev).
- `--timeout SECONDS` — HTTP timeout (default 60). The server is a Lambda; a cold start adds ~3–5 s to the first request after a period of idleness, so the timeout is set higher than a typical HTTP default.

`SLIDERULE_SEARCH_BASE` env var picks a different base URL — the
skill appends `/nsidc/search`.

## Output

The skill prints JSON to stdout, byte-for-byte the server response:

```json
{
  "query": "how are photons classified in ATL08?",
  "results": [
    {
      "score": 0.58,
      "category": "atbd",
      "source_product": "ATL08",
      "source_version": "v007",
      "source_page": 23,
      "source_url": "https://nsidc.org/.../icesat2_atl08_atbd_v007.pdf",
      "url": "https://nsidc.org/.../icesat2_atl08_atbd_v007.pdf",
      "title": "ATL08 v007 atbd",
      "section": "Photon Classification",
      "text": "...",
      "matched_tokens": ["atl08", "photon"]
    }
  ],
  "corpus_meta": {
    "chunk_count": 1757,
    "embedder": "sentence-transformers/all-MiniLM-L6-v2",
    "embedding_dim": 384,
    "corpus_sha256": "...",
    "built_at": "2026-04-21T...",
    "sources_ok": 12,
    "source_host": "nsidc.org + ornldaac.earthdata.nasa.gov"
  }
}
```

## Agent instructions

1. Invoke `python scripts/search.py "<user's question>"` with a concise,
   information-rich query. If the user asked a verbose question, trim it
   to the key nouns and concepts — this typically lifts retrieval
   quality.

   **Example of trimming a verbose user question:**
   - User: *"I'm trying to understand how ATL08 decides which photons
     are ground versus canopy versus noise, what's the classification
     algorithm actually doing"*
   - Good search query: `"atl08 photon classification algorithm"`
   - The retrieval is cosine + IDF; concise technical nouns beat
     natural language.
2. Parse the JSON response. Each `results[i]` has a `score` (cosine
   similarity, higher is better), `url`, `title`, `section`, `category`,
   `source_product`, `source_version`, `source_page`, and `text`. When
   present, `matched_tokens` lists which of the user's query tokens
   appeared literally in the chunk. For identifier-heavy queries
   (product names, variable names, flag names) this is the
   **primary disambiguator** — verify the exact identifier appears
   in `matched_tokens` on the top result. If only a related variant
   matched (e.g. user asked about `ATL06` but the top hit only
   matched `ATL03`), either re-run with a more specific query or
   flag the mismatch to the user rather than citing a near-miss as
   an answer. Note: ranking reflects RRF-fused semantic + lexical
   scores, so `score` (which is cosine only) doesn't always
   monotonically decrease down the list.

3. **Use `category` and `source_product` to weigh results against
   user intent:**

   | category       | what it is                                     | prioritize when the user's question is...                                |
   | -------------- | ---------------------------------------------- | ------------------------------------------------------------------------ |
   | `atbd`         | Algorithm Theoretical Basis Documents          | "how does algorithm X work", "what's the error model", signal-processing theory |
   | `user_guide`   | NSIDC User Guides + GEDI L4A product guide     | "what's in this dataset", HDF5 structure, variable names, units, quality flags |

   `source_product` lets you pick the right product's doc when the
   query names one. A query about ATL06 should prefer chunks with
   `source_product: "ATL06"`; if you see results split across
   products, prefer the in-product hits over cross-product ones.
   You can also narrow with `--categories atbd` (or `user_guide`)
   when you're confident about the target content type.

4. Synthesize an answer from the top results. **Cite like this:**
   *"ATL08 ATBD v007, page 23, Photon Classification section"* plus
   the URL — users often want the page number to locate the exact
   paragraph in the source PDF.
5. If the top `score` is low (e.g., below ~0.3) or the results look
   off-topic, tell the user the reference docs don't seem to cover
   their question and suggest:
   - Rephrasing with product-specific terms, or
   - Using `sliderule-docsearch` if the question is actually about
     how SlideRule exposes the data rather than the underlying
     NSIDC/GEDI science.

## Not covered

For SlideRule's Python client, API usage, configuration,
parameters, or "how do I query SlideRule for X" questions, use the
`sliderule-docsearch` skill instead — that's sourced from
docs.slideruleearth.io.
