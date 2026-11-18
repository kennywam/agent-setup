from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from agentbus import events, store


def writer(directory, start):
    os.environ['AGENT_BUS_DIR'] = directory
    for number in range(start, start + 20):
        if events.emit('task.created', project_id='demo', source='test', natural_key=str(number)) is None:
            raise RuntimeError('write failed')


def interrupted(directory):
    conn = store.connect(directory)
    conn.execute('BEGIN IMMEDIATE')
    store.insert_event(conn, events.build_event('task.created', project_id='demo', source='test', natural_key='interrupted'))
    os._exit(0)


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, AGENT_BUS_DIR=self.temp.name, AGENT_BUS_HC='/nonexistent-hc')
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *args):
        return subprocess.run([str(ROOT / 'bin/agent-event'), *args], text=True, capture_output=True, check=True)

    def emit(self, key='one', **kwargs):
        return events.emit('task.created', project_id='demo', source='test', natural_key=key, **kwargs)

    def test_ids_dedup_replay_and_migration(self):
        first = self.emit()
        self.assertIsNone(self.emit())
        second = self.emit('two')
        self.assertGreater(second['seq'], first['seq'])
        self.assertEqual(events.event_id('one'), first['event_id'])
        self.assertNotEqual(events.event_id(None), events.event_id(None))
        with closing(store.connect()) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
            self.assertEqual(store.query_events(conn, after_seq=first['seq']), [second])
        self.assertEqual([json.loads(line) for line in self.cli('export', '--after', str(first['seq'])).stdout.splitlines()], [second])

    def test_redaction_validation_and_limits(self):
        secrets = ['sk-' + 'a' * 20, 'ghp_' + 'a' * 25, 'gho_' + 'a' * 25,
                   'xoxb-' + 'a' * 20, 'AKIA' + 'A' * 16,
                   'eyJ' + 'a' * 15 + '.' + 'b' * 15 + '.' + 'c' * 15,
                   'password=unsafe', 'SECRET:unsafe', 'api_key=unsafe', 'token=unsafe']
        for secret in secrets:
            self.assertEqual(events.redact(secret, 300), '[redacted]')
        result = self.emit(summary='line\n' + secrets[0] + ' ' + 'x' * 400,
                           metadata={'title': secrets[1], 'files': [secrets[2]], 'deps': []})
        self.assertEqual(len(result['summary']), 300)
        self.assertNotIn('\n', result['summary'])
        self.assertEqual(result['metadata']['files'], ['[redacted]'])
        idle = events.emit('agent.idle', project_id='demo', source='test', metadata={'last_words': 'a' * 500})
        self.assertEqual(len(idle['metadata']['last_words']), 200)
        self.assertIsNone(events.emit('unknown', project_id='demo', source='test'))
        self.assertIsNone(self.emit('bad', metadata={'credentials': 'no'}))
        self.assertIsNone(self.emit('bad-type', metadata={'files': 'not-a-list'}))
        self.assertIsNone(self.emit('bad-time', timestamp='yesterday'))
        self.assertTrue((Path(self.temp.name) / 'events-errors.log').exists())
        with patch('agentbus.store.connect', side_effect=OSError('token=bad')):
            self.assertIsNone(self.emit())
        self.assertNotIn('token=bad', (Path(self.temp.name) / 'events-errors.log').read_text())
        self.assertIsNotNone(events.emit('scheduling.decided', project_id='demo', source='agent-capacity',
                                        metadata={'class': 'light', 'metrics': {'cpu': 2}, 'reasons': []}))

    def test_concurrent_writers_and_interruption(self):
        # Includes simultaneous first-open migrations.
        workers = [multiprocessing.Process(target=writer, args=(self.temp.name, i * 20)) for i in range(4)]
        for process in workers:
            process.start()
        for process in workers:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        process = multiprocessing.Process(target=interrupted, args=(self.temp.name,))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        with closing(store.connect()) as conn:
            self.assertEqual(len(store.query_events(conn)), 80)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertIsNotNone(self.emit('after-crash'))

    def test_wal_switch_retries_concurrent_first_writer(self):
        path = Path(self.temp.name) / 'events.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE held (id)')
        ready, attempted = threading.Event(), threading.Event()
        original_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                try:
                    return super().execute(sql, *args)
                except sqlite3.OperationalError:
                    if sql == 'PRAGMA journal_mode=WAL':
                        attempted.set()
                    raise

        def writer():
            with closing(original_connect(path)) as conn:
                conn.execute('BEGIN IMMEDIATE')
                ready.set()
                attempted.wait(2)
                # Keep the first failure real; let a later retry switch to WAL.
                time.sleep(0.02)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            with patch('agentbus.store.sqlite3.connect',
                       side_effect=lambda *a, **kw: original_connect(*a, **kw, factory=ObservedConnection)):
                with closing(store.connect()) as conn:
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertTrue(attempted.is_set())
        finally:
            attempted.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_wal_switch_still_raises_when_writer_keeps_lock(self):
        with closing(sqlite3.connect(Path(self.temp.name) / 'events.db')) as holder:
            holder.execute('CREATE TABLE held (id)')
            holder.commit()
            for begin in ('BEGIN IMMEDIATE', 'BEGIN'):
                with self.subTest(lock=begin):
                    holder.execute(begin)
                    holder.execute('SELECT * FROM held').fetchall()
                    start = time.monotonic()
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        store.connect()
                    self.assertLess(time.monotonic() - start, 0.6)
                    holder.rollback()

    def test_cli_timeline_task_and_correlation(self):
        for task in ('T1', 'T2'):
            self.cli('emit', 'task.created', '--project', 'demo', '--task', task, '--key', task,
                     '--meta', '{"title":"a task"}')
        # Correlation is provided by Python producers, not guessed from task ids.
        self.emit('root', task_id='T1', correlation_id='demo:T1')
        self.emit('child', task_id='T2', correlation_id='demo:T1')
        by_task = [json.loads(s) for s in self.cli('timeline', '--project', 'demo', '--task', 'T1', '--json').stdout.splitlines()]
        by_tree = [json.loads(s) for s in self.cli('timeline', '--correlation', 'demo:T1', '--json').stdout.splitlines()]
        self.assertEqual(len(by_task), 2)
        self.assertEqual({r['task_id'] for r in by_tree}, {'T1', 'T2'})
        self.assertTrue(self.cli('timeline', '--project', 'demo', '--task', 'T1').stdout)
        self.assertEqual(self.cli('export', '--after', str(by_tree[-1]['seq'])).stdout, '')

    def test_prune_protects_open_trees_and_unknown_projects(self):
        path = Path(self.temp.name)
        (path / 'demo.tasks.json').write_text(json.dumps({'tasks': {
            'T1': {'id': 'T1', 'status': 'completed'},
            'T2': {'id': 'T2', 'parent': 'T1', 'status': 'in_progress'},
            'T3': {'id': 'T3', 'status': 'completed'}}}))
        for key, corr in [('open', 'demo:T1'), ('closed', 'demo:T3'), ('unknown', None)]:
            self.emit(key, correlation_id=corr, timestamp='2000-01-01T00:00:00.000Z')
        events.emit('agent.started', project_id='unknown', source='test', timestamp='2000-01-01T00:00:00.000Z')
        self.cli('prune', '--days', '180')
        with closing(store.connect()) as conn:
            rows = store.query_events(conn)
            self.assertEqual({r['correlation_id'] for r in rows}, {'demo:T1', None})
            self.assertEqual(len(rows), 3)
        tasks = json.loads((path / 'demo.tasks.json').read_text())
        tasks['tasks']['T2']['status'] = 'completed'
        (path / 'demo.tasks.json').write_text(json.dumps(tasks))
        self.cli('prune', '--days', '180')
        with closing(store.connect()) as conn:
            self.assertEqual([r['project_id'] for r in store.query_events(conn)], ['unknown'])

    def test_installer_dry_run(self):
        home = Path(self.temp.name) / 'home'
        result = subprocess.run([str(ROOT / 'install.sh'), '--dry-run'], env=dict(os.environ, HOME=str(home)),
                                capture_output=True, text=True, check=True)
        self.assertIn(str(home / '.local/lib/agentbus/events.py'), result.stdout)
        self.assertIn(str(home / '.local/bin/agent-event'), result.stdout)
        self.assertFalse(home.exists())

    def test_review_connect_does_not_take_writer_lock_for_current_schema(self):
        with closing(store.connect()) as writer_conn:
            writer_conn.execute('BEGIN IMMEDIATE')
            reader = store.connect()
            try:
                self.assertEqual(store.query_events(reader), [])
            finally:
                reader.close()

    def test_review_prune_unassigned_events_by_age(self):
        old = events.emit('agent.heartbeat', project_id=None, source='probe', timestamp='2000-01-01T00:00:00.000Z')
        recent = events.emit('agent.heartbeat', project_id=None, source='probe')
        self.cli('prune')
        with closing(store.connect()) as conn:
            remaining = [e['event_id'] for e in store.query_events(conn)]
        self.assertNotIn(old['event_id'], remaining)
        self.assertIn(recent['event_id'], remaining)

    def test_review_timeline_project_required_and_limit_replay(self):
        result = subprocess.run([str(ROOT / 'bin/agent-event'), 'timeline', '--task', 'T1'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        for number in range(3):
            self.emit(str(number), task_id='T1')
        events.emit('task.created', project_id='other', task_id='T1', source='test')
        page = [json.loads(s) for s in self.cli('timeline', '--project', 'demo', '--task', 'T1', '--limit', '2', '--json').stdout.splitlines()]
        self.assertEqual(len(page), 2)
        rest = [json.loads(s) for s in self.cli('timeline', '--project', 'demo', '--task', 'T1', '--after', str(page[-1]['seq']), '--limit', '2', '--json').stdout.splitlines()]
        self.assertEqual(len(rest), 1)
        self.assertTrue(all(e['project_id'] == 'demo' for e in page + rest))

    def test_review_installer_lists_library_before_binaries(self):
        result = subprocess.run([str(ROOT / 'install.sh'), '--dry-run'], capture_output=True, text=True, check=True)
        destinations = [s.split(' -> ')[1] for s in result.stdout.splitlines()]
        kinds = ['lib' if '/lib/agentbus/' in p else 'bin' for p in destinations]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: k == 'bin'))
