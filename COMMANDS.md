# Coordinator commands

Cheat sheet for the coordinator setup. The coordinator runs on the Mini; the full
rules are in your rules file (template: `docs/templates/coordination-rules.md`,
"Operating modes"). Background: [docs/modes.md](docs/modes.md),
[docs/mini-coordinator.md](docs/mini-coordinator.md),
[docs/macbook-overflow.md](docs/macbook-overflow.md).

## Modes: type these yourself, no Claude needed

From any MacBook terminal, or over ssh on the Mini (phone too).

| Command | What it does |
|---|---|
| `coord status` | Mode, the coordinator, and each agent with its state and tab |
| `coord interactive` | Switch to interactive |
| `coord autonomous` | Switch to autonomous |
| `coord suspend` | Agents stand down after their current step; nothing new starts |
| `coord suspend now` | The same, but interrupt them mid-step (Esc) |
| `coord resume` | Lift the suspension and return to the previous mode |
| `coord resume interactive` / `coord resume autonomous` | Lift it and switch to that mode |
| `coord off` | Let working agents finish their current task, close each one as it finishes, then stop the coordinator; nothing starts again (not the timer, not a reboot) |
| `coord off now` | Stop all agents and the coordinator at once (uncommitted work stays in their worktrees) |
| `coord on` | Start the coordinator again and lift the suspension; while `coord off` is still waiting, cancel it |

## Mode phrases in the coordinator's chat

Only the whole first line counts, alone or followed by punctuation
("take over the login bug" is a task, not a switch).

| Mode | Phrases |
|---|---|
| Interactive | `interactive`, `switch to interactive`, `I'm back`, `I'm available`, `let's collaborate` |
| Autonomous | `autonomous`, `switch to autonomous`, `I'm away`, `I'm going to sleep`, `goodnight`, `take over` |
| Suspend | `suspend`, `suspended`, `pause all`, `stand down`, `stop agents`, `switch to suspended` (add `now` to interrupt) |
| Resume | `resume`, `unsuspend`, `continue agents`, `lift suspension`, optionally followed by `interactive` or `autonomous` |

After 45 minutes without a message the coordinator switches to autonomous by
itself; your next message switches it back, with a return briefing first.

## Reaching the coordinator

| Command | What it does |
|---|---|
| `attach coord` | A shell alias you define (`ssh -t mac-mini hc attach coordinator`); `ctrl+\` detaches and leaves it running |
| Herdr, `coordinator` space | The same in a tab; closing the tab doesn't stop it |
| `ssh mac-mini`, then `hc attach coordinator` | The manual way |

## Watching the agents

| Command | What it does |
|---|---|
| `hc ls` | Every terminal on both machines |
| `hc read mac-mini/t160 --lines 40` | An agent's recent screen |
| `hc attach mac-mini/t160` | Open an agent's terminal |
| `hc exec --on mac-mini -- bash -lc 'agent-msg read --project project-a --last 20'` | A project's board |
| `hc exec --on mac-mini -- bash -lc 'agent-task ls --project project-a'` | A project's task registry |
| `hc exec --on mac-mini -- bash -lc 'agent-metrics'` | Each agent's PR, CI, review rounds and questions |
| `hc exec --on mac-mini -- bash -lc 'agent-capacity --class light'` | Would the Mini allow another agent now |
| Herdr `fleet` mod (Cmd-Shift-F) | Live view of all agents |

## Coordinator service on the Mini (after `ssh mac-mini`)

`coord off` / `coord on` cover the usual cases. By hand:

| Command | What it does |
|---|---|
| `systemctl --user start coord-up.service` | Start the coordinator now (does nothing if it runs, or while it's off) |
| `systemctl --user stop coord-up.timer` / `start coord-up.timer` | Turn the 2-minute auto-restart off or on |
| `hc kill coordinator` | End the coordinator; the timer restarts it unless stopped or `coord off` |

## Maintenance (on the MacBook)

| Command | What it does |
|---|---|
| `./macbook/coord-push` (in the agent-bus checkout) | Install this checkout's Mini side plus your rules and delegate skill on the Mini; run after editing them or pulling |
| `coord-tabs` | Give coordinator-started agents Herdr tabs; launchd runs it every minute |
| `hc exec --on mac-mini -- cat ~/.claude/projects/<coord-dir-slug>/memory/MEMORY.md` | The topic notes index (the Mini has the main copy) |

## Used by the coordinator, not you

`coord-mode tick` (30-minute pacer), `coord-mode brief`, `coord-mode log`,
`coord-mode count`, `coord-mode due`, `coord-watch`, `macbook-capacity`, and
`herdr-app` on the Mini (stand-in for Herdr's tab commands).
