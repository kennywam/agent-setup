"""Observe Herdr executions; never write authoritative task state."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from . import events, store

PROBE_INTERVAL = 120
HEARTBEAT_INTERVAL = 900
RENEW_INTERVAL = 120
DISCONNECT_INTERVALS = 5


def hc_call(*args):
    try:
        result = subprocess.run([os.environ.get('AGENT_BUS_HC', 'hc'), *args, '--json'],
                                capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def hc_get(terminal):
    result = hc_call('get', terminal)
    return result if isinstance(result, dict) else None


def execution_id_for(terminal, machine_id='mac-mini'):
    info = hc_get(terminal)
    if info is None or info.get('created_ms') is None:
        return None
    return f'{machine_id}/{terminal}@{info["created_ms"]}'


def upsert_agent(conn, **row):
    if not row.get('execution_id') or set(row) - set(store.AGENT_COLUMNS):
        raise ValueError('invalid agent columns or missing execution_id')
    if row.get('state') == 'idle':
        row['hook_at'] = None
    for key in ('last_activity',):
        if row.get(key) is not None:
            row[key] = events.redact(row[key], 200)
    columns = list(row)
    updates = [c + '=excluded.' + c for c in columns if c != 'execution_id']
    conn.execute('INSERT INTO agents (' + ','.join(columns) + ') VALUES ('
                 + ','.join('?' for _ in columns) + ') ON CONFLICT(execution_id) '
                 + ('DO UPDATE SET ' + ','.join(updates) if updates else 'DO NOTHING'), list(row.values()))


def get_agent(conn, execution_id):
    row = conn.execute('SELECT * FROM agents WHERE execution_id=?', (execution_id,)).fetchone()
    return dict(row) if row else None


def live_executions(conn, *, project_id=None, task_id=None):
    clauses, values = ["coalesce(state,'') NOT IN ('exited','disconnected')"], []
    for key, value in [('project_id', project_id), ('task_id', task_id)]:
        if value is not None:
            clauses.append(key + '=?')
            values.append(value)
    return [dict(r) for r in conn.execute('SELECT * FROM agents WHERE ' + ' AND '.join(clauses), values)]


def seconds(timestamp):
    if not timestamp:
        return 0
    return datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()


def registries(conn):
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2]).parent
    assignments = {}
    for file in path.glob('*.tasks.json'):
        try:
            tasks = json.loads(file.read_text())['tasks']
            for task in tasks.values():
                for execution in task.get('executions', []):
                    assignments[execution['id']] = (file.name[:-11], task, execution, tasks)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return path, assignments


def correlation(project, task, tasks):
    seen = set()
    while task.get('parent') and task['parent'] in tasks and task['parent'] not in seen:
        seen.add(task['id'])
        task = tasks[task['parent']]
    return f'{project}:{task["id"]}'


def board_blocker(path, row, cache):
    project = row.get('project_id')
    if not project:
        return None
    if project not in cache:
        posts = cache[project] = []
        try:
            for line in (path / (project + '.jsonl')).read_text().splitlines():
                try:
                    post = json.loads(line)
                    if isinstance(post, dict):
                        posts.append(post)
                except ValueError:
                    continue
        except OSError:
            pass
    posts = cache[project]
    own = [p for p in posts if p.get('exec') == row['execution_id']]
    if not own or own[-1].get('kind') not in ('blocker', 'question', 'ask-user'):
        return None
    last = own[-1]
    if any(p.get('kind') == 'answer' and str(p.get('re')) == str(last['id']) for p in posts):
        return None
    return last


def notify_ended(row):
    try:
        result = subprocess.run(['agent-task', '_exec-ended', row['task_id'], '--project', row['project_id'],
                                 '--exec', row['execution_id'], '--reason', row['end_reason']],
                                capture_output=True, timeout=10)
        if result.returncode != 0:
            events.log_error(RuntimeError(f'_exec-ended {row["execution_id"]} returned {result.returncode}; retry pending'))
        return result.returncode == 0
    except FileNotFoundError:
        return True
    except (OSError, subprocess.SubprocessError) as error:
        events.log_error(RuntimeError(f'_exec-ended {row["execution_id"]}: {error}; retry pending'))
        return False


def probe(conn):
    observed = hc_call('ls', '--all')
    terminals = observed.get('terminals') if isinstance(observed, dict) else observed
    if terminals is not None and not isinstance(terminals, list):
        terminals = None
    known = {r['execution_id']: dict(r) for r in conn.execute("SELECT * FROM agents WHERE coalesce(state,'') != 'exited' OR callback_pending='1'")}
    listed = {t['address'] for t in terminals or [] if isinstance(t, dict) and isinstance(t.get('address'), str)}
    details = {}
    for terminal in terminals or []:
        try:
            if terminal.get('state') == 'live' and terminal.get('agent') in ('claude-code', 'codex'):
                name = terminal['address']
                details[name] = hc_get(name)
                info = details[name]
                if info and info.get('created_ms') is not None and (info.get('status') or {}).get('app'):
                    xid = f'mac-mini/{name}@{info["created_ms"]}'
                    known.setdefault(xid, {'execution_id': xid, 'agent_id': f'mac-mini/{name}',
                                          'machine_id': 'mac-mini', 'role': 'worker', 'state': None})
        except Exception as error:
            events.log_error(error)
    path, assignments = registries(conn)
    for xid, (project, task, execution, tasks) in assignments.items():
        if xid and not execution.get('ended') and get_agent(conn, xid) is None:
            known.setdefault(xid, dict(execution_id=xid, agent_id=execution.get('agent_id') or xid.split('@')[0],
                                       machine_id=xid.split('/')[0], role=execution.get('role', 'worker'), state=None))
    # An empty enumeration during a restart cannot verify that the fleet vanished.
    if terminals == [] and any(r.get('role') != 'coordinator' and r.get('state') != 'exited'
                               for r in known.values()):
        terminals = None
    for row in known.values():
        if row.get('role') != 'coordinator' and row.get('state') != 'exited':
            terminal = row['agent_id'].split('/')[-1]
            if terminal not in details:
                details[terminal] = hc_get(terminal) if terminal in listed else None
    boards = {}
    emitted = []
    now, epoch = events.now_utc(), time.time()
    with conn:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        for xid, row in known.items():
            conn.execute('SAVEPOINT probe_row')
            event_start = len(emitted)
            try:
                row = get_agent(conn, xid) or row
                previous = row.get('state')
                context = assignments.get(xid)
                task = execution = None
                if context:
                    project, task, execution, tasks = context
                    row.update(project_id=project, task_id=task['id'], coordinator_id=task.get('coordinator'),
                               role=execution.get('role', 'worker'), provider=execution.get('provider'))
                if row.get('role') == 'coordinator':
                    row['state'] = ('stale' if epoch - seconds(row.get('last_heartbeat_at')) > 2 * RENEW_INTERVAL else 'idle')
                    upsert_agent(conn, **row)
                    continue
                terminal = row['agent_id'].split('/')[-1]
                info = details.get(terminal)
                if info is not None:
                    if not isinstance(info.get('created_ms'), int):
                        raise ValueError('hc created_ms must be an integer')
                    for field, expected in [('pid', int), ('turn', (int, str)), ('status', dict)]:
                        if info.get(field) is not None and not isinstance(info[field], expected):
                            raise ValueError(f'invalid hc {field}')
                status = (info.get('status') or {}) if info else {}
                absent = terminals is not None and (terminal not in listed or
                        (info is not None and str(info.get('created_ms')) != xid.rsplit('@', 1)[-1]))
                counters = json.loads(row.get('counters') or '{}')
                counters['missing_probes'] = counters.get('missing_probes', 0) + 1 if absent else 0
                gone = counters['missing_probes'] >= 2
                if absent:
                    info, status = None, {}


                def record(kind, metadata):
                    counters[kind] = counters.get(kind, 0) + 1
                    key = (f'hb:{xid}:{int(epoch // HEARTBEAT_INTERVAL)}' if kind == 'agent.heartbeat'
                           else f'agent:{xid}:{kind}:{counters[kind]}')
                    event = events.build_event(kind, project_id=row.get('project_id'), task_id=row.get('task_id'),
                        parent_task_id=task.get('parent') or None if task else None,
                        correlation_id=correlation(project, task, tasks) if context else None,
                        coordinator_id=row.get('coordinator_id'), agent_id=row['agent_id'], provider=row.get('provider'),
                        machine_id=row.get('machine_id', 'mac-mini'), execution_id=xid,
                        session_id=row.get('session_id'), source='probe', status=row['state'],
                        summary=f'{row["agent_id"]}: {kind.split(".")[-1]}', metadata=metadata, natural_key=key)
                    result = store.insert_event(conn, event)
                    if result:
                        emitted.append(result)

                reason = None
                blocker = None
                if row.get('state') == 'exited':
                    pass
                elif gone:
                    reason = 'terminal-gone'
                elif info is None:
                    unverified_since = row.get('verified_at') or counters.setdefault('unverified_since', now)
                    age = epoch - seconds(unverified_since)
                    if age > DISCONNECT_INTERVALS * PROBE_INTERVAL:
                        reason = 'disconnected-timeout'
                    elif terminals is None or absent:
                        if age > 2 * PROBE_INTERVAL:
                            row['state'] = 'stale'
                    else:
                        row['state'] = 'disconnected'
                elif info.get('state') == 'finished' or (info.get('state') == 'live' and not status.get('app')):
                    reason = 'cli-exited' if info.get('state') == 'live' else str(info.get('reason') or 'exit:unknown')
                    row.update(state='exited', ended_at=now, end_reason=reason, verified_at=now)
                else:
                    row['provider'] = {'claude-code': 'claude', 'codex': 'codex'}.get(status.get('app'))
                    row['started_at'] = row.get('started_at') or datetime.fromtimestamp(info['created_ms'] / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                    row['verified_at'] = now
                    counters.pop('unverified_since', None)
                    turn = str(info.get('turn'))
                    if row.get('turn') != turn:
                        row.update(turn=turn, turn_changed_at=now, last_activity_at=now)
                    row['last_activity'] = events.redact(status.get('msg') or '', 200)
                    blocker = board_blocker(path, row, boards)
                    state = status.get('state')
                    row['state'] = 'blocked' if blocker or state in ('blocked', 'waiting', 'waiting_for_input', 'input') else 'idle' if state in ('done', 'idle') else 'working'
                    if row['state'] != 'blocked' and epoch - seconds(row.get('hook_at')) < HEARTBEAT_INTERVAL:
                        row['state'] = 'working'
                    if row['state'] == 'working' and epoch - max(seconds(row.get('turn_changed_at')), seconds(row.get('hook_at'))) > 2700:
                        row['state'] = 'stale'
                    if not counters.get('agent.started'):
                        record('agent.started', dict(terminal=terminal, pid=info.get('pid'), started_ms=info['created_ms']))
                    if epoch - seconds(row.get('last_heartbeat_at')) >= HEARTBEAT_INTERVAL:
                        row['last_heartbeat_at'] = now
                        record('agent.heartbeat', {'agent_state': row['state']})
                if reason:
                    row.update(state='exited', ended_at=now, end_reason=reason)
                if row['state'] != previous:
                    kind = {'working': 'agent.busy', 'idle': 'agent.idle', 'blocked': 'agent.blocked',
                            'disconnected': 'agent.disconnected', 'exited': 'agent.exited'}.get(row['state'])
                    if kind:
                        if kind in ('agent.busy', 'agent.idle'):
                            meta = {'turn': info.get('turn')}
                        elif kind == 'agent.blocked' and blocker:
                            meta = {'reason': blocker.get('text', ''), 'board_id': blocker['id']}
                        elif kind in ('agent.blocked', 'agent.exited'):
                            meta = {'reason': reason or status.get('msg') or 'waiting for input'}
                        else:
                            meta = {'detail': 'hc get unavailable'}
                        record(kind, meta)
                if reason and context and execution.get('outcome') != 'done' and not execution.get('done_rev') and not execution.get('done_at') and not execution.get('ended'):
                    record('agent.failed', {'reason': reason, 'task_status': task.get('status')})
                    row['callback_pending'] = '1'
                row['counters'] = json.dumps(counters)
                upsert_agent(conn, **row)
            except Exception as error:
                conn.execute('ROLLBACK TO probe_row')
                del emitted[event_start:]
                events.log_error(RuntimeError(f'probe {xid}: {error}'))
            finally:
                conn.execute('RELEASE probe_row')
    # Do not hold the SQLite writer lock while T3 updates tasks and emits its own events.
    for row in conn.execute("SELECT * FROM agents WHERE callback_pending='1'").fetchall():
        row = dict(row)
        if notify_ended(row):
            with conn:
                upsert_agent(conn, execution_id=row['execution_id'], callback_pending=None)
    return emitted
