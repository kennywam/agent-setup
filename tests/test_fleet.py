from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from agentbus import events, fleet, store


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class FleetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        fake = self.path / 'hc'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['AGENT_BUS_DIR'])
with (p / 'hc-calls').open('a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')
data = json.loads((p / 'hc.json').read_text())
if data.get('unavailable'): sys.exit(1)
if sys.argv[1] == 'ls':
 print(json.dumps([{'address': k, 'agent': (v.get('status') or {}).get('app') or 'bash', 'state': v['state'], 'status': (v.get('status') or {}).get('state')} for k, v in data.items()]))
else:
 value = data.get(sys.argv[2])
 if value is None or value.get('_get_error'): sys.exit(1)
 print(json.dumps(value))
''')
        fake.chmod(0o755)
        self.env = patch.dict(os.environ, AGENT_BUS_DIR=str(self.path), AGENT_BUS_HC=str(fake), PATH=str(self.path))
        # The fake uses env python3, so retain Python alone in the isolated PATH.
        (self.path / 'python3').symlink_to(sys.executable)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.info = dict(address='t1', state='live', status=dict(app='codex', state='working', msg='editing'),
                         created_ms=1794264763416, turn=1, pid=123)
        self.xid = 'mac-mini/t1@1794264763416'
        self.set_hc({'t1': self.info})

    def set_hc(self, data):
        (self.path / 'hc.json').write_text(json.dumps(data))

    def row(self):
        return fleet.get_agent(self.conn, self.xid)

    def update(self, **values):
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id=self.xid, **values)

    def test_shapes_start_idle_blocked_and_heartbeat(self):
        self.assertEqual(fleet.execution_id_for('t1'), self.xid)
        emitted = fleet.probe(self.conn)
        self.assertEqual({e['event_type'] for e in emitted}, {'agent.started', 'agent.busy', 'agent.heartbeat'})
        self.assertEqual(self.row()['provider'], 'codex')
        self.assertEqual(self.row()['state'], 'working')
        self.assertEqual(fleet.probe(self.conn), [])
        self.info['status']['state'] = 'done'
        self.set_hc({'t1': self.info})
        self.assertEqual(fleet.probe(self.conn)[0]['event_type'], 'agent.idle')
        self.info['status']['state'] = 'blocked'
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(len(fleet.live_executions(self.conn)), 1)
        self.assertTrue(all(c == 'ls --all --json' for c in (self.path / 'hc-calls').read_text().splitlines() if c.startswith('ls')))

    def test_cli_exited_and_finished(self):
        fleet.probe(self.conn)
        self.info['status'] = None
        self.set_hc({'t1': self.info})
        emitted = fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'exited')
        self.assertEqual(emitted[-1]['metadata']['reason'], 'cli-exited')
        self.assertEqual(fleet.live_executions(self.conn), [])
        self.assertEqual(fleet.probe(self.conn), [])
        self.info.update(created_ms=1794264763417, state='finished', reason='killed')
        self.update(state='working')
        self.info['created_ms'] = 1794264763416
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['end_reason'], 'killed')

    def test_disconnected_stale_and_hook_override(self):
        fleet.probe(self.conn)
        self.set_hc({'t1': dict(self.info, _get_error=True)})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'disconnected')
        self.update(verified_at=ago(300))
        self.set_hc({'unavailable': True})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'stale')
        self.update(turn_changed_at=ago(2800))
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'stale')
        self.update(hook_at=events.now_utc())
        self.info['status']['state'] = 'idle'
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'working')
        self.info['turn'] = 2
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['turn'], '2')

    def task(self, outcome=None, ended=None):
        (self.path / 'demo.tasks.json').write_text(json.dumps({'tasks': {
            'T1': {'id': 'T1', 'status': 'in_progress', 'coordinator': 'claude', 'executions': [
                {'id': self.xid, 'role': 'worker', 'provider': 'codex', 'outcome': outcome, 'ended': ended}]}}}))

    def test_assignment_board_blocker_and_answer(self):
        self.task()
        board = self.path / 'demo.jsonl'
        board.write_text(json.dumps({'id': 1, 'kind': 'question', 'exec': self.xid}) + '\n')
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(self.row()['task_id'], 'T1')
        with board.open('a') as file:
            file.write(json.dumps({'id': 2, 'kind': 'answer', 're': '1'}) + '\n')
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'working')

    def test_failed_callback_missing_done_and_retry(self):
        self.task()
        fleet.probe(self.conn)
        self.info.update(state='finished', reason='exit:1')
        self.set_hc({'t1': self.info})
        emitted = fleet.probe(self.conn)
        self.assertIn('agent.failed', [e['event_type'] for e in emitted])
        self.assertIsNone(self.row()['callback_pending'])  # Missing agent-task is a no-op.
        self.assertEqual(fleet.probe(self.conn), [])
        self.update(state='working', ended_at=None, end_reason=None)
        self.task(outcome='done', ended=events.now_utc())
        self.assertNotIn('agent.failed', [e['event_type'] for e in fleet.probe(self.conn)])
        self.update(state='working', ended_at=None, end_reason=None)
        self.task()
        data = json.loads((self.path / 'demo.tasks.json').read_text())
        data['tasks']['T1']['executions'][0]['done_rev'] = 'abc'
        (self.path / 'demo.tasks.json').write_text(json.dumps(data))
        self.assertNotIn('agent.failed', [e['event_type'] for e in fleet.probe(self.conn)])
        self.update(state='working', ended_at=None, end_reason=None)
        self.task()
        callback = self.path / 'agent-task'
        callback.write_text('#!/bin/sh\nexit 1\n')
        callback.chmod(0o755)
        fleet.probe(self.conn)
        self.assertEqual(self.row()['callback_pending'], '1')
        callback.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$AGENT_BUS_DIR/callback"\n')
        fleet.probe(self.conn)
        self.assertIsNone(self.row()['callback_pending'])
        self.assertEqual((self.path / 'callback').read_text().splitlines(),
                         ['_exec-ended', 'T1', '--project', 'demo', '--exec', self.xid, '--reason', 'exit:1'])

    def test_hook_registered_execution_still_gets_started(self):
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id=self.xid, agent_id='mac-mini/t1',
                               state='working', hook_at=events.now_utc())
        emitted = fleet.probe(self.conn)
        self.assertIn('agent.started', [e['event_type'] for e in emitted])
        self.assertNotIn('agent.started', [e['event_type'] for e in fleet.probe(self.conn)])

    def test_exit_before_first_probe_is_not_lost(self):
        self.task()
        self.info.update(state='finished', reason='exit:0')
        self.set_hc({'t1': self.info})
        emitted = fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'exited')
        self.assertEqual([e['event_type'] for e in emitted], ['agent.exited', 'agent.failed'])

    def test_coordinator_stale_partial_upsert_and_reused_terminal(self):
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id='macbook/claude', agent_id='macbook/claude',
                               role='coordinator', state='idle', last_heartbeat_at=ago(300))
        fleet.probe(self.conn)
        self.assertEqual(fleet.get_agent(self.conn, 'macbook/claude')['state'], 'stale')
        self.update(last_activity='token=unsafe' + 'x' * 300)
        self.assertEqual(self.row()['last_activity'], '[redacted]')
        self.info['created_ms'] += 1
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertNotEqual(self.row()['state'], 'exited')
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'exited')
        self.assertEqual(len(fleet.live_executions(self.conn)), 2)  # Coordinator and replacement.

    def test_review_coordinator_renew_shape_recovers_and_is_visible(self):
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id='macbook/claude', agent_id='macbook/claude',
                               machine_id='macbook', provider='claude', role='coordinator',
                               last_heartbeat_at=events.now_utc())
        self.assertIn('macbook/claude', [r['execution_id'] for r in fleet.live_executions(self.conn)])
        snapshot = subprocess.run([str(ROOT / 'bin/agent-event'), 'agents', '--json'],
                                  capture_output=True, text=True, check=True)
        self.assertIn('macbook/claude', [r['execution_id'] for r in json.loads(snapshot.stdout)])
        fleet.probe(self.conn)
        self.assertEqual(fleet.get_agent(self.conn, 'macbook/claude')['state'], 'idle')
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id='macbook/claude', last_heartbeat_at=ago(300))
        fleet.probe(self.conn)
        self.assertEqual(fleet.get_agent(self.conn, 'macbook/claude')['state'], 'stale')
        with self.conn:
            fleet.upsert_agent(self.conn, execution_id='macbook/claude', last_heartbeat_at=events.now_utc())
        fleet.probe(self.conn)
        self.assertEqual(fleet.get_agent(self.conn, 'macbook/claude')['state'], 'idle')

    def test_review_turn_end_clears_freshness(self):
        fleet.probe(self.conn)
        self.update(state='working', hook_at=events.now_utc())
        self.update(state='idle', last_activity_at=events.now_utc())
        self.assertIsNone(self.row()['hook_at'])
        self.info['status']['state'] = 'done'
        self.set_hc({'t1': self.info})
        emitted = fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'idle')
        self.assertNotIn('agent.busy', [e['event_type'] for e in emitted])

    def test_review_gone_and_reused_terminal_end_task_execution(self):
        for reused in (False, True):
            with self.subTest(reused=reused):
                self.task()
                self.set_hc({'t1': self.info})
                fleet.probe(self.conn)
                self.update(state='working', ended_at=None, end_reason=None)
                replacement = dict(self.info, created_ms=self.info['created_ms'] + 1)
                self.set_hc({'t1': replacement} if reused else {'t2': dict(self.info, address='t2')})
                self.assertNotEqual(self.row()['state'], 'exited')
                fleet.probe(self.conn)
                self.assertNotEqual(self.row()['state'], 'exited')
                with patch('agentbus.fleet.notify_ended', return_value=True) as callback:
                    emitted = fleet.probe(self.conn)
                self.assertEqual(self.row()['state'], 'exited')
                self.assertEqual(self.row()['end_reason'], 'terminal-gone')
                self.assertIn('agent.failed', [e['event_type'] for e in emitted])
                callback.assert_called_once()

    def test_review_transient_ls_error_stale_and_timeout(self):
        self.task()
        fleet.probe(self.conn)
        self.set_hc({'unavailable': True})
        with patch('agentbus.fleet.hc_call', return_value={}):
            self.assertEqual(fleet.probe(self.conn), [])
        self.assertEqual(fleet.probe(self.conn), [])
        self.assertEqual(self.row()['state'], 'working')
        self.update(verified_at=ago(300))
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'stale')
        self.update(verified_at=ago(700))
        with patch('agentbus.fleet.notify_ended', return_value=True) as callback:
            emitted = fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'exited')
        self.assertEqual(self.row()['end_reason'], 'disconnected-timeout')
        self.assertIn('agent.failed', [e['event_type'] for e in emitted])
        callback.assert_called_once()

    def test_review_get_error_disconnects_and_ages_out(self):
        fleet.probe(self.conn)
        self.info['_get_error'] = True
        self.set_hc({'t1': self.info})
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'disconnected')
        self.update(verified_at=ago(700))
        fleet.probe(self.conn)
        self.assertEqual(self.row()['state'], 'exited')

    def test_review_bad_terminal_does_not_rollback_good_terminal(self):
        for field, bad in [('pid', []), ('turn', {}), ('created_ms', 'invalid')]:
            with self.subTest(field=field):
                with self.conn:
                    self.conn.execute('DELETE FROM agents')
                    self.conn.execute('DELETE FROM events')
                self.set_hc({'t1': dict(self.info, **{field: bad}),
                             't2': dict(self.info, address='t2', created_ms=1234)})
                emitted = fleet.probe(self.conn)
                good = fleet.get_agent(self.conn, 'mac-mini/t2@1234')
                self.assertEqual(good['state'], 'working')
                self.assertTrue(all(e['agent_id'] != 'mac-mini/t1' for e in emitted))
                self.assertTrue(all(e['agent_id'] != 'mac-mini/t1' for e in store.query_events(self.conn)))
                self.assertTrue((self.path / 'events-errors.log').exists())

    def test_review_unknown_exit_code(self):
        self.task()
        self.info.update(state='finished')
        self.set_hc({'t1': self.info})
        emitted = fleet.probe(self.conn)
        self.assertEqual(self.row()['end_reason'], 'exit:unknown')
        self.assertEqual(emitted[0]['metadata']['reason'], 'exit:unknown')

    def test_review_board_reason_and_one_read_per_project(self):
        self.task()
        data = json.loads((self.path / 'demo.tasks.json').read_text())
        data['tasks']['T1']['executions'].append({'id': 'mac-mini/t2@1234', 'role': 'reviewer'})
        (self.path / 'demo.tasks.json').write_text(json.dumps(data))
        board = self.path / 'demo.jsonl'
        board.write_text(json.dumps({'id': 9, 'kind': 'blocker', 'exec': self.xid, 'text': 'token=unsafe'}) + '\n')
        self.set_hc({'t1': self.info, 't2': dict(self.info, created_ms=1234)})
        reads = []
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == board:
                reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', autospec=True, side_effect=read):
            emitted = fleet.probe(self.conn)
        self.assertEqual(len(reads), 1)
        blocked = next(e for e in emitted if e['event_type'] == 'agent.blocked')
        self.assertEqual(blocked['metadata'], {'reason': '[redacted]', 'board_id': 9})

    def test_review_callback_failure_is_logged(self):
        row = dict(task_id='T1', project_id='demo', execution_id=self.xid, end_reason='terminal-gone')
        with patch('agentbus.fleet.subprocess.run', return_value=subprocess.CompletedProcess([], 7)):
            self.assertFalse(fleet.notify_ended(row))
        log = (self.path / 'events-errors.log').read_text()
        self.assertIn('_exec-ended', log)
        self.assertIn('7', log)

    def test_review_malformed_existing_terminal_is_not_treated_as_reused(self):
        self.task()
        fleet.probe(self.conn)
        for field, bad in [('created_ms', 'invalid'), ('pid', []), ('turn', {})]:
            with self.subTest(field=field):
                self.set_hc({'t1': dict(self.info, **{field: bad})})
                with patch('agentbus.fleet.notify_ended', return_value=True) as callback:
                    emitted = fleet.probe(self.conn)
                self.assertEqual(self.row()['state'], 'working')
                self.assertEqual(emitted, [])
                callback.assert_not_called()

    def test_empty_and_partial_ls_require_verified_consecutive_absence(self):
        self.task()
        fleet.probe(self.conn)
        with patch('agentbus.fleet.notify_ended', return_value=True) as callback:
            self.set_hc({})
            for _ in range(3):
                fleet.probe(self.conn)
                self.assertEqual(self.row()['state'], 'working')
            partial = {'t2': dict(self.info, address='t2')}
            self.set_hc(partial)
            fleet.probe(self.conn)
            self.assertEqual(self.row()['state'], 'working')
            # An empty response interrupts the consecutive verified absences.
            self.set_hc({})
            fleet.probe(self.conn)
            self.set_hc(partial)
            fleet.probe(self.conn)
            self.assertEqual(self.row()['state'], 'working')
            self.set_hc({'t1': self.info})
            fleet.probe(self.conn)
            self.set_hc(partial)
            fleet.probe(self.conn)
            self.assertEqual(self.row()['state'], 'working')
            callback.assert_not_called()
            fleet.probe(self.conn)
            self.assertEqual(self.row()['end_reason'], 'terminal-gone')
            callback.assert_called_once()
