# escr-upload-large-pdf

A temporary workaround for uploading large PDFs (roughly 200 MB and up) to eScriptorium while the server-side PDF import is being fixed.

It turns each page of the PDF into an image **on your own computer**, then uploads the images to an existing eScriptorium document one page at a time through the REST API. The server never receives the PDF, so the server's PDF import (which is what crashes) is never used.

## Why the normal import fails

eScriptorium's PDF importer (`PdfParser` in [`app/apps/imports/parsers.py`](https://gitlab.com/scripta/escriptorium/-/blob/develop/app/apps/imports/parsers.py)) loads the entire PDF into memory, twice. On a very large file that can run the worker out of memory. The upload itself can also exceed the server's request-size limit (150 MB in the stock nginx/Django config). This script avoids both problems: each request carries one page image, and the PDF is read from disk one page at a time.

## How close is it to the built-in import?

As close as possible. It uses the same library (libvips, through `pyvips`) with the same call and settings as the server:

| | Built-in import | This script |
|---|---|---|
| Renderer | libvips `pdfload` | libvips `pdfload` |
| Resolution | 300 DPI | 300 DPI |
| Output | PNG via `write_to_buffer('.png')` | same |
| Page names | `<pdfname>_page_<n>.png` | same |
| Page "source" | `pdf//<pdfname>` | same |
| Reads PDF from | a copy of the whole file in memory | the file on disk, one page at a time |

Given the same PDF, libvips version and PDF backend, the images are **byte-for-byte identical** to the built-in import's. This was tested by comparing against the server's exact call.

The server's libvips is built with either Poppler or PDFium. Different versions or backends can cause tiny rendering differences. The script prints your local libvips version when it starts. To compare:

```bash
# on your machine
vips --vips-version
vips -l foreign | grep -i 'load PDF with'

# inside the eScriptorium web/celery container (ask your admin); if the
# vips command isn't installed there, the version is still available via:
python -c "import pyvips; print(pyvips.version(0), pyvips.version(1), pyvips.version(2))"
```

Other differences, none of which should matter in practice:

- Pages uploaded this way show a status of *created* rather than *converted*. Pages uploaded as ordinary images in the web interface behave the same way.
- The web interface's import-progress report isn't used. Progress is printed in your terminal instead.

## Requirements

- macOS or Linux (on Windows, use WSL)
- Python 3.8 or newer
- **libvips with PDF support**, installed at the system level:
  - macOS: `brew install vips`
  - Debian/Ubuntu: `sudo apt install libvips42 libpoppler-glib8`. If `pip` has to build `pyvips` from source, also run `sudo apt install libvips-dev pkg-config`.
- An eScriptorium account that can edit the target document

`vips -l foreign | grep -i pdf` should list a `pdfload` loader. If nothing is listed, your libvips was built without PDF support.

## Setup

```bash
git clone https://github.com/oeshera/escr-upload-large-pdf.git
cd escr-upload-large-pdf
cp .env.example .env
$EDITOR .env           # fill in ESCRIPTORIUM_URL and ESCRIPTORIUM_TOKEN
```

On first run, `./run.sh` creates a virtualenv in `.venv/` and installs `pyvips` and `requests` into it. It reinstalls them automatically if `requirements.txt` changes. If you'd rather set up the virtualenv yourself:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python upload_large_pdf.py --help
```

### Credentials

The script needs the server URL plus **either** an API token **or** a username and password:

| Variable | Option | Meaning |
|---|---|---|
| `ESCRIPTORIUM_URL` | `--url` | e.g. `https://escriptorium.umd.edu` |
| `ESCRIPTORIUM_TOKEN` | `--token` | your API key (recommended) |
| `ESCRIPTORIUM_USERNAME` | `--username` | used only if no token is given |
| `ESCRIPTORIUM_PASSWORD` | `--password` | used only if no token is given |

**Order of precedence:** command-line option, then environment variable, then `.env` file. The `.env` file is looked for in the current directory, then in the folder containing the script. Use `--env-file path/to/file` to point at a specific one, e.g. one per server (`./run.sh --env-file .env.umd …`).

**Getting your API key:** log in, open your profile (top right), then go to the **API key** tab (`/profile/apikey/`).

**Username and password:** the script asks the server for your existing API key and never generates a new one, so logging in this way won't break other tools that use your key. It won't work for accounts that sign in through single sign-on; use the API key for those.

## Usage

1. In eScriptorium, create the document the pages should go into (or pick an existing one).
2. Open it and note the number in the URL: `https://escriptorium.umd.edu/document/1234/` means the document id is `1234`.
3. Run:

