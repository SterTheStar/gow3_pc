#!/usr/bin/env bash
# Starts the God of War III launcher (GTK4/libadwaita, launcher/gow3_launcher_linux.py). With Python, PyGObject, GTK 4 and
# libadwaita installed it runs directly; otherwise through nix-shell (shell.nix).
# `--play` starts the game with the saved settings and no window (Steam Deck game mode).
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
if python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
    exec python3 "$here/gow3_launcher_linux.py" "$@"
fi
exec nix-shell "$here/../shell.nix" --run "python3 '$here/gow3_launcher_linux.py' $*"
