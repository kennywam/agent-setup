# agent-bus specification (v2)

The one canonical spec for how Claude and Codex coordinators and workers coordinate, record state and
produce activity data. Policy (who does what) lives in the coordination rules (`docs/templates/coordination-rules.md`) ("Claude and
Codex"); this file defines the mechanics. Code: `~/projects/agent-bus` on the Mac mini (git, branch `dev`).

## 1. Components and sources of truth

| Data | Store | Authority | Writers |
|---|---|---|---|
| Task state | `~/.agent-bus/<project>.tasks.json` | **authoritative** for tasks | `agent-task` only |
| Conversation | `~/.agent-bus/<project>.jsonl` (the board) | authoritative for messages | `agent-msg` only |
| Events + agent registry | `~/.agent-bus/events.db` (SQLite, WAL) | derived audit trail and fleet view; never decides task state | `agentbus` library (via `agent-task`, `agent-msg`, `agent-hook`, `agent-event probe`) |

All three live on the Mac mini. MacBook coordinators reach them over `hc exec --on mac-mini`.
No other state stores. A dashboard or collector reads `events.db` read-only (section 9).

Code layout (`~/projects/agent-bus`):
- `lib/agentbus/` Python 3 stdlib-only package: `events.py` (schema, ids, redaction, emit),
  `store.py` (SQLite), `fleet.py` (agent registry + probe), `tasks.py` (task state machine).
- `bin/agent-event`, `bin/agent-task` (Python), `bin/agent-msg`, `bin/agent-hook`, `bin/agent-metrics`,
  `bin/server-turn`, `bin/e2e-worktree` (bash), `macbook/coord-watch` (zsh, runs on the MacBook).
- `systemd/agent-probe.service` + `.timer` (user units on the Mini), `install.sh`.
- `tests/`: `unittest` suites (`python3 -m unittest discover -s tests`) and `tests/agent-task-test` (bash).
Installed copies: `~/.local/bin/*`, `~/.local/lib/agentbus/`. Install is atomic per file (copy to a temp
name in the same directory, then `mv`), because agents run these tools while they are replaced.

## 2. Identifiers

| Id | Format | Example |
|---|---|---|
| `project_id` | board name (git common dir basename) | `project-a` |
| `task_id` | `T<n>` per project | `T5` |
| `correlation_id` | `<project>:<root task id>` (follow `parent` up to the root) | `project-b:T1` |
| `machine_id` | `mac-mini` or `macbook` | |
| `agent_id` | worker/reviewer: `<machine>/<terminal>`; coordinator: `macbook/<coord>` | `mac-mini/t62`, `macbook/codex` |
| `execution_id` | `<agent_id>@<terminal created_ms>` from `hc get` (one launch of a CLI in a terminal) | `mac-mini/t62@1794264763416` |
| `session_id` | provider session id from hook input (`session_id`), when known | |
| `coordinator_id` | `claude` or `codex` | |
| `provider` | `claude` or `codex` (from `hc get` `.status.app`: `claude-code` -> `claude`, `codex` -> `codex`) | |

Board messages gain an `exec` field: `agent-msg` resolves it once per post with `hc get $HC_TERMINAL_ID --json`
(`created_ms`). Without `HC_TERMINAL_ID` there is no execution (coordinator posts).

## 3. Event contract

Schema version 1. One JSON object per event:

```json
{
  "schema_version": 1,
  "event_id": "uuid (see below)",
  "seq": 1234,
  "event_type": "task.assigned",
  "timestamp": "2026-11-10T00:52:13.481Z",
  "project_id": "project-b",
  "task_id": "T5",
  "parent_task_id": "T1",
  "correlation_id": "project-b:T1",
  "coordinator_id": "codex",
  "agent_id": "mac-mini/t62",
  "provider": "codex",
  "machine_id": "mac-mini",
  "execution_id": "mac-mini/t62@1794264763416",
  "session_id": null,
  "status": "assigned",
  "summary": "T5 assigned to codex worker mac-mini/t62",
  "metadata": {},
  "source": "agent-task"
}
```

- `timestamp`: UTC, ISO 8601, millisecond precision, `Z` suffix. Always the time the fact was observed.
- `seq`: assigned by the store (SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`), strictly increasing in insert
  order. Order within an execution = order by `seq`. Consumers resume with "events after seq N".
- `event_id`: deterministic `uuid5(NAMESPACE, natural_key)` when the event has a natural key, so a repeated
  emit of the same fact is a no-op (`INSERT OR IGNORE`). Natural keys:
  - task transitions and task field events: `task:<project>:<task>:rev<rev>:<event_type>` (`rev` is the
    task's revision counter, section 4)
  - board-derived events: `board:<project>:<message id>:<event_type>`; `commit.created` appends
    `:<full sha>` so one done post can record multiple commits without deduplication loss.
  - agent state changes: `agent:<execution_id>:<event_type>:<n>` where `n` is that execution's
    per-type transition counter in the registry
  - heartbeats: `hb:<execution_id>:<floor(epoch/heartbeat_interval)>`
  Otherwise `uuid4`. `NAMESPACE` is the fixed UUID `3d0c6a5c-474f-56d3-910f-fc2cbe48ca51`.
- `status`: the subject's state after the event (task status, agent state or review verdict); null if n/a.
- `summary`: one line, at most 300 characters, written for a human timeline.
- `metadata`: typed per event type (below); only listed keys. Never credentials, environment variables,
  tokens, file contents or full prompts. `summary` and string metadata pass through `redact()`: replace
  matches of `(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|
  AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|
  (?i:(password|secret|token|api[_-]?key)\s*[=:]\s*\S+))` with `[redacted]`, and cut to the length limits.
- `source`: the component that observed it: `agent-task`, `agent-msg`, `agent-hook`, `probe`, `coord-watch`.

### Event types, when they are emitted, and their metadata

Only emit what the emitting component has verified itself. "Verified by" says how.

| event_type | Emitted by, when | Verified by | metadata |
|---|---|---|---|
| `agent.started` | probe, first time it sees an execution whose `.status.app` is set | `hc get` | `terminal`, `pid`, `started_ms` |
| `agent.heartbeat` | probe, at most once per 15 min per live execution | `hc get` live + app set | `agent_state` |
| `agent.busy` | agent-hook turn-start; probe when state changes to working | hook ran inside the agent / `hc get` | `turn` |
| `agent.idle` | agent-hook turn-end; probe when state changes to done/idle | same | `turn`, `last_words` (<=200 chars) |
| `agent.blocked` | agent-msg on `blocker`/`question`/`ask-user`; probe if Herdr reports blocked | board post / `hc get` | `reason`, `board_id` |
| `agent.disconnected` | probe: terminal not found, or the Mini was unreachable (coord-watch) | `hc get` error | `detail` |
| `agent.exited` | probe: terminal finished, or terminal live but `.status.app` null (CLI exited to a shell) | `hc get` | `reason` (`killed`, `exit:<code>`, `cli-exited`) |
| `scheduling.decided` | `agent-capacity --record`, when a coordinator checks capacity before starting an agent | /proc + `hc ls` | `class` (`light`/`heavy`), `metrics`, `reasons`; `status` `allow`/`defer` |
| `agent.failed` | probe: as exited, while its task execution had not posted `done` | `hc get` + registry | `reason`, `task_status` |
| `task.created` | agent-task new | registry write | `title`, `issue`, `deps`, `files` |
| `task.assigned` | agent-task assign | registry write | `provider`, `model`, `role` |
| `task.started` | agent-task, transition into `in_progress` the first time | registry write | `by` |
| `task.progress` | agent-task, any gate change; agent-msg `status` posts | registry write / board | `gate`, `value`, `board_id` |
| `task.blocked` / `task.resumed` | agent-task, into / out of `blocked` | registry write | `reason` |
| `task.completed` / `task.failed` / `task.cancelled` | agent-task terminal transitions | registry write | `gates`, `reason`, `forced` |
| `delegation.created` | agent-task assign (worker or reviewer) | registry write | `role`, `brief_rev` |
| `delegation.accepted` | agent-hook turn-start, first turn of an execution that has `AGENT_TASK` | hook in the agent | `role` |
| `handoff.created` / `handoff.completed` | agent-task: ownership handoff offered / accepted; recovery claim; context handoff (assign to a new execution when an earlier execution exists) created at assign and completed at `delegation.accepted` | registry write / hook | `kind` (`ownership`, `recovery`, `context`), `from`, `to`, `brief_rev` |
| `decision.recorded` | agent-msg `decision` post | board | `board_id` |
| `context.shared` | agent-msg `finding` / `plan` post | board | `board_id`, `kind` |
| `review.requested` | agent-task review-assign | registry write | `reviewer`, `author`, `revision` |
| `review.started` | agent-hook turn-start, first turn of a reviewer execution | hook | `revision` |
| `review.completed` | agent-msg `review` post with verdict `approved` or `blocked` | board + registry | `verdict`, `revision`, `findings` (count by severity) |
| `review.changes_requested` | agent-msg `review` post with verdict `changes_requested` | board + registry | `revision`, `findings` |
| `test.completed` | `agent-msg verify` (ran the checks: `verified:true`); agent-task gate tests (coordinator-recorded: `verified:false` unless `--evidence` names a check run) | ran it / registry | `result` (`passed`/`failed`), `command`, `revision`, `verified` |
| `commit.created` | agent-msg `done`, for each new commit on the execution's branch since the last recorded revision (`git rev-list`) | git in the worktree | `sha`, `branch`, `subject` |
| `pull_request.created` | agent-task set `--pr`, after `gh pr view <url>` succeeds | GitHub | `url`, `number` |
| `integration.completed` | agent-task gate integration done | registry write (coordinator) | `target`, `revision`, `evidence` |

## 4. Task lifecycle (registry v2)

Tasks created after the v2 upgrade have `"v": 2`. Tasks without `v` are legacy: they keep the v1 rules
(no gates), get `rev`, `history` and events from now on, and are listed as legacy by `agent-task ls`.

### States and transitions

States: `pending`, `assigned`, `in_progress`, `blocked`, `review`, `completed`, `failed`, `cancelled`.

| From | Allowed to |
|---|---|
| pending | assigned, in_progress (parent run by its coordinator), blocked, cancelled |
| assigned | in_progress, review, pending, blocked, failed, cancelled |
| in_progress | review, completed, blocked, failed, cancelled |
| review | in_progress, completed, blocked, failed, cancelled |
| blocked | pending, assigned, in_progress, review, failed, cancelled (each emits `task.resumed` except failed/cancelled) |
| failed | pending (retry) |
| completed, cancelled | none |

A transition to the current state is a no-op: no write, no event, exit 0 with "already <state>". This is
what makes repeated `done` posts and watcher restarts harmless.

Every write increments `rev` and appends to `history`:
`{"rev":7,"at":"<utc>","by":"<coordinator|agent_id|probe>","from":"in_progress","to":"review","exec":"<execution_id|null>","reason":"..."}`.

### Gates (v2 only)

`gates` separate the facts that "completed" used to blur:

| Gate | Values | Set by |
|---|---|---|
| `execution` | `pending`, `done`, `failed` | `done` post from the current execution -> `done`; probe exit without done -> `failed` |
| `tests` | `pending`, `passed`, `failed`, `waived` | `agent-msg verify` (verified), or the owning coordinator (`agent-task gate <T> tests passed --evidence "..."`) |
| `review` | `pending`, `approved`, `changes_requested`, `blocked`, `waived` | reviewer's structured review; `waived` only by the owning coordinator with `--reason` (trivial tasks, per policy) |
| `integration` | `pending`, `done`, `n/a` | owning coordinator (`agent-task gate <T> integration done --evidence "<merge sha / PR merged>"`) |

`completed` (v2) requires: `execution=done`, `tests in (passed, waived)`, `review in (approved, waived)` **for
the current revision**, no open finding of severity `blocking` or `major`, and `integration in (done, n/a)`.
Otherwise refused with the list of unmet gates. `--force --reason "..."` overrides; the event carries
`forced:true` and the reason. A parent task (has children) instead requires all children `completed` or
`cancelled`, plus its own `integration` gate.

A worker exiting, even cleanly, never sets `completed`. `done` only sets `execution=done` and moves the task
to `review`.

### Executions

`executions` is a list; the last entry with `ended == null` is current. Entry:
`{"id":"<execution_id>","agent_id":"mac-mini/t62","provider":"codex","model":"","role":"worker","started":"<utc>","ended":null,"outcome":null}`.
`role` is `worker` or `reviewer`. `outcome` on end: `done`, `exited`, `failed`, `superseded`, `cancelled`.

- `assign` resolves the execution id with `hc get <terminal> --json` (on the Mini). Assigning the same
  execution again is a no-op. Assigning a different execution while the current one has not ended is
  refused unless `--replace` (old execution gets `outcome: superseded`).
- `_auto` (called by `agent-msg`) applies a post only if the post's `exec` equals the task's current
  worker execution (or, for reviews, its current reviewer execution). Other posts are recorded on the board
  but change nothing (the reply says "stale execution, ignored").
- The legacy field `agent` (`{cli, model, terminal}`) stays in sync with the current worker execution, so v1
  readers and `coord-watch` keep working.

### Reviews

`reviews` entries:
```json
{"n":1,"reviewer":"mac-mini/t63","reviewer_provider":"claude","author":"mac-mini/t62","author_provider":"codex",
 "revision":"2f920eb...","requested":"<utc>","completed":"<utc>","verdict":"approved",
 "findings":[{"id":"F1","severity":"minor","files":["lib/slug.js:2"],"description":"...","action":"...",
              "status":"open","resolution":null}]}
```
- `agent-task review-assign <T> --coord C --reviewer <terminal>`: refused if the reviewer's agent_id equals
  the author of any execution of this task. Records `author` (the agent of the execution that posted `done`)
  and `revision` (that `done`'s HEAD sha). The legacy `set --reviewer` maps to this.
- A review is recorded only from a structured post: `agent-msg post review "<text>" --verdict
  approved|changes_requested|blocked [--finding "<severity>|<files>|<description>|<action>"]...` from the
  assigned reviewer execution. Severity: `blocking`, `major`, `minor`, `info`. A legacy post with
  `VERDICT: approve|changes` in its text is parsed (approve -> approved, changes -> changes_requested) and
  flagged `parsed:true`. A reviewer process exiting never records a review.
- `changes_requested` or `blocked` sets the review gate accordingly and moves the task back to
  `in_progress` (changes) or `blocked` (blocked).
- Findings: `agent-task finding <T> <F-id> resolve|accept --coord C --reason "..."` (owning coordinator).
  `blocking`/`major` findings must be `resolved` or `accepted` before completion.
- Re-review: when a new `done` arrives with a different revision after the review gate was `approved`, the
  gate returns to `pending` and the task waits in `review` for a new review, unless the owning coordinator
  runs `agent-task gate <T> review approved --carry --reason "<why the change is insignificant>"`.
- Cross-model preference is policy (CLAUDE.md); the registry records both providers so it is auditable.

### Ownership, handoff and recovery

- `owner` = `coordinator`. Every mutating command needs `--coord` equal to the owner. Locking: `flock` on
  `<project>.tasks.json.lock` for the whole read-modify-write, temp file + `fsync` + `rename` for the write.
- Lease: owners renew (`agent-task renew --coord C`, done by `coord-watch` every loop). `lease_until` =
  now + `AGENT_TASK_TTL_MIN` (default 180).
- Ownership handoff is two-phase: `agent-task handoff <T> --coord A --to B` sets `handoff_to=B`
  (`handoff.created`); `agent-task accept <T> --coord B` makes B the owner (`handoff.completed`). A can
  cancel with `agent-task handoff <T> --coord A --cancel`.
- Recovery: a non-owner may take a task only with `agent-task claim <T> --coord B --recover --reason "..."`,
  and only when the owner's lease expired **and** the task has no live execution in the agent registry.
  Emits `handoff.completed` with `kind: recovery`. A plain `claim` of another coordinator's task is refused.
- Nothing disappears: the probe (section 5) marks executions that end without `done` (`execution=failed`,
  task -> `blocked`, reason recorded), so an interrupted task is always visible in
  `agent-task attention`.

### Commands (agent-task v2; v1 commands keep their syntax)

`new`, `claim [--recover --reason]`, `handoff [--to | --cancel]`, `accept`, `assign [--replace]`,
`review-assign`, `status`, `set`, `gate <T> <gate> <value> [--evidence|--reason|--carry]`,
`finding <T> <F> resolve|accept`, `renew`, `ls [--all] [--json]`, `show <T> [--json]`, `brief <T> [--json]`,
`attention [--coord C] [--json]`, `stale`, `_auto` (internal). All accept `--project P`.

## 5. Agent registry (fleet)

Table `agents` in `events.db`, one row per execution:

| Column | Meaning |
|---|---|
| `execution_id` (PK), `agent_id`, `provider`, `role` (`coordinator`/`worker`/`reviewer`), `machine_id` | identity |
| `project_id`, `task_id`, `coordinator_id` | current assignment (from the task registry) |
| `session_id` | provider session id, from hook input |
| `state` | `working`, `idle`, `blocked`, `stale`, `disconnected`, `exited` |
| `started_at`, `last_heartbeat_at`, `last_activity_at`, `last_activity` (Herdr `.status.msg` or last board kind, <=200 chars) | timing |
| `ended_at`, `end_reason` | when exited/failed |

State rules (the probe applies them; hooks update `working`/`idle` and heartbeats in between):
- `working`: `hc get` -> terminal `live`, `.status.app` set, `.status.state == working`, or a turn-start
  hook within the last heartbeat interval.
- `idle`: live, app set, `.status.state` in (`done`, `idle`).
- `blocked`: the agent's last board post is `blocker`/`question`/`ask-user` with no later `answer` to it,
  or Herdr reports the agent waiting for input.
- `stale`: not verified for more than 2 probe intervals (probe or hooks could not see it), or `working`
  with no hook activity and an unchanged Herdr `turn` for more than 45 minutes.
- `disconnected`: terminal not found, or the Mini unreachable when last checked.
- `exited`: terminal `finished`, or terminal `live` with `.status.app` null (the CLI exited, only a shell
  is left). A running terminal is never evidence of a running agent.

Coordinators appear as rows `macbook/claude` and `macbook/codex` (role `coordinator`): `renew` updates their
heartbeat; `stale` after 2 renew intervals without one.

Herdr output shapes (verified 2026-11-10): `hc ls [--all] --json` gives
`{address, agent: "claude-code"|"codex"|"bash"|..., state: "live"|"finished", status: "working"|"done"|..., turn, cwd, where}`
(no start time); `hc get <t> --json` gives `{address, state, status: {app, state, msg} | null, reason, turn,
created_ms, finished_ms, pid, labels}`. Use `ls` to enumerate and `get` for start time, pid and activity.

Probe: `agent-event probe` runs every 2 minutes from a systemd user timer on the Mini (`agent-probe.timer`,
`OnUnitActiveSec=2min`, oneshot, timeout 60 s). It checks every execution not yet `exited`, plus every live
terminal whose `.status.app` is set, with one `hc ls --all --json` call. It writes state changes and events,
and calls `agent-task _exec-ended` for executions that ended without `done`.

## 6. Storage (`~/.agent-bus/events.db`)

- SQLite, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`. Atomic transactions give safe
  concurrent writers and recovery from interrupted writes. Stdlib `sqlite3` only.
- `PRAGMA user_version` = storage schema version (1). Migrations are numbered functions run at open.
- Tables: `events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, schema_version,
  event_type, timestamp, project_id, task_id, parent_task_id, correlation_id, coordinator_id, agent_id,
  provider, machine_id, execution_id, session_id, status, summary, metadata TEXT /* JSON */, source)`
  with indexes on `(project_id, task_id, seq)`, `(correlation_id, seq)`, `(execution_id, seq)`,
  `(agent_id, seq)`, `(timestamp)`; `agents` (section 5).
- Emit failures never break the caller: `agent-task`, `agent-msg` and hooks log the failure to
  `~/.agent-bus/events-errors.log` and carry on. Task state stays authoritative in the registry.
- Retention: `agent-event prune --days 180` deletes events older than 180 days whose correlation tree has no
  open task, and `agents` rows exited more than 30 days ago; then `VACUUM`. The probe timer runs it weekly
  (`agent-prune.timer`). Board JSONL files are not rotated yet.

## 7. Handoff brief

`agent-task brief <T> [--json]` renders one structured handoff, the same for Claude and Codex. Sections, in
order (Markdown headings, JSON keys in brackets):
1. Objective [`objective`]: task title and issue.
2. Project and repository [`project`]: project id, repo path on the Mini, base branch.
3. Requirements [`requirements`]: the issue reference plus the task's `requirements` text (set with
   `agent-task set --requirements`).
4. Contracts and architecture [`contracts`]: `finding` posts on this task and its dependencies whose text
   starts with `CONTRACT:` (full text), else all findings of the dependencies.
5. Decisions [`decisions`]: project `decision` posts and this task's notes.
6. Status [`status`]: state, gates, current execution, open review findings.
7. Branches, commits, artifacts [`artifacts`]: branch, worktree, revisions, PR.
8. Tests [`tests`]: test gate, `test.completed` events with results.
9. Remaining work [`remaining`]: `agent-task set --remaining` text, open findings.
10. Blockers [`blockers`]: open `blocker`/`question` posts, blocked reason.
11. Deliverables [`deliverables`]: `agent-task set --deliverables` text.
Size limit 12 KB; longer post texts are truncated with a pointer to `agent-msg read`. The task contract for a
worker or reviewer says: "Run `agent-task brief <T>` first." `brief` records `brief_rev` = the task `rev`
it rendered.

## 8. Coordinator isolation

- `coord-watch` (MacBook, one per coordinator per project): per-coordinator cursor; ignores posts of tasks
  and executions owned by the other coordinator, unless the task has `handoff_to` equal to this coordinator;
  shows posts whose `exec` is not the task's current execution with a `stale` marker on owned or
  incoming-handoff tasks; prints the `exec` and `task` of each post.
- Wake events are hints. The coordinator acts on registry state, and every transition is idempotent, so a
  watcher that restarts or replays its cursor causes no duplicate work.
- `agent-task attention --coord C` lists everything C must act on: tasks in `review` without a review,
  `changes_requested`, `blocked`, executions ended without `done`, open blocking findings, pending
  handoffs to C, and stale or expired leases. Run it at the start of every coordinator turn.

## 9. Dashboard integration (future, read-only)

- The Agent Command Center (a separate web dashboard) never reaches the Mini and has no write or exec path.
- A future collector process on the Mini reads `events.db` read-only (`agent-event export --after <seq>
  --limit 500` prints JSONL) and pushes batches outbound over HTTPS to an authenticated ingest endpoint,
  idempotent by `event_id`, keeping its own cursor. The fleet view is pushed as `agent-event agents --json`
  snapshots. Nothing in this repo opens a listening port.
- Not built in this version: the collector, the endpoint, Cloudflare configuration.

## 10. Versioning

- Event `schema_version` 1. Storage `user_version` 1. Task registry `v` 2.
- Additive changes keep the version; renames or removals bump it, and consumers must handle both during
  migration.

## 11. Internal API (Python, `lib/agentbus`)

Scripts find the package with `sys.path.insert(0, <dir of the script>/../lib)`; that is `~/.local/lib` once
installed and `<repo>/lib` in a checkout. Environment: `AGENT_BUS_DIR` (default `~/.agent-bus`) holds
everything, so tests use a temp dir. `AGENT_BUS_HC` (default `hc`) names the Herdr CLI, so tests can use a fake.

```python
# agentbus.events
SCHEMA_VERSION = 1
def now_utc() -> str                                   # "2026-11-10T00:52:13.481Z"
def event_id(natural_key: str | None) -> str           # uuid5 or uuid4
def redact(text: str, limit: int) -> str
def emit(event_type: str, *, project_id: str, natural_key: str | None = None, task_id=None,
         parent_task_id=None, correlation_id=None, coordinator_id=None, agent_id=None, provider=None,
         machine_id="mac-mini", execution_id=None, session_id=None, status=None, summary="",
         metadata: dict | None = None, source: str, timestamp: str | None = None) -> dict | None
    # validates event_type against the table in section 3, fills defaults, INSERT OR IGNORE;
    # returns the stored event, or None if it was a duplicate. Never raises to the caller:
    # on any error it appends to events-errors.log and returns None.

# agentbus.store
def connect(bus_dir: str | None = None) -> sqlite3.Connection   # opens/migrates events.db
def query_events(conn, *, project_id=None, task_id=None, correlation_id=None, agent_id=None,
                 execution_id=None, after_seq=0, since=None, until=None, limit=1000) -> list[dict]

# agentbus.fleet
def hc_get(terminal: str) -> dict | None                # parsed `hc get --json`, None if not found
def execution_id_for(terminal: str, machine_id="mac-mini") -> str | None
def upsert_agent(conn, **row) -> None
def get_agent(conn, execution_id) -> dict | None
def live_executions(conn, *, project_id=None, task_id=None) -> list[dict]   # state not exited/disconnected
def probe(conn) -> list[dict]                           # applies section 5 rules, returns emitted events

# agentbus.tasks (used by bin/agent-task; uses events.emit and fleet.live_executions/execution_id_for)
```

`bin/agent-event` subcommands: `emit <type> --project P [--task T] [--agent A] [--exec X] [--provider]
[--coord C] [--status S] [--summary "..."] [--meta JSON] [--key NATURAL_KEY] [--source S]` (for bash
callers), `timeline [--project P] [--task T | --correlation C] [--after SEQ] [--json]` (a task's timeline
includes its subtasks' events when given `--correlation`), `agents [--all] [--json]`, `probe`,
`export --after SEQ [--limit N]`, `prune [--days N]`.
