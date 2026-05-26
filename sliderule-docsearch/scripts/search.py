"""Thin HTTP client for the sliderule-docsearch service.

POSTs the query to https://<base>/docsearch/search and prints the
server's JSON response. All retrieval work (embedding, cosine, IDF,
RRF fusion) happens server-side; this script is a transport wrapper.

See skills/sliderule-docsearch/SKILL.md for the response contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from urllib.parse import urlparse


def _missing_deps_exit(exc: ModuleNotFoundError) -> None:
    # The install hint uses the bare package name rather than
    # `-r .../requirements.txt` because the skill's root path on disk
    # depends on where it was installed — an agent's skill directory,
    # the repo layout locally, etc. `pip install
    # requests` works everywhere and says exactly what's needed.
    print(
        f"\nERROR: required package '{exc.name}' is not installed.\n\n"
        f"This skill's only Python dependency is `requests`. Install it:\n"
        f"\n"
        f"  pip install requests\n",
        file=sys.stderr,
    )
    sys.exit(2)


try:
    import requests
except ModuleNotFoundError as e:
    _missing_deps_exit(e)


# Points at the test/staging environment by design — we're still
# iterating on the skill against testsliderule.org. Flip to
# https://search.slideruleearth.io once we cut over to production.
DEFAULT_BASE_URL = "https://search.testsliderule.org"
DEFAULT_SEARCH_PATH = "/docsearch/search"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def resolve_search_url(args: argparse.Namespace) -> str:
    """Return the full search URL based on flags + env + defaults.

    Precedence: --search-url (full URL) > SLIDERULE_SEARCH_BASE env var
    > built-in default. The base strips to scheme://host so the path
    suffix is appended at the root.
    """
    if args.search_url:
        parsed = urlparse(args.search_url)
        if not parsed.scheme or not parsed.netloc:
            print(
                f"ERROR: --search-url must be a full URL with scheme and host "
                f"(e.g. https://search.example.com/docsearch/search). "
                f"Got: {args.search_url!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        return args.search_url

    base = os.environ.get("SLIDERULE_SEARCH_BASE", DEFAULT_BASE_URL).rstrip("/")
    return f"{base}{DEFAULT_SEARCH_PATH}"


SERVER_TOP_K_MAX = 50  # keep in sync with server/app.py's Pydantic Field(le=50)


def _top_k(s: str) -> int:
    """argparse validator that matches the server's top_k bounds.
    Failing client-side gives a clean argparse error instead of an HTTP
    round-trip that returns 422."""
    v = int(s)
    if not (1 <= v <= SERVER_TOP_K_MAX):
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {SERVER_TOP_K_MAX} (server max), got {v}"
        )
    return v


def _parse_categories(s: str | None) -> list[str] | None:
    if not s:
        return None
    items = [c.strip() for c in s.split(",") if c.strip()]
    return items or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="The search query.")
    parser.add_argument("--top-k", type=_top_k, default=5,
                        help=f"Number of results to return (1..{SERVER_TOP_K_MAX}).")
    parser.add_argument("--disable-lexical", action="store_true",
                        help=(
                            "Ask the server to skip lexical rank fusion; "
                            "results become pure cosine similarity. "
                            "Mainly for A/B comparison."
                        ))
    parser.add_argument("--categories", default=None,
                        help=(
                            "Comma-separated category allowlist "
                            "(e.g. user_guide,api_reference). "
                            "Filter is applied server-side before ranking."
                        ))
    parser.add_argument("--search-url", default=None,
                        help=(
                            "Full search URL override. Otherwise derived from "
                            "SLIDERULE_SEARCH_BASE env var or the default base."
                        ))
    parser.add_argument("--timeout", type=float, default=60.0,
                        help=(
                            "HTTP timeout in seconds (default: 60). "
                            "The server is a Lambda; a cold start adds ~3–5s "
                            "to the first request after a period of idleness."
                        ))
    args = parser.parse_args()

    url = resolve_search_url(args)
    body = {
        "query": args.query,
        "top_k": args.top_k,
        "disable_lexical": args.disable_lexical,
    }
    categories = _parse_categories(args.categories)
    if categories is not None:
        body["categories"] = categories

    # The service is fronted by CloudFront → Lambda Function URL with OAC,
    # which SigV4-signs the origin request including a hash of the body.
    # CloudFront doesn't buffer the request body to hash it itself; it
    # copies the viewer's `x-amz-content-sha256` header into the signed
    # request. Without this header (or with a mismatched value) the Lambda
    # URL returns 403. Serialize the body to bytes once so the hash and
    # the wire payload are exactly the same bytes.
    body_bytes = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-amz-content-sha256": hashlib.sha256(body_bytes).hexdigest(),
    }

    log(f"POST {url}")
    try:
        resp = requests.post(url, data=body_bytes, headers=headers, timeout=args.timeout)
    except requests.RequestException as e:
        print(f"\nERROR: request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if resp.status_code != 200:
        print(
            f"\nERROR: server returned {resp.status_code}\n"
            f"  url={url}\n"
            f"  body={resp.text[:500]}",
            file=sys.stderr,
        )
        return 2

    try:
        payload = resp.json()
    except ValueError as e:
        print(f"\nERROR: non-JSON response: {e}", file=sys.stderr)
        return 2

    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
