#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ${1:-} == --software ]]; then
    shift
    if [[ -z ${VK_DRIVER_FILES:-} ]]; then
        for candidate in /run/opengl-driver/share/vulkan/icd.d/lvp_icd*.json /usr/share/vulkan/icd.d/lvp_icd*.json; do
            if [[ -f $candidate ]]; then export VK_DRIVER_FILES=$candidate; break; fi
        done
    fi
    if [[ -z ${VK_DRIVER_FILES:-} ]]; then echo 'Lavapipe not found; set VK_DRIVER_FILES.' >&2; exit 1; fi
    export VK_LOADER_LAYERS_DISABLE='~implicit~'
fi
# GOW3_PREBUILT=1 (packaged builds, the AppImage): out/gow3-probe and its GPU library are installed
# next to this script; nothing is built and no nix-shell is needed.
# GOW3_DATA_DIR: writable directory for the generated files (out/), saves (user/) and gow3.ini;
# by default this directory.
data=${GOW3_DATA_DIR:-.}
out=$data/out
mkdir -p "$out"
export GOW3_CONFIG=${GOW3_CONFIG:-$data/gow3.ini}
# FSR 4.1.1 assets (tools/fsr4cap/build_assets.sh): next to run.sh or in the data directory.
if [[ -z ${GOW3_FSR411_DIR:-} && ! -d fsr4_411 && -d $data/fsr4_411 ]]; then
    export GOW3_FSR411_DIR=$data/fsr4_411
