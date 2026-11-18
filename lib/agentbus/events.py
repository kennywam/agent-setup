"""Validated, redacted events with retry-safe identifiers."""
from datetime import datetime, timezone
import json
import re
import uuid
from . import store

SCHEMA_VERSION = 1
NAMESPACE = uuid.UUID('3d0c6a5c-474f-56d3-910f-fc2cbe48ca51')  # fixed: changing it changes every event id
METADATA = {
    'agent.started': 'terminal pid started_ms', 'agent.heartbeat': 'agent_state',
    'agent.busy': 'turn', 'agent.idle': 'turn last_words', 'agent.blocked': 'reason board_id',
    'agent.disconnected': 'detail', 'agent.exited': 'reason', 'agent.failed': 'reason task_status',
    'scheduling.decided': 'class metrics reasons', 'task.created': 'title issue deps files',
    'task.assigned': 'provider model role', 'task.started': 'by', 'task.progress': 'gate value board_id',
    'task.blocked': 'reason', 'task.resumed': 'reason', 'delegation.created': 'role brief_rev',
    'delegation.accepted': 'role', 'decision.recorded': 'board_id', 'context.shared': 'board_id kind',
    'review.requested': 'reviewer author revision', 'review.started': 'revision',
    'review.completed': 'verdict revision findings', 'review.changes_requested': 'revision findings',
    'test.completed': 'result command revision verified', 'commit.created': 'sha branch subject',
    'pull_request.created': 'url number', 'integration.completed': 'target revision evidence',
}
for _kind in ('task.completed', 'task.failed', 'task.cancelled'):
    METADATA[_kind] = 'gates reason forced'
for _kind in ('handoff.created', 'handoff.completed'):
    METADATA[_kind] = 'kind from to brief_rev'
SECRET = re.compile(r'sk-[A-Za-z0-9_-]{16,}|gh[po]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|'
                    r'AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|'
                    r'(?i:(password|secret|token|api[_-]?key)\s*[=:]\s*\S+)')


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def event_id(natural_key):
    return str(uuid.uuid5(NAMESPACE, natural_key) if natural_key is not None else uuid.uuid4())


def redact(text, limit):
    return SECRET.sub('[redacted]', str(text))[:limit]


def clean(value, limit=300):
    if isinstance(value, str):
        return redact(value, limit)
    if isinstance(value, list):
        return [clean(v, limit) for v in value]
    if isinstance(value, dict):
        return {redact(k, 300): clean(v, limit) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError('metadata must contain JSON values')


def build_event(event_type, *, project_id, natural_key=None, task_id=None, parent_task_id=None,
                correlation_id=None, coordinator_id=None, agent_id=None, provider=None,
                machine_id='mac-mini', execution_id=None, session_id=None, status=None, summary='',
                metadata=None, source, timestamp=None):
    if event_type not in METADATA:
        raise ValueError('unknown event type')
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, dict) or set(metadata) - set(METADATA[event_type].split()):
        raise ValueError('unlisted metadata keys')
    for key, value in metadata.items():
        expected = (bool if key in ('forced', 'verified') else
                    list if key in ('deps', 'files', 'reasons') else
                    dict if key in ('metrics', 'gates', 'findings') else
                    int if key in ('pid', 'started_ms', 'number') else
                    (int, str) if key in ('turn', 'board_id', 'brief_rev') else
                    (str, int) if key == 'issue' else
                    (str, int, float, bool, dict, list) if key == 'value' else str)
        if value is not None and not isinstance(value, expected):
            raise ValueError(f'invalid metadata type for {key}')
    if timestamp is not None and not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', timestamp):
        raise ValueError('timestamp must be UTC ISO 8601 with milliseconds')
    if timestamp is not None:
        datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    result = dict(event_id=event_id(natural_key), schema_version=SCHEMA_VERSION, event_type=event_type,
                  timestamp=timestamp or now_utc(), project_id=project_id, task_id=task_id,
                  parent_task_id=parent_task_id, correlation_id=correlation_id, coordinator_id=coordinator_id,
                  agent_id=agent_id, provider=provider, machine_id=machine_id, execution_id=execution_id,
                  session_id=session_id, status=status, summary=redact(' '.join(summary.splitlines()), 300),
                  metadata={k: clean(v, 200 if k == 'last_words' else 300) for k, v in metadata.items()}, source=source)
    for key, value in result.items():
        if key not in ('metadata', 'schema_version') and value is not None and not isinstance(value, str):
            raise ValueError(f'{key} must be a string or null')
    json.dumps(result, allow_nan=False)
    return result


def log_error(error):
    try:
        store.bus_path().mkdir(parents=True, exist_ok=True)
        with (store.bus_path() / 'events-errors.log').open('a') as log:
            log.write(f'{now_utc()} {type(error).__name__}: {redact(str(error), 300)}\n')
    except Exception:
        pass


def emit(event_type, **kwargs):
    conn = None
    try:
        event = build_event(event_type, **kwargs)
        conn = store.connect()
        with conn:
            return store.insert_event(conn, event)
    except Exception as error:
        log_error(error)
        return None
    finally:
        if conn is not None:
            conn.close()
