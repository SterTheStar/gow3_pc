#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared settings logic of the Linux launcher (launcher/gow3_launcher_linux.py).

Pure Python, no GTK: INI/settings load and save plus the process environment for
run.sh. The option tables (defaults, controls, upscalers, ...) live in
launcher/gow3_launcher_win.py so both launchers share a single source of truth.
"""

import json
import locale
import os
from pathlib import Path
import sys

PORT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PORT_DIR / 'scripts'))
sys.path.insert(0, str(PORT_DIR / 'launcher'))

import gow3_launcher_win as core  # noqa: E402  (option tables, no Tkinter at import)
import gow3_lang as lang  # noqa: E402  (UI language names)

PACKAGED = bool(os.environ.get('GOW3_PREBUILT'))
if os.environ.get('GOW3_DATA_DIR'):
    DATA_DIR = Path(os.environ['GOW3_DATA_DIR'])
elif PACKAGED:
    DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'gow3'
else:
    DATA_DIR = PORT_DIR
CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gow3-launcher'
CONFIG_FILE = CONFIG_DIR / 'settings.json'
MOD_PRESETS = DATA_DIR / 'mods_presets.json'
APP_NAME = core.APP_NAME
VERSION = core.VERSION

# Re-exported option tables (single source of truth in gow3_launcher_win.py).
INI_FLAGS = core.INI_FLAGS
INI_DEFAULTS = core.INI_DEFAULTS
APP_DEFAULTS = core.APP_DEFAULTS
CONTROLS = core.CONTROLS
PS_BUTTON_NAMES = core.PS_BUTTON_NAMES
PAD_STYLES = core.PAD_STYLES
UPSCALERS = core.UPSCALERS
PRESETS = core.PRESETS
OUTPUTS = core.OUTPUTS
LIVE = core.LIVE
LANGUAGES = core.LANGUAGES
DRAW_PIPE = core.DRAW_PIPE
READBACKS = core.READBACKS
STREAM_MEMORY = core.STREAM_MEMORY
FRAMES_QUEUED = core.FRAMES_QUEUED
DISPLAY_MODES = core.DISPLAY_MODES
FPS_LIMITS = core.FPS_LIMITS
RESOLUTION_CHOICES = core.RESOLUTION_CHOICES
ENGINE_FPS_CHOICES = core.ENGINE_FPS_CHOICES


def system_language():
    """UI language code from the system locale, falling back to English."""
    try:
        code = (locale.getdefaultlocale()[0] or 'en').split('_')[0].lower()
    except (AttributeError, ValueError):
        return 'en'
    available = {value for value, _text in lang.LANGUAGE_NAMES} - {''}
    return code if code in available else 'en'


def load_settings():
    settings = {**APP_DEFAULTS, 'ui_language': ''}
    try:
        settings.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        pass
    if not isinstance(settings.get('controls'), dict):
        settings['controls'] = {}
    if not isinstance(settings.get('game_patches'), dict):
        settings['game_patches'] = {}
    return settings


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding='utf-8')


def ini_path():
    return Path(os.environ.get('GOW3_CONFIG', DATA_DIR / 'gow3.ini'))


def load_ini():
    values, lines = dict(INI_DEFAULTS), []
    try:
        lines = ini_path().read_text(encoding='utf-8').splitlines()
    except OSError:
        pass
    for line in lines:
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values, lines


def save_ini(values, lines):
    """Rewrites the edited keys in place, appends missing ones, keeps comments and other keys."""
    written, out = set(), []
    for line in lines:
        if '=' in line and not line.lstrip().startswith('#'):
            key = line.split('=', 1)[0].strip()
            if key in values:
                out.append(f'{key}={values[key]}')
                written.add(key)
                continue
        out.append(line)
    if not lines:
        out.append('# gow3 settings (in-game menu: Insert / R3+L2)')
    out += [f'{key}={value}' for key, value in values.items() if key not in written]
    ini_path().write_text('\n'.join(out) + '\n', encoding='utf-8')


def game_dir_of(settings):
    return Path(settings.get('game_dir') or APP_DEFAULTS['game_dir']).expanduser()


def list_gpus():
    """The Vulkan GPUs in the order the game numbers them, or [] when the tool cannot tell."""
    import subprocess
    tool = core.find_tool('gow3-gpu-capabilities')
    if not tool:
        return []
    try:
        done = subprocess.run([tool, '--list'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return core.parse_gpu_list(done.stdout) if done.returncode == 0 else []


def user_dir_of(settings):
    custom = settings.get('user_dir')
    return Path(custom).expanduser() if custom else DATA_DIR / 'user'


def derive_graphics_env(env, settings, ini, game_dir):
    """run.py's per-start graphics derivation, for run.sh (which has none of its own).

    The resolution and engine frame rate patches follow gow3.ini: they are appended to the
    launcher's patch selection (GOW3_PATCHES_ONLY=1), with the direct-memory and VBlank
    requirements the patch notes ask for. Returns the applied engine frame rate.
    """
    import graphics_options
    from patches import game_patchable, game_profile, game_title_id, patch_requirements, selected_patches

    options = graphics_options.startup_options(ini)
    env.update(graphics_options.environment(options))
    engine_fps = 60
    title_id = game_title_id(str(game_dir)) if game_dir else None
    profile = game_profile(title_id)
    patched = bool(profile) and (game_patchable(str(game_dir)) or bool(env.get('GOW3_FORCE_PATCHES')))
    if patched:
        names = selected_patches(profile[1], profile[2], ';'.join(
            settings.get('game_patches', {}).get(title_id, [])), True)
        names = graphics_options.patch_names(names, options)
        env['GOW3_PATCHES'] = ';'.join(names)
        env['GOW3_PATCHES_ONLY'] = '1'
        engine_fps = graphics_options.applied_engine_fps(names)
        dmem, vblank = patch_requirements(profile[1], names, profile[2])
        if dmem and 'GOW3_DMEM_MB' not in env:
            env['GOW3_DMEM_MB'] = str(dmem)
        if vblank and 'GOW3_VBLANK_HZ' not in env:
            env['GOW3_VBLANK_HZ'] = str(vblank)
    env.setdefault('GOW3_VBLANK_HZ', '60')
    # Without the frame rate patch the engine steps 1/60 s per frame: the present thread must
    # not flip faster, or the game speeds up (same rule as run.py).
    env['GOW3_ENGINE_FPS'] = str(engine_fps)
    return engine_fps


def game_environment(settings, ini):
    """Environment for run.sh from the launcher settings and gow3.ini values."""
    env = dict(os.environ)
    game_dir = game_dir_of(settings)
    user_dir = user_dir_of(settings)
    env['GOW3_GAME_DIR'] = str(game_dir)
    env['GOW3_DATA_DIR'] = str(DATA_DIR)
    env['GOW3_CONFIG'] = str(ini_path())
    # The launcher's patch selection is the whole list (patches.py), as in run.py.
    title_id, profile = core.game_profile_of(str(game_dir))
    chosen = settings.get('game_patches', {}).get(title_id) if title_id else None
    if profile and chosen is not None:
        env['GOW3_PATCHES'] = ';'.join(chosen)
        env['GOW3_PATCHES_ONLY'] = '1'
    env['GOW3_USER_DIR'] = str(user_dir)
    env['GOW3_GPU_USER_DIR'] = str(user_dir)
    env['GOW3_MODS_DIR'] = str(settings.get('mods_dir') or DATA_DIR / 'mods')
    env['GOW3_MODS_CONFIG'] = str(DATA_DIR / 'mods.json')
    env['GOW3_MODS_ENABLED'] = '1' if settings.get('mods_enabled', True) else '0'
    env['GOW3_PATCHES_DIR'] = str(settings.get('patches_dir') or DATA_DIR / 'patches')
    env['GOW3_PATCHES_CONFIG'] = str(DATA_DIR / 'patches.json')
    env['GOW3_LANGUAGE'] = str(settings.get('language', '1'))
    if str(settings.get('player_name', '')).strip():
        env['GOW3_USER_NAME'] = str(settings['player_name']).strip()
    if settings.get('hdr'):
        env['GOW3_HDR'] = '1'
    for key, name in (('draw_pipe', 'GOW3_DRAW_PIPE'), ('readbacks', 'GOW3_READBACKS')):
        if settings.get(key):
            env[name] = settings[key]
    gpu_id = str(settings.get('gpu_id', ''))
    env.pop('GOW3_GPU_ID', None)
    if gpu_id.isdigit():
        env['GOW3_GPU_ID'] = gpu_id
    stream_memory = settings.get('stream_memory', APP_DEFAULTS['stream_memory'])
    env['GOW3_STREAM_MEMORY'] = stream_memory if stream_memory in ('cached', 'host', 'device') else 'cached'
    env['GOW3_BACKGROUND_WARMUP'] = '1' if settings.get('background_warmup', True) else '0'
    env['GOW3_PARALLEL_WARMUP'] = '1' if settings.get('parallel_warmup', True) else '0'
    env['GOW3_PIPELINE_BINARIES'] = '1' if settings.get('pipeline_binary_cache', True) else '0'
    env['GOW3_PERF_DIAG'] = '1' if settings.get('perf_diag', False) else '0'
    for key, name in (('frame_stats', 'GOW3_FRAME_STATS'), ('gpu_profile', 'GOW3_GPU_PROFILE'),
                      ('vk_validation', 'GOW3_VK_VALIDATION'),
                      ('frame_capture_key', 'GOW3_FRAME_CAPTURE_KEY')):
        if settings.get(key):
            env[name] = '1'
    for kind, name in (('key', 'GOW3_KEY_MAP'), ('pad', 'GOW3_PAD_MAP')):
        if value := core.control_map(settings.get('controls', {}), kind):
            env[name] = value
    # The temporal upscalers and motion vectors are not calibrated for this game yet; linear
    # image readbacks fix its corrupted textures (same defaults as run.py).
    env.setdefault('GOW3_UPSCALER', 'none')
    env.setdefault('GOW3_READBACK_LINEAR', '1')
    derive_graphics_env(env, settings, ini, str(game_dir) if game_dir else '')
    for extra in str(settings.get('extra_env', '')).split():
        if '=' in extra:
            key, value = extra.split('=', 1)
            env[key] = value
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def window_icon(out_dir):
    """BMP window icon from the game's icon0.png (SDL loads it without extra libraries)."""
    try:
        from PIL import Image
        icon = out_dir / 'window_icon.bmp'
        with Image.open(game_icon_source()) as image:
            image.convert('RGBA').resize((64, 64), Image.LANCZOS).save(icon)
        return icon
    except (ImportError, OSError):
        return None


def game_icon_source():
    return game_dir_of(load_settings()) / 'sce_sys' / 'icon0.png'
