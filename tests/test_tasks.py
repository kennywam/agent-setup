"""Tests for lib/agentbus/tasks.py (docs/SPEC.md sections 4, 7, 8). Temp AGENT_BUS_DIR, fake hc and gh only;
agentbus.events / fleet / store are replaced by in-memory fakes."""
import io
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from agentbus import tasks  # noqa: E402


class FakeEvents:
    def __init__(self):
        self.stored, self.keys = [], set()

    def now_utc(self):
        return "2026-11-10T00:00:00.000Z"

    def emit(self, event_type, **kw):
        key = kw.get("natural_key")
        if key in self.keys:
            return None
        self.keys.add(key)
        ev = {"event_type": event_type, **kw}
        self.stored.append(ev)
        return ev

    def types(self):
        return [e["event_type"] for e in self.stored]


class FakeFleet:
    def __init__(self):
        self.created = {}  # terminal -> created_ms
        self.live = []
        self.upserts = []

    def execution_id_for(self, terminal, machine_id="mac-mini"):
        return f"{machine_id}/{terminal}@{self.created.get(terminal, 1)}"

    def hc_get(self, terminal):
        return {"status": {"app": "claude-code"}}

    def live_executions(self, conn, project_id=None, task_id=None):
        return [x for x in self.live if x.get("task_id") == task_id]

    def upsert_agent(self, conn, **row):
        self.upserts.append(row)


class FakeStore:
    def __init__(self, ev):
        self.ev = ev
        self.closed = 0

    def connect(self, bus_dir=None):
        return self

    def close(self):
        self.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query_events(self, conn, project_id=None, task_id=None, **kw):
        return [e for e in self.ev.stored if e["project_id"] == project_id and e["task_id"] == task_id]


