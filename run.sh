#!/bin/bash
# Run the Nabu Trader Listener
cd "$(dirname "$0")"
exec /home/it26/.hermes/venvs/netra/bin/python src/listener.py
