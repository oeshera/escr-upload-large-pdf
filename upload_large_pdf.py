#!/usr/bin/env python3
"""Upload a large PDF to an existing eScriptorium document, one page at a time.

Workaround for server-side PDF imports that crash on large files: the server's
PdfParser reads the whole PDF into memory (pdfload_buffer) and the upload itself
can exceed nginx/Django body-size limits. Instead, this script rasterizes each
page locally and posts it as a plain image to /api/documents/<pk>/parts/.

Rasterization mirrors eScriptorium's app/apps/imports/parsers.py:PdfParser as closely as
possible: the same library (libvips via pyvips), the same loader options
(page=n, dpi=300, access='sequential'), the same PNG encoder call
(write_to_buffer('.png')), the same filenames ("<pdfname>_page_<n>.png") and
the same part source ("pdf//<pdfname>"). The only intentional difference is
that pages are read from the file on disk rather than from an in-memory copy
of the whole PDF, which is what keeps memory usage low. Pixel-identical output
additionally requires the same libvips version and PDF backend (poppler or
PDFium) as the server; compare `vips --vips-version` / `vips -l foreign | grep
-i pdf` locally and inside the eScriptorium container.

Re-running is safe: pages already present in the document (by filename) are
skipped, so an interrupted upload can simply be restarted.

Credentials are read, in order of precedence, from command-line options, the
environment, and a .env file (./.env, or the one next to this script); see
.env.example.

Usage (run.sh creates the virtualenv on first use):
    ./run.sh --document 123 path/to/big.pdf

See README.md for setup, limitations and troubleshooting.
"""
import argparse
import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import pyvips
import requests

# Same as PdfParser.__init__
pyvips.voperation.cache_set_max(10)

DPI = 300  # PdfParser.parse hardcodes this


def load_env_file(path):
    """Minimal .env reader: KEY=VALUE lines, optional `export`, quotes and comments.
    Never overrides variables already set in the environment."""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] in ("'", '"') and value.find(value[0], 1) != -1:
                value = value[1:value.find(value[0], 1)]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            os.environ.setdefault(key, value)


