"""run.sh helpers: scripts/derive_env.py and launcher/gow3_vulkan.py --print-env."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paths import ROOT


def run_helper(name, args, env):
    full = dict(os.environ)
    full.update(env)
    for key in ('VK_DRIVER_FILES', 'LD_LIBRARY_PATH'):
        full.pop(key, None)
    done = subprocess.run([sys.executable, str(ROOT / name), *args],
                          capture_output=True, text=True, env=full, timeout=60)
    return done


def exports(stdout):
    import shlex
    values = {}
    for line in stdout.splitlines():
        assert line.startswith('export '), line
        key, _, value = shlex.split(line[len('export '):])[0].partition('=')
        values[key] = value
    return values


class DeriveEnvTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        (root / 'out').mkdir()
        (root / 'data').mkdir()
        (root / 'data' / 'gow3.ini').write_text('upscaler=none\n', encoding='utf-8')
        self.base = {'GOW3_CONFIG': str(root / 'data' / 'gow3.ini'),
                     'GOW3_DATA_DIR': str(root / 'data')}
        self.args = ['--game-dir', str(root / 'no-game'),
                     '--out', str(root / 'out')]

    def test_defaults_without_known_game(self):
        done = run_helper('scripts/derive_env.py', self.args, self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        values = exports(done.stdout)
        self.assertEqual(values['GOW3_ENGINE_FPS'], '60')
        self.assertEqual(values['GOW3_VBLANK_HZ'], '60')
        self.assertIn('GOW3_ENGINE_FPS', values['GOW3_DERIVED'].split(','))
        self.assertEqual(values['GOW3_UPSCALER'], 'none')

    def test_launcher_selection_survives_and_stale_derived_is_redone(self):
        env = dict(self.base, GOW3_PATCHES='Some Patch', GOW3_PATCHES_ONLY='1',
                   GOW3_ENGINE_FPS='999', GOW3_DERIVED='GOW3_ENGINE_FPS')
        done = run_helper('scripts/derive_env.py', self.args, env)
        self.assertEqual(done.returncode, 0, done.stderr)
        values = exports(done.stdout)
        self.assertEqual(values['GOW3_PATCHES'], 'Some Patch')
        self.assertEqual(values['GOW3_PATCHES_ONLY'], '1')
        self.assertEqual(values['GOW3_ENGINE_FPS'], '60')

    def test_vulkan_print_env_empty_without_bundle(self):
        done = run_helper('launcher/gow3_vulkan.py', ['--print-env'], self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), '')

    def test_vulkan_print_env_exports_bundle(self):
        env = dict(self.base, GOW3_BUNDLED_VK_DRIVER_FILES='/tmp/radeon_icd.json')
        done = run_helper('launcher/gow3_vulkan.py', ['--print-env'], env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('VK_DRIVER_FILES', exports(done.stdout))


if __name__ == '__main__':
    unittest.main()
