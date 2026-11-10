#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3.12 >/dev/null 2>&1; then
  demo_python=python3.12
else
  demo_python=python3
fi
"$demo_python" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or newer is required"'
if [ ! -d .venv ]; then "$demo_python" -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
echo 'Open http://127.0.0.1:8000 (Ctrl+C to stop). Default demo uses fictional data and makes no model calls.'
exec .venv/bin/uvicorn sourcing_api.main:app --host 127.0.0.1 --port 8000 --no-access-log
