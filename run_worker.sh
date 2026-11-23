#!/usr/bin/env sh
# BOQ -> RFQ background worker on Linux (production: use deploy/linux/boq2rfq-worker.service instead)
cd "$(dirname "$0")"
exec .venv/bin/python worker.py "$@"
