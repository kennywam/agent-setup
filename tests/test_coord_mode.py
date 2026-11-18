"""coord-mode: mode phrases switch only as a whole first line; suspension holds until resume."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CoordMode(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)  # no ~/.local/bin/hc here, so agent nudges are no-ops
        self.env = dict(os.environ, HOME=str(self.home), COORD_NAME='claude')

    def run_mode(self, *args):
        return subprocess.run([str(ROOT / 'bin' / 'coord-mode'), *args], env=self.env, text=True,
                              capture_output=True, timeout=20).stdout.strip()

    def say(self, prompt):
        return self.run_mode('hook-text', json.dumps({'prompt': prompt, 'hook_event_name': 'UserPromptSubmit'}))

    def test_phrases_switch_only_as_whole_first_line(self):
        self.assertEqual(self.run_mode('get'), 'interactive')
        self.say('take over the login bug')
        self.assertEqual(self.run_mode('get'), 'interactive')
        self.assertIn('AUTONOMOUS', self.say('Take over.'))
        self.assertEqual(self.run_mode('get'), 'autonomous')
        self.assertIn('Return Briefing', self.say("I'm back"))
        self.assertIn('~/projects/CLAUDE.md', self.say('autonomous') + self.say('interactive'))

    def test_suspension_holds_until_resume_and_restores_mode(self):
        self.say('autonomous')
        self.say('suspend')
        self.assertEqual(self.run_mode('get'), 'suspended')
        self.assertIn('Still SUSPENDED', self.say('interactive'))
        self.say('resume the billing work')
        self.assertEqual(self.run_mode('get'), 'suspended')
        self.say('resume')
        self.assertEqual(self.run_mode('get'), 'autonomous')
        self.assertIn('Not suspended', self.say('resume'))


if __name__ == '__main__':
    unittest.main()
