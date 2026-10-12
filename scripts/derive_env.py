#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Per-start environment derivation for run.sh (mirrors run.py).

Reads gow3.ini through graphics_options.prepare (restoring the last confirmed
options when a restarted launch never reached the game), selects the game
patches the ini implies (resolution, frame rate) on top of GOW3_PATCHES when
GOW3_PATCHES_ONLY=1, and prints `export KEY='value'` lines for run.sh to eval.
Everything else goes to stderr, never to stdout.
"""

import argparse
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graphics_options  # noqa: E402
from patches import (  # noqa: E402
    game_patchable,
    game_profile,
    game_title_id,
    patch_requirements,
    selected_patches,
)

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game-dir', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    env = dict(os.environ)
    # Values derived on the previous start are derived again from the new options.
    for key in env.pop('GOW3_DERIVED', '').split(','):
        env.pop(key, None)
    derived = []

    config = env.get('GOW3_CONFIG', str(Path(args.out).parent / 'gow3.ini'))
    graphics, reverted = graphics_options.prepare(config, Path(args.out) / 'graphics_state.json')
    if reverted:
        print('Graphics: the last restart never reached the game; previous options restored',
              flush=True, file=sys.stderr)
    env.update(graphics_options.environment(graphics))
    data = Path(env.get('GOW3_DATA_DIR', str(ROOT)))
    if not env.get('GOW3_FSR411_DIR') and not (ROOT / 'fsr4_411').is_dir() \
            and (data / 'fsr4_411').is_dir():
        env['GOW3_FSR411_DIR'] = str(data / 'fsr4_411')

    engine_fps = 60
    title_id = game_title_id(args.game_dir)
    profile = game_profile(title_id)
    patched = bool(profile) and (game_patchable(args.game_dir) or bool(env.get('GOW3_FORCE_PATCHES')))
    if patched:
        names = selected_patches(profile[1], profile[2], env.get('GOW3_PATCHES', ''),
                                 env.get('GOW3_PATCHES_ONLY') == '1')
        names = graphics_options.patch_names(names, graphics)
        env['GOW3_PATCHES'] = ';'.join(names)
        env['GOW3_PATCHES_ONLY'] = '1'
        engine_fps = graphics_options.applied_engine_fps(names)
        dmem, vblank = patch_requirements(profile[1], names, profile[2])
        for key, value in (('GOW3_DMEM_MB', dmem), ('GOW3_VBLANK_HZ', vblank)):
            if value and key not in env:
                env[key] = str(value)
                derived.append(key)
    # The temporal upscalers and motion vectors are not calibrated for this game yet; linear
    # image readbacks fix its corrupted textures (same defaults as run.py).
    env.setdefault('GOW3_UPSCALER', 'none')
    env.setdefault('GOW3_READBACK_LINEAR', '1')
    if 'GOW3_VBLANK_HZ' not in env:
        env['GOW3_VBLANK_HZ'] = '60'
        derived.append('GOW3_VBLANK_HZ')
    # Without the frame rate patch the engine steps 1/60 s per frame: the present thread must
    # not flip faster, or the game speeds up.
    env['GOW3_ENGINE_FPS'] = str(engine_fps)
    derived.append('GOW3_ENGINE_FPS')
    print(f'Engine frame rate: {engine_fps} FPS'
          + ('' if engine_fps > 60 or graphics['engine_fps'] == '60' else
             ' (the frame rate patch is not available for this game build)'),
          flush=True, file=sys.stderr)
    env['GOW3_DERIVED'] = ','.join(derived)

    watched = ('GOW3_PATCHES', 'GOW3_PATCHES_ONLY', 'GOW3_DMEM_MB', 'GOW3_VBLANK_HZ',
               'GOW3_ENGINE_FPS', 'GOW3_DERIVED', 'GOW3_UPSCALER', 'GOW3_READBACK_LINEAR',
               'GOW3_FSR411_DIR', 'GOW3_DEFERRED_READBACK', 'GOW3_STALE_READBACK',
               'GOW3_ASYNC_SHADERS')
    for key in watched:
        if key in env:
            print(f'export {key}={shlex.quote(env[key])}')


if __name__ == '__main__':
    main()
