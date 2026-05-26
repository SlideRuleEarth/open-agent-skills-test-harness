"""Loader and slicer for the SlideRule OpenAPI 3.1 specification.

Loads the bundled spec from either an HTTPS URL or a local file, normalizes
a small set of known issues, and slices the spec to the fragment relevant
to a given query (one endpoint, one parameter, one schema).

See skills/sliderule-openapi/SKILL.md for the URL layout, normalization
rules, and how to interpret the returned documents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _missing_deps_exit(exc: ModuleNotFoundError) -> None:
    print(
        f"\nERROR: required package '{exc.name}' is not installed.\n\n"
        f"This skill's only Python dependency (for HTTPS loading) is `requests`. Install it:\n"
        f"\n"
        f"  pip install requests\n",
        file=sys.stderr,
    )
    sys.exit(2)


# requests is only needed for the HTTPS path; --spec-path works without it
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _HAS_REQUESTS = True
except ModuleNotFoundError:
    _HAS_REQUESTS = False


# Points at the production endpoint. Once live, this is the default.
# During development before the endpoint is live, pass --spec-path to
# a local sliderule.json instead.
DEFAULT_BASE_URL = "https://sliderule.slideruleearth.io/openapi/sliderule.json"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────

def resolve_source(spec_path: str | None, base_url: str | None) -> tuple[str, str]:
    """Return (kind, value) where kind is 'file' or 'url'.

    Precedence:
      --spec-path > SLIDERULE_OPENAPI_SPEC_PATH > --base-url
        > SLIDERULE_OPENAPI_BASE > DEFAULT_BASE_URL
    """
    path = spec_path or os.environ.get("SLIDERULE_OPENAPI_SPEC_PATH")
    if path:
        return ("file", path)
    url = base_url or os.environ.get("SLIDERULE_OPENAPI_BASE", DEFAULT_BASE_URL)
    return ("url", url)


def load_from_file(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        print(f"\nERROR: spec file not found: {p}", file=sys.stderr)
        sys.exit(2)
    try:
        with p.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"\nERROR: spec file is not valid JSON: {p}\n  {e}", file=sys.stderr)
        sys.exit(2)


def load_from_url(url: str, timeout: float) -> dict:
    if not _HAS_REQUESTS:
        _missing_deps_exit(ModuleNotFoundError("No module named 'requests'", name="requests"))

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        print(
            f"ERROR: --base-url must be a full URL with scheme and host "
            f"(e.g. https://sliderule.slideruleearth.io/openapi/sliderule.json). Got: {url!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Retry pattern: absorbs server cold-start 503s without
    # leaking them to the agent.
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET",),
        )),
    )

    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as e:
        print(f"\nERROR: request failed: {type(e).__name__}: {e}\n  url={url}", file=sys.stderr)
        sys.exit(2)

    if resp.status_code != 200:
        print(
            f"\nERROR: server returned {resp.status_code}\n"
            f"  url={url}\n"
            f"  body={resp.text[:500]}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        return resp.json()
    except ValueError as e:
        print(f"\nERROR: non-JSON response from {url}: {e}", file=sys.stderr)
        sys.exit(2)


# ─────────────────────────────────────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────────────────────────────────────

# Inline corrections for fields the upstream spec mis-types or
# under-specifies. Each entry is the full replacement schema for a
# node that has matching key and format:"x-object". These rewrites
# stop being no-ops once upstream fixes the type annotations.
#
# Sources for the corrected shapes:
#   datum     — RequestFields.cpp convertFromLua (datum_t)
#   proj      — RequestFields.cpp convertFromLua (proj_t)
#   aoi_bbox  — GeoFields.cpp convertFromLua (bbox_t)
#   coord     — RequestFields.cpp convertFromLua (coord_t)
#
# Note on `datum`: the upstream description lists ITRF2020 as an option,
# but the server's string-input parser does not accept it — only the
# integer-input pathway can produce ITRF2020 state, and that pathway is
# not user-facing through JSON. The corrected enum lists only the three
# strings the parser actually accepts.
INLINE_OVERRIDES: dict[str, dict] = {
    "datum": {
        "type": "string",
        "enum": ["ITRF2014", "EGM08", "NAVD88"],
        "description": (
            "Vertical datum to use when returning elevation data; not all "
            "endpoints support all datums. Omit the field to use the "
            "server's default."
        ),
    },
    "proj": {
        "type": "string",
        "enum": ["auto", "plate_carree", "north_polar", "south_polar"],
        "default": "auto",
        "description": (
            "Projection used when subsetting data; in most cases, do not "
            "specify and the code will choose the best projection for the "
            "area of interest."
        ),
    },
    "aoi_bbox": {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 4,
        "maxItems": 4,
        "description": (
            "Area of interest bounding box as "
            "[lon_min, lat_min, lon_max, lat_max]; helps PROJ select the "
            "best transform."
        ),
    },
    "coord": {
        "type": "object",
        "properties": {
            "lon": {"type": "number"},
            "lat": {"type": "number"},
        },
        "required": ["lon", "lat"],
        "default": {"lon": 0, "lat": 0},
        "description": (
            "Coordinate {lon, lat} selecting the inland body of water "
            "that contains it."
        ),
    },
}

# Polygon vertex shape used by every endpoint's `poly` parameter.
# The vertex is keyed by `lat`/`lon` per the parent `poly.description`
# prose; the spec encodes `poly.items` as a bare x-object with no
# inner schema. Detected structurally (parent property is named "poly",
# is an array, has items with format:"x-object") since the "items"
# key alone isn't a stable signal.
POLY_VERTEX_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "lat": {"type": "number"},
        "lon": {"type": "number"},
    },
    "required": ["lat", "lon"],
}


def normalize(spec: dict) -> dict:
    """In-place normalization of known spec quirks.

    Three transforms:
      1. Strip `format: "binary"` from string-typed fields — these are
         textual identifiers (asset names, resource IDs, ancillary
         field-name arrays), not binary payloads.
      2. Replace `format: "x-object"` nodes case-by-case:
         a. Property name matches INLINE_OVERRIDES — substitute the
            corrected schema in place. Covers `datum`, `proj`,
            `aoi_bbox`, `coord`, and (structurally) the items
            schema under `poly`.
         b. `description` field exactly matches an existing component
            schema name — rewrite as a `$ref` to that schema. Covers
            all inner-record cases (e.g., `atl06rec.elevation`,
            `gedi01brec.footprint`, etc.).
         c. Fallback — strip `format` and add `x-shape: "see-references"`.
            Should be empty in practice once arms (a) and (b) cover
            all known cases.

    Bypassed when --no-normalize is passed.

    See SKILL.md > "Spec quirks the helper smooths over" for the
    consumer-facing summary.
    """
    schemas = spec.get("components", {}).get("schemas", {})

    def rewrite_x_object(node: dict, parent_key: str | None) -> None:
        # Arm 1: known wrong-type or property-missing field.
        if parent_key in INLINE_OVERRIDES:
            node.clear()
            # deep-ish copy of the override to keep INLINE_OVERRIDES pristine
            node.update(json.loads(json.dumps(INLINE_OVERRIDES[parent_key])))
            return
        # Arm 2: inner-record reference (description is the schema name).
        desc = node.get("description", "")
        if desc and desc in schemas:
            node.clear()
            node["$ref"] = f"#/components/schemas/{desc}"
            return
        # Arm 3: fallback. Strip the misleading format annotation and
        # mark for references-folder lookup.
        if "format" in node:
            del node["format"]
        node["x-shape"] = "see-references"

    def walk(node, parent_key=None):
        if isinstance(node, dict):
            # Strip format:binary on string-typed fields.
            if node.get("type") == "string" and node.get("format") == "binary":
                del node["format"]
            # Special structural case: poly's items schema. Polygon vertex
            # is {lat, lon}; the spec's items dict is a bare x-object.
            if (parent_key == "poly"
                    and node.get("type") == "array"
                    and isinstance(node.get("items"), dict)
                    and node["items"].get("format") == "x-object"):
                node["items"] = json.loads(json.dumps(POLY_VERTEX_SCHEMA))
            # Rewrite any remaining x-object nodes.
            if node.get("format") == "x-object":
                rewrite_x_object(node, parent_key)
            # Recurse with the immediate key as parent context.
            for k, v in node.items():
                walk(v, parent_key=k)
        elif isinstance(node, list):
            for v in node:
                walk(v, parent_key=parent_key)

    walk(spec)
    return spec


# ─────────────────────────────────────────────────────────────────────────
# $ref walking
# ─────────────────────────────────────────────────────────────────────────

def collect_refs(node: Any, refs: set[str]) -> None:
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            refs.add(node["$ref"])
        for v in node.values():
            collect_refs(v, refs)
    elif isinstance(node, list):
        for v in node:
            collect_refs(v, refs)


def resolve_ref(spec: dict, ref: str) -> Any:
    """Resolve a JSON-pointer ref like '#/components/schemas/Atl06Parameters'."""
    if not ref.startswith("#/"):
        return None
    node = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def collect_transitive_components(spec: dict, root: Any) -> dict:
    """Walk all $refs reachable from `root` and return the minimal
    components subtree needed to keep them resolvable."""
    seen: set[str] = set()
    pending: set[str] = set()
    collect_refs(root, pending)

    while pending:
        ref = pending.pop()
        if ref in seen or not ref.startswith("#/components/"):
            continue
        seen.add(ref)
        target = resolve_ref(spec, ref)
        if target is None:
            continue
        new = set()
        collect_refs(target, new)
        pending |= new - seen

    out: dict = {}
    for ref in seen:
        # ref shape: #/components/<kind>/<name>
        _, _, kind, name = ref.split("/", 3)
        out.setdefault(kind, {})[name] = resolve_ref(spec, ref)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────────────────

def cmd_index(spec: dict) -> dict:
    """Slim summary: server, tags, all endpoints with their schemas."""
    endpoints = []
    for path in sorted(spec.get("paths", {}).keys()):
        item = spec["paths"][path]
        op = item.get("post") or item.get("get") or {}
        request_schema = None
        if "requestBody" in op:
            for _, v in op["requestBody"].get("content", {}).items():
                ref = v.get("schema", {}).get("$ref")
                if ref:
                    request_schema = ref.split("/")[-1]
                    break
        response_schema = None
        r200 = op.get("responses", {}).get("200", {})
        for _, v in r200.get("content", {}).items():
            s = v.get("schema", {})
            if "$ref" in s:
                response_schema = s["$ref"].split("/")[-1]
            elif "oneOf" in s:
                response_schema = "oneOf[" + "|".join(
                    o.get("$ref", "?").split("/")[-1] for o in s["oneOf"]
                ) + "]"
            elif "allOf" in s:
                response_schema = "allOf[" + "+".join(
                    o.get("$ref", "?").split("/")[-1] for o in s["allOf"]
                ) + "]"
            break
        endpoints.append({
            "path": path,
            "tag": (op.get("tags") or [None])[0],
            "summary": op.get("summary"),
            "request_schema": request_schema,
            "response_schema": response_schema,
        })
    return {
        "openapi_version": spec.get("openapi"),
        "info_version": spec.get("info", {}).get("version"),
        "server": (spec.get("servers") or [{}])[0].get("url"),
        "tags": spec.get("tags", []),
        "endpoints": endpoints,
    }


def cmd_endpoint(spec: dict, name: str) -> dict:
    """Full slice for one endpoint + all transitively-referenced components."""
    path_key = "/" + name.lstrip("/")
    if path_key not in spec.get("paths", {}):
        available = sorted(p.lstrip("/") for p in spec.get("paths", {}).keys())
        print(
            f"\nERROR: no endpoint named {name!r}.\n"
            f"Available endpoints (first 10): {available[:10]}\n"
            f"(total: {len(available)})",
            file=sys.stderr,
        )
        sys.exit(2)
    op = spec["paths"][path_key]
    components = collect_transitive_components(spec, op)
    return {
        "openapi": spec.get("openapi"),
        "info": {"version": spec.get("info", {}).get("version")},
        "paths": {path_key: op},
        "components": {k: v for k, v in components.items() if v},
    }


def cmd_schema(spec: dict, name: str) -> dict:
    """One component schema by name, plus transitively-referenced peers."""
    schemas = spec.get("components", {}).get("schemas", {})
    if name not in schemas:
        available = sorted(schemas.keys())
        # bias toward schemas whose name contains the user's query
        matches = [s for s in available if name.lower() in s.lower()]
        suggestion = matches[:10] if matches else available[:10]
        print(
            f"\nERROR: no schema named {name!r}.\n"
            f"Suggestions: {suggestion}\n"
            f"(total schemas: {len(available)})",
            file=sys.stderr,
        )
        sys.exit(2)
    root = schemas[name]
    components = collect_transitive_components(spec, root)
    # ensure the target schema itself is included
    components.setdefault("schemas", {})[name] = root
    return {"components": {k: v for k, v in components.items() if v}}


def cmd_param(spec: dict, name: str) -> dict:
    """Find a parameter across all *Parameters schemas.

    Returns all occurrences with the schema they live in, the endpoints
    that ref that schema, and the parameter definition. Flags drift if
    descriptions differ across occurrences.
    """
    schemas = spec.get("components", {}).get("schemas", {})

    # build schema → list-of-endpoints lookup
    schema_to_endpoints: dict[str, list[str]] = {}
    for path, item in spec.get("paths", {}).items():
        op = item.get("post") or item.get("get") or {}
        body = op.get("requestBody", {}).get("content", {})
        for _, v in body.items():
            ref = v.get("schema", {}).get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                schema_to_endpoints.setdefault(ref.split("/")[-1], []).append(path)

    occurrences = []
    for schema_name, schema_def in schemas.items():
        if "Parameters" not in schema_name:
            continue
        props = schema_def.get("properties", {})
        if name in props:
            occurrences.append({
                "schema": schema_name,
                "endpoints": sorted(schema_to_endpoints.get(schema_name, [])),
                "definition": props[name],
            })

    distinct_descriptions = len({
        (o["definition"].get("description") or "")[:200] for o in occurrences
    })

    return {
        "parameter": name,
        "occurrences": occurrences,
        "occurrence_count": len(occurrences),
        "distinct_descriptions": distinct_descriptions,
    }


def cmd_applies_to(spec: dict, name: str) -> dict:
    """List all parameters accepted by an endpoint, flat presentation."""
    path_key = "/" + name.lstrip("/")
    if path_key not in spec.get("paths", {}):
        print(f"\nERROR: no endpoint named {name!r}", file=sys.stderr)
        sys.exit(2)
    op = spec["paths"][path_key].get("post") or spec["paths"][path_key].get("get") or {}
    body = op.get("requestBody", {}).get("content", {})
    schema_ref = None
    for _, v in body.items():
        ref = v.get("schema", {}).get("$ref")
        if ref:
            schema_ref = ref
            break
    if not schema_ref:
        return {"endpoint": path_key, "request_schema": None, "parameters": []}

    schema_name = schema_ref.split("/")[-1]
    schema = spec.get("components", {}).get("schemas", {}).get(schema_name, {})
    params = []
    for pname, pdef in sorted(schema.get("properties", {}).items()):
        params.append({
            "name": pname,
            "type": pdef.get("type"),
            "format": pdef.get("format"),
            "default": pdef.get("default"),
            "description": pdef.get("description", "")[:240],
        })
    return {
        "endpoint": path_key,
        "request_schema": schema_name,
        "parameters": params,
        "parameter_count": len(params),
    }


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", default="index",
        choices=["index", "endpoint", "schema", "param", "applies-to"],
        help="Subcommand to run. Default: index.",
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="Name argument for endpoint/schema/param/applies-to.")
    parser.add_argument("--spec-path", default=None,
                        help="Load from a local file instead of the URL.")
    parser.add_argument("--base-url", default=None,
                        help="Override the distribution base URL.")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="HTTP timeout in seconds (default: 30).")
    parser.add_argument("--no-normalize", action="store_true",
                        help="Skip in-helper normalization (return spec verbatim).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print the source URL/path to stderr before loading.")
    args = parser.parse_args()

    # subcommands other than 'index' require a target
    if args.command != "index" and args.target is None:
        print(f"\nERROR: subcommand {args.command!r} requires a target argument.", file=sys.stderr)
        return 2

    # Load
    kind, source = resolve_source(args.spec_path, args.base_url)
    if args.verbose:
        log(f"LOAD {kind}={source}")
    if kind == "file":
        spec = load_from_file(source)
    else:
        spec = load_from_url(source, args.timeout)

    # Normalize
    if not args.no_normalize:
        normalize(spec)

    # Dispatch
    if args.command == "index":
        result = cmd_index(spec)
    elif args.command == "endpoint":
        result = cmd_endpoint(spec, args.target)
    elif args.command == "schema":
        result = cmd_schema(spec, args.target)
    elif args.command == "param":
        result = cmd_param(spec, args.target)
    elif args.command == "applies-to":
        result = cmd_applies_to(spec, args.target)
    else:
        print(f"\nERROR: unknown command {args.command!r}", file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