def W(term):
    return f"mac-mini/{term}@1"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old_env, self.old_cwd = dict(os.environ), os.getcwd()
        bindir = os.path.join(self.dir, "fakebin")
        os.makedirs(bindir)
        for name in ("gh", "hc"):
            with open(os.path.join(bindir, name), "w") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(bindir, name), 0o755)
        os.environ.update(AGENT_BUS_DIR=self.dir, AGENT_BUS_HC=os.path.join(bindir, "hc"),
                          PATH=bindir + os.pathsep + os.environ["PATH"], AGENT_TASK="", HC_TERMINAL_ID="")
        os.environ.pop("AGENT_TASK_TTL_MIN", None)
        os.chdir(self.dir)
        self.saved = (tasks.events, tasks.fleet, tasks.store)
        self.ev, self.fl = FakeEvents(), FakeFleet()
        tasks.events, tasks.fleet, tasks.store = self.ev, self.fl, FakeStore(self.ev)
        self.saved_msg, tasks.AGENT_MSG = tasks.AGENT_MSG, "true"  # board posts: only Views.test_show_* posts for real

    def tearDown(self):
        tasks.events, tasks.fleet, tasks.store = self.saved
        tasks.AGENT_MSG = self.saved_msg
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.dir)

    def run_(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = tasks.main(list(argv) + ["--project", "demo"], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        rc, out, err = self.run_(*argv)
        self.assertEqual(rc, 0, f"{argv}: {err}")
        return out

    def no(self, *argv, rc=1):
        got, out, err = self.run_(*argv)
        self.assertEqual(got, rc, f"{argv} should be refused: {out}")
        return err

    def state(self):
        with open(os.path.join(self.dir, "demo.tasks.json")) as fh:
            return json.load(fh)

    def task(self, tid="T1"):
        return self.state()["tasks"][tid]

    def patch(self, tid="T1", **fields):
        s = self.state()
        s["tasks"][tid].update(fields)
        with open(os.path.join(self.dir, "demo.tasks.json"), "w") as fh:
            json.dump(s, fh)

    def auto(self, kind, frm, *flags, tid="T1", exec_=True):
        extra = ["--exec", W(frm)] if exec_ else []
        return self.ok("_auto", tid, kind, frm, *extra, *flags)

    def worked(self, rev="r1"):
        """T1 assigned to t1, posted plan and done at rev."""
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        self.auto("plan", "t1")
        self.auto("done", "t1", "--rev", rev)

    def approved(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2", "--reviewer-agent", "claude")
        self.auto("review", "t2", "--verdict", "approved")


class Transitions(Base):
    def test_every_pair_v2_and_legacy(self):
        self.ok("new", "x", "--coord", "claude")
        base = self.state()
        for legacy in (False, True):
            for frm in tasks.STATES:
                for to in tasks.STATES:
                    s = json.loads(json.dumps(base))
                    t = s["tasks"]["T1"]
                    t["status"] = frm
                    if legacy:
                        for k in ("v", "gates", "executions", "reviews"):
                            t.pop(k)
                    with open(os.path.join(self.dir, "demo.tasks.json"), "w") as fh:
                        json.dump(s, fh)
                    rc, out, err = self.run_("status", "T1", to, "--coord", "claude", "--force", "--reason", "matrix")
                    with self.subTest(legacy=legacy, frm=frm, to=to):
                        if frm == to:
                            self.assertEqual((rc, out.strip()), (0, f"T1 already {to}"))
                        elif to in tasks.NEXT[frm]:
                            self.assertEqual(rc, 0, err)
                            self.assertEqual(self.task()["status"], to)
                        else:
                            self.assertEqual(rc, 1)
                            self.assertIn("is not allowed", err)
                            self.assertEqual(self.task()["status"], frm)

    def test_noop_writes_nothing_and_emits_nothing(self):
        self.ok("new", "x", "--coord", "claude")
        self.ok("status", "T1", "blocked", "--coord", "claude")
        rev, n = self.task()["rev"], len(self.ev.stored)
        self.assertEqual(self.ok("status", "T1", "blocked", "--coord", "claude").strip(), "T1 already blocked")
        self.assertEqual((self.task()["rev"], len(self.ev.stored)), (rev, n))

    def test_history_and_events(self):
        self.ok("new", "x", "--coord", "claude")
        self.ok("status", "T1", "blocked", "--coord", "claude", "--note", "env broken")
        self.ok("status", "T1", "pending", "--coord", "claude")
        h = self.task()["history"]
        self.assertEqual([(x["rev"], x["from"], x["to"]) for x in h],
                         [(1, "pending", "pending"), (2, "pending", "blocked"), (3, "blocked", "pending")])
        self.assertEqual(self.ev.types(), ["task.created", "task.blocked", "task.resumed"])
        self.assertEqual(self.ev.stored[1]["natural_key"], "task:demo:T1:rev2:task.blocked")
        self.assertEqual(self.ev.stored[1]["correlation_id"], "demo:T1")

    def test_non_owner_refused(self):
        self.ok("new", "x", "--coord", "claude")
        self.assertIn("owned by claude", self.no("status", "T1", "cancelled", "--coord", "codex"))


class Gates(Base):
    def test_completed_needs_every_gate(self):
        self.worked()
        err = self.no("status", "T1", "completed", "--coord", "claude")
        for g in ("tests=pending", "review=pending", "integration=pending"):
            self.assertIn(g, err)
        self.assertNotIn("execution=", err)
        self.ok("gate", "T1", "tests", "passed", "--coord", "claude", "--evidence", "make test")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--verdict", "approved")
        err = self.no("status", "T1", "completed", "--coord", "claude")
        self.assertEqual(err.split("gates: ")[1].split(" (")[0], "integration=pending")
        self.ok("gate", "T1", "integration", "done", "--coord", "claude", "--evidence", "merged abc123")
        self.ok("status", "T1", "completed", "--coord", "claude")
        done = [e for e in self.ev.stored if e["event_type"] == "task.completed"][0]
        self.assertFalse(done["metadata"]["forced"])
        self.assertIn("integration.completed", self.ev.types())

    def test_force_needs_reason_and_is_recorded(self):
        self.worked()
        self.no("status", "T1", "completed", "--coord", "claude", "--force", rc=2)
        self.ok("status", "T1", "completed", "--coord", "claude", "--force", "--reason", "hotfix")
        ev = [e for e in self.ev.stored if e["event_type"] == "task.completed"][0]
        self.assertEqual((ev["metadata"]["forced"], ev["metadata"]["reason"]), (True, "hotfix"))

    def test_gate_rules(self):
        self.worked()
        self.no("gate", "T1", "execution", "done", "--coord", "claude")
        self.no("gate", "T1", "review", "approved", "--coord", "claude", "--reason", "x")
        self.no("gate", "T1", "tests", "passed", "--coord", "claude", rc=2)
        self.no("gate", "T1", "tests", "bogus", "--coord", "claude", "--evidence", "x", rc=2)
        self.no("gate", "T1", "tests", "passed", "--coord", "codex", "--evidence", "x")
        self.ok("gate", "T1", "review", "waived", "--coord", "claude", "--reason", "trivial")
        self.assertIn("already", self.ok("gate", "T1", "review", "waived", "--coord", "claude", "--reason", "trivial"))
        self.ok("gate", "T1", "tests", "passed", "--coord", "claude", "--evidence", "https://ci/run/1")
        tc = [e for e in self.ev.stored if e["event_type"] == "test.completed"][0]
        self.assertTrue(tc["metadata"]["verified"])

    def test_parent_needs_children(self):
        self.ok("new", "parent", "--coord", "claude")
        self.ok("new", "child", "--coord", "claude", "--parent", "T1")
        self.ok("status", "T1", "in_progress", "--coord", "claude")
        err = self.no("status", "T1", "completed", "--coord", "claude")
        self.assertIn("child T2 is pending", err)
        self.assertNotIn("tests=", err)
        self.ok("status", "T2", "cancelled", "--coord", "claude")
        self.ok("gate", "T1", "integration", "n/a", "--coord", "claude", "--reason", "nothing to merge")
        self.ok("status", "T1", "completed", "--coord", "claude")
        self.assertEqual(self.ev.stored[-1]["correlation_id"], "demo:T1")

    def test_legacy_has_no_gates(self):
        self.ok("new", "x", "--coord", "claude")
        s = self.state()
        for k in ("v", "gates"):
            s["tasks"]["T1"].pop(k)
        with open(os.path.join(self.dir, "demo.tasks.json"), "w") as fh:
            json.dump(s, fh)
        self.assertIn("legacy", self.no("gate", "T1", "tests", "passed", "--coord", "claude", "--evidence", "x"))
        self.ok("status", "T1", "in_progress", "--coord", "claude")
        self.ok("status", "T1", "completed", "--coord", "claude")
        self.assertIn("\tlegacy", self.ok("ls", "--all"))


class Executions(Base):
    def test_done_moves_to_review_and_duplicate_is_noop(self):
        self.worked()
        t = self.task()
        self.assertEqual((t["status"], t["gates"]["execution"], t["revisions"]), ("review", "done", ["r1"]))
        rev, n = t["rev"], len(self.ev.stored)
        self.assertIn("already review", self.auto("done", "t1", "--rev", "r1"))
        self.assertEqual((self.task()["rev"], len(self.ev.stored)), (rev, n))
        self.assertIsNone(self.task()["executions"][0]["ended"])  # done does not end the execution

    def test_stale_execution_ignored(self):
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        before = self.task()
        out = self.ok("_auto", "T1", "done", "t1", "--exec", "mac-mini/t1@999", "--rev", "r1")
        self.assertIn("stale execution, ignored", out)
        self.assertIn("stale execution", self.ok("_auto", "T1", "plan", "t9", "--exec", "mac-mini/t9@1"))
        self.assertIn("stale execution", self.ok("_auto", "T1", "plan", "t9"))
        self.assertEqual(self.task(), before)

    def test_post_without_exec_matches_terminal(self):  # today's bash agent-msg passes no --exec
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        self.auto("plan", "t1", exec_=False)
        self.assertEqual(self.task()["status"], "in_progress")
        self.assertIn("task.started", self.ev.types())

    def test_unresolved_exec_adopts_first_post(self):
        tasks.fleet = None
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        tasks.fleet = self.fl
        self.auto("plan", "t1")
        self.assertEqual(self.task()["executions"][0]["id"], W("t1"))

    def test_assign_same_exec_noop_other_refused_replace_supersedes(self):
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        rev = self.task()["rev"]
        self.assertIn("already assigned", self.ok("assign", "T1", "--coord", "claude", "--agent", "codex",
                                                  "--terminal", "t1"))
        self.assertEqual(self.task()["rev"], rev)
        self.assertIn("--replace", self.no("assign", "T1", "--coord", "claude", "--agent", "claude", "--terminal", "t5"))
        self.ok("assign", "T1", "--coord", "claude", "--agent", "claude", "--terminal", "t5", "--replace")
        ex = self.task()["executions"]
        self.assertEqual([(e["outcome"], e["ended"] is None) for e in ex], [("superseded", False), (None, True)])
        self.assertEqual(self.task()["agent"], {"cli": "claude", "model": "", "terminal": "t5"})
        self.assertIn("stale execution", self.auto("plan", "t1"))
        handoff = [e for e in self.ev.stored if e["event_type"] == "handoff.created"]
        self.assertEqual(handoff[0]["metadata"]["kind"], "context")

    def test_blocker(self):
        self.worked()
        self.ok("status", "T1", "in_progress", "--coord", "claude")
        self.auto("blocker", "t1", "--text", "db down")
        self.assertEqual(self.task()["status"], "blocked")
        self.assertIn("blocked: db down", self.ok("attention", "--coord", "claude"))

    def test_exec_ended_without_done(self):
        self.ok("new", "work", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        self.auto("plan", "t1")
        self.ok("_exec-ended", "T1", "--exec", W("t1"), "--reason", "exit:1")
        t = self.task()
        self.assertEqual((t["status"], t["gates"]["execution"], t["executions"][0]["outcome"]),
                         ("blocked", "failed", "failed"))
        rev = t["rev"]
        self.ok("_exec-ended", "T1", "--exec", W("t1"), "--reason", "exit:1")
        self.ok("_exec-ended", "T9", "--exec", W("t1"))
        self.assertEqual(self.task()["rev"], rev)
        self.assertIn("execution ended without done", self.ok("attention", "--coord", "claude"))

    def test_exec_ended_after_done(self):
        self.worked()
        self.ok("_exec-ended", "T1", "--exec", W("t1"), "--reason", "cli-exited")
        t = self.task()
        self.assertEqual((t["status"], t["gates"]["execution"], t["executions"][0]["outcome"]), ("review", "done", "done"))

    def test_tests_callback(self):
        self.worked()
        self.ok("_tests", "T1", "passed", "--exec", W("t1"), "--rev", "r1", "--command", "make test")
        self.assertEqual(self.task()["gates"]["tests"], "passed")
        tc = [e for e in self.ev.stored if e["event_type"] == "test.completed"]
        self.assertEqual((len(tc), tc[0]["metadata"]["verified"], tc[0]["source"]), (1, True, "agent-msg"))
        self.assertIn("stale", self.ok("_tests", "T1", "failed", "--exec", "mac-mini/t9@1", "--rev", "r1"))
        self.assertEqual(self.task()["gates"]["tests"], "passed")


class Reviews(Base):
    def test_changes_requested_lands_in_real_timeline(self):
        from contextlib import closing
        from agentbus import events, store
        from unittest.mock import patch
        with patch.object(tasks, "events", events):
            self.worked()
            self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
            self.auto("review", "t2", "--verdict", "changes_requested",
                      "--finding", "major|feature.py:1|broken|fix it")
            with closing(store.connect()) as conn:
                rows = [e for e in store.query_events(conn, project_id="demo", task_id="T1")
                        if e["event_type"] == "review.changes_requested"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["metadata"], {"revision": "r1", "findings": {
                "blocking": 0, "major": 1, "minor": 0, "info": 0}})

    def test_author_cannot_review_own_work(self):
        self.worked()
        self.assertIn("wrote this task", self.no("review-assign", "T1", "--coord", "claude", "--reviewer", "t1"))
        self.assertIn("wrote this task", self.no("set", "T1", "--coord", "claude", "--reviewer", "t1"))
        self.ok("set", "T1", "--coord", "claude", "--reviewer", "t2", "--reviewer-agent", "claude")
        r = self.task()["reviews"][0]
        self.assertEqual((r["reviewer"], r["author"], r["revision"]), ("mac-mini/t2", "mac-mini/t1", "r1"))

    def test_only_assigned_reviewer_records(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.assertIn("stale", self.auto("review", "t3", "--verdict", "approved"))
        self.assertIn("stale", self.auto("review", "t1", "--verdict", "approved"))
        self.assertIn("no verdict", self.auto("review", "t2", "--text", "looks fine"))
        self.ok("_exec-ended", "T1", "--exec", W("t2"), "--reason", "killed")  # exiting never records a review
        self.assertEqual(self.task()["gates"]["review"], "pending")

    def test_changes_requested_and_findings(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--verdict", "changes_requested",
                  "--finding", "major|lib/a.py:2|off by one|fix the bound", "--finding", "info|x|nit|")
        t = self.task()
        self.assertEqual((t["status"], t["gates"]["review"]), ("in_progress", "changes_requested"))
        self.assertEqual([f["id"] for f in t["reviews"][0]["findings"]], ["F1", "F2"])
        self.assertIn("review.changes_requested", self.ev.types())
        self.assertIn("changes requested", self.ok("attention", "--coord", "claude"))
        self.auto("done", "t1", "--rev", "r2")
        self.assertEqual((self.task()["status"], self.task()["gates"]["review"]), ("review", "pending"))
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.assertEqual(self.task()["reviews"][1]["revision"], "r2")
        self.auto("review", "t2", "--verdict", "approved")
        self.ok("gate", "T1", "tests", "passed", "--coord", "claude", "--evidence", "x")
        self.ok("gate", "T1", "integration", "n/a", "--coord", "claude", "--reason", "x")
        err = self.no("status", "T1", "completed", "--coord", "claude")
        self.assertIn("finding F1 (major) is open", err)
        self.assertNotIn("F2", err)
        self.no("finding", "T1", "F1", "resolve", "--coord", "claude", rc=2)
        self.no("finding", "T1", "F9", "resolve", "--coord", "claude", "--reason", "x")
        self.ok("finding", "T1", "F1", "resolve", "--coord", "claude", "--reason", "fixed in r2")
        self.ok("status", "T1", "completed", "--coord", "claude")

    def test_blocked_verdict(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--verdict", "blocked", "--finding", "blocking|a|unsafe|stop")
        self.assertEqual((self.task()["status"], self.task()["gates"]["review"]), ("blocked", "blocked"))
        self.assertIn("open blocking finding F1", self.ok("attention", "--coord", "claude"))

    def test_legacy_verdict_text_parsed(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--text", "All good.\nVERDICT: approve", exec_=False)
        r = self.task()["reviews"][0]
        self.assertEqual((r["verdict"], r.get("parsed"), self.task()["gates"]["review"]), ("approved", True, "approved"))

    def test_rereview_after_new_revision_and_carry(self):
        self.approved()
        self.assertIn("already", self.auto("done", "t1", "--rev", "r1"))
        self.assertEqual(self.task()["gates"]["review"], "approved")
        self.auto("done", "t1", "--rev", "r2")
        self.assertEqual((self.task()["status"], self.task()["gates"]["review"]), ("review", "pending"))
        self.assertIn("waiting for a review of r2", self.ok("attention", "--coord", "claude"))
        self.no("gate", "T1", "review", "approved", "--coord", "claude", "--carry", rc=1)
        self.ok("gate", "T1", "review", "approved", "--coord", "claude", "--carry", "--reason", "typo only")
        self.assertEqual(self.task()["gates"]["review"], "approved")

    def test_review_of_old_revision_does_not_set_gate(self):
        self.worked()
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("done", "t1", "--rev", "r2")
        self.auto("review", "t2", "--verdict", "approved")
        self.assertEqual(self.task()["gates"]["review"], "pending")


def _claim(coord, barrier, q):
    barrier.wait()
    rc = tasks.main(["claim", "T1", "--coord", coord, "--recover", "--reason", "race", "--project", "demo"],
                    out=io.StringIO(), err=io.StringIO())
    q.put((coord, rc))


class Ownership(Base):
    def expire(self):
        self.patch(lease_until=0)

    def test_plain_claim_refused_recovery_rules(self):
        self.ok("new", "x", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        self.no("claim", "T1", "--coord", "codex")
        self.no("claim", "T1", "--coord", "codex", "--recover", rc=2)
        self.assertIn("still active", self.no("claim", "T1", "--coord", "codex", "--recover", "--reason", "x"))
        self.expire()
        self.fl.live = [{"execution_id": W("t1"), "task_id": "T1"}]
        self.assertIn("live execution", self.no("claim", "T1", "--coord", "codex", "--recover", "--reason", "x"))
        self.no("claim", "T1", "--coord", "codex")
        self.fl.live = []
        self.assertIn("recoverable", self.ok("attention", "--coord", "codex"))
        self.ok("claim", "T1", "--coord", "codex", "--recover", "--reason", "claude gone")
        self.assertEqual(self.task()["coordinator"], "codex")
        ev = [e for e in self.ev.stored if e["event_type"] == "handoff.completed"][0]
        self.assertEqual(ev["metadata"]["kind"], "recovery")

    def test_recovery_without_fleet_counts_unended_executions(self):
        self.ok("new", "x", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t1")
        self.expire()
        tasks.fleet = None
        self.no("claim", "T1", "--coord", "codex", "--recover", "--reason", "x")

    def test_concurrent_recovery_exactly_one_wins(self):
        self.ok("new", "x", "--coord", "claude")
        for round_ in range(3):
            self.patch(lease_until=0, coordinator="claude")
            ctx = multiprocessing.get_context("fork")
            barrier, q = ctx.Barrier(2), ctx.Queue()
            ps = [ctx.Process(target=_claim, args=(c, barrier, q)) for c in ("codex", "other")]
            for p in ps:
                p.start()
            for p in ps:
                p.join(30)
            results = dict(q.get(timeout=5) for _ in ps)
            winners = [c for c, rc in results.items() if rc == 0]
            self.assertEqual(len(winners), 1, results)
            self.assertEqual(self.task()["coordinator"], winners[0])

    def test_handoff_two_phase(self):
        self.ok("new", "x", "--coord", "claude")
        self.no("handoff", "T1", "--coord", "codex", "--to", "claude")
        self.assertIn("must run: agent-task accept", self.ok("handoff", "T1", "--coord", "claude", "--to", "codex"))
        self.assertEqual((self.task()["coordinator"], self.task()["handoff_to"]), ("claude", "codex"))
        self.assertIn("handoff offered", self.ok("attention", "--coord", "codex"))
        self.no("accept", "T1", "--coord", "other")
        self.ok("handoff", "T1", "--coord", "claude", "--cancel")
        self.no("accept", "T1", "--coord", "codex")
        self.ok("handoff", "T1", "--coord", "claude", "--to", "codex")
        self.ok("accept", "T1", "--coord", "codex")
        self.assertEqual((self.task()["coordinator"], self.task()["handoff_to"]), ("codex", None))
        self.assertIn("already yours", self.ok("accept", "T1", "--coord", "codex"))
        kinds = [e["event_type"] for e in self.ev.stored if e["event_type"].startswith("handoff.")]
        self.assertEqual(kinds, ["handoff.created", "handoff.created", "handoff.completed"])

    def test_fleet_and_brief_read_connections_are_closed(self):
        from types import SimpleNamespace
        self.ok("new", "work", "--coord", "claude")
        before = tasks.store.closed
        tasks.live_executions(SimpleNamespace(project="demo"), self.task())
        self.assertEqual(tasks.store.closed, before + 1)
        self.ok("brief", "T1")
        self.assertEqual(tasks.store.closed, before + 2)

    def test_renew_heartbeats_coordinator(self):
        self.ok("new", "x", "--coord", "claude")
        self.patch(lease_until=0)
        self.ok("renew", "--coord", "claude")
        self.assertGreater(self.task()["lease_until"], 0)
        self.assertEqual(self.fl.upserts[0]["execution_id"], "macbook/claude")


class Views(Base):
    def post_board(self, **m):
        path = os.path.join(self.dir, "demo.jsonl")
        n = len(tasks.read_board("demo")) + 1
        with open(path, "a") as fh:
            fh.write(json.dumps({"id": n, "ts": "2026-11-10T01:00:00+03:00", "from": "t1", "as": "", "to": "all",
                                 "re": "", **m}) + "\n")
        return n

    def test_brief_has_all_sections_and_brief_rev(self):
        self.ok("new", "dep", "--coord", "claude")
        self.ok("new", "work", "--coord", "claude", "--deps", "T1", "--issue", "7")
        self.post_board(kind="finding", task="T1", text="CONTRACT: f(x) returns y")
        self.post_board(kind="decision", text="use sqlite")
        q = self.post_board(kind="question", task="T2", text="which db?")
        self.post_board(kind="finding", task="T2", text="x" * 30000)
        self.ok("set", "T2", "--coord", "claude", "--requirements", "do it", "--remaining", "all",
                "--deliverables", "a PR")
        md = self.ok("brief", "T2")
        heads = [line for line in md.splitlines() if line.startswith("## ")]
        self.assertEqual([h.split(". ", 1)[1] for h in heads], [t for _, t in tasks.TITLES])
        for s in ("CONTRACT: f(x) returns y", "use sqlite", "which db?", "do it", "a PR", "Issue #7"):
            self.assertIn(s, md)
        self.assertLessEqual(len(md.encode()), tasks.BRIEF_LIMIT)
        j = json.loads(self.ok("brief", "T2", "--json"))
        self.assertEqual(list(j), [k for k, _ in tasks.TITLES])
        self.assertEqual(j["blockers"]["open"][0]["id"], q)
        t = self.task("T2")
        self.assertEqual(t["brief_rev"], t["rev"])

    def test_brief_truncates_long_posts(self):
        self.ok("new", "work", "--coord", "claude")
        for _ in range(30):
            self.post_board(kind="finding", task="T1", text="CONTRACT: " + "y" * 2000)
        md = self.ok("brief", "T1")
        self.assertLessEqual(len(md.encode()), tasks.BRIEF_LIMIT)
        self.assertIn("truncated", md)

    def test_show_ls_json_and_board_posts(self):
        tasks.AGENT_MSG = self.saved_msg
        self.ok("new", "work", "--coord", "claude")
        shown = json.loads(self.ok("show", "T1", "--json"))
        self.assertEqual(shown["task"]["id"], "T1")
        self.assertEqual(shown["messages"][0]["kind"], "task")  # agent-task posted to the board
        self.assertEqual(json.loads(self.ok("ls", "--json"))[0]["v"], 2)
        out = self.ok("show", "T1")
        self.assertIn("--- board messages for T1", out)
        self.assertIn("[task]: T1 created by claude: work", out)

    def test_attention_scoped_to_coordinator(self):
        self.worked()
        self.assertIn("waiting for a review", self.ok("attention", "--coord", "claude"))
        self.assertEqual(self.ok("attention", "--coord", "codex"), "")
        items = json.loads(self.ok("attention", "--coord", "claude", "--json"))
        self.assertEqual(items[0]["task"], "T1")



class ReviewFixes(Base):
    """Regression tests for the sequences reproduced in review #49."""

    def test_invalid_registry_refuses_every_mutation_and_keeps_bytes(self):
        self.ok("new", "keep me", "--coord", "claude")
        path = os.path.join(self.dir, "demo.tasks.json")
        broken = b'{"next":2,"tasks":'
        with open(path, "wb") as fh:
            fh.write(broken)
        for argv in (("new", "x", "--coord", "claude"), ("status", "T1", "blocked", "--coord", "claude"),
                     ("claim", "T1", "--coord", "claude"), ("renew", "--coord", "claude"),
                     ("_auto", "T1", "plan", "t1"), ("_exec-ended", "T1", "--exec", W("t1")),
                     ("brief", "T1"), ("ls",), ("show", "T1"), ("attention",)):
            with self.subTest(argv=argv):
                self.assertIn("not valid JSON", self.no(*argv))
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), broken)
        with open(path, "w") as fh:
            fh.write('[1, 2]')
        self.assertIn("not a task registry", self.no("new", "x", "--coord", "claude"))
        with open(path, "w") as fh:
            fh.write("")
        self.assertEqual(self.ok("new", "fresh", "--coord", "claude").strip(), "T1")  # empty file: new registry

    def test_delayed_review_of_old_revision_after_reassign(self):
        self.worked("r1")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("done", "t1", "--rev", "r2")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--rev", "r1", "--verdict", "approved")
        t = self.task()
        self.assertEqual(t["gates"]["review"], "pending")
        self.assertEqual([(r["revision"], r["verdict"]) for r in t["reviews"]], [("r1", "approved"), ("r2", None)])
        self.auto("review", "t2", "--rev", "r2", "--verdict", "approved")
        self.assertEqual(self.task()["gates"]["review"], "approved")

    def test_reviewer_who_becomes_worker_cannot_approve(self):
        self.worked("r1")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.ok("status", "T1", "in_progress", "--coord", "claude")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "claude", "--terminal", "t2", "--replace")
        self.auto("done", "t2", "--rev", "r1")
        self.assertIn("stale", self.auto("review", "t2", "--verdict", "approved"))
        self.assertEqual(self.task()["gates"]["review"], "pending")
        rex = [e for e in self.task()["executions"] if e["role"] == "reviewer"][0]
        self.assertEqual(rex["outcome"], "superseded")

    def test_review_from_an_author_is_ignored(self):  # defence in depth: reviewer row still open
        self.worked("r1")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        s = self.state()
        s["tasks"]["T1"]["executions"].append({"id": "mac-mini/t2@7", "agent_id": "mac-mini/t2", "role": "worker",
                                               "terminal": "t2", "ended": "x", "outcome": "superseded"})
        with open(os.path.join(self.dir, "demo.tasks.json"), "w") as fh:
            json.dump(s, fh)
        self.assertIn("author", self.auto("review", "t2", "--verdict", "approved"))
        self.assertEqual(self.task()["gates"]["review"], "pending")

    def test_restarted_reviewer_can_be_reassigned(self):
        self.worked("r1")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.fl.created["t2"] = 2  # same terminal, new CLI launch
        self.assertNotIn("already", self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2"))
        self.ok("_auto", "T1", "review", "t2", "--exec", "mac-mini/t2@2", "--verdict", "approved")
        self.assertEqual(self.task()["gates"]["review"], "approved")
        self.assertIn("stale", self.ok("_auto", "T1", "review", "t2", "--exec", "mac-mini/t2@1", "--verdict", "blocked"))

    def test_replace_worker_after_it_exited_in_review(self):
        self.worked("r1")
        self.ok("_exec-ended", "T1", "--exec", W("t1"), "--reason", "cli-exited")
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        self.auto("review", "t2", "--verdict", "changes_requested")
        self.assertEqual(self.task()["status"], "in_progress")
        self.ok("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t3")
        self.assertEqual(self.task()["status"], "in_progress")
        self.auto("done", "t3", "--rev", "r2")
        self.assertEqual((self.task()["status"], self.task()["gates"]["execution"]), ("review", "done"))
        self.assertIn("--replace", self.no("assign", "T1", "--coord", "claude", "--agent", "codex", "--terminal", "t4"))

    def test_large_brief_json_stays_valid(self):
        self.worked("r1")
        self.ok("set", "T1", "--coord", "claude", "--requirements", "q" * 15000, "--remaining", "r" * 9000)
        self.ok("review-assign", "T1", "--coord", "claude", "--reviewer", "t2")
        findings = [x for i in range(200) for x in ("--finding", f"major|f{i}.py|{'d' * 300}|fix")]
        self.auto("review", "t2", "--verdict", "changes_requested", *findings)
        for argv in (("brief", "T1", "--json"), ("brief", "T1")):
            out = self.ok(*argv)
            self.assertLessEqual(len(out.encode()), tasks.BRIEF_LIMIT, argv)
            self.assertIn("truncated", out)
        j = json.loads(self.ok("brief", "T1", "--json"))
        self.assertEqual(list(j), [k for k, _ in tasks.TITLES])
        self.assertTrue(j["requirements"]["text"].startswith("qqq"))

    def test_show_missing_task_exits_5_like_v1(self):
        self.ok("new", "x", "--coord", "claude")
        self.no("show", "T9", rc=5)


if __name__ == "__main__":
    unittest.main()
