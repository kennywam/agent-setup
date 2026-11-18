"""agent-task: shared task registry for both MacBook coordinators (claude, codex) and the Mini agents.

State: <AGENT_BUS_DIR>/<project>.tasks.json, changed under flock(<file>.lock) and swapped in atomically.
Implements docs/SPEC.md sections 4 (lifecycle), 7 (brief) and 8 (attention). Tasks with "v": 2 follow the
v2 rules (gates, executions, reviews, two-phase handoff); tasks without "v" keep the v1 rules.
"""
from contextlib import closing
import copy
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time

# T2 modules (section 11). Missing or broken = events are a no-op and fleet lookups fall back.
try:
    from agentbus import events
except Exception:
    events = None
try:
    from agentbus import fleet
except Exception:
    fleet = None
try:
    from agentbus import store
except Exception:
    store = None

USAGE = """\
agent-task new "<objective>" --coord C [--issue N] [--parent T] [--deps T1,T2] [--files a,b] [--force]
agent-task claim <T> --coord C [--recover --reason "..."]
                                            take ownership (free or yours; another coordinator's task only
                                            with --recover, after its lease expired and no execution is live)
agent-task handoff <T> --coord C --to D     offer ownership to the other coordinator (D runs accept)
agent-task handoff <T> --coord C --cancel   withdraw the offer
agent-task accept <T> --coord D             take a task offered to you
agent-task assign <T> --coord C --agent claude|codex --terminal t5x [--model M] [--force] [--replace]
agent-task review-assign <T> --coord C --reviewer t5y [--reviewer-agent claude|codex]
agent-task status <T> <status> --coord C [--note "..."] [--force --reason "..."]
agent-task set <T> --coord C [--files a,b] [--branch B] [--worktree W] [--pr URL] [--verify "..."]
                             [--reviewer t5y --reviewer-agent claude|codex] [--note "..."] [--force]
                             [--requirements "..."] [--remaining "..."] [--deliverables "..."]
agent-task gate <T> <gate> <value> --coord C [--evidence "..."] [--reason "..."] [--carry]
agent-task finding <T> <F-id> resolve|accept --coord C --reason "..."
agent-task renew --coord C                  refresh the lease on all your open tasks (coord-watch does this)
agent-task ls [--all] [--coord C] [--json]  open tasks (all with --all)
agent-task show <T> [--json]                the task plus every board message linked to it, and project decisions:
                                            the context an agent needs to continue the work
agent-task brief <T> [--json]               the structured handoff brief (run it first when you take a task)
agent-task attention [--coord C] [--json]   everything a coordinator must act on (run it every turn)
agent-task stale                            open tasks whose coordinator lease expired
Common: --project P (default: the repo you're in)

Lifecycle: pending -> assigned -> in_progress -> review -> completed, plus blocked, failed, cancelled.
failed -> pending retries. Files: two open tasks can't own overlapping paths unless --force (deliberate).
Reviewer must be a different agent from the author. Agents don't call this; their board posts with
--task (or AGENT_TASK set) move assigned->in_progress, done->review, blocker->blocked automatically.
v2 tasks complete only when the gates pass: execution done, tests passed|waived, review approved|waived
(for the current revision), no open blocking/major finding, integration done|n/a.
"""

STATES = ("pending", "assigned", "in_progress", "blocked", "review", "completed", "failed", "cancelled")
TERMINAL = ("completed", "failed", "cancelled")
NEXT = {
    "pending": ["assigned", "in_progress", "blocked", "cancelled"],  # in_progress: a parent its coordinator runs
    "assigned": ["in_progress", "review", "pending", "blocked", "failed", "cancelled"],
    "in_progress": ["review", "completed", "blocked", "failed", "cancelled"],
    "review": ["in_progress", "completed", "blocked", "failed", "cancelled"],
    "blocked": ["pending", "assigned", "in_progress", "review", "failed", "cancelled"],
    "failed": ["pending"], "completed": [], "cancelled": [],
}
GATE_VALUES = {
    "execution": ("pending", "done", "failed"),
    "tests": ("pending", "passed", "failed", "waived"),
    "review": ("pending", "approved", "changes_requested", "blocked", "waived"),
    "integration": ("pending", "done", "n/a"),
}
SEVERITIES = ("blocking", "major", "minor", "info")
WORK_KINDS = ("plan", "status", "claim", "finding", "notice", "question")
VALUE_FLAGS = ("project", "coord", "to", "agent", "model", "terminal", "issue", "parent", "deps", "files", "branch",
               "worktree", "pr", "verify", "reviewer", "reviewer-agent", "note", "reason", "evidence", "exec", "rev",
               "board-id", "verdict", "text", "requirements", "remaining", "deliverables", "command")
BOOL_FLAGS = ("force", "all", "replace", "recover", "cancel", "carry", "json")
BRIEF_LIMIT = 12 * 1024
MACHINE = "mac-mini"
AGENT_MSG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "agent-msg")  # <lib>/../bin


class Refused(Exception):
    """A refusal: printed to stderr, exit 1 (or the given code), nothing written."""

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


class Usage(Exception):
    """Bad invocation: printed to stderr, exit 2."""


# ---------------------------------------------------------------- helpers

def bus_dir():
    return os.environ.get("AGENT_BUS_DIR") or os.path.expanduser("~/.agent-bus")


def now_utc():
    if events is not None:
        try:
            return events.now_utc()
        except Exception:
            pass
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_local():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")  # same as `date -Is`


def csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def overlap(a, b):
    return any(x.startswith(y) or y.startswith(x) for x in a for y in b)


def is_v2(t):
    return t.get("v", 1) >= 2


def stale(t, epoch):
    return (t.get("lease_until") or 0) < epoch


def project_default():
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        return os.path.basename(os.path.dirname(common))
    except Exception:
        return os.path.basename(os.getcwd())


def agent_id_of(terminal):
    return f"{MACHINE}/{terminal}"


def terminal_of(exec_id):
    """mac-mini/t62@1794264763416 -> t62"""
    m = re.match(r"^[^/]+/([^@]+)", exec_id or "")
    return m.group(1) if m else ""


def resolve_exec(terminal):
    if fleet is None:
        return None
    try:
        return fleet.execution_id_for(terminal, machine_id=MACHINE)
    except Exception:
        return None


def provider_of(terminal):
    if fleet is None:
        return ""
    try:
        app = ((fleet.hc_get(terminal) or {}).get("status") or {}).get("app") or ""
    except Exception:
        return ""
    return {"claude-code": "claude", "codex": "codex"}.get(app, "")


def current_exec(t, role="worker"):
    for e in reversed(t.get("executions") or []):
        if e.get("role") == role and e.get("ended") is None:
            return e
    return None


def current_revision(t):
    revs = t.get("revisions") or []
    return revs[-1] if revs else None


def all_findings(t):
    return [f for r in t.get("reviews") or [] for f in r.get("findings") or []]


def open_findings(t, severities=SEVERITIES):
    return [f for f in all_findings(t) if f.get("status") == "open" and f.get("severity") in severities]


def read_board(project):
    path = os.path.join(bus_dir(), f"{project}.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def load_state(project):
    """The registry, or None if the file is missing or empty. Anything unreadable refuses: never start over on it."""
    path = os.path.join(bus_dir(), f"{project}.tasks.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    if not raw.strip():
        return None
    try:
        state = json.loads(raw)
    except ValueError as e:
        raise Refused(f"{path} is not valid JSON ({e}); nothing was changed: restore or fix it by hand")
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), dict) or not isinstance(state.get("next"), int):
        raise Refused(f"{path} is not a task registry (needs \"next\" and \"tasks\"); nothing was changed")
    return state


def root_of(state, tid):
    seen = set()
    while tid in state["tasks"] and state["tasks"][tid].get("parent") and tid not in seen:
        seen.add(tid)
        tid = state["tasks"][tid]["parent"]
    return tid


# ---------------------------------------------------------------- context: one invocation

