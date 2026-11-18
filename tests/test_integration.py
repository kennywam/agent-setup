"""Exercise the shipped tools together; all state and Herdr traffic are disposable."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ZSH = os.environ.get('AGENT_BUS_TEST_ZSH') or shutil.which('zsh')

FAKE_HC = '''#!/usr/bin/python3 -S
import json, os, pathlib, re, sys, time
bus = pathlib.Path(os.environ['AGENT_BUS_DIR'])
a = sys.argv[1:]
if a[0] == 'machine':
 if os.environ.get('WATCH_HOLD'):
  (bus / 'held-child').write_text(str(os.getpid()))
  time.sleep(5)
 counter = bus / 'machines'
 n = int(counter.read_text()) + 1 if counter.exists() else 1
 counter.write_text(str(n))
 print(json.dumps({'machines':[{'name':'mac-mini','state':'connected' if n == 1 else 'disconnected'}]}))
elif a[0] == 'exec':
 cmd = a[-1]
 if 'agent-msg read' in cmd:
  after = int(re.search(r'--after (\\d+)', cmd)[1])
  for line in (bus / 'demo.jsonl').read_text().splitlines():
   if json.loads(line)['id'] > after: print(line)
 elif 'cat ' in cmd:
  print((bus / 'demo.tasks.json').read_text())
 elif 'renew' in cmd:
  (bus / 'renewed').touch()
  if os.environ.get('WATCH_WAIT'): time.sleep(5)
elif a[0] == 'read': print('fake terminal output')
else:
 if os.environ.get('HC_DELAY'): time.sleep(float(os.environ['HC_DELAY']))
 data = json.loads((bus / 'hc.json').read_text())
 if a[0] == 'ls':
  print(json.dumps([{'address': k, 'agent': (v.get('status') or {}).get('app') or 'bash',
                    'state': v['state']} for k, v in data.items()]))
 else:
  value = data.get(a[1].split('/')[-1])
  if value is None: sys.exit(1)
  print(json.dumps(value))
'''


class ToolFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.bus = self.path / 'bus'
        self.bus.mkdir()
        self.repo = self.path / 'demo'
        self.repo.mkdir()
        self.fake = self.path / 'hc'
        (self.bus / 'hc.py').write_text(FAKE_HC)
        self.fake.write_text('''#!/usr/bin/env bash
if [[ $1 == get ]]; then
  printf '%s\\n' "$*" >> "$AGENT_BUS_DIR/hc-calls"
  [[ -z ${HC_DELAY:-} ]] || sleep "$HC_DELAY"
  exec jq -e --arg terminal "${2##*/}" '.[$terminal] // empty' "$AGENT_BUS_DIR/hc.json"
fi
exec python3 -S "$AGENT_BUS_DIR/hc.py" "$@"
''')
        self.fake.chmod(0o755)
        self.env = dict(os.environ, AGENT_BUS_DIR=str(self.bus), AGENT_BUS_HC=str(self.fake),
                        AGENT_TASK='', HC_TERMINAL_ID='', HOME=str(self.path),
                        PATH=str(ROOT / 'bin') + os.pathsep + os.environ['PATH'])
        self.env.pop('AGENT_TASK_TTL_MIN', None)
        self.hc = {t: dict(address=t, created_ms=n, pid=42, turn=1, state='live',
                          status=dict(app='codex' if t == 't1' else 'claude-code', state='working', msg='editing'))
                   for t, n in [('t1', 100), ('t2', 200)]}
        self.write_hc()
        self.git('init', '-q', '-b', 'dev')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.commit('base')
        self.git('checkout', '-qb', 'feat/integration')
        (self.bus / 'demo.jsonl').touch()

    def write_hc(self):
        (self.bus / 'hc.json').write_text(json.dumps(self.hc))

    def run_tool(self, tool, *args, terminal='', task='', rc=0, extra=None, input=None):
        env = dict(self.env, HC_TERMINAL_ID=terminal, AGENT_TASK=task, **(extra or {}))
        result = subprocess.run([str(ROOT / 'bin' / tool), *args], cwd=self.repo, env=env,
                                input=input, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        return result

    def task(self, *args, rc=0):
        return self.run_tool('agent-task', *args, '--project', 'demo', rc=rc).stdout

    def msg(self, kind, text='post', *args, terminal='t1', task='T1', rc=0):
        return self.run_tool('agent-msg', 'post', kind, text, *args, '--project', 'demo',
                             terminal=terminal, task=task, rc=rc).stdout

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env, text=True).strip()

    def commit(self, name):
        (self.repo / name).write_text(name)
        self.git('add', name)
        self.git('commit', '-qm', 'test: ' + name)
        return self.git('rev-parse', 'HEAD')

    def state(self):
        return json.loads((self.bus / 'demo.tasks.json').read_text())['tasks']['T1']

    def events(self, kind=None):
        result = self.run_tool('agent-event', 'timeline', '--project', 'demo', '--task', 'T1', '--json')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        return [r for r in rows if kind is None or r['event_type'] == kind]

    def new(self):
        self.task('new', 'work', '--coord', 'claude')
        self.task('assign', 'T1', '--coord', 'claude', '--agent', 'codex', '--terminal', 't1')

    def verify(self, command='true', rc=0):
        (self.bus / 'demo.verify').write_text(command)
        return self.run_tool('agent-msg', 'verify', '--project', 'demo', terminal='t1', task='T1', rc=rc)



class Integration(ToolFixture):
    def test_lifecycle_reviews_findings_order_and_redaction(self):
        self.new()
        self.msg('plan', 'edit the feature')
        self.assertEqual(self.state()['status'], 'in_progress')
        revision = self.commit('first')
        self.verify()
        self.msg('done')
        before = self.state()
        count = len(self.events())
        self.msg('done')
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.events()), count)
        self.task('review-assign', 'T1', '--coord', 'claude', '--reviewer', 't1', rc=1)
        self.msg('review', 'author approval', '--verdict', 'approved')
        self.assertEqual(self.state()['gates']['review'], 'pending')
        self.task('review-assign', 'T1', '--coord', 'claude', '--reviewer', 't2')
        self.msg('review', 'needs a fix', '--verdict', 'changes_requested',
                 '--finding', 'major|feature.py:1|broken edge case|fix it',
                 '--finding', 'minor|feature.py:2|wording|clarify', terminal='t2')
        self.assertEqual(self.state()['status'], 'in_progress')
        self.assertEqual(len(self.state()['reviews'][0]['findings']), 2)
        self.assertEqual(self.events('review.changes_requested')[0]['metadata']['findings']['major'], 1)
        revision2 = self.commit('fix')
        self.verify()
        self.msg('done')
        self.task('review-assign', 'T1', '--coord', 'claude', '--reviewer', 't2')
        self.msg('review', 'looks good', '--verdict', 'approved', terminal='t2')
        self.task('gate', 'T1', 'integration', 'n/a', '--coord', 'claude', '--reason', 'no merge needed')
        self.task('status', 'T1', 'completed', '--coord', 'claude', rc=1)
        self.task('finding', 'T1', 'F1', 'accept', '--coord', 'claude', '--reason', 'accepted tradeoff')
        fake_token = 'sk-' + 'x' * 24
        self.msg('finding', 'tested ' + fake_token)
        self.task('status', 'T1', 'completed', '--coord', 'claude')
        rows = self.events()
        self.assertNotIn(fake_token, json.dumps(rows))
        self.assertIn('[redacted]', json.dumps(rows))
        types = [r['event_type'] for r in rows]
        ordered = ['task.assigned', 'task.started', 'context.shared', 'test.completed',
                   'commit.created', 'review.requested', 'review.changes_requested', 'review.completed', 'task.completed']
        cursor = -1
        for kind in ordered:
            cursor = types.index(kind, cursor + 1)
        self.assertEqual([r['seq'] for r in rows], sorted({r['seq'] for r in rows}))
        self.assertEqual([e['metadata']['sha'] for e in self.events('commit.created')], [revision, revision2])
        tests = self.events('test.completed')
        self.assertEqual(len(tests), 2)
        self.assertTrue(all(e['metadata']['verified'] for e in tests))

    def test_two_commits_one_done_and_repeated_done(self):
        self.new()
        first, second = self.commit('one'), self.commit('two')
        self.msg('done')
        commits = self.events('commit.created')
        self.assertEqual([e['metadata']['sha'] for e in commits], [first, second])
        all_events = self.events()
        self.msg('done')
        self.assertEqual(self.events('commit.created'), commits)
        self.assertEqual(self.events(), all_events)

    def test_done_after_handoff_only_records_new_commits(self):
        self.new()
        first = self.commit('first-worker')
        self.msg('done')
        self.task('assign', 'T1', '--coord', 'claude', '--agent', 'claude', '--terminal', 't2', '--replace')
        second = self.commit('second-worker')
        self.msg('done', terminal='t2')
        self.assertEqual([e['metadata']['sha'] for e in self.events('commit.created')], [first, second])

    def test_done_after_unresolved_execution_uses_recorded_task_revision(self):
        self.new()
        first = self.commit('before-hc-recovery')
        self.msg('done', 'identity unavailable', '--exec', '')
        second = self.commit('after-hc-recovery')
        self.msg('done')
        self.assertEqual([e['metadata']['sha'] for e in self.events('commit.created')], [second])
        self.assertEqual(self.state()['revisions'], [first, second])

    def test_legacy_review_and_failed_verify(self):
        self.new()
        self.commit('work')
        self.verify('false', rc=1)
        self.assertEqual(self.state()['gates']['tests'], 'failed')
        self.assertEqual(self.events('test.completed')[0]['metadata']['result'], 'failed')
        self.verify()
        self.msg('done')
        self.task('review-assign', 'T1', '--coord', 'claude', '--reviewer', 't2')
        self.msg('review', 'VERDICT: approve', terminal='t2')
        self.assertTrue(self.state()['reviews'][0]['parsed'])
        self.assertEqual(len(self.events('review.completed')), 1)

    def test_exit_without_done_is_blocked_and_in_attention(self):
        self.new()
        self.msg('plan')
        self.run_tool('agent-event', 'probe')
        self.hc['t1'].update(state='finished', reason='exit:1')
        self.write_hc()
        self.run_tool('agent-event', 'probe')
        task = self.state()
        self.assertEqual((task['status'], task['gates']['execution']), ('blocked', 'failed'))
        self.assertEqual(task['executions'][0]['outcome'], 'failed')
        self.assertIn('T1', self.task('attention', '--coord', 'claude'))
        self.assertEqual(len(self.events('agent.failed')), 1)

    def test_stale_execution_post_is_board_only(self):
        self.new()
        self.hc['t1']['created_ms'] = 101
        self.write_hc()
        self.task('assign', 'T1', '--coord', 'claude', '--agent', 'codex', '--terminal', 't1', '--replace')
        self.hc['t1']['created_ms'] = 100
        self.write_hc()
        before = self.state()
        self.commit('stale')
        self.assertIn('stale execution, ignored', self.msg('done'))
        self.assertEqual(self.events('commit.created'), [])
        self.assertEqual(self.state(), before)
        self.assertEqual(json.loads((self.bus / 'demo.jsonl').read_text().splitlines()[-1])['exec'], 'mac-mini/t1@100')

    def test_renew_persists_coordinator_registry_and_heartbeat(self):
        self.new()
        self.task('renew', '--coord', 'claude')
        row = self.agent_row('macbook/claude')
        self.assertEqual((row['agent_id'], row['provider'], row['role'], row['machine_id']),
                         ('macbook/claude', 'claude', 'coordinator', 'macbook'))
        self.assertTrue(row['last_heartbeat_at'])
        first = row['last_heartbeat_at']
        self.task('renew', '--coord', 'claude')
        self.assertGreaterEqual(self.agent_row('macbook/claude')['last_heartbeat_at'], first)

    def test_concurrent_coordinator_recovery(self):
        self.task('new', 'work', '--coord', 'claude')
        path = self.bus / 'demo.tasks.json'
        registry = json.loads(path.read_text())
        registry['tasks']['T1']['lease_until'] = 0
        path.write_text(json.dumps(registry))
        processes = [subprocess.Popen([str(ROOT / 'bin/agent-task'), 'claim', 'T1', '--project', 'demo',
                                      '--coord', c, '--recover', '--reason', 'expired'],
                                     cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for c in ['codex', 'other']]
        results = [p.communicate(timeout=10) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        self.assertIn(self.state()['coordinator'], ['codex', 'other'])

    def test_structured_review_validation_and_unavailable_identity(self):
        self.new()
        before = (self.bus / 'demo.jsonl').read_text()
        self.msg('review', 'bad verdict', '--verdict', 'approve', rc=2)
        self.msg('review', 'bad finding', '--finding', 'severe|x|broken|fix', rc=2)
        self.assertEqual((self.bus / 'demo.jsonl').read_text(), before)
        self.hc = {}
        self.write_hc()
        self.msg('notice', 'hc unavailable')
        post = json.loads((self.bus / 'demo.jsonl').read_text().splitlines()[-1])
        self.assertIn('exec', post)
        self.assertIsNone(post['exec'])

    def test_board_events_and_emit_failure_are_nonfatal(self):
        self.new()
        for kind in ('finding', 'decision', 'question', 'ask-user', 'blocker'):
            self.msg(kind)
        self.assertEqual(len(self.events('context.shared')), 1)
        self.assertEqual(len(self.events('decision.recorded')), 1)
        self.assertEqual(len(self.events('agent.blocked')), 3)
        (self.bus / 'events.db').unlink()
        (self.bus / 'events.db').mkdir()
        self.msg('finding', 'event store broken')
        self.assertIn('event store broken', (self.bus / 'demo.jsonl').read_text())

    def hook(self, mode, **extra):
        start = time.monotonic()
        result = self.run_tool('agent-hook', mode, terminal='t1', task='T1', extra=extra,
                              input=json.dumps(dict(cwd=str(self.repo), session_id='session-123',
                                                    last_assistant_message='last words')))
        self.assertLess(time.monotonic() - start, 0.3 if mode == 'turn-start' else 3.5)
        self.assertEqual((result.stdout, result.stderr), ('', ''))

    def agent_row(self, xid='mac-mini/t1@100'):
        with closing(sqlite3.connect(self.bus / 'events.db')) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute('SELECT * FROM agents WHERE execution_id=?', (xid,)).fetchone())

    def test_hooks_registry_first_turn_and_authoritative_idle(self):
        self.new()
        self.hook('turn-start')
        row = self.agent_row()
        self.assertEqual((row['state'], row['session_id']), ('working', 'session-123'))
        self.assertTrue(row['last_heartbeat_at'])
        self.hook('turn-start')
        self.assertEqual(len(self.events('delegation.accepted')), 1)
        self.msg('plan')
        self.hook('turn-end')
        self.assertEqual(self.agent_row()['state'], 'idle')
        self.assertIsNone(self.agent_row()['hook_at'])
        self.hc['t1']['status']['state'] = 'done'
        self.write_hc()
        busy_events = self.events('agent.busy')
        self.run_tool('agent-event', 'probe')
        self.assertEqual(self.agent_row()['state'], 'idle')
        self.assertEqual(self.events('agent.busy'), busy_events)
        self.assertIsNone(self.agent_row()['hook_at'])
        self.hook('turn-start')
        self.hook('turn-end')
        self.assertIn('stopped', (self.bus / 'demo.jsonl').read_text())

    def test_reviewer_hook_and_context_handoff_first_turn(self):
        self.new()
        self.msg('done')
        self.task('review-assign', 'T1', '--coord', 'claude', '--reviewer', 't2')
        for _ in range(2):
            self.run_tool('agent-hook', 'turn-start', terminal='t2', task='T1',
                          input=json.dumps(dict(cwd=str(self.repo), session_id='review-session')))
        self.assertEqual(len(self.events('review.started')), 1)
        self.assertEqual(self.events('review.started')[0]['metadata']['revision'], self.git('rev-parse', 'HEAD'))
        self.hc['t1']['created_ms'] = 101
        self.write_hc()
        self.task('assign', 'T1', '--coord', 'claude', '--agent', 'codex', '--terminal', 't1', '--replace')
        self.hook('turn-start')
        handoffs = self.events('handoff.completed')
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]['metadata']['kind'], 'context')

    def test_quiet_turn_end_survives_slow_hc_without_second_lookup(self):
        self.new()
        self.hook('turn-start')
        calls = self.bus / 'hc-calls'
        calls.write_text('')
        self.hook('turn-end', HC_DELAY='0.15')
        posts = [json.loads(line) for line in (self.bus / 'demo.jsonl').read_text().splitlines()]
        self.assertEqual(posts[-1]['kind'], 'stopped')
        self.assertEqual(posts[-1]['exec'], 'mac-mini/t1@100')
        self.assertEqual(len(calls.read_text().splitlines()), 1)

    def test_hook_fail_open_on_locked_sqlite_and_slow_hc(self):
        self.new()
        self.hook('turn-start')
        with closing(sqlite3.connect(self.bus / 'events.db')) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.hook('turn-start')
            self.hook('turn-end')
        posts = [json.loads(line) for line in (self.bus / 'demo.jsonl').read_text().splitlines()]
        self.assertEqual(posts[-1]['kind'], 'stopped')
        before = len(posts)
        self.hook('turn-start', HC_DELAY='2')
        self.hook('turn-end', HC_DELAY='2')
        posts = [json.loads(line) for line in (self.bus / 'demo.jsonl').read_text().splitlines()]
        self.assertGreater(len(posts), before)
        self.assertEqual(posts[-1]['kind'], 'stopped')
        self.assertIsNone(posts[-1]['exec'])
        self.run_tool('agent-hook', 'turn-start', terminal='t1', task='T1', input='{')


@unittest.skipUnless(ZSH, 'zsh is required for MacBook watcher integration tests')
class Watcher(ToolFixture):
    def watch(self, posts, task=None, coord='claude', terms=(), tasks=None):
        (self.bus / 'demo.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in posts))
        registry = tasks if tasks is not None else ({'T1': task} if task else {})
        (self.bus / 'demo.tasks.json').write_text(json.dumps({'tasks': registry}))
        (self.bus / 'machines').unlink(missing_ok=True)
        result = subprocess.run([ZSH, str(ROOT / 'macbook/coord-watch'), 'demo', *terms],
                                env=dict(self.env, COORD_NAME=coord), text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def post(self, execution='mac-mini/t1@100', **fields):
        return dict(id=1, kind='done', task='T1', **{'from': 't1'}, to='coordinator', text='done',
                    **{'exec': execution}, **fields)

    def assigned(self, **fields):
        return dict(id='T1', coordinator='claude', agent={'terminal': 't1'},
                    executions=[dict(id='mac-mini/t1@100', ended=None, role='worker'),
                                dict(id='mac-mini/t2@200', ended=None, role='reviewer')], **fields)

    def test_watch_owner_handoff_stale_and_printed_identity(self):
        current = self.post()
        self.assertIn('task=T1 exec=mac-mini/t1@100', self.watch([current], self.assigned()))
        cursor = self.path / '.local/state/coord/demo.cursor'
        self.assertEqual(cursor.read_text().strip(), '1')
        cursor.write_text('0')
        stale = self.post(execution='mac-mini/t1@99')
        stale_output = self.watch([stale], self.assigned())
        self.assertIn('BOARD', stale_output)
        self.assertIn('exec=mac-mini/t1@99 [stale]', stale_output)
        self.assertEqual(cursor.read_text().strip(), '1')
        self.assertNotIn('BOARD', self.watch([current], self.assigned(), coord='codex'))
        self.assertTrue((self.bus / 'renewed').exists())
        (self.path / '.local/state/coord/demo.codex.cursor').write_text('0')
        self.assertIn('BOARD', self.watch([current], self.assigned(handoff_to='codex'), coord='codex'))
        cursor.write_text('0')
        reviewer = self.post(execution='mac-mini/t2@200')
        reviewer['from'] = 't2'
        self.assertIn('BOARD', self.watch([reviewer], self.assigned()))

    def test_watch_taskless_posts_and_legacy_posts(self):
        post = self.post()
        del post['task']
        self.assertNotIn('BOARD', self.watch([post], self.assigned(), coord='codex'))
        cursor = self.path / '.local/state/coord/demo.cursor'
        self.assertIn('BOARD', self.watch([post], self.assigned()))
        cursor.write_text('0')
        post['exec'] = None
        self.assertIn('exec=-', self.watch([post], self.assigned()))

    def test_watch_prints_only_owned_hits_and_finished_over_working(self):
        unrelated = self.post()
        unrelated['task'] = 'T2'
        unrelated['text'] = 'other task hidden'
        current = self.post()
        current['id'] = 2
        output = self.watch([unrelated, current], tasks={
            'T1': self.assigned(), 'T2': dict(id='T2', coordinator='codex')})
        self.assertIn('BOARD', output)
        self.assertNotIn('other task hidden', output)
        self.hc['t1']['state'] = 'finished'
        self.write_hc()
        output = self.watch([], terms=('mac-mini/t1',))
        self.assertIn('state=finished', output)

    def test_watch_lock_error_is_not_reported_as_an_existing_watcher(self):
        shim = self.path / 'lock-error.zsh'
        shim.write_text('zsystem() { return 1; }\nsource "$@"\n')
        result = subprocess.run([ZSH, str(shim), str(ROOT / 'macbook/coord-watch'), 'demo'],
                                env=self.env, text=True, capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot lock cursor', result.stderr)
        self.assertNotIn('already running', result.stdout + result.stderr)

    def test_watch_lock_survives_cursor_update_without_external_flock(self):
        # Any call to the non-portable flock binary must fail this test.
        fake_flock = self.path / 'flock'
        fake_flock.write_text('#!/bin/sh\necho unexpected-flock >&2\nexit 127\n')
        fake_flock.chmod(0o755)
        env = dict(self.env, WATCH_WAIT='1', PATH=str(self.path) + os.pathsep + self.env['PATH'])
        post = self.post()
        post['kind'] = 'status'
        (self.bus / 'demo.jsonl').write_text(json.dumps(post) + '\n')
        command = [ZSH, str(ROOT / 'macbook/coord-watch'), 'demo']
        process = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 2
            while not (self.bus / 'renewed').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.bus / 'renewed').exists())
            self.assertEqual((self.path / '.local/state/coord/demo.cursor').read_text().strip(), '1')
            second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=2)
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertIn('already running', second.stdout)
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)

    def test_watch_lock_releases_when_parent_exits_with_live_child(self):
        command = [ZSH, str(ROOT / 'macbook/coord-watch'), 'demo']
        process = subprocess.Popen(command, env=dict(self.env, WATCH_HOLD='1'), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 2
            while not (self.bus / 'held-child').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.bus / 'held-child').exists())
            child = int((self.bus / 'held-child').read_text())
            process.kill()
            process.wait(timeout=2)
            os.kill(child, 0)  # The hc child still exists when its parent watcher is restarted.
            second = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=2)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertNotIn('already running', second.stdout)
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)

    def test_watch_lock_excludes_second_watcher_and_preserves_cursor(self):
        env = dict(self.env, WATCH_HOLD='1')
        command = [ZSH, str(ROOT / 'macbook/coord-watch'), 'demo']
        process = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            cursor = self.path / '.local/state/coord/demo.cursor'
            deadline = time.monotonic() + 2
            while not cursor.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            # The first process holds the flock before running fake hc.
            import fcntl
            while time.monotonic() < deadline:
                with cursor.open('a') as file:
                    try:
                        fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        break
                time.sleep(0.01)
            cursor.write_text('17\n')
            second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=2)
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertIn('already running for demo/claude', second.stdout)
            self.assertEqual(cursor.read_text(), '17\n')
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)
