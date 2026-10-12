#!/usr/bin/env bash
# Builds dist/gow3-x86_64.AppImage: the port's current build (run build.sh first),
# its scripts and the launcher, bundled with their Nix closure (nix-appimage: the AppImage mounts
# its /nix/store with user namespaces, available on SteamOS and most desktops).
# Running it opens the launcher; `--play` starts the game with the launcher's saved settings.
# Data (generated files, saves, gow3.ini): ~/.local/share/gow3 (GOW3_DATA_DIR).
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
[[ -f out/gow3-probe && -f out/gpu/libgow3gpu.so ]] || { echo 'Build first: bash build.sh' >&2; exit 1; }
root=$PWD
# The package version follows the launcher (packaging/default.nix reads GOW3_VERSION).
export GOW3_VERSION
GOW3_VERSION=$(python3 -c 'import re; print(re.search(r"^VERSION = .([0-9.]+).", open("launcher/gow3_launcher_win.py").read(), re.M).group(1))')
# The libraries' store paths (RUNPATH entries and their closures come along).
{
    echo '['
    for elf in out/gow3-probe out/gpu/libgow3gpu.so; do
        readelf -d "$elf" | sed -n 's/.*\[\(.*\)\]/\1/p' | tr ':' '\n'
    done | grep -o '^/nix/store/[^/]*' | sort -u | grep -v -- '-nix-shell$' | sed 's/.*/  "&"/'
    echo ']'
} > packaging/runtime-paths.nix
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
(cd "$work" && nix bundle --impure --bundler github:ralismark/nix-appimage \
    --expr "import $root/packaging {}")
mkdir -p dist
install -m755 "$work/gow3.AppImage" dist/gow3-x86_64.AppImage
# Light packaging check: the AppImage embeds the launcher and desktop entry.
if (cd "$work" && "$work/gow3.AppImage" --appimage-extract launcher/gow3_launcher_linux.py \
    launcher/io.github.gow3pc.Launcher.desktop >/dev/null 2>&1); then
    rm -rf "$work/squashfs-root"
else
    echo 'warning: could not verify AppImage contents (needs user namespaces)' >&2
fi
ls -lh dist/gow3-x86_64.AppImage
