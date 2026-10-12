#!/usr/bin/env bash
# Starts the God of War III launcher (GTK4/libadwaita, launcher/gow3_launcher_linux.py).
# Needs the system Python with PyGObject, GTK 4 and libadwaita installed.
# `--play` starts the game with the saved settings and no window (Steam Deck game mode).
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
if ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
    echo 'Need system Python with PyGObject, GTK 4 and libadwaita installed.' >&2; exit 1
fi
exec python3 "$here/gow3_launcher_linux.py" "$@"
