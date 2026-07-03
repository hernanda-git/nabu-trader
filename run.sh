#!/bin/bash
# Run the Auto-Trade Pipeline
cd "$(dirname "$0")"
exec /home/it26/.hermes/venvs/netra/bin/python src/main.py
