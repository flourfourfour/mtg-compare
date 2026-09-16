#!/usr/bin/env bash
# Start MTG Compare and open it in your browser.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 server.py "$@"
