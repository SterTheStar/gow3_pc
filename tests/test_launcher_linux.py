"""Linux launcher settings (launcher/gow3_config.py): round-trips and run.sh environment."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from paths import ROOT
sys.path.insert(0, str(ROOT / 'launcher'))
import gow3_config as cfg  # noqa: E402


class LinuxConfigTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.patchers = [
            mock.patch.object(cfg, 'DATA_DIR', root),
            mock.patch.object(cfg, 'CONFIG_DIR', root / 'config'),
            mock.patch.object(cfg, 'CONFIG_FILE', root / 'config' / 'settings.json'),
            mock.patch.object(cfg, 'MOD_PRESETS', root / 'mods_presets.json'),
            mock.patch.dict(os.environ, {'GOW3_CONFIG': str(root / 'gow3.ini')}, clear=False),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ('GOW3_PATCHES', 'GOW3_PATCHES_ONLY', 'GOW3_DMEM_MB', 'GOW3_VBLANK_HZ',
                    'GOW3_ENGINE_FPS', 'GOW3_KEY_MAP', 'GOW3_PAD_MAP', 'GOW3_FORCE_PATCHES'):
            os.environ.pop(key, None)

    def test_settings_round_trip(self):
        settings = cfg.load_settings()
        self.assertEqual(settings['stream_memory'], 'cached')
        settings['stream_memory'] = 'device'
        settings['game_dir'] = '/games/CUSA01623'
        cfg.save_settings(settings)
        reopened = cfg.load_settings()
        self.assertEqual(reopened['stream_memory'], 'device')
        self.assertEqual(reopened['game_dir'], '/games/CUSA01623')

    def test_ini_round_trip_keeps_comments(self):
        cfg.ini_path().write_text('# comment\nupscaler=fsr3\n', encoding='utf-8')
        values, lines = cfg.load_ini()
        self.assertEqual(values['upscaler'], 'fsr3')
        self.assertEqual(values['engine_fps'], cfg.INI_DEFAULTS['engine_fps'])
        values['engine_fps'] = '240'
        cfg.save_ini({k: values[k] for k in cfg.INI_DEFAULTS}, lines)
        text = cfg.ini_path().read_text(encoding='utf-8')
        self.assertIn('# comment', text)
        self.assertIn('engine_fps=240', text)

    def test_environment_defaults(self):
        settings = {**cfg.APP_DEFAULTS, 'game_dir': '/games/CUSA01623', 'user_dir': ''}
        env = cfg.game_environment(settings, dict(cfg.INI_DEFAULTS))
        self.assertEqual(env['GOW3_GAME_DIR'], '/games/CUSA01623')
        self.assertEqual(env['GOW3_USER_DIR'], str(Path(self.folder.name) / 'user'))
        self.assertEqual(env['GOW3_GPU_USER_DIR'], env['GOW3_USER_DIR'])
        self.assertEqual(env['GOW3_MODS_ENABLED'], '1')
        self.assertEqual(env['GOW3_STREAM_MEMORY'], 'cached')
        # Same safety defaults as run.py: no calibrated temporal upscaler yet.
        self.assertEqual(env['GOW3_UPSCALER'], 'none')
        self.assertEqual(env['GOW3_READBACK_LINEAR'], '1')
        # No known game here: the engine stays at its original rate.
        self.assertEqual(env['GOW3_ENGINE_FPS'], '60')
        self.assertEqual(env['GOW3_VBLANK_HZ'], '60')
        self.assertNotIn('GOW3_KEY_MAP', env)
        self.assertNotIn('GOW3_PAD_MAP', env)

    def test_environment_control_maps_and_overrides(self):
        settings = {**cfg.APP_DEFAULTS, 'game_dir': '/games/CUSA01623',
                    'stream_memory': 'device',
                    'controls': {'key': {'cross': 'Q'}, 'pad': {'circle': 'a'}},
                    'extra_env': 'GOW3_TIMEOUT=30'}
        with mock.patch.dict(os.environ, {'GOW3_STREAM_MEMORY': 'host'}):
            env = cfg.game_environment(settings, dict(cfg.INI_DEFAULTS))
            self.assertEqual(os.environ.get('GOW3_STREAM_MEMORY'), 'host')
        self.assertEqual(env['GOW3_STREAM_MEMORY'], 'device')
        self.assertEqual(env['GOW3_KEY_MAP'], 'cross=Q')
        self.assertEqual(env['GOW3_PAD_MAP'], 'circle=a')
        self.assertEqual(env['GOW3_TIMEOUT'], '30')

    def test_environment_invalid_stream_memory_falls_back_to_cached(self):
        settings = {**cfg.APP_DEFAULTS, 'game_dir': '/games/CUSA01623', 'stream_memory': 'invalid'}
        env = cfg.game_environment(settings, dict(cfg.INI_DEFAULTS))
        self.assertEqual(env['GOW3_STREAM_MEMORY'], 'cached')

    def test_control_changes_with_unmapped_triggers(self):
        # L2/R2 have no gamepad mapping (None default): the stored selection must stay
        # strings so control_changes never crashes on them.
        import gow3_launcher_win as core
        stored = {'key': {}, 'pad': {'circle': 'a'}}
        values = {kind: core.control_changes(
            {b: (stored[kind].get(b) or d or '') for b, k, p in core.CONTROLS
             for d in [(k if kind == 'key' else p)]}, kind) for kind in ('key', 'pad')}
        self.assertEqual(values, {'key': {}, 'pad': {'circle': 'a'}})

    def test_entry_files(self):
        entry = ROOT / 'launcher' / 'gow3-launcher.sh'
        desktop = ROOT / 'launcher' / 'io.github.gow3pc.Launcher.desktop'
        self.assertTrue(entry.is_file())
        self.assertTrue(os.access(entry, os.X_OK))
        self.assertIn('gow3_launcher_linux.py', entry.read_text(encoding='utf-8'))
        content = desktop.read_text(encoding='utf-8')
        self.assertIn('io.github.gow3pc.Launcher', content)

    def test_gtk_launcher_source_compiles(self):
        import py_compile
        py_compile.compile(str(ROOT / 'launcher' / 'gow3_launcher_linux.py'), doraise=True)

    def test_option_tables_match_windows_launcher(self):
        import gow3_launcher_win as core
        for name in ('INI_DEFAULTS', 'APP_DEFAULTS', 'CONTROLS', 'UPSCALERS', 'PRESETS',
                     'RESOLUTION_CHOICES', 'ENGINE_FPS_CHOICES', 'STREAM_MEMORY'):
            self.assertIs(getattr(cfg, name), getattr(core, name))


if __name__ == '__main__':
    unittest.main()
