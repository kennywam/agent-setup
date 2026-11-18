---
name: delegate
description: Hand project work to an agent on the Mac mini and run it to a merged-ready PR - task contract, plan gate, reviewer agent, verify gate. Use whenever you (the MacBook coordinator) start, review or finish work for a project in the Mini table of ~/projects/CLAUDE.md, or pick up ready-for-agent issues.
---

<!-- Template from agent-bus. Install as ~/.claude/skills/delegate/SKILL.md on the MacBook; ./macbook/coord-push copies it to the Mini. Placeholders: <project>, <repo>, <n>, <T>. -->

# Delegate to the Mini

You are the coordinator: `claude` if you are Claude Code, `codex` if you are Codex. Both can run at once and share one task registry, but each works only with agents of its own CLI (CLAUDE.md "Claude and Codex"). You don't write project code; you write the contract, approve the plan, get it reviewed, and report. CLAUDE.md ("How to hand off", "Talking with agents") has the mechanics; this skill is the loop.

## 1. Before starting anyone

- Registry first: `agent-task ls --project <p>`. If the issue already has an open task owned by the other
  coordinator, leave it alone and tell the user. Otherwise
  `agent-task new "<objective>" --coord <you> --issue <n> --files <paths>`, or `claim` a stale one.
- Agent and shape: use your own CLI (`claude` if you are Claude Code, `codex` if you are Codex) and decide whether to
  split the work (CLAUDE.md "Splitting a feature"). For a split, create the parent task and then subtasks with `--parent`,
  `--deps` and disjoint `--files`.
- Slots: 5 agents on the Mini (reviewers included) and 2 working agents on the MacBook besides
  the coordinator, for work that needs no Docker or full build. Defaults, not ceilings: check
  CPU and RAM first and go above them when there is room, below when loaded (CLAUDE.md
  "Agent limits"). Count the Mini's with
  `~/.local/bin/herdr-app ls | jq '[.spaces[]|select(.machine=="mac-mini")|.tabs[]|select(.name!="1")]|length'`
  and check which are still working (`hc get <t>`). Close finished tabs first (CLAUDE.md step 7).
- Read the issue (`hc exec --on mac-mini -- bash -lc 'gh issue view <n> -R <repo> --comments'`) and the board's standing decisions
  (`agent-msg read --kind decision --project <p>`). Skip issues that depend on an open one.
- Queue: when a slot frees, take the next open `ready-for-agent` issue (oldest first) in the project the user is working on. Don't start a new project's queue unasked.
- Before picking an issue, look for PRs that already deliver it: `gh pr list --state all --search "<n> in:body,title"`.
  Teams often merge with `Refs #<n>` and close after release, so an open `ready-for-agent` issue may already be done on
  `dev` (seen 2026-11-05 on several issues). Skip those and list them for the user.

## 2. Task contract (paste into `--task`, filled in)

```
Issue: <repo>#<n> <title> - read it with gh issue view <n> --comments.
Task: <T> (AGENT_TASK is set, so your board posts link to it). Run `agent-task show <T>` first for its context<, and
`agent-task show <dep>` for the tasks you build on: their plans, findings and decisions are the spec you extend>.
You own the technical decisions inside this scope; ask only about scope, product, or risky or irreversible steps.
Post contracts, dependencies, risks, test results and the reasons for key choices as `agent-msg post finding`,
written so another agent can continue or review your work from them alone.
Done means: <acceptance criteria from the issue, as checkable bullets>.
In scope: <files/areas>. Out of scope: <what not to touch, incl. other agents' claims>.
Base branch: <dev | develop | main>.
Constraints from the user: <any, or "none">.

How we work:
- Worktree: you were started in the shared checkout; first run
  git worktree add ~/projects/<project>-<n> -b <branch> origin/<base> and work only there.
  Other agents work in this repo; don't touch their branches or worktrees or the shared checkout's branch.
- Start with `agent-msg read --last 20` and `agent-msg read --kind decision`.
- PLAN GATE <on|off>: if on, after exploring post `agent-msg post plan "<files to touch, approach, how you'll test>"`
  and end your turn. Don't edit files until the coordinator approves.
- Post a claim before editing files other agents might change; a notice when you change something shared.
- question = needs a decision; ask-user = needs the human; blocker = can't continue. After posting any of these, end your turn and wait.
- Servers take turns on this machine: before starting a Supabase/Docker stack, a dev or production server, Playwright or
  a seeded test environment, run `server-turn acquire "<project> <what>"` (exit 3 = busy) or `server-turn wait "<label>"` in the
  background. When done, stop your servers by the PIDs you started and run `server-turn release`. verify needs no turn.
- <project> e2e: run Playwright in the shared ~/projects/<project>-e2e worktree via `e2e-worktree use <project> <branch>`
  (after taking a server turn), with the stack ids in the board decision; never in your own worktree, never next dev.
- Finish: `agent-msg verify` (runs this project's checks, can take 10+ minutes: run it in the background or with a long timeout)
  must pass on your final commit before `agent-msg post done` is accepted. Then push, open the PR, post done with the PR link,
  what the reviewer must know, and anything you couldn't verify.
- Before done, check your branch has only your commits: git log --oneline origin/<base>..HEAD.
- Keep your worktree until the coordinator says the PR is final: review fixes may come.
```

