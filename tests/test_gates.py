"""Operating-mode gates and MacBook forwarding in agent-capacity, agent-hook and agent-msg."""
import json
import subprocess

from test_integration import ROOT, ToolFixture


class Gates(ToolFixture):
    def set_mode(self, text):
        mode = self.path / '.config' / 'coord' / 'mode'
        mode.parent.mkdir(parents=True, exist_ok=True)
        mode.write_text(text + '\n')
        return mode

    def capacity(self):
        # Not run_tool: the verdict depends on this machine's real pressure, so only the reasons are checked.
        result = subprocess.run([str(ROOT / 'bin' / 'agent-capacity'), '--json'], cwd=self.repo, env=self.env,
                                text=True, capture_output=True, timeout=60)
        return json.loads(result.stdout)

    def mode_reasons(self, d):
        return [r for r in d['reasons'] if r.startswith(('suspended', 'draining'))]

    def test_capacity_refuses_while_suspended_or_draining_and_fails_closed(self):
        self.assertEqual(self.mode_reasons(self.capacity()), [])
        self.set_mode('interactive')
        self.assertEqual(self.mode_reasons(self.capacity()), [])
        for mode in ('suspended interactive', 'draining autonomous'):
            self.set_mode(mode)
            d = self.capacity()
            self.assertFalse(d['allow'])
            self.assertEqual(len(self.mode_reasons(d)), 1, d['reasons'])
        self.set_mode('interactive').chmod(0)
        self.assertTrue(self.mode_reasons(self.capacity()))

    def test_capacity_counts_only_local_agents_not_viewers(self):
        self.hc['macbook/t9'] = dict(self.hc['t2'], address='macbook/t9')
        self.hc['t3'] = dict(self.hc['t2'], address='t3', argv=['bash'], foreground={'argv': ['/x/hc', 'attach', 't1']})
        self.hc['t4'] = dict(self.hc['t2'], address='t4', argv=['hc', 'attach', 't2'])
        self.write_hc()
        self.assertEqual(self.capacity()['metrics']['agents_live'], 2)

    def hook_out(self, mode, **extra):
        return self.run_tool('agent-hook', mode, terminal='t1', task='T1', extra=extra,
                             input=json.dumps(dict(cwd=str(self.repo)))).stdout

    def test_hook_tells_agents_to_stop_while_suspended_or_draining(self):
        self.new()
        self.assertEqual(self.hook_out('turn-start'), '')
        self.set_mode('suspended interactive')
        self.assertIn('[suspended]', self.hook_out('turn-start'))
        self.set_mode('draining interactive')
        self.assertIn('[going off]', self.hook_out('turn-start'))
        self.assertEqual(self.hook_out('turn-end'), '')

    def test_hook_ignores_coordinators(self):
        self.new()
        self.set_mode('suspended interactive')
        self.assertEqual(self.hook_out('turn-start', COORD_NAME='claude'), '')
        self.hook_out('turn-end', COORD_NAME='claude')
        self.assertNotIn('stopped', (self.bus / 'demo.jsonl').read_text())

    def test_forwarded_macbook_done_skips_verify_and_exec(self):
        (self.bus / 'demo.verify').write_text('false')
        self.msg('done', 'local', rc=2)
        self.run_tool('agent-msg', 'post', 'done', 'PR link', '--project', 'demo',
                      terminal='macbook/t9', extra=dict(AGENT_REMOTE='macbook'))
        post = json.loads((self.bus / 'demo.jsonl').read_text().splitlines()[-1])
        self.assertEqual((post['kind'], post['from'], post['exec']), ('done', 'macbook/t9', None))
