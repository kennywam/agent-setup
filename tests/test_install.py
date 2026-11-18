"""install.sh puts each machine's pieces in place without clobbering the user's edited files."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Install(unittest.TestCase):
    def install(self, *args, rc=0):
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), *args], env=dict(os.environ, HOME=str(self.home)),
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, rc, result.stderr)
        return result.stdout

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_mini(self):
        self.assertIn('coordinator/bin/hc', self.install('--dry-run'))
        self.assertEqual(list(self.home.iterdir()), [])
        self.install('mini')
        h = self.home
        for path in ('.local/bin/agent-msg', '.local/bin/coord-mode', '.local/lib/agentbus/store.py',
                     'coord/bin/hc', 'coord/bin/coord-watch', 'coord/CLAUDE.md', 'coord/.claude/settings.json',
                     '.config/systemd/user/coord-up.timer', '.config/systemd/user/agent-probe.service'):
            self.assertTrue((h / path).is_file(), path)
        self.assertEqual(os.readlink(h / '.local/bin/coord'), 'coord-mode')
        self.assertFalse((h / '.local/bin/hc').exists())  # the wrapper must never shadow the real hc
        (h / 'coord/CLAUDE.md').write_text('edited')
        self.assertIn('exists, kept', self.install())
        self.assertEqual((h / 'coord/CLAUDE.md').read_text(), 'edited')

    def test_macbook(self):
        self.install('macbook')
        h = self.home
        for name in ('agent-msg', 'agent-task', 'coord', 'coord-tabs', 'coord-watch', 'coord-mode', 'coord-statusline'):
            self.assertTrue(os.access(h / '.local/bin' / name, os.X_OK), name)
        self.assertEqual((h / '.local/bin/agent-msg').read_bytes(), (ROOT / 'macbook/agent-msg').read_bytes())
        plist = (h / 'Library/LaunchAgents/dev.coord.tabs.plist').read_text()
        self.assertIn(f'{h}/.local/bin/coord-tabs', plist)
        self.assertNotIn('@HOME@', plist)
        self.assertFalse((h / 'coord').exists())

    def test_usage(self):
        self.install('laptop', rc=2)


if __name__ == '__main__':
    unittest.main()
