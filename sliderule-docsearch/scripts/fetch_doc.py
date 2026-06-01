"""Fetch a single SlideRule docs page and print its readable text.

Fallback for sliderule-docsearch: when a search result's chunk `text` is
too thin or truncated to fully answer, fetch the full page at that
result's `url` and read the complete content. Every docsearch result
carries a fully-qualified, directly-fetchable URL on the docs host.

The host is allowlisted to docs.slideruleearth.io (override with the
SLIDERULE_DOCS_HOST env var, mirroring search.py's SLIDERULE_SEARCH_BASE)
so this can't be turned into a general-purpose fetcher for arbitrary URLs.

Usage:
    python scripts/fetch_doc.py <url-or-path> [--raw] [--timeout S]

    # full URL straight from a search result
    python scripts/fetch_doc.py https://docs.slideruleearth.io/user_guide/icesat2.html

    # bare path (the docs host is prefixed for you)
    python scripts/fetch_doc.py /user_guide/icesat2.html

    # the docs root, to navigate when no result URL is usable
    python scripts/fetch_doc.py /

See SKILL.md ("Direct-fetch fallback") for when to reach for this.
"""

from __future__ import annotations

import argparse
import os
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


def _missing_deps_exit(exc: ModuleNotFoundError) -> None:
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


DEFAULT_DOCS_HOST = "docs.slideruleearth.io"
DEFAULT_DOCS_BASE = f"https://{DEFAULT_DOCS_HOST}"

# Tags whose contents are noise, not page text.
_SKIP_CONTENT = {"script", "style", "noscript", "template", "svg"}
# Tags that introduce a line break in the rendered text.
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "br", "li", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol", "blockquote",
}


class _TextExtractor(HTMLParser):
    """Minimal stdlib HTML-to-text: drop script/style, keep block breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank lines / trailing whitespace into something
        # readable without pulling in a parser dependency.
        lines = [ln.strip() for ln in raw.splitlines()]
        out: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip() + "\n"


def resolve_url(target: str) -> str:
    """Resolve a full URL or bare path against the docs host, and enforce
    the host allowlist so this stays a docs fetcher, not an open proxy."""
    allowed_host = os.environ.get("SLIDERULE_DOCS_HOST", DEFAULT_DOCS_HOST).lower()
    base = f"https://{allowed_host}"

    parsed = urlparse(target)
    if not parsed.scheme:
        # Bare path (or host-relative) — anchor it to the docs base.
        url = urljoin(base + "/", target.lstrip("/"))
        parsed = urlparse(url)
    else:
        url = target

    if parsed.scheme not in ("http", "https"):
        print(f"ERROR: unsupported URL scheme {parsed.scheme!r}", file=sys.stderr)
        sys.exit(2)
    if parsed.hostname is None or parsed.hostname.lower() != allowed_host:
        print(
            f"ERROR: refusing to fetch host {parsed.hostname!r}; this helper only "
            f"fetches {allowed_host} (set SLIDERULE_DOCS_HOST to override).",
            file=sys.stderr,
        )
        sys.exit(2)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="Full docs URL or a bare path (e.g. /user_guide/icesat2.html).")
    parser.add_argument("--raw", action="store_true",
                        help="Print the raw HTML instead of extracted text.")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="HTTP timeout in seconds (default: 30).")
    args = parser.parse_args()

    url = resolve_url(args.target)

    print(f"GET {url}", file=sys.stderr, flush=True)
    try:
        resp = requests.get(url, timeout=args.timeout, allow_redirects=True,
                            headers={"User-Agent": "sliderule-docsearch/fetch_doc"})
    except requests.RequestException as e:
        print(f"\nERROR: request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if resp.status_code != 200:
        print(
            f"\nERROR: server returned {resp.status_code} for {url}\n"
            f"  body={resp.text[:300]}",
            file=sys.stderr,
        )
        return 2

    if args.raw:
        sys.stdout.write(resp.text)
        if not resp.text.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    extractor = _TextExtractor()
    extractor.feed(resp.text)
    sys.stdout.write(extractor.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
