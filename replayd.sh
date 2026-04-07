#!/bin/sh
export PYTHONPATH="/app/share/replayd:${PYTHONPATH}"
exec python3 "/app/share/replayd/main.py" "$@"