def find_env_file(explicit):
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(f"--env-file {explicit} does not exist.")
        return explicit
    for candidate in (os.path.join(os.getcwd(), ".env"),
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        if os.path.isfile(candidate):
            return candidate
    return None


def authenticate(session, base, token, username, password):
    if not token:
        if not (username and password):
            sys.exit("No credentials: set ESCRIPTORIUM_TOKEN, or ESCRIPTORIUM_USERNAME "
                     "and ESCRIPTORIUM_PASSWORD (in .env, the environment, or as options).")
        # Returns the user's existing token (never regenerates it)
        resp = session.post(f"{base}/api/token-auth/",
                            data={"username": username, "password": password}, timeout=60)
        if resp.status_code != 200:
            sys.exit(f"Login failed ({resp.status_code}): {resp.text[:300]}")
        token = resp.json()["token"]
    session.headers["Authorization"] = f"Token {token}"


def check_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        sys.exit(f"Invalid server URL {url!r}: expected something like https://escriptorium.example.org")
    # Credentials are sent with every request; only allow plain http for local testing
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        sys.exit(f"Refusing to send credentials over plain http to {parts.hostname}; use https.")
    return url.rstrip("/")


def same_origin(base, url):
    """Re-anchor a server-generated absolute URL (e.g. a pagination `next` link) on `base`.
    Behind a misconfigured proxy DRF can build these with http:// or an internal host name,
    which would leak the token or simply fail."""
    b, u = urlsplit(base), urlsplit(url)
    return urlunsplit((b.scheme, b.netloc, u.path, u.query, ""))


def existing_filenames(session, base, doc_pk):
    names = set()
    url = f"{base}/api/documents/{doc_pk}/parts/"
    while url:
        resp = session.get(url, timeout=60)
        if resp.status_code in (401, 403, 404):
            sys.exit(f"Cannot access document {doc_pk} ({resp.status_code}): {resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        names.update(p["original_filename"] for p in data["results"])
        url = data.get("next") and same_origin(base, data["next"])
    return names


def render_page(pdf_path, page_nb):
    # Same call as PdfParser.parse, but pdfload (from file) instead of pdfload_buffer
    page = pyvips.Image.pdfload(pdf_path, page=page_nb, dpi=DPI, access='sequential')
    return page.write_to_buffer('.png')


def upload_page(session, base, doc_pk, fname, content, source, retries):
    url = f"{base}/api/documents/{doc_pk}/parts/"
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(
                url,
                files={"image": (fname, content, "image/png")},
                data={"source": source},
                timeout=600,
            )
            if resp.status_code in (200, 201):
                return
            # 4xx other than rate limiting won't get better by retrying
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                sys.exit(f"Upload of {fname} rejected ({resp.status_code}): {resp.text[:500]}")
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            err = str(e)
        wait = min(60, 5 * attempt)
        print(f"  attempt {attempt}/{retries} failed ({err}); retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    sys.exit(f"Giving up on {fname} after {retries} attempts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--document", required=True, type=int, help="target document pk (from its URL)")
    parser.add_argument("--env-file", help="path to a .env file (default: ./.env or next to this script)")
    parser.add_argument("--url", help="default: $ESCRIPTORIUM_URL")
    parser.add_argument("--token", help="default: $ESCRIPTORIUM_TOKEN")
    parser.add_argument("--username", help="default: $ESCRIPTORIUM_USERNAME")
    parser.add_argument("--password", help="default: $ESCRIPTORIUM_PASSWORD (prefer .env: options end up "
                                           "in shell history and are visible to other users via ps)")
    parser.add_argument("--start", type=int, default=1, help="first page to upload (1-based)")
    parser.add_argument("--end", type=int, help="last page to upload (inclusive)")
    parser.add_argument("--retries", type=int, default=5)
    args = parser.parse_args()

    env_file = find_env_file(args.env_file)
    if env_file:
        load_env_file(env_file)

    url = args.url or os.environ.get("ESCRIPTORIUM_URL")
    if not url:
        sys.exit("No server URL: set ESCRIPTORIUM_URL (in .env or the environment) or pass --url.")
    base = check_url(url)

    pdfname = os.path.basename(args.pdf)
    source = f"pdf//{pdfname}"
    if not os.path.isfile(args.pdf):
        sys.exit(f"No such file: {args.pdf}")
    try:
        n_pages = pyvips.Image.pdfload(args.pdf, access='sequential').get('n-pages')
    except pyvips.error.Error as e:
        sys.exit(f"Invalid PDF file: {e}")

    last = args.end or n_pages
    if not 1 <= args.start <= n_pages:
        parser.error(f"--start must be between 1 and {n_pages} (the PDF's page count)")
    if not args.start <= last <= n_pages:
        parser.error(f"--end must be between --start ({args.start}) and {n_pages}")

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    authenticate(session, base,
                 args.token or os.environ.get("ESCRIPTORIUM_TOKEN"),
                 args.username or os.environ.get("ESCRIPTORIUM_USERNAME"),
                 args.password or os.environ.get("ESCRIPTORIUM_PASSWORD"))
    already = existing_filenames(session, base, args.document)

    print(f"{pdfname}: {n_pages} pages, uploading {args.start}-{last} to document "
          f"{args.document} on {base} (libvips {pyvips.version(0)}.{pyvips.version(1)}."
          f"{pyvips.version(2)}{', using ' + env_file if env_file else ''})")
    n = args.start
    try:
        for n in range(args.start, last + 1):
            fname = '%s_page_%d.png' % (pdfname, n)  # same naming as PdfParser.parse
            if fname in already:
                print(f"[{n}/{last}] {fname} already uploaded, skipping")
                continue
            try:
                content = render_page(args.pdf, n - 1)
            except pyvips.error.Error as e:
                hint = f" Use --start {n + 1} to skip it." if n < last else ""
                sys.exit(f"Could not render page {n}: {str(e).strip()}\n"
                         f"Pages before {n} are uploaded.{hint}")
            print(f"[{n}/{last}] {fname} ({len(content) / 1e6:.1f} MB)")
            upload_page(session, base, args.document, fname, content, source, args.retries)
    except KeyboardInterrupt:
        sys.exit(f"\nInterrupted at page {n}. Re-run the same command to resume; "
                 f"pages already uploaded are skipped.")

    print("Done.")


if __name__ == "__main__":
    main()
