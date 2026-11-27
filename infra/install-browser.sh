#!/usr/bin/env bash
# Installs the Chromium that the pinned playwright (versions.md, Dev tools) drives for the demo rehearsal (P5.4): the
# browser into ~/.cache/ms-playwright, and with --with-deps the Ubuntu libraries it needs, through sudo apt.
# Idempotent: playwright skips a browser already there, apt a package already installed.
# Usage: install-browser.sh
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "usage: $0" >&2
  exit 2
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

cd "$REPO_ROOT"
uv run --locked playwright install --with-deps chromium