fi
if [[ -z ${GOW3_PREBUILT:-} && -z ${GOW3_IN_NIX_SHELL:-} ]] && ! { command -v pkg-config >/dev/null && pkg-config --exists vulkan sdl3; } && command -v nix-shell >/dev/null; then
    args=''; if (( $# )); then args=$(printf '%q ' "$@"); fi
    exec env GOW3_IN_NIX_SHELL=1 nix-shell shell.nix --run "bash run.sh $args"
fi
if [[ -z ${PYTHON:-} ]]; then
    PYTHON=$(command -v python3 || true)
    if [[ -z $PYTHON ]]; then
        for candidate in /nix/store/*-python3-*/bin/python3; do
            if [[ -x $candidate ]]; then PYTHON=$candidate; break; fi
        done
    fi
fi
if [[ -z ${PYTHON:-} ]]; then echo 'Install Python 3 or set PYTHON.' >&2; exit 1; fi
# Vulkan drivers: nix-shell Mesa ICDs plus the host NVIDIA driver when present
# (gow3_vulkan.py). Only when a bundle exists, so plain system runs keep full enumeration.
if [[ -n ${GOW3_BUNDLED_VK_DRIVER_FILES:-} || -n ${GOW3_NVIDIA_LIB_DIR:-} ]]; then
    eval "$("$PYTHON" launcher/gow3_vulkan.py --print-env)"
fi
# GOW3_GAME_DIR: the game's folder (eboot.bin, sce_module, ...); default next to this directory.
game=${GOW3_GAME_DIR:-../CUSA01623}
if [[ ! -f $game/eboot.bin ]]; then echo "No eboot.bin in $game (set GOW3_GAME_DIR)." >&2; exit 1; fi
original_game=$game
# Saves are backed up before every start (the launcher sets GOW3_SAVE_BACKED_UP when it
# already did, so a launcher start backs up once, not twice).
user=${GOW3_USER_DIR:-$data/user}
mkdir -p "$user"
if [[ -z ${GOW3_SAVE_BACKED_UP:-} ]]; then
    if made=$("$PYTHON" -c 'import sys; sys.path.insert(0, "scripts"); from pathlib import Path
from save_backup import backup
made = backup(Path(sys.argv[1]))
print(made if made else "")' "$user"); then
        [[ -n $made ]] && echo "Saves backed up to $made"
    else
        echo 'Save backup failed, starting anyway.' >&2
    fi
fi
# The window icon for the game (SDL loads BMP without extra libraries); the launcher
# sets GOW3_WINDOW_ICON itself when Pillow is available.
if [[ -z ${GOW3_WINDOW_ICON:-} ]]; then
    if "$PYTHON" -c 'import sys; from PIL import Image
with Image.open(sys.argv[1]) as image:
    image.convert("RGBA").resize((64, 64), Image.LANCZOS).save(sys.argv[2])' \
        "$original_game/sce_sys/icon0.png" "$out/window_icon.bmp" 2>/dev/null; then
        export GOW3_WINDOW_ICON=$out/window_icon.bmp
    fi
fi
game=$("$PYTHON" scripts/mods.py "$game" --out "$out" \
    --mods-dir "${GOW3_MODS_DIR:-$data/mods}" --config "${GOW3_MODS_CONFIG:-$data/mods.json}" \
    --enabled "${GOW3_MODS_ENABLED:-1}")
# A private merged view lasts for this launch, including restarts. Cleanup only our own view.
if [[ $game != "$(realpath "$original_game")" ]]; then
    mod_game=$game
    trap '"$PYTHON" -c '\''import shutil,sys; shutil.rmtree(sys.argv[1])'\'' "$mod_game"' EXIT
fi
"$PYTHON" scripts/prepare.py "$game" --out "$out"
"$PYTHON" scripts/link_libc.py "$game" --out "$out"
"$PYTHON" scripts/link_modules.py "$game" --out "$out"
"$PYTHON" scripts/content_profile.py "$game" --out "$out" --sku "${GOW3_CONTENT_SKU:-full}"
# Per-start graphics derivation (gow3.ini options, restart confirmation, patch selection,
# engine frame rate), like run.py. On top of the launcher's own derivation it is a no-op.
eval "$("$PYTHON" scripts/derive_env.py --game-dir "$game" --out "$out")"
# GOW3_PATCHES adds patch names ("a;b") to the game's built-in patch file.
"$PYTHON" scripts/patches.py --out "$out" --extra "${GOW3_PATCHES:-}" --game-dir "$game"     --patches-dir "${GOW3_PATCHES_DIR:-$data/patches}" --patches-config "${GOW3_PATCHES_CONFIG:-$data/patches.json}"
export GOW3_VBLANK_HZ=${GOW3_VBLANK_HZ:-60}
# In-game restart re-execs this script from the port directory (probe.c); the overlay
# also reads this as its restart-available flag (run.py sets a command line on Windows).
export GOW3_RESTART_COMMAND="bash run.sh"
# FSR 4: faster post passes next to the downloaded ones (incremental; tools/fsr4_optimize.sh).
if [[ -z ${GOW3_PREBUILT:-} && -d fsr4_shaders ]] && command -v spirv-cross >/dev/null; then
    bash tools/fsr4_optimize.sh || echo 'FSR 4: optimized post passes not built' >&2
fi
if [[ -n ${GOW3_PREBUILT:-} ]]; then
    probe=${GOW3_PROBE:-bin/gow3-probe}
else
    bash build.sh
    probe=out/gow3-probe
fi
probe_args=("$out/boot-linked.bin" --content-profile "$out/content.bin" --patches "$out/patches.bin" --app0 "$game" --user "${GOW3_USER_DIR:-$data/user}" --timeout "${GOW3_TIMEOUT:-0}" "$@")
if [[ -n ${mod_game:-} ]]; then
    "$probe" "${probe_args[@]}" &
    mod_pid=$!
    trap 'kill -TERM "$mod_pid" 2>/dev/null || true' TERM INT
    mod_status=0
    wait "$mod_pid" || mod_status=$?
    # An interrupted wait must finish the child before removing its mounted view.
    if kill -0 "$mod_pid" 2>/dev/null; then wait "$mod_pid" || mod_status=$?; fi
    exit "$mod_status"
fi
exec "$probe" "${probe_args[@]}"
