"""SQLite audit trail. Task registries remain authoritative."""
import json
import os
from pathlib import Path
import sqlite3
import time

EVENT_COLUMNS = ('event_id schema_version event_type timestamp project_id task_id parent_task_id '
                 'correlation_id coordinator_id agent_id provider machine_id execution_id session_id '
                 'status summary metadata source').split()
AGENT_COLUMNS = ('execution_id agent_id provider role machine_id project_id task_id coordinator_id '
                 'session_id state started_at last_heartbeat_at last_activity_at last_activity '
                 'ended_at end_reason verified_at turn turn_changed_at counters hook_at callback_pending').split()


def bus_path(bus_dir=None):
    return Path(bus_dir or os.environ.get('AGENT_BUS_DIR', Path.home() / '.agent-bus'))


def migration_1(conn):
    conn.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                 + ', '.join(c + (' TEXT UNIQUE NOT NULL' if c == 'event_id' else
                                 ' INTEGER' if c == 'schema_version' else ' TEXT') for c in EVENT_COLUMNS) + ')')
    for name, columns in [('task', 'project_id, task_id, seq'), ('correlation', 'correlation_id, seq'),
                          ('execution', 'execution_id, seq'), ('agent', 'agent_id, seq'), ('time', 'timestamp')]:
        conn.execute(f'CREATE INDEX events_{name} ON events ({columns})')
    conn.execute('CREATE TABLE agents (' + ', '.join(c + (' TEXT PRIMARY KEY' if c == 'execution_id'
                                                         else ' TEXT') for c in AGENT_COLUMNS) + ')')


def connect(bus_dir=None):
    path = bus_path(bus_dir)
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / 'events.db', timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=0')
        # SQLite can reject the WAL switch immediately despite busy_timeout.
        for delay in (0.01, 0.02, 0.04, 0.08, 0.1, None):
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                break
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or delay is None:
                    raise
                time.sleep(delay)
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            raise ValueError(f'unsupported storage version {version}')
        if version < 1:
            conn.execute('BEGIN IMMEDIATE')
            # Another process may have migrated while this connection waited.
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError(f'unsupported storage version {version}')
            if version == 0:
                migration_1(conn)
                conn.execute('PRAGMA user_version=1')
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def event_row(row):
    result = dict(row)
    result['metadata'] = json.loads(result['metadata'])
    return result


def insert_event(conn, event):
    values = dict(event, metadata=json.dumps(event['metadata'], separators=(',', ':')))
    cur = conn.execute('INSERT OR IGNORE INTO events (' + ','.join(EVENT_COLUMNS) + ') VALUES ('
                       + ','.join('?' for _ in EVENT_COLUMNS) + ')', [values[c] for c in EVENT_COLUMNS])
    if not cur.rowcount:
        return None
    return event_row(conn.execute('SELECT * FROM events WHERE seq=?', (cur.lastrowid,)).fetchone())


def query_events(conn, *, project_id=None, task_id=None, correlation_id=None, agent_id=None,
                 execution_id=None, after_seq=0, since=None, until=None, limit=1000):
    clauses, values = ['seq > ?'], [after_seq]
    for key, value in [('project_id', project_id), ('task_id', task_id), ('correlation_id', correlation_id),
                       ('agent_id', agent_id), ('execution_id', execution_id)]:
        if value is not None:
            clauses.append(key + ' = ?')
            values.append(value)
    for operator, value in [('>=', since), ('<=', until)]:
        if value is not None:
            clauses.append('timestamp ' + operator + ' ?')
            values.append(value)
    values.append(limit)
    return [event_row(r) for r in conn.execute('SELECT * FROM events WHERE ' + ' AND '.join(clauses)
                                              + ' ORDER BY seq LIMIT ?', values)]
