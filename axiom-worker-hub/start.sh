#!/usr/bin/env bash
# Start the Axiom Worker Hub.   ./start.sh          -> this computer only
#                               ./start.sh --lan    -> also your phone on the same Wi-Fi
cd "$(dirname "$0")" || exit 1
exec python3 server.py "$@"
