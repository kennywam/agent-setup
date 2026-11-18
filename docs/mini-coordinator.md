# The coordinator on the Mini

The Claude coordinator runs on the always-on Mini instead of the MacBook, so it keeps working while the
MacBook sleeps. It is an `hc` terminal named `coordinator` with its own home, `~/coord`, installed from
[`coordinator/`](../coordinator) by `install.sh mini` (or `./macbook/coord-push` from the MacBook).

| File (repo) | Installed as | Role |
|---|---|---|
| `coordinator/CLAUDE.md` | `~/coord/CLAUDE.md` | Preamble: how the MacBook-written rules apply on the Mini. Installed once, then yours to edit. |
| `coordinator/settings.json` | `~/coord/.claude/settings.json` | `coord-mode hook` on prompt and session start, the mode status line. Installed once. |
| `coordinator/bin/hc` | `~/coord/bin/hc` | Wrapper: the rules address the Mini as a remote (`hc exec --on mac-mini`, `mac-mini/t7`); here that is local, so the wrapper strips it. Other machines pass through. |
| `coordinator/bin/herdr-app` | `~/coord/bin/herdr-app` | Stand-in for the Herdr desktop app's `ls`, `space.new`, `tab.new`, `tab.close`: a space is a project folder (`~/coord/state/spaces.json`), a tab is one `hc` terminal. `tab.new` refuses while suspended or draining. |
| `coordinator/bin/coord-watch` | `~/coord/bin/coord-watch` | Bash watcher: reads the board locally and exits (waking the coordinator) on actionable posts, a stopped or vanished agent, or after 60 quiet minutes. Renews the coordinator's task leases. |
| `coordinator/bin/coord-up` | `~/coord/bin/coord-up` | Starts the coordinator (`claude --continue`) in its own Herdr space unless it runs or `~/.config/coord/off` exists. |
| `coordinator/bin/macbook-capacity` | `~/coord/bin/macbook-capacity` | ALLOW/DEFER for one more light agent on the MacBook ([macbook-overflow.md](macbook-overflow.md)). |
| `systemd/coord-up.{service,timer}` | `~/.config/systemd/user/` | Runs `coord-up` at boot and every 2 minutes. |
| `bin/coord-mode`, `bin/coord-statusline` | `~/.local/bin/` (+ `coord` symlink) | [Operating modes](modes.md). |

`~/coord/bin` comes first on the coordinator's `PATH` only (`coord-up` sets it), so the `hc` wrapper
never shadows the real `hc` for agents.

## Setup

1. On the Mini: `./install.sh mini`, then fill in the placeholders in `~/coord/CLAUDE.md`.
2. Put your rules at `~/coord/rules.md` and the delegate skill at `~/coord/.claude/skills/delegate/`
   (templates in [templates/](templates)), or run `./macbook/coord-push` from the MacBook, which does both.
3. `systemctl --user daemon-reload && systemctl --user enable --now coord-up.timer`
   (and `agent-probe.timer` / `agent-prune.timer` for the event store, see [SPEC.md](SPEC.md)).
4. Agents and the Mini's own CLIs need `agent-hook` as their Stop / UserPromptSubmit hook
   (`agent-hook turn-end` / `agent-hook turn-start`). The coordinator sets `COORD_NAME`, so the hook
   ignores it.

The coordinator counts against the Mini's own limits: it is one of the live agent CLIs `agent-capacity`
counts.

## Known gap: the bash coord-watch

`coordinator/bin/coord-watch` is a bash port of an earlier version of the MacBook's zsh watcher
(`macbook/coord-watch`). It does not have the v2 changes in [SPEC.md](SPEC.md) section 8 yet:

- It filters other coordinators' posts by task owner and agent terminal only, not by execution
  identity, so it neither hides stale executions of another owner's task nor marks stale posts on its own
  tasks with `[stale]`.
- It has no single-instance lock: two watchers for the same project and coordinator share one cursor,
  and one of them can miss a wake-up.

Run one watcher per project per coordinator until it is ported.
