"""coord-mode's agent list and coord-tabs against fake hc / herdr-app in a temporary HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FakeHome(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.bin = self.home / '.local' / 'bin'
        self.bin.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home))

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/usr/bin/env bash\n' + body)
        path.chmod(0o755)


class CoordModeAgents(FakeHome):
    def test_status_skips_hc_attach_viewers_by_argv_or_foreground(self):
        terms = {'t1': {}, 't2': {'argv': ['bash'], 'foreground': {'argv': ['/x/hc', 'attach', 't1']}},
                 't3': {'argv': ['hc', 'attach', 't1']}}
        (self.home / 'terms.json').write_text(json.dumps(terms))
        self.fake('hc', '''case $1 in
  ls) jq '{terminals: [keys[] | {address: ., state: "live", agent: "claude-code", name: .}]}' "$HOME/terms.json";;
  get) jq --arg t "$2" '.[$t] + {state: "live", status: {state: "working"}}' "$HOME/terms.json";;
  *) exit 1;;
esac
''')
        out = subprocess.run([str(ROOT / 'bin' / 'coord-mode'), 'status'], env=self.env, text=True,
                             capture_output=True, timeout=20).stdout
        agents = [line.split()[0] for line in out.split('Agents:')[1].splitlines() if line.strip()]
        self.assertEqual(agents, ['t1'])


class CoordTabs(FakeHome):
    def test_opens_tab_for_name_with_spaces_on_a_fresh_home(self):
        mini = {'spaces': [{'name': 'project a', 'folder': '/srv/project a', 'machine': 'mac-mini',
                            'tabs': [{'name': 'fix the login', 'terminals': [{'terminal': 'mac-mini/t7'}]}]}]}
        (self.home / 'mini.json').write_text(json.dumps(mini))
        self.fake('hc', 'cat "$HOME/mini.json"\n')
        self.fake('herdr-app', '''printf '%s\\n' "$*" >> "$HOME/calls"
case $1 in
  ls) echo '{"spaces": []}';;
  do) case $2 in space.new) echo '{"space": "s1"}';; tab.new) echo '{"tab": "tab1"}';; esac;;
esac
''')
        subprocess.run([str(ROOT / 'macbook' / 'coord-tabs')], env=self.env, check=True, timeout=20)
        calls = (self.home / 'calls').read_text()
        self.assertIn('--name fix the login --argv ["hc","attach","t7"]', calls)
        self.assertIn('--name project a --folder /srv/project a', calls)
        state = json.loads((self.home / '.config/coord/tabs.json').read_text())
        self.assertEqual(state, {'mac-mini/t7': 'tab1'})


if __name__ == '__main__':
    unittest.main()