class Ctx:
    def __init__(self, cmd, opts, args):
        self.cmd, self.o, self.args = cmd, opts, args
        self.project = opts.get("project") or project_default()
        self.coord = opts.get("coord", "")
        self.epoch = int(time.time())
        self.now = now_local()
        self.ttl = int(os.environ.get("AGENT_TASK_TTL_MIN", "180")) * 60
        self.file = os.path.join(bus_dir(), f"{self.project}.tasks.json")
        self.pending_events = []
        self.posts = []
        self.out = []

    def opt(self, name):
        return self.o.get(name, "")

    def flag(self, name):
        return bool(self.o.get(name))

    def need(self, *names):
        for n in names:
            val = self.id if n == "id" else self.o.get("terminal" if n == "term" else n)
            if not val:
                raise Usage(f"missing --{n}")

    @property
    def id(self):
        return self.args[0] if self.args else ""

    # --- registry writes (call inside mutate)

    def lease(self, t):
        t["lease_until"] = self.epoch + self.ttl
        t["updated"] = self.now

    def record(self, state, t, to=None, reason="", exec_id=None, by=None):
        """Bump rev and append history; to=None records a field change in the current state."""
        frm = t["status"]
        if to is not None:
            t["status"] = to
        t["rev"] = t.get("rev", 0) + 1
        t.setdefault("history", []).append({"rev": t["rev"], "at": now_utc(), "by": by or self.coord or "agent",
                                            "from": frm, "to": t["status"], "exec": exec_id, "reason": reason})
        t["updated"] = self.now

    def event(self, state, t, etype, summary, metadata=None, status=None, source="agent-task", agent_id=None,
              provider=None, execution_id=None, key=None):
        root = root_of(state, t["id"])
        self.pending_events.append(dict(
            event_type=etype, project_id=self.project,
            natural_key=key or f"task:{self.project}:{t['id']}:rev{t.get('rev', 0)}:{etype}",
            task_id=t["id"], parent_task_id=t.get("parent") or None, correlation_id=f"{self.project}:{root}",
            coordinator_id=t.get("coordinator") or None, agent_id=agent_id, provider=provider,
            execution_id=execution_id, status=t["status"] if status is None else status, summary=summary,
            metadata=metadata or {}, source=source))

    def transition_events(self, state, t, frm, to, reason="", forced=False, by=None, source="agent-task"):
        """task.started / blocked / resumed / completed / failed / cancelled for a status change."""
        kw = dict(source=source)
        if to == "in_progress" and not t.get("started"):
            t["started"] = now_utc()
            self.event(state, t, "task.started", f"{t['id']} started", {"by": by or self.coord}, **kw)
        if to == "blocked":
            self.event(state, t, "task.blocked", f"{t['id']} blocked: {reason}"[:300], {"reason": reason}, **kw)
        elif frm == "blocked" and to not in ("failed", "cancelled"):
            self.event(state, t, "task.resumed", f"{t['id']} resumed ({to})", {"reason": reason}, **kw)
        if to in TERMINAL:
            self.event(state, t, f"task.{to}", f"{t['id']} {to}" + (f": {reason}" if reason else ""),
                       {"gates": dict(t.get("gates") or {}), "reason": reason, "forced": forced}, **kw)

    def post(self, text, as_=None):
        self.posts.append((text, as_ if as_ is not None else (self.coord or "agent")))

    def say(self, text):
        self.out.append(text)

    # --- the read-modify-write cycle

    def mutate(self, fn):
        os.makedirs(bus_dir(), exist_ok=True)
        with open(self.file + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = load_state(self.project)
            if state is None:
                state = {"next": 1, "tasks": {}}
            before = json.dumps(state, sort_keys=True)
            result = fn(state)  # raises Refused: nothing written
            if json.dumps(state, sort_keys=True) != before:
                tmp = f"{self.file}.tmp.{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.rename(tmp, self.file)
        self.flush()
        return result

    def flush(self):
        for ev in self.pending_events:
            emit(**ev)
        self.pending_events = []
        for text, as_ in self.posts:
            try:
                subprocess.run([AGENT_MSG, "post", "task", text, "--task", self.id, "--project", self.project, "--as", as_],
                               env={**os.environ, "AGENT_TASK": ""}, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            except Exception:
                pass
        self.posts = []


def emit(**kw):
    if events is None:
        return None
    try:
        return events.emit(kw.pop("event_type"), **kw)
    except Exception:
        return None


# ---------------------------------------------------------------- shared checks

def owned(ctx, state, tid=None):
    tid = tid or ctx.id
    t = state["tasks"].get(tid)
    if t is None:
        raise Refused(f"no task {tid}")
    o = t.get("coordinator") or ""
    if o and o != ctx.coord:
        hint = ""
        if stale(t, ctx.epoch):
            hint = (f", or recover it with claim --recover --reason \"...\" (its lease expired)" if is_v2(t)
                    else ", or claim it (its lease expired)")
        raise Refused(f"{tid} is owned by {o}; ask it to hand off{hint}")
    return t


def files_free(ctx, state, fs, tid):
    hit = [t["id"] for t in state["tasks"].values()
           if t["id"] != tid and t["status"] not in TERMINAL and overlap(t.get("files") or [], fs)]
    if hit and not ctx.flag("force"):
        raise Refused(f"files overlap open task(s) {','.join(hit)}; coordinate first or use --force")


def unmet_gates(state, t):
    kids = [k for k in state["tasks"].values() if k.get("parent") == t["id"]]
    g = t.get("gates") or {}
    unmet = []
    if kids:
        unmet += [f"child {k['id']} is {k['status']}" for k in kids if k["status"] not in ("completed", "cancelled")]
    else:
        if g.get("execution") != "done":
            unmet.append(f"execution={g.get('execution')}")
        if g.get("tests") not in ("passed", "waived"):
            unmet.append(f"tests={g.get('tests')}")
        if g.get("review") not in ("approved", "waived"):
            unmet.append(f"review={g.get('review')}")
        unmet += [f"finding {f['id']} ({f['severity']}) is open" for f in open_findings(t, ("blocking", "major"))]
    if g.get("integration") not in ("done", "n/a"):
        unmet.append(f"integration={g.get('integration')}")
    return unmet


def set_gate(ctx, state, t, gate, value, reason="", by=None, exec_id=None, source="agent-task", board_id=None):
    """Change one gate (own rev, so each gate change has its own task.progress event). False if unchanged."""
    if t["gates"].get(gate) == value:
        return False
    t["gates"][gate] = value
    ctx.record(state, t, reason=f"gate {gate}={value}" + (f": {reason}" if reason else ""), by=by, exec_id=exec_id)
    ctx.event(state, t, "task.progress", f"{t['id']} gate {gate} = {value}", {"gate": gate, "value": value,
              "board_id": board_id}, source=source, execution_id=exec_id)
    return True


def move(ctx, state, t, to, reason="", by=None, exec_id=None, forced=False, source="agent-task"):
    frm = t["status"]
    ctx.record(state, t, to=to, reason=reason, by=by, exec_id=exec_id)
    ctx.transition_events(state, t, frm, to, reason, forced=forced, by=by, source=source)


def live_executions(ctx, t):
    if fleet is not None and store is not None:
        try:
            with closing(store.connect()) as conn:
                return fleet.live_executions(conn, project_id=ctx.project, task_id=t["id"])
        except Exception:
            pass
    return [e for e in t.get("executions") or [] if e.get("ended") is None]  # fallback: unended counts as live


def new_v2_fields():
    return {"v": 2, "rev": 0, "history": [],
            "gates": {"execution": "pending", "tests": "pending", "review": "pending", "integration": "pending"},
            "executions": [], "reviews": [], "handoff_to": None, "revisions": [],
            "requirements": "", "remaining": "", "deliverables": "", "brief_rev": None}


# ---------------------------------------------------------------- commands

def cmd_new(ctx):
    ctx.need("coord")
    title = " ".join(ctx.args)
    if not title:
        raise Usage(USAGE)
    issue, parent = ctx.opt("issue"), ctx.opt("parent")

    def fn(state):
        tid = f"T{state['next']}"
        if issue and not parent and not ctx.flag("force"):
            dup = [t["id"] for t in state["tasks"].values()
                   if t.get("issue") == issue and not t.get("parent") and t["status"] not in TERMINAL]
            if dup:
                raise Refused(f"issue #{issue} already has open task {','.join(dup)}; use it, or --force")
        if parent and parent not in state["tasks"]:
            raise Refused(f"no parent task {parent}")
        fs = csv(ctx.opt("files"))
        files_free(ctx, state, fs, tid)
        state["next"] += 1
        t = {"id": tid, "title": title, "issue": issue, "parent": parent, "deps": csv(ctx.opt("deps")),
             "status": "pending", "coordinator": ctx.coord, "agent": None, "author": "", "reviewer": None,
             "files": fs, "branch": "", "worktree": "", "pr": "", "verify": "", "notes": [], "created": ctx.now}
        ctx.lease(t)
        t.update(new_v2_fields())
        state["tasks"][tid] = t
        ctx.args = [tid]
        ctx.record(state, t, reason="created")
        ctx.event(state, t, "task.created", f"{tid} created by {ctx.coord}: {title}"[:300],
                  {"title": title, "issue": issue, "deps": t["deps"], "files": fs})
        ctx.post(f"{tid} created by {ctx.coord}: {title}")
        return tid

    ctx.say(ctx.mutate(fn))


def cmd_claim(ctx):
    ctx.need("coord", "id")
    recover = ctx.flag("recover")
    if recover and not ctx.opt("reason"):
        raise Usage("--recover needs --reason")

    def fn(state):
        t = state["tasks"].get(ctx.id)
        if t is None:
            raise Refused(f"no task {ctx.id}")
        o = t.get("coordinator") or ""
        if not is_v2(t):  # v1: free, yours, or stale
            if not stale(t, ctx.epoch):
                owned(ctx, state)
            if o != ctx.coord:
                t["coordinator"] = ctx.coord
                ctx.record(state, t, reason=f"claimed from {o or 'nobody'}")
                ctx.event(state, t, "handoff.completed", f"{ctx.id} claimed by {ctx.coord}",
                          {"kind": "recovery", "from": o, "to": ctx.coord, "brief_rev": t.get("brief_rev")})
            ctx.lease(t)
            ctx.post(f"{ctx.id} claimed by {ctx.coord}")
            return
        if o and o != ctx.coord:
            if not recover:
                raise Refused(f"{ctx.id} is owned by {o}; ask it to hand off (once its lease expired and no "
                              f"execution is live: claim --recover --reason \"...\")")
            if not stale(t, ctx.epoch):
                lu = datetime.datetime.fromtimestamp(t.get("lease_until") or 0).astimezone().isoformat(timespec="seconds")
                raise Refused(f"{ctx.id}: {o}'s lease is still active (until {lu}); ask it to hand off")
            live = live_executions(ctx, t)
            if live:
                ids = ",".join(str(e.get("execution_id") or e.get("id")) for e in live)
                raise Refused(f"{ctx.id} has a live execution ({ids}); recovery needs no live execution")
            t["coordinator"], t["handoff_to"] = ctx.coord, None
            ctx.lease(t)
            ctx.record(state, t, reason=f"recovered from {o}: {ctx.opt('reason')}")
            ctx.event(state, t, "handoff.completed", f"{ctx.id} recovered by {ctx.coord} from {o}",
                      {"kind": "recovery", "from": o, "to": ctx.coord, "brief_rev": t.get("brief_rev")})
            ctx.post(f"{ctx.id} recovered by {ctx.coord} from {o}: {ctx.opt('reason')}")
            return
        if o != ctx.coord:
            t["coordinator"] = ctx.coord
            ctx.record(state, t, reason="claimed")
            ctx.post(f"{ctx.id} claimed by {ctx.coord}")
        ctx.lease(t)

    ctx.mutate(fn)
    ctx.say(f"{ctx.id} is yours ({ctx.coord})")


def cmd_handoff(ctx):
    ctx.need("coord", "id")
    cancel, to = ctx.flag("cancel"), ctx.opt("to")
    if not cancel:
        ctx.need("to")

    def fn(state):
        t = owned(ctx, state)
        if not is_v2(t):
            if cancel:
                raise Refused(f"{ctx.id} is a legacy task: handoff is immediate, nothing to cancel")
            t["coordinator"] = to
            ctx.lease(t)
            ctx.record(state, t, reason=f"handed off to {to}")
            ctx.event(state, t, "handoff.completed", f"{ctx.id} handed off from {ctx.coord} to {to}",
                      {"kind": "ownership", "from": ctx.coord, "to": to, "brief_rev": t.get("brief_rev")})
            ctx.post(f"{ctx.id} handed off from {ctx.coord} to {to}")
            return f"{ctx.id} now owned by {to}"
        if cancel:
            if not t.get("handoff_to"):
                return f"{ctx.id} has no pending handoff"
            was = t["handoff_to"]
            t["handoff_to"] = None
            ctx.lease(t)
            ctx.record(state, t, reason=f"handoff to {was} cancelled")
            ctx.post(f"{ctx.id} handoff to {was} cancelled by {ctx.coord}")
            return f"{ctx.id} handoff to {was} cancelled"
        if to == ctx.coord:
            raise Refused(f"{ctx.id} is already yours")
        if t.get("handoff_to") == to:
            return f"{ctx.id} already offered to {to}"
        t["handoff_to"] = to
        ctx.lease(t)
        ctx.record(state, t, reason=f"handoff offered to {to}")
        ctx.event(state, t, "handoff.created", f"{ctx.id} handoff offered by {ctx.coord} to {to}",
                  {"kind": "ownership", "from": ctx.coord, "to": to, "brief_rev": t.get("brief_rev")})
        ctx.post(f"{ctx.id} handoff offered from {ctx.coord} to {to}; {to} must accept")
        return f"{ctx.id} offered to {to}; {to} must run: agent-task accept {ctx.id} --coord {to}"

    ctx.say(ctx.mutate(fn))


def cmd_accept(ctx):
    ctx.need("coord", "id")

    def fn(state):
        t = state["tasks"].get(ctx.id)
        if t is None:
            raise Refused(f"no task {ctx.id}")
        if t.get("handoff_to") != ctx.coord:
            if t.get("coordinator") == ctx.coord:
                return f"{ctx.id} is already yours ({ctx.coord})"
            raise Refused(f"{ctx.id} has no pending handoff to {ctx.coord}")
        was = t.get("coordinator") or ""
        t["coordinator"], t["handoff_to"] = ctx.coord, None
        ctx.lease(t)
        ctx.record(state, t, reason=f"accepted handoff from {was}")
        ctx.event(state, t, "handoff.completed", f"{ctx.id} handed off from {was} to {ctx.coord}",
                  {"kind": "ownership", "from": was, "to": ctx.coord, "brief_rev": t.get("brief_rev")})
        ctx.post(f"{ctx.id} handed off from {was} to {ctx.coord}")
        return f"{ctx.id} now owned by {ctx.coord}"

    ctx.say(ctx.mutate(fn))


def cmd_assign(ctx):
    ctx.need("coord", "id", "agent", "term")
    agent, model, term = ctx.opt("agent"), ctx.opt("model"), ctx.opt("terminal")
    if agent not in ("claude", "codex"):
        raise Usage("--agent is claude or codex")
    exec_id = resolve_exec(term)

    def fn(state):
        t = owned(ctx, state)
        open_deps = [d for d in t.get("deps") or [] if (state["tasks"].get(d) or {}).get("status") != "completed"]
        if open_deps and not ctx.flag("force"):
            raise Refused(f"{ctx.id} waits on {','.join(open_deps)} (not completed); use --force to start anyway")
        st = t["status"]
        cur = current_exec(t) if is_v2(t) else None
        # v2: an in_progress/review task may get a new worker (same live one = no-op, other live one = --replace)
        if st != "assigned" and "assigned" not in NEXT.get(st, []) and not (is_v2(t) and st in ("in_progress", "review")):
            raise Refused(f"{ctx.id} is {st}; cannot assign")
        t["agent"] = {"cli": agent, "model": model, "terminal": term}
        if not t.get("author"):
            t["author"] = term
        t["coordinator"] = ctx.coord
        ctx.lease(t)
        if not is_v2(t):
            if st != "assigned":
                move(ctx, state, t, "assigned", reason=f"assigned to {agent} on {term}")
            else:
                ctx.record(state, t, reason=f"reassigned to {agent} on {term}")
            ctx.event(state, t, "task.assigned", f"{ctx.id} assigned to {agent} worker {agent_id_of(term)}",
                      {"provider": agent, "model": model, "role": "worker"}, agent_id=agent_id_of(term), provider=agent)
            ctx.post(f"{ctx.id} assigned to {agent} ({term}{', ' + model if model else ''}) by {ctx.coord}")
            return
        if cur is not None:
            same = (cur["id"] == exec_id) if (cur.get("id") and exec_id) else cur.get("terminal") == term
            if same:
                if cur.get("id") is None and exec_id:
                    cur["id"] = exec_id
                if st in ("assigned", "in_progress", "review"):
                    return "noop"
                move(ctx, state, t, "assigned", reason=f"reassigned to the same execution on {term}")
                return
            if not ctx.flag("replace"):
                raise Refused(f"{ctx.id} has a live execution {cur.get('id') or cur.get('terminal')} "
                              f"({cur.get('provider')} on {cur.get('terminal')}); use --replace to supersede it")
            cur["ended"], cur["outcome"] = now_utc(), "superseded"
        rcur = current_exec(t, "reviewer")
        if rcur and rcur["agent_id"] == agent_id_of(term):  # now an author: no longer an independent reviewer
            rcur["ended"], rcur["outcome"] = now_utc(), "superseded"
        earlier = [e for e in t["executions"] if e.get("role") == "worker"]
        t["executions"].append({"id": exec_id, "agent_id": agent_id_of(term), "provider": agent, "model": model,
                                "role": "worker", "terminal": term, "started": now_utc(), "ended": None,
                                "outcome": None})
        t["gates"]["execution"] = "pending"
        if st != "assigned" and "assigned" in NEXT[st]:
            move(ctx, state, t, "assigned", reason=f"assigned to {agent} on {term}", exec_id=exec_id)
        else:
            ctx.record(state, t, reason=f"reassigned to {agent} on {term}", exec_id=exec_id)
        kw = dict(agent_id=agent_id_of(term), provider=agent, execution_id=exec_id)
        ctx.event(state, t, "task.assigned", f"{ctx.id} assigned to {agent} worker {agent_id_of(term)}",
                  {"provider": agent, "model": model, "role": "worker"}, **kw)
        ctx.event(state, t, "delegation.created", f"{ctx.id} delegated to {agent_id_of(term)}",
                  {"role": "worker", "brief_rev": t.get("brief_rev")}, **kw)
        if earlier:
            ctx.event(state, t, "handoff.created", f"{ctx.id} context handoff {earlier[-1]['agent_id']} -> "
                      f"{agent_id_of(term)}", {"kind": "context", "from": earlier[-1]["agent_id"],
                                               "to": agent_id_of(term), "brief_rev": t.get("brief_rev")}, **kw)
        ctx.post(f"{ctx.id} assigned to {agent} ({term}{', ' + model if model else ''}) by {ctx.coord}")

    if ctx.mutate(fn) == "noop":
        ctx.say(f"{ctx.id} already assigned to {agent} on {term}")
    else:
        ctx.say(f"{ctx.id} assigned to {agent} on {term}")


def assign_reviewer(ctx, state, t, term, ragent, exec_id):
    """review-assign inside a mutation. Returns False if it was a no-op."""
    rid = agent_id_of(term)
    authors = {e["agent_id"] for e in t.get("executions") or [] if e.get("role") == "worker"}
    if term in (t.get("author"), (t.get("agent") or {}).get("terminal")) or rid in authors:
        raise Refused(f"reviewer {term} wrote this task; pick a different agent for an independent review")
    t["reviewer"] = {"terminal": term, "cli": ragent}
    if not is_v2(t):
        return True
    done = [e for e in t["executions"] if e.get("role") == "worker" and e.get("done_at")]
    author = done[-1] if done else current_exec(t) or {}
    revision = (author.get("done_rev") if done else None) or current_revision(t)
    cur = current_exec(t, "reviewer")
    last = t["reviews"][-1] if t["reviews"] else None
    same = cur is not None and cur.get("terminal") == term and (not exec_id or cur.get("id") in (None, exec_id))
    if same and last and last["reviewer"] == rid and last["revision"] == revision and last.get("completed") is None:
        if exec_id and cur.get("id") is None:
            cur["id"] = exec_id
            ctx.record(state, t, reason=f"reviewer execution resolved: {exec_id}", exec_id=exec_id)
            return True
        return False
    if cur and not same:  # another reviewer, or the same terminal restarted as a new execution
        cur["ended"], cur["outcome"] = now_utc(), "superseded"
        cur = None
    if cur is None:
        t["executions"].append({"id": exec_id, "agent_id": rid, "provider": ragent, "model": "", "role": "reviewer",
                                "terminal": term, "started": now_utc(), "ended": None, "outcome": None})
    t["reviews"].append({"n": len(t["reviews"]) + 1, "reviewer": rid, "reviewer_provider": ragent,
                         "author": author.get("agent_id"), "author_provider": author.get("provider"),
                         "revision": revision, "requested": now_utc(), "completed": None, "verdict": None,
                         "findings": []})
    ctx.record(state, t, reason=f"review assigned to {rid}", exec_id=exec_id)
    kw = dict(agent_id=rid, provider=ragent or None, execution_id=exec_id)
    ctx.event(state, t, "review.requested", f"{t['id']} review of {revision or 'current work'} requested from {rid}",
              {"reviewer": rid, "author": author.get("agent_id"), "revision": revision}, **kw)
    ctx.event(state, t, "delegation.created", f"{t['id']} review delegated to {rid}",
              {"role": "reviewer", "brief_rev": t.get("brief_rev")}, **kw)
    return True


def cmd_review_assign(ctx):
    ctx.need("coord", "id", "reviewer")
    term = ctx.opt("reviewer")
    ragent = ctx.opt("reviewer-agent") or provider_of(term)
    exec_id = resolve_exec(term)

    def fn(state):
        t = owned(ctx, state)
        changed = assign_reviewer(ctx, state, t, term, ragent, exec_id)
        ctx.lease(t)
        if changed:
            ctx.post(f"{ctx.id} review assigned to {term} by {ctx.coord}")
        return changed

    changed = ctx.mutate(fn)
    ctx.say(f"{ctx.id} review assigned to {term}" if changed else f"{ctx.id} review already assigned to {term}")


def cmd_status(ctx):
    ctx.need("coord", "id")
    st = ctx.args[1] if len(ctx.args) > 1 else ""
    if not st:
        raise Usage(USAGE)
    note, reason, force = ctx.opt("note"), ctx.opt("reason"), ctx.flag("force")
    if st == "completed" and force and not reason:
        raise Usage("--force needs --reason")

    def fn(state):
        t = owned(ctx, state)
        if t["status"] == st:
            return "noop"
        if st not in NEXT.get(t["status"], []):
            raise Refused(f"{ctx.id}: {t['status']} -> {st} is not allowed")
        forced = False
        if st == "completed" and is_v2(t):
            unmet = unmet_gates(state, t)
            if unmet and not force:
                raise Refused(f"{ctx.id} cannot complete, unmet gates: {', '.join(unmet)} "
                              f"(override: --force --reason \"...\")")
            forced = bool(unmet)
        if st == "cancelled":
            for e in t.get("executions") or []:
                if e.get("ended") is None:
                    e["ended"], e["outcome"] = now_utc(), "cancelled"
        ctx.lease(t)
        if note:
            t["notes"].append({"at": ctx.now, "by": ctx.coord, "text": note})
        move(ctx, state, t, st, reason=reason or note, forced=forced)
        ctx.post(f"{ctx.id} -> {st}" + (f": {note}" if note else "") + (f" (forced: {reason})" if forced else ""))

    if ctx.mutate(fn) == "noop":
        ctx.say(f"{ctx.id} already {st}")
    else:
        ctx.say(f"{ctx.id} is {st}")


def gh_pr_view(url):
    try:
        r = subprocess.run(["gh", "pr", "view", url, "--json", "url,number"], capture_output=True, text=True,
                           timeout=15)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def cmd_set(ctx):
    ctx.need("coord", "id")
    o = ctx.opt
    note, reviewer = o("note"), o("reviewer")
    pr_info = gh_pr_view(o("pr")) if o("pr") else None
    rexec = resolve_exec(reviewer) if reviewer else None
    ragent = (o("reviewer-agent") or provider_of(reviewer)) if reviewer else ""

    def fn(state):
        t = owned(ctx, state)
        if o("files"):
            files_free(ctx, state, csv(o("files")), ctx.id)
        before = copy.deepcopy(t)
        ctx.lease(t)
        if reviewer and is_v2(t):
            assign_reviewer(ctx, state, t, reviewer, ragent, rexec)
        elif reviewer:
            assign_reviewer(ctx, state, t, reviewer, o("reviewer-agent"), None)
        if o("files"):
            t["files"] = csv(o("files"))
        for k in ("branch", "worktree", "pr", "verify") + (("requirements", "remaining", "deliverables")
                                                         if is_v2(t) else ()):
            if o(k):
                t[k] = o(k)
        if note:
            t["notes"].append({"at": ctx.now, "by": ctx.coord, "text": note})
        strip = lambda d: {k: v for k, v in d.items() if k not in ("lease_until", "updated", "rev", "history")}
        if strip(t) != strip(before) and t.get("rev") == before.get("rev"):
            ctx.record(state, t, reason=note or "fields updated")
        if o("pr") and before.get("pr") != o("pr") and pr_info:
            ctx.event(state, t, "pull_request.created", f"{ctx.id} PR {pr_info.get('url')}",
                      {"url": pr_info.get("url"), "number": pr_info.get("number")})
        ctx.post(f"{ctx.id} updated by {ctx.coord}" + (f": {note}" if note else ""))

    ctx.mutate(fn)
    ctx.say(f"{ctx.id} updated")


def cmd_gate(ctx):
    ctx.need("coord", "id")
    if len(ctx.args) < 3:
        raise Usage("agent-task gate <T> <gate> <value> --coord C [--evidence ...] [--reason ...] [--carry]")
    gate, value = ctx.args[1], ctx.args[2]
    evidence, reason, carry = ctx.opt("evidence"), ctx.opt("reason"), ctx.flag("carry")
    if gate not in GATE_VALUES:
        raise Usage(f"unknown gate {gate} (one of: {', '.join(GATE_VALUES)})")
    if value not in GATE_VALUES[gate]:
        raise Usage(f"gate {gate} is one of: {', '.join(GATE_VALUES[gate])}")
    if gate == "execution":
        raise Refused("the execution gate is set by the worker's done post or the probe; "
                      "use status completed --force --reason to override")
    if gate == "review" and not ((value == "waived" and reason) or (value == "approved" and carry and reason)):
        raise Refused("the review gate is set by the reviewer's structured review; coordinators may only waive it "
                      "(waived --reason) or carry an approval (approved --carry --reason)")
    if gate == "tests" and value in ("passed", "failed") and not evidence:
        raise Usage("tests passed|failed needs --evidence")
    if value == "waived" and not reason:
        raise Usage("waived needs --reason")
    if gate == "integration" and value in ("done", "n/a") and not (evidence or reason):
        raise Usage(f"integration {value} needs --evidence (or --reason)")

    def fn(state):
        t = owned(ctx, state)
        if not is_v2(t):
            raise Refused(f"{ctx.id} is a legacy task: no gates")
        if gate == "review" and carry and not any(r.get("verdict") == "approved" for r in t["reviews"]):
            raise Refused(f"{ctx.id} has no earlier approval to carry")
        ctx.lease(t)
        if not set_gate(ctx, state, t, gate, value, reason=evidence or reason):
            return "noop"
        rev = current_revision(t)
        if gate == "tests" and value in ("passed", "failed"):
            ctx.event(state, t, "test.completed", f"{ctx.id} tests {value} (recorded by {ctx.coord})",
                      {"result": value, "command": evidence, "revision": rev,
                       "verified": bool(re.match(r"^https?://", evidence))}, status=value)
        if gate == "integration" and value == "done":
            ctx.event(state, t, "integration.completed", f"{ctx.id} integrated: {evidence or reason}"[:300],
                      {"target": None, "revision": rev, "evidence": evidence or reason})
        ctx.post(f"{ctx.id} gate {gate} = {value} by {ctx.coord}: {evidence or reason}")

    if ctx.mutate(fn) == "noop":
        ctx.say(f"{ctx.id} gate {gate} already {value}")
    else:
        ctx.say(f"{ctx.id} gate {gate} = {value}")


def cmd_finding(ctx):
    ctx.need("coord", "id", "reason")
    if len(ctx.args) < 3 or ctx.args[2] not in ("resolve", "accept"):
        raise Usage("agent-task finding <T> <F-id> resolve|accept --coord C --reason \"...\"")
    fid, action = ctx.args[1], ctx.args[2]
    new = {"resolve": "resolved", "accept": "accepted"}[action]

    def fn(state):
        t = owned(ctx, state)
        f = next((f for f in all_findings(t) if f.get("id") == fid), None)
        if f is None:
            raise Refused(f"{ctx.id} has no finding {fid}")
        if f["status"] == new:
            return "noop"
        f["status"], f["resolution"] = new, ctx.opt("reason")
        ctx.lease(t)
        ctx.record(state, t, reason=f"finding {fid} {new}: {ctx.opt('reason')}")
        ctx.event(state, t, "task.progress", f"{ctx.id} finding {fid} {new}", {"gate": f"finding:{fid}", "value": new})
        ctx.post(f"{ctx.id} finding {fid} {new} by {ctx.coord}: {ctx.opt('reason')}")

    ctx.say(f"{ctx.id} {fid} already {new}" if ctx.mutate(fn) == "noop" else f"{ctx.id} {fid} {new}")


def cmd_renew(ctx):
    ctx.need("coord")
    if not (os.path.exists(ctx.file) and os.path.getsize(ctx.file)):
        return

    def fn(state):
        for t in state["tasks"].values():
            if t.get("coordinator") == ctx.coord and t["status"] not in TERMINAL:
                ctx.lease(t)

    ctx.mutate(fn)
    if fleet is not None and store is not None:
        try:
            with closing(store.connect()) as conn, conn:
                fleet.upsert_agent(conn, execution_id=f"macbook/{ctx.coord}", agent_id=f"macbook/{ctx.coord}",
                                   provider=ctx.coord, role="coordinator", machine_id="macbook",
                                   last_heartbeat_at=now_utc())
        except Exception:
            pass


# ---------------------------------------------------------------- internal: agent-msg and probe callbacks

def match_exec(t, role, frm, post_exec):
    """The current execution of `role` if the post came from it, else None (stale)."""
    cur = current_exec(t, role)
    if cur is None:
        return None
    if post_exec:
        if cur.get("id"):
            return cur if cur["id"] == post_exec else None
        if cur.get("terminal") == (frm or terminal_of(post_exec)):
            cur["id"] = post_exec  # assigned while hc could not resolve it: adopt the first real id
            return cur
        return None
    return cur if frm and cur.get("terminal") == frm else None


def parse_finding(spec, n):
    parts = spec.split("|")
    if len(parts) < 3 or parts[0].strip() not in SEVERITIES:
        return None
    parts += [""] * (4 - len(parts))
    return {"id": f"F{n}", "severity": parts[0].strip(), "files": csv(parts[1]), "description": parts[2].strip(),
            "action": "|".join(parts[3:]).strip(), "status": "open", "resolution": None}


def cmd_auto(ctx):
    kind = ctx.args[1] if len(ctx.args) > 1 else ""
    frm = ctx.args[2] if len(ctx.args) > 2 else ""
    if not (os.path.exists(ctx.file) and os.path.getsize(ctx.file)):
        return
    post_exec, rev, text = ctx.opt("exec") or None, ctx.opt("rev") or None, ctx.opt("text")
    board_id = int(ctx.opt("board-id")) if ctx.opt("board-id").isdigit() else None
    by = agent_id_of(frm) if frm else (post_exec or "agent")
    src = "agent-msg"

    def legacy(state, t):
        want = {**{k: "in_progress" for k in WORK_KINDS}, "done": "review", "blocker": "blocked"}.get(kind)
        st = t["status"]
        if (not want or (t.get("agent") or {}).get("terminal", "") != frm or st == want or want not in NEXT[st]
                or (want == "in_progress" and st != "assigned")):
            return "skip"
        move(ctx, state, t, want, reason=f"agent {frm} posted {kind}", by=by, source=src)
        ctx.post(f"{ctx.id} -> {want} (agent {frm} posted {kind})", as_="agent")

    def fn(state):
        t = state["tasks"].get(ctx.id)
        if t is None:
            return "skip"
        if not is_v2(t):
            return legacy(state, t)
        if kind == "review":
            return review(state, t)
        if kind not in WORK_KINDS + ("done", "blocker"):
            return "skip"
        e = match_exec(t, "worker", frm, post_exec)
        if e is None:
            return "stale"
        eid, st = e.get("id"), t["status"]
        if kind in WORK_KINDS:
            if st == "assigned":
                move(ctx, state, t, "in_progress", reason=f"agent {frm} posted {kind}", by=by, exec_id=eid, source=src)
                ctx.post(f"{ctx.id} -> in_progress (agent {frm} posted {kind})", as_="agent")
            elif kind == "status" and board_id is not None:
                ctx.event(state, t, "task.progress", f"{ctx.id} status from {by}", {"board_id": board_id},
                          source=src, execution_id=eid, key=f"board:{ctx.project}:{board_id}:task.progress")
            return
        if kind == "blocker":
            if st == "blocked" or "blocked" not in NEXT[st]:
                return "skip"
            move(ctx, state, t, "blocked", reason=(text or f"agent {frm} posted blocker")[:200], by=by, exec_id=eid,
                 source=src)
            ctx.post(f"{ctx.id} -> blocked (agent {frm} posted blocker)", as_="agent")
            return
        # done
        new_rev = rev is not None and rev != current_revision(t)
        if t["gates"]["execution"] == "done" and not new_rev and st == "review":
            return "already"
        if st not in ("review",) and "review" not in NEXT[st]:
            return "skip"
        if new_rev:
            t["revisions"].append(rev)
        e["done_at"], e["done_rev"] = now_utc(), rev or e.get("done_rev")
        set_gate(ctx, state, t, "execution", "done", by=by, exec_id=eid, source=src, board_id=board_id)
        g = t["gates"]["review"]
        if g in ("changes_requested", "blocked") or (g == "approved" and new_rev):
            set_gate(ctx, state, t, "review", "pending", reason=f"new revision {rev or '?'}", by=by, exec_id=eid,
                     source=src, board_id=board_id)
        if st != "review":
            move(ctx, state, t, "review", reason=f"agent {frm} posted done" + (f" at {rev}" if rev else ""), by=by,
                 exec_id=eid, source=src)
        ctx.post(f"{ctx.id} -> review (agent {frm} posted done{' at ' + rev[:12] if rev else ''})", as_="agent")

    def review(state, t):
        e = match_exec(t, "reviewer", frm, post_exec)
        if e is None:
            return "stale"
        verdict, parsed = ctx.opt("verdict"), False
        if not verdict:
            m = re.search(r"VERDICT:\s*(approve|changes)", text or board_text(ctx, frm, board_id), re.I)
            if not m:
                return "noverdict"
            verdict, parsed = {"approve": "approved", "changes": "changes_requested"}[m.group(1).lower()], True
        if verdict not in ("approved", "changes_requested", "blocked"):
            return "noverdict"
        if e["agent_id"] in {x["agent_id"] for x in t["executions"] if x.get("role") == "worker"}:
            return "author"
        mine = [r for r in t["reviews"] if r["reviewer"] == e["agent_id"]]
        if rev is not None:  # the post names the revision it reviewed: only that request matches
            mine = [r for r in mine if r["revision"] == rev]
        entry = next((r for r in reversed(mine) if r.get("completed") is None), None)
        if entry is None:
            base = mine[-1] if mine else {"author": None, "author_provider": None,
                                          "revision": rev if rev is not None else current_revision(t)}
            if mine and base.get("verdict") == verdict and not ctx.o.get("finding"):
                return "already"
            entry = {"n": len(t["reviews"]) + 1, "reviewer": e["agent_id"], "reviewer_provider": e.get("provider"),
                     "author": base["author"], "author_provider": base["author_provider"],
                     "revision": base["revision"], "requested": now_utc(), "completed": None, "verdict": None,
                     "findings": []}
            t["reviews"].append(entry)
        n = len(all_findings(t))
        for spec in ctx.o.get("finding") or []:
            f = parse_finding(spec, n + 1)
            if f:
                n += 1
                entry["findings"].append(f)
        entry.update(completed=now_utc(), verdict=verdict, board_id=board_id)
        if parsed:
            entry["parsed"] = True
        eid = e.get("id")
        applies = entry["revision"] == current_revision(t)
        if applies:
            set_gate(ctx, state, t, "review", verdict, by=by, exec_id=eid, source=src, board_id=board_id)
        else:
            ctx.record(state, t, reason=f"review {verdict} of old revision {entry['revision']}", by=by, exec_id=eid)
        counts = {s: sum(1 for f in entry["findings"] if f["severity"] == s) for s in SEVERITIES}
        etype = "review.changes_requested" if verdict == "changes_requested" else "review.completed"
        metadata = {"revision": entry["revision"], "findings": counts}
        if etype == "review.completed":
            metadata["verdict"] = verdict
        ctx.event(state, t, etype, f"{ctx.id} review {verdict} by {e['agent_id']}",
                  metadata, status=verdict, source=src,
                  agent_id=e["agent_id"], provider=e.get("provider"), execution_id=eid)
        to = {"changes_requested": "in_progress", "blocked": "blocked"}.get(verdict)
        if applies and to and t["status"] != to and to in NEXT[t["status"]]:
            move(ctx, state, t, to, reason=f"review {verdict}", by=by, exec_id=eid, source=src)
        ctx.post(f"{ctx.id} review {verdict} by {frm or e['agent_id']}", as_="agent")

    r = ctx.mutate(fn)
    if r == "stale":
        ctx.say("stale execution, ignored")
    elif r == "author":
        ctx.say("reviewer is an author of this task, review ignored")
    elif r == "noverdict":
        ctx.say("review has no verdict, not recorded")
    elif r == "already":
        ctx.say(f"{ctx.id} already {state_of(ctx)}")


def board_text(ctx, frm, board_id):
    """Text of the review post being applied, for agent-msg versions that pass neither --text nor --verdict."""
    posts = [m for m in read_board(ctx.project) if m.get("task") == ctx.id and m.get("kind") == "review"
             and (m.get("id") == board_id if board_id is not None else m.get("from") == frm)]
    return posts[-1].get("text") or "" if posts else ""


def state_of(ctx):
    return ((load_state(ctx.project) or {"tasks": {}})["tasks"].get(ctx.id) or {}).get("status")


def cmd_tests(ctx):
    result = ctx.args[1] if len(ctx.args) > 1 else ""
    post_exec, rev = ctx.opt("exec") or None, ctx.opt("rev") or None
    if result not in ("passed", "failed") or not (os.path.exists(ctx.file) and os.path.getsize(ctx.file)):
        return

    def fn(state):
        t = state["tasks"].get(ctx.id)
        if t is None or not is_v2(t):
            return "skip"
        e = match_exec(t, "worker", terminal_of(post_exec), post_exec)
        if e is None:
            return "stale"
        if t["gates"]["tests"] == result and t.get("tests_revision") == rev:
            return "skip"
        t["tests_revision"] = rev
        by = e["agent_id"]
        if not set_gate(ctx, state, t, "tests", result, by=by, exec_id=e.get("id"), source="agent-msg"):
            ctx.record(state, t, reason=f"tests {result} at {rev}", by=by, exec_id=e.get("id"))
        ctx.event(state, t, "test.completed", f"{ctx.id} tests {result} ({ctx.opt('command')})"[:300],
                  {"result": result, "command": ctx.opt("command"), "revision": rev, "verified": True},
                  status=result, source="agent-msg", agent_id=by, provider=e.get("provider"), execution_id=e.get("id"))

    if ctx.mutate(fn) == "stale":
        ctx.say("stale execution, ignored")


def cmd_exec_ended(ctx):
    xid, reason = ctx.opt("exec"), ctx.opt("reason") or "ended"
    if not xid or not (os.path.exists(ctx.file) and os.path.getsize(ctx.file)):
        return

    def fn(state):
        t = state["tasks"].get(ctx.id)
        if t is None or not is_v2(t):
            return
        e = next((e for e in reversed(t["executions"]) if e.get("id") == xid and e.get("ended") is None), None)
        if e is None:
            return
        current = e is current_exec(t)
        e["ended"] = now_utc()
        if not current:  # reviewer, or a worker execution that is no longer the current one
            e["outcome"] = "exited"
            ctx.record(state, t, reason=f"reviewer execution {xid} ended: {reason}", by="probe", exec_id=xid)
            return
        if e.get("done_at"):
            e["outcome"] = "done"
            ctx.record(state, t, reason=f"execution {xid} ended after done: {reason}", by="probe", exec_id=xid)
            return
        e["outcome"] = "failed"
        why = f"execution {xid} ended without done: {reason}"
        set_gate(ctx, state, t, "execution", "failed", reason=reason, by="probe", exec_id=xid, source="probe")
        if t["status"] != "blocked" and "blocked" in NEXT[t["status"]]:
            move(ctx, state, t, "blocked", reason=why, by="probe", exec_id=xid, source="probe")
        ctx.post(f"{ctx.id} -> blocked ({why})", as_="probe")

    ctx.mutate(fn)


# ---------------------------------------------------------------- read-only views

def row_agent(t):
    a = t.get("agent") or {}
    return f"{a.get('cli') or '-'}/{a.get('terminal') or '-'}"


def cmd_ls(ctx):
    state = load_state(ctx.project)
    if state is None:
        return
    rows = [t for t in state["tasks"].values() if (ctx.flag("all") or t["status"] not in TERMINAL)
            and (not ctx.coord or t.get("coordinator") == ctx.coord)]
    if ctx.flag("json"):
        ctx.say(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for t in rows:
        deps = "deps:" + ",".join(t["deps"]) if t.get("deps") else ""
        ctx.say(f"{t['id']}\t{t['status']}\t{t.get('coordinator')}\t{row_agent(t)}\t#{t.get('issue')}\t{deps}\t"
                f"{t['title'][:70]}" + ("" if is_v2(t) else "\tlegacy"))


def cmd_stale(ctx):
    state = load_state(ctx.project)
    if state is None:
        return
    for t in state["tasks"].values():
        if t["status"] not in TERMINAL and stale(t, ctx.epoch):
            ctx.say(f"{t['id']}\t{t['status']}\t{t.get('coordinator')}\tidle since {(t.get('updated') or '')[:16]}\t"
                    f"{t['title'][:60]}")


def msg_line(m):
    as_ = m.get("as")
    return f"#{m['id']} {m.get('ts', '')[5:16]} {m.get('from')}{'/' + as_ if as_ else ''} [{m.get('kind')}]: {m.get('text')}"


def cmd_show(ctx):
    ctx.need("id")
    state = load_state(ctx.project)
    if state is None:
        raise Refused(f"no tasks in {ctx.project}")
    t = state["tasks"].get(ctx.id)
    if t is None:
        raise Refused(f"no task {ctx.id}", 5)  # v1 (jq -e error) exited 5
    board = read_board(ctx.project)
    msgs = [m for m in board if (m.get("task") or "") == ctx.id]
    decisions = [m for m in board if m.get("kind") == "decision"]
    if ctx.flag("json"):
        ctx.say(json.dumps({"task": t, "messages": msgs, "decisions": decisions}, indent=2, ensure_ascii=False))
        return
    ctx.say(json.dumps(t, indent=2, ensure_ascii=False))
    ctx.say(f"--- board messages for {ctx.id}")
    ctx.out += [msg_line(m) for m in msgs]
    ctx.say("--- project decisions")
    ctx.out += [f"#{m['id']} {m.get('text')}" for m in decisions]


def test_events(project, tid):
    if store is None:
        return []
    try:
        with closing(store.connect()) as conn:
            evs = store.query_events(conn, project_id=project, task_id=tid)
            return [e for e in evs if e.get("event_type") == "test.completed"]
    except Exception:
        return []


def repo_info(t):
    cwd = t.get("worktree") if t.get("worktree") and os.path.isdir(os.path.expanduser(t["worktree"])) else os.getcwd()
    cwd = os.path.expanduser(cwd)
    try:
        g = lambda *a: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=10)
        common = g("rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode:
            return None, None
        repo = os.path.dirname(common.stdout.strip())
        base = next((b for b in ("dev", "main", "master") if g("rev-parse", "--verify", "-q", f"refs/heads/{b}").returncode == 0), None)
        return repo, base
    except Exception:
        return None, None


def open_asks(board, tid):
    answered = {str(m.get("re")) for m in board if m.get("kind") == "answer" and m.get("re")}
    return [m for m in board if m.get("task") == tid and m.get("kind") in ("blocker", "question")
            and str(m["id"]) not in answered]


def shrink(obj, limit, keep, ptr):
    """Cut every string to `limit` chars and every list to its last `keep` items, with pointers to the full data."""
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + f" ... [truncated: {ptr}]"
    if isinstance(obj, list):
        items = [shrink(x, limit, keep, ptr) for x in obj[-keep:]]
        return items if len(obj) <= keep else [f"[{len(obj) - keep} earlier items omitted: {ptr}]"] + items
    if isinstance(obj, dict):
        if "text" in obj and "id" in obj and "kind" in obj:  # a board post: point at its message
            ptr = f"board message #{obj['id']}"
        return {k: shrink(v, limit, keep, ptr) for k, v in obj.items()}
    return obj


def build_brief(ctx, state, t):
    board = read_board(ctx.project)
    post = lambda m: {"id": m["id"], "kind": m.get("kind"), "from": m.get("from"), "task": m.get("task"),
                      "text": m.get("text") or ""}
    tid, v2 = t["id"], is_v2(t)
    scope = [tid] + list(t.get("deps") or [])
    findings = [m for m in board if m.get("kind") == "finding" and m.get("task") in scope]
    contracts = [m for m in findings if (m.get("text") or "").startswith("CONTRACT:")]
    if not contracts:
        contracts = [m for m in findings if m.get("task") in (t.get("deps") or [])]
    repo, base = repo_info(t)
    open_f = open_findings(t) if v2 else []
    cur = current_exec(t) if v2 else None
    blocked_reason = next((h.get("reason") for h in reversed(t.get("history") or []) if h.get("to") == "blocked"
                           and h.get("from") != "blocked"), None) if t["status"] == "blocked" else None
    issue_ref = f"#{t['issue']}" if t.get("issue") else ""
    return {
        "objective": {"title": t["title"], "issue": issue_ref},
        "project": {"project_id": ctx.project, "repo": repo, "base_branch": base},
        "requirements": {"issue": issue_ref, "text": t.get("requirements") or ""},
        "contracts": [post(m) for m in contracts],
        "decisions": {"project": [post(m) for m in board if m.get("kind") == "decision"], "notes": t.get("notes") or []},
        "status": {"state": t["status"], "legacy": not v2, "gates": t.get("gates"), "coordinator": t.get("coordinator"),
                   "handoff_to": t.get("handoff_to"), "current_execution": cur or t.get("agent"),
                   "open_findings": open_f},
        "artifacts": {"branch": t.get("branch"), "worktree": t.get("worktree"), "revisions": t.get("revisions") or [],
                      "pr": t.get("pr")},
        "tests": {"gate": (t.get("gates") or {}).get("tests"), "verify": t.get("verify"),
                  "events": [{"result": (e.get("metadata") or {}).get("result"), "revision": (e.get("metadata") or {}).get("revision"),
                              "verified": (e.get("metadata") or {}).get("verified"), "at": e.get("timestamp")}
                             for e in test_events(ctx.project, tid)]},
        "remaining": {"text": t.get("remaining") or "", "open_findings": [f["id"] for f in open_f]},
        "blockers": {"open": [post(m) for m in open_asks(board, tid)], "blocked_reason": blocked_reason},
        "deliverables": t.get("deliverables") or "",
    }


TITLES = [("objective", "Objective"), ("project", "Project and repository"), ("requirements", "Requirements"),
          ("contracts", "Contracts and architecture"), ("decisions", "Decisions"), ("status", "Status"),
          ("artifacts", "Branches, commits, artifacts"), ("tests", "Tests"), ("remaining", "Remaining work"),
          ("blockers", "Blockers"), ("deliverables", "Deliverables")]


def render_md(tid, b):
    def each(items, fmt, sep="\n"):  # shrink() may put an "items omitted" string in front of a list
        return sep.join(f"- {x}" if isinstance(x, str) else fmt(x) for x in items)

    p = lambda m: f"- #{m['id']} {m.get('task') or ''} {m['from']} [{m['kind']}]: {m['text']}"
    none = "(none)"
    s = b["status"]
    sec = {
        "objective": f"{b['objective']['title']}" + (f" (issue {b['objective']['issue']})" if b["objective"]["issue"] else ""),
        "project": f"- project: {b['project']['project_id']}\n- repo: {b['project']['repo'] or 'unknown'}\n"
                   f"- base branch: {b['project']['base_branch'] or 'unknown'}",
        "requirements": "\n".join(x for x in (b["requirements"]["issue"] and f"Issue {b['requirements']['issue']}",
                                              b["requirements"]["text"]) if x) or none,
        "contracts": each(b["contracts"], p) or none,
        "decisions": "\n".join(x for x in (each(b["decisions"]["project"], p), each(
            b["decisions"]["notes"], lambda n: f"- note {n.get('at', '')[:16]} {n.get('by')}: {n.get('text')}")) if x) or none,
        "status": f"- state: {s['state']}{' (legacy task, v1 rules)' if s['legacy'] else ''}\n"
                  f"- gates: {json.dumps(s['gates']) if s['gates'] else 'n/a'}\n- coordinator: {s['coordinator']}"
                  + (f" (handoff offered to {s['handoff_to']})" if s["handoff_to"] else "")
                  + f"\n- current execution: {json.dumps(s['current_execution'])}\n- open findings: "
                  + (each(s["open_findings"], lambda f: f"{f['id']} {f['severity']} {','.join(f['files'])}: {f['description']}", "; ") or none),
        "artifacts": f"- branch: {b['artifacts']['branch'] or none}\n- worktree: {b['artifacts']['worktree'] or none}\n"
                     f"- revisions: {', '.join(b['artifacts']['revisions']) or none}\n- PR: {b['artifacts']['pr'] or none}",
        "tests": f"- gate: {b['tests']['gate'] or 'n/a'}\n- verify: {b['tests']['verify'] or none}\n" +
                 (each(b["tests"]["events"], lambda e: f"- {e['at']} {e['result']} at {e['revision']} (verified: {e['verified']})") or "- runs: none"),
        "remaining": "\n".join(x for x in (b["remaining"]["text"], b["remaining"]["open_findings"] and
                                           "Open findings: " + ", ".join(b["remaining"]["open_findings"])) if x) or none,
        "blockers": "\n".join(x for x in (each(b["blockers"]["open"], p), b["blockers"]["blocked_reason"] and
                                            f"Blocked: {b['blockers']['blocked_reason']}") if x) or none,
        "deliverables": b["deliverables"] or none,
    }
    out = [f"# Brief {tid}"]
    for i, (k, title) in enumerate(TITLES, 1):
        out.append(f"\n## {i}. {title}\n{sec[k]}")
    return "\n".join(out)


def cmd_brief(ctx):
    ctx.need("id")
    state = load_state(ctx.project)
    t = (state or {"tasks": {}})["tasks"].get(ctx.id)
    if t is None:
        raise Refused(f"no task {ctx.id}")
    full = build_brief(ctx, state, t)
    ptr = f"agent-task show {ctx.id}"
    for limit, keep in ((100000, 1000), (1500, 50), (600, 30), (250, 20), (100, 10), (60, 5), (30, 2)):
        b = shrink(full, limit, keep, ptr)
        text = json.dumps(b, indent=2, ensure_ascii=False) if ctx.flag("json") else render_md(ctx.id, b)
        if len(text.encode()) <= BRIEF_LIMIT:
            break
    rendered = t.get("rev", 0)

    def fn(st):  # brief_rev is bookkeeping: no rev bump
        x = st["tasks"].get(ctx.id)
        if x is not None and x.get("brief_rev") != rendered:
            x["brief_rev"] = rendered

    ctx.mutate(fn)
    ctx.say(text)


def attention_items(state, coord, epoch):
    items = []
    add = lambda t, why: items.append({"task": t["id"], "reason": why, "status": t["status"],
                                       "coordinator": t.get("coordinator"), "title": t["title"]})
    for t in state["tasks"].values():
        mine = not coord or t.get("coordinator") == coord
        if t["status"] in TERMINAL:
            continue
        if t.get("handoff_to") and (not coord or t["handoff_to"] == coord):
            add(t, f"handoff offered to {t['handoff_to']} by {t.get('coordinator')}: agent-task accept {t['id']}")
        if not mine:
            if stale(t, epoch):
                add(t, f"recoverable: {t.get('coordinator')}'s lease expired")
            continue
        g = t.get("gates") or {}
        if t["status"] == "review" and is_v2(t) and g.get("review") == "pending":
            add(t, f"review: waiting for a review of {current_revision(t) or 'the current revision'}")
        if t["status"] == "review" and not is_v2(t) and not t.get("reviewer"):
            add(t, "review: no reviewer assigned")
        if g.get("review") == "changes_requested":
            add(t, "changes requested")
        if t["status"] == "blocked":
            why = next((h.get("reason") for h in reversed(t.get("history") or []) if h.get("to") == "blocked"), "")
            add(t, f"blocked: {why}" if why else "blocked")
        if g.get("execution") == "failed":
            add(t, "execution ended without done")
        for f in open_findings(t, ("blocking", "major")):
            add(t, f"open {f['severity']} finding {f['id']}: {f['description'][:80]}")
        if stale(t, epoch):
            add(t, "lease expired: agent-task renew")
    return items


def cmd_attention(ctx):
    state = load_state(ctx.project)
    items = attention_items(state, ctx.coord, ctx.epoch) if state else []
    if ctx.flag("json"):
        ctx.say(json.dumps(items, indent=2, ensure_ascii=False))
        return
    for i in items:
        ctx.say(f"{i['task']}\t{i['status']}\t{i['coordinator']}\t{i['reason']}\t{i['title'][:60]}")


# ---------------------------------------------------------------- entry point

COMMANDS = {"new": cmd_new, "claim": cmd_claim, "handoff": cmd_handoff, "accept": cmd_accept, "assign": cmd_assign,
            "review-assign": cmd_review_assign, "status": cmd_status, "set": cmd_set, "gate": cmd_gate,
            "finding": cmd_finding, "renew": cmd_renew, "ls": cmd_ls, "stale": cmd_stale, "show": cmd_show,
            "brief": cmd_brief, "attention": cmd_attention, "_auto": cmd_auto, "_tests": cmd_tests,
            "_exec-ended": cmd_exec_ended}


def parse(argv):
    cmd = argv[0] if argv else ""
    opts, args, i, rest = {}, [], 0, argv[1:]
    while i < len(rest):
        a = rest[i]
        name = a[2:] if a.startswith("--") else None
        if name in VALUE_FLAGS and i + 1 < len(rest):
            val = rest[i + 1]
            if name == "issue":
                val = val.lstrip("#")
            if name == "finding":
                opts.setdefault("finding", []).append(val)
            else:
                opts[name] = val
            i += 2
        elif name == "finding" and i + 1 < len(rest):
            opts.setdefault("finding", []).append(rest[i + 1])
            i += 2
        elif name in BOOL_FLAGS:
            opts[name] = True
            i += 1
        else:
            args.append(a)
            i += 1
    return cmd, opts, args


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    cmd, opts, args = parse(sys.argv[1:] if argv is None else argv)
    if cmd in ("help", "-h", "--help", ""):
        out.write(USAGE)
        return 0
    if cmd not in COMMANDS:
        err.write(USAGE)
        return 2
    ctx = Ctx(cmd, opts, args)
    try:
        COMMANDS[cmd](ctx)
    except Usage as e:
        err.write(str(e).rstrip("\n") + "\n")
        return 2
    except Refused as e:
        err.write(str(e) + "\n")
        return e.code
    finally:
        for line in ctx.out:
            out.write(line + "\n")
    return 0
