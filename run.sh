#!/usr/bin/env bash
# Create the virtualenv on first use (and refresh it when requirements.txt changes),
# then run the uploader with all arguments passed through.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="$here/.venv"
stamp="$venv/.requirements-installed"

if [ ! -x "$venv/bin/python" ]; then
    echo "Creating virtualenv in $venv" >&2
    python3 -m venv "$venv"
fi
if [ ! -f "$stamp" ] || [ "$here/requirements.txt" -nt "$stamp" ]; then
    echo "Installing requirements" >&2
    "$venv/bin/pip" install --quiet --disable-pip-version-check -r "$here/requirements.txt"
    touch "$stamp"
fi

exec "$venv/bin/python" "$here/upload_large_pdf.py" "$@"
