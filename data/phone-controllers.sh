#!/bin/sh
# Starts the desktop app; "phone-controllers --headless" runs only the server.
export PYTHONPATH="/app/share/phone-controllers${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m phonecontrollers "$@"
