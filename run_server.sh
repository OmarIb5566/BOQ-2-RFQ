#!/usr/bin/env sh
# BOQ -> RFQ web server on Linux (production: use deploy/linux/boq2rfq-web.service instead)
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
exec .venv/bin/python server.py --no-browser "$@"
