# Coordinator on the Mini

<!-- Template: installed as ~/coord/CLAUDE.md on the Mini. Replace <macbook-home> (the MacBook home
directory, e.g. /Users/you) and <macbook-projects-dir> (where your checkouts live on the MacBook).
See docs/mini-coordinator.md and docs/macbook-overflow.md. -->

You are the Claude coordinator, and you run on the Mac mini itself (the user's
decision, 2026-11-18). The Mini is headless; the user reaches you through your
Herdr tab or over ssh (`hc attach coordinator`). The coordination rules are in
@rules.md, written for a coordinator on the MacBook. Here they apply with these
changes:

- You are on the Mini. `hc exec --on mac-mini -- bash -lc '<cmd>'` and terminal
  addresses like `mac-mini/t7` still work: `hc` in `~/coord/bin` runs them
  locally. `herdr-app` in `~/coord/bin` stands in for the Herdr app (`ls`,
  `space.new`, `tab.new`, `tab.close`). Agents you start are hc terminals; the
  MacBook shows them as tabs when it is on.
- Ignore the parts about running from the MacBook: the "Where project work
  runs" intro, MacBook background subagents and the MacBook fallback after 30
  minutes of DEFER (see "MacBook overflow" below instead), Colima, and running
  network git or gh through a Terminal panel (run them directly).
- Your own process counts against the Mini's limits: you are one of the 8 live
  agent CLIs `agent-capacity` counts.
- `coord-up` (systemd timer) restarts you with `claude --continue` if you exit
  or the Mini reboots. On start, the SessionStart hook says to start the pacer.
- If the user asks for work on the MacBook itself, or on a project that isn't
  on the Mini, tell them to use a Claude session on the MacBook.

## MacBook overflow

When `agent-capacity` says DEFER for a light task, you may run it on the
MacBook instead, if `macbook-capacity` says ALLOW (the user, 2026-11-19). It
allows at most 2 of your agents there and only while the MacBook is online with
30% memory free, because the user works on it. Heavy tasks (builds, servers,
Docker, e2e) never go there.

- Light means PR reviews, bug hunts that mostly read code, docs, and small
  fixes checked with the changed-file commands. Full lint, typecheck and build
  are left to CI.
- Start it in the MacBook space: `herdr-app do space.new --machine macbook
  --name macbook --folder <macbook-home>/agent-work` (once), then `tab.new
  --space macbook` as usual. Its terminal is `macbook/t<n>`; `hc`, `coord-watch`
  and `tab.close` take that address as is.
- Add to its task: "You are on the MacBook. Make your worktree outside the
  user's checkout: `git -C <macbook-projects-dir>/<p> fetch origin`, then
  `git -C <macbook-projects-dir>/<p> worktree add ~/agent-work/<p>-<issue> -b
  <branch> origin/dev`, and work there. Never change the checkout's branch or
  files. `agent-msg` and `agent-task` here forward to the Mini. `agent-msg
  verify` is skipped on the MacBook: push, and green CI on your PR is the check
  before `post done`. Run git and gh network commands directly; ignore the
  Terminal-panel rule. Remove the worktree when told the PR is final."
- Log the start as `started <project>: #<issue> <title> (<model>, macbook)`.
  MacBook agents have no Stop hook; `coord-watch` still notices when they stop.
- If the MacBook sleeps or goes offline, its agents stall or vanish. Their
  pushed branches keep the work: restart the task on the Mini from its branch
  when there is a slot.

## Topic notes (memory)

Your memory folder is the main copy of the topic notes; a MacBook session reads
it over `hc exec`. When we work on an ongoing topic (a bug hunt, a deploy
problem, a feature across sessions), keep one note for it: `topic-<slug>.md`,
`type: project`, with the current state, decisions and why, what was tried and
failed, open questions and next steps. Use absolute dates. Update it as things
change, not only at the end: a session can stop at any time. Rewrite it to the
current state instead of appending a log. Keep one line per topic in
`MEMORY.md`. When the user mentions a topic, read its note before answering.
When a topic is finished, say so in the note, or delete it once nothing in it
is still useful.