Plan gate: on unless the change is clearly small (about 3 files or fewer, no shared files, no schema/API change).

## 3. Answering the board

`coord-watch` wakes you. Handle by kind:

- `plan`: check it against the issue's "done means" and the claims of other agents. Reply `Approved` or the specific corrections, by `hc send` and `agent-msg post answer "<text>" --re <id> --to <terminal> --project <p>`.
- `question`: answer it if the issue, docs, rules or a decision settles it; otherwise relay to the user (CLAUDE.md routing table). If the answer is a rule other agents will need, also post it as `agent-msg post decision "<rule>" --project <p>`.
- `stopped`: the agent ended a turn without posting. Read its last words (in the message) and its screen (`hc read <t> --lines 60`). Nudge it with what to do next, or treat it as a question.
- `CHECK-IN` from the watcher (60 min without events): read the summary. An agent with many minutes and no plan/done is probably looping: `hc read` it, then redirect or interrupt (`hc interrupt <t>`).
- `done`: go to step 4.

## 4. Review every PR with a fresh agent

Don't review the diff yourself first. Start a reviewer in the same project space (it takes a slot), on your own
CLI, a fresh agent that didn't write the PR (CLAUDE.md "Review"). Launch it with
`AGENT_TASK=<T>` and record it: `agent-task set <T> --coord <you> --reviewer <t> --reviewer-agent <cli>`, which refuses the author.

```
Review <repo> PR #<pr> for issue #<n> (task <T>). You are read-only: don't push, don't edit, don't comment on GitHub.
Run `agent-task show <T>` for the author's plan, findings and decisions.
Check out with: git worktree add ~/projects/<project>-review-<pr> origin/<head-branch> (gh pr view <pr> --json headRefName).
Claude: run /code-review:code-review <pr> for high-signal bugs. Codex: review `git diff origin/<base>...HEAD` yourself
for the same. Look for correctness, regressions, security, performance and maintainability. Then check the spec yourself:
every "done means" bullet of the issue is met, nothing out of scope changed, and tests cover the new behaviour. Ignore style nits.
Post `agent-msg post review "<BLOCKING: ... / NON-BLOCKING: ... / VERDICT: approve|changes>"` (one message, concise,
file:line for each finding), then remove your worktree and end your turn.
```

Then:
- `changes`: `agent-task status <T> in_progress --coord <you>`, then send the blocking findings to the author (`hc send <author> "Review of PR #<pr>: <findings>. Fix, run agent-msg verify, push, post done again."`). Re-review only what changed, by the same reviewer (`hc send` it), at most 2 rounds; after that, ask the user.
- `approve`: check CI (`gh pr checks <pr>`). Merge only where the user pre-approved it (CLAUDE.md "Merging"); otherwise report it as ready.
  Then `agent-task set <T> --coord <you> --pr <url> --verify "<CI result>"` and `agent-task status <T> completed --coord <you>`.
- The models disagree on architecture: decide it if the issue, docs or decisions settle it; otherwise ask the user, giving both
  views and your recommendation.
- Close the reviewer's tab when done. Close the author's tab once the PR is merged or the user has it.

## Fleet view and metrics

- `agent-metrics [--gh] [project]` on the Mini: per agent issue, state, waiting-on, PR + CI, review rounds, questions, minutes; per project totals. `--json` for scripts. Use it for status reports instead of reading whole boards.
- The user's Herdr window has the `fleet` mod (Cmd-Shift-F): the same data, live. Rows are clickable to focus an agent. Keep issue numbers (`#123`) and PR links in board posts, or the view can't fill them in.

## Agent teams (trial)

Claude Code agent teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) let one Mini agent lead teammates in its own session. Use only for a single issue that splits into independent parts, and count the team as one slot plus one per teammate. Start it with `--argv '["env","CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1","claude"]'` and tell the lead in the task: at most 2 teammates, each in its own worktree, only the lead posts to the board. The Stop hook already ignores teammates.

## 5. After each merged PR

One line in the retro: what went wrong or what the agent had to ask. If it will recur, post it as a `decision` on the board, or propose a CLAUDE.md/AGENTS.md line for the repo to the user. Report to the user: PR, what was verified, review rounds, open questions.
