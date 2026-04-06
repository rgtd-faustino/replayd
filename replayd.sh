#!/bin/sh
# replayd.sh – launcher shim installed to $FLATPAK_DEST/bin/replayd
#
# Sets PYTHONPATH so Python finds the app source installed in
# $FLATPAK_DEST/share/replayd, then hands off to main.py.
# This avoids needing a setup.py / pyproject.toml for a pure-script app.

export PYTHONPATH="${FLATPAK_DEST}/share/replayd:${PYTHONPATH}"
exec python3 "${FLATPAK_DEST}/share/replayd/main.py" "$@"