```bash
./run.sh --document 1234 /path/to/big.pdf
```

Output looks like:

```
big.pdf: 412 pages, uploading 1-412 to document 1234 on https://escriptorium.umd.edu (libvips 8.17.1, using /…/escr-upload-large-pdf/.env)
[1/412] big.pdf_page_1.png (24.3 MB)
[2/412] big.pdf_page_2.png (23.9 MB)
…
Done.
```

### Resuming

If the run stops for any reason (network drop, Ctrl-C, laptop sleep), **re-run the same command**. Pages already in the document are recognised by filename and skipped:

```
[1/412] big.pdf_page_1.png already uploaded, skipping
```

Temporary failures (timeouts, server errors, HTTP 429) are retried up to `--retries` times (default 5), waiting longer after each attempt. Retrying is safe: if the server did receive a page before the connection dropped, uploading it again replaces it rather than creating a duplicate.

### Uploading a range of pages

```bash
./run.sh --document 1234 --start 101 --end 200 big.pdf
```

`--start` and `--end` are 1-based and inclusive.

### All options

```
./run.sh --help
```

## Limitations and caveats

- **Page order is upload order.** New pages are added to the end of the document. Use a document that is empty, or that only contains earlier pages of the same PDF, and upload page ranges in ascending order. If you upload pages 101–200 before 1–100, they'll be out of order in the document and you'll have to reorder them in the web interface.
- **Skipping is by filename.** If the document already has a page called `big.pdf_page_7.png` from a *different* PDF with the same filename, page 7 will be skipped. Rename the PDF to avoid this.
- **Very large pages.** Each page is uploaded as a 300 DPI PNG, typically 5–40 MB for a scan. Oversized pages such as maps or foldouts can still be rejected. One reason is the server's request-size limit (150 MB by default). Another is the image-size safety check in Pillow, the server's image library, which triggers at around 179 megapixels (larger than A0 at 300 DPI). The built-in import doesn't run that check. If a page is rejected, the script stops and tells you which page it was.
- **Disk quota.** The server's storage quota still applies. If you run out, the upload stops with the server's error message.
- **Speed.** Pages are rendered and uploaded one at a time, so the upload speed of your connection is usually what limits it. For example, 400 pages of about 25 MB each is about 10 GB to send.

## Security notes

- **Don't commit or share your `.env`.** This repo's `.gitignore` excludes `.env` and `.env.*` (but not `.env.example`).
- Prefer `.env` over `--password` or `--token` on the command line. Command-line arguments end up in your shell history and can be seen by other users on the same machine (via `ps`).
- The script won't send credentials over plain `http://`, except to `localhost` for testing.
- It never follows server-supplied links to another host. Pagination links are always sent back to the URL you configured.

## Troubleshooting

| Message | Likely cause |
|---|---|
| `No credentials: …` | No `.env` found (check which directory you're running from) or the token line is empty. |
| `Login failed (400)` | Wrong username or password, or a single-sign-on account. Use `ESCRIPTORIUM_TOKEN` instead. |
| `Cannot access document N (401)` | The token is wrong or has been regenerated. Copy it again from `/profile/apikey/`. |
| `Cannot access document N (403/404)` | Wrong document id, or your account can't edit that document. |
| `Upload of … rejected (400): … disk storage …` | Storage quota reached. |
| `Upload of … rejected (413)` | That page image is larger than the server's upload limit. |
| `Invalid PDF file` | The file isn't a PDF, is damaged, or libvips was built without PDF support. |
| `Could not render page N` | That page couldn't be rendered. Earlier pages are uploaded. Re-run with `--start N+1` to skip it, then add that page manually. |
| `OSError: cannot load library 'libvips…'` / `ModuleNotFoundError: pyvips` | libvips isn't installed at the system level (see Requirements), or you're not using `./run.sh` or the `.venv`. On Apple Silicon Macs, if libvips is installed but can't be found, `export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`. |

## Files

| File | Purpose |
|---|---|
| `upload_large_pdf.py` | the script |
| `run.sh` | creates the virtualenv if it's missing, then runs the script |
| `requirements.txt` | Python dependencies (`pyvips`, `requests`) |
| `.env.example` | template for your `.env` |
| `.gitignore` | keeps `.env*` and `.venv/` out of git |
| `LICENSE` | The Unlicense (public domain) |

## License

Public domain, via [The Unlicense](https://unlicense.org/): use, copy, modify and redistribute it for any purpose, no attribution required. See [`LICENSE`](LICENSE).
