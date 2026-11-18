# vigil: sessions page, joining from a phone, session reviews

Date: 2026-11-17
Status: draft, awaiting approval
Builds on: `2026-11-15-vigil-design.md` (v1 and v2 are built: index, web UI, resume, usage,
remote access, peers)

## Intent

Three things came out of using vigil:

1. **The sessions page is too long.** Every session of every project is a full table row, so
   the few sessions that matter right now are buried under months of idle ones.
2. **The author wants to pick up a session from their phone.** Remote access and peers already
   let the phone reach vigil, but Resume from a phone only starts the session and shows an
   attach command, which is no use on a phone. Tapping a session should land the author in it:
   in the Claude app if installed, else the browser.
3. **The "Never tried" list does not coach.** It lists every unused flag with no reason to care
   about any of them. The author wants to pick one session and have Claude review it: which
   flags, subcommands and features would have helped, and how to spend fewer tokens next time.
   Acting on a suggestion should take one tap.

The three parts ship in this order, one issue and PR each (part 3 may split further; see
"Left to the builder").

## Goals

- At a glance, on a phone: what is running now, and where. Everything quiet is one line per
  project.
- From a phone, one tap on a session gets the author into it through Claude's Remote Control.
- A session review that cites what actually happened in that session, with real token numbers,
  and turns each suggestion into an action: resume or start a session with suggested flags,
  make them the default, or apply a change to the project's `CLAUDE.md` or
  `.claude/settings.json`.

## Non-goals

- A terminal in the browser. Joining from a phone goes through Claude's own Remote Control.
- Automatic or scheduled reviews. A review runs only when the author presses the button.
- Reviewing many sessions at once, or trends across sessions. One session per review.
- Changing the "Never tried" list on the Usage page. It stays as is.

## Anti-goals (failure even if it works)

- vigil runs a flag, writes a file or starts a process because Claude's review said so, without
  the author pressing a button for that specific action.
- vigil writes anything under the Claude home (`~/.claude` or `claude_home`). Unchanged from v1.
- A review that edits, extends or persists into the session it reviews.
- A suggestion that widens what Claude may do unattended (skip permissions, run hooks, add MCP
  servers) applied with one tap. Those are copy-only.

## Rules that change (update CLAUDE.md "Global Constraints" in the PR that changes each)

| Rule today | After |
|---|---|
| Resume may only run `claude` (or `tmux`) against a session ID present in the index. | Also: start a **new** session, only in a folder that is the cwd of an indexed session (part 3); run `claude -p` to review an indexed session (part 3). |
| Extra flags are only those in `resume.default_flags`, allow-listed to five flags. | One allow-list in code (below) governs default flags, "Resume with these" and "Start new session with these". Per-flag value rules apply. |
| (none) | vigil may write exactly two files, `CLAUDE.md` and `.claude/settings.json`, inside a project folder, and only on a confirmed Apply (part 3). |
| (none; vigil never stops a session) | vigil may stop a background session with `claude stop <job id>`, only to restart it with Remote Control, and only after the author confirms a warning that this interrupts it (part 2). |
| Peers forward only Resume. | Peers also forward Review, Start new session, Always use, Show change and Apply (part 3), and Restart with Remote Control (part 2). The peer token now authorizes these writes on the peer. |
| v1 non-goal: no LLM-generated summaries. | Superseded for reviews only, which are on demand. The index still holds no LLM text unless a review was run. |

The Claude home rule and the 500-character limit on indexed transcript text stay.

## Facts found during design (Claude Code 2.1.312)

- When Remote Control is on, the transcript gets a record
  `{"type": "system", "subtype": "bridge_status", "url": "https://claude.ai/code/session_…", "content": "/remote-control is active · …", "timestamp": …}`.
  This is undocumented and may move. The parser must treat it as optional.
- `claude agents --json` lists live sessions (pid, cwd, sessionId, name, status) but not the
  Remote Control URL.
- Assistant records carry `message.usage` with `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens` and `cache_creation.ephemeral_5m/1h`.
- `claude --help` lists `-p/--print`, `--resume`, `--fork-session`,
  `--no-session-persistence` (print only), `--tools`, `--json-schema` (print only),
  `--output-format`, `--max-budget-usd` (print only), `--session-id`, `--bare`, `--model`,
  `--effort`.
- `--permission-mode` choices: `acceptEdits`, `auto`, `bypassPermissions`, `manual`,
  `dontAsk`, `plan`.
- Found on a real machine after building: `claude attach`, `claude stop` and `claude logs` take
  the 8-character job id (the first 8 characters of the session id); given a full session id
  they print `No job matching '<id>'. Run 'claude agents' to list running sessions.` and exit 1.
- `claude --bg` prints `backgrounded · <job id> (idle — …)` once it has started a session. It
  ignores `--session-id` (with a warning on stderr) and picks the new session's id itself.
- `claude --bg --resume <id>` with flags, for a session that was itself started with `--bg`,
  starts a copy under a new id ("keeps its own saved options, so the flags you passed started a
  copy as …"); the copy has its own transcript (where the `bridge_status` link goes), and the
  original stays stopped. For a session that was never a background session it continues the
  same id.

## Part 1: the sessions page

The author approved a mockup (private to the author); the description here is complete without
it.

- **Summary line** at the top: counts of running, background and idle sessions, and the number
  of projects. Counts follow the current search and filters.
- **"Active now"**: projects with at least one `running` or `background` session (my call:
  background counts as active), open by default. Inside, only the active sessions show. The
  project's other sessions fold behind a "Show N idle sessions" control at the bottom of the
  project.
- **"Quiet"**: every other project, collapsed to one line: project label, path, session count,
  last activity. Tapping opens it and shows all its sessions.
- Order within each section: newest activity first, as today.
- **Session rows** become two-line items at every width (my call: one layout, no table): name
  (with pinned/archived/stale badges and note as today), then status pill, branch and last
  activity, and the actions on the right. This replaces the table and the narrow-screen rule
  that hides the branch column.
- **Search**: a non-empty search opens every project with a match and unfolds matching idle
  sessions. The "Include archived…" checkbox and `?project=` filter keep working.
- Peers: a peer's sessions take part as they do today, with the machine shown in the row's
  second line. Stale peer sessions count as quiet whatever their last status.
- Built with `<details>`/`<summary>` and a small inline script for the idle fold, so it works
  without htmx and the live search can re-render the results block. Open/closed state is not
  remembered across loads (my call).
- Touch targets at least 40px tall. No horizontal scroll at 360px wide.

## Part 2: joining from a phone

### Picking up the link

- The scanner records the newest `bridge_status` URL per session, with its timestamp, in a new
  index column (new migration). It accepts only `https://claude.ai/code/…` URLs; anything else
  is ignored. If the record type disappears in a future Claude Code version, the column stays
  empty and the UI simply shows no Open button.
- The session API and `session_to_dict` gain `remote_url` (null when unknown).

### What the buttons do

- **Session is live (`running` or `background`) and has a `remote_url` newer than its live
  start time**: the row and detail page show **Open**, a plain link to `remote_url`. On a
  phone the OS hands it to the Claude app if installed. Nothing runs on the machine.
- **Session is not live**: Resume from a remote request (the existing "no terminal for remote
  resume" path) runs `claude --bg --resume <id> --remote-control [other default flags]`, adding
  `--remote-control` even if it is not in `resume.default_flags`. vigil reads which session
  started from what `claude --bg` prints (`backgrounded · <job id>`) and `claude agents --json`
  (the session whose id starts with that job id, in the same folder); it may be a copy (see the
  facts above), and then the answer says so (`copy_of`, `session_id`) and the page says
  "Started as a copy (<job id>): this background session keeps its own options." When it
  printed another job id but that session cannot be found (not listed yet, listed in another
  folder), vigil attaches by that job id, leaves `session_id` out and answers pending (it has no
  transcript to wait on). Only when it printed no job id, or the requested session's, does it
  carry on with the id it asked for. It then waits up to 30 s
  (my call; configurable `resume.remote_link_timeout`) for a new `bridge_status` record with a
  timestamp after the launch, reading the transcript directly rather than waiting for the
  watcher. On success the response carries the URL and the page navigates to it. On timeout it
  says the session started and the link is not available yet, with a link to the session page,
  which shows Open once the link arrives.
- **Session is open in an ordinary terminal without a link**: refuse as today ("already open"
  with machine and folder) and add: "type `/remote-control` in that terminal to make it
  joinable."
- **Session is running in the background (`--bg`) without a link** (decided with the author:
  offer both): the answer explains the session has no Remote Control link and offers two
  choices:
  - the hint: attach at a desk (`claude attach <job id>`, shown) and type `/remote-control`;
  - **Restart with Remote Control**: an in-page confirm step warns that this stops the session
    and interrupts anything it is doing. When the live status says the session is busy, the
    warning says so plainly. On confirm, vigil runs `claude stop <job id>`, waits for the
    session to leave `claude agents --json` (up to 10 s, my call), then resumes it as in the
    not-live case and waits for the link. If the stop does not take, it refuses and starts
    nothing. Such a session was started in the background, so it comes back as a copy under a
    new id, and the answer says so.
- vigil adds `--remote-control` to every background session it starts (resume and start-new,
  desk or phone), so the case above only comes up for sessions started outside vigil (my call).
- **At the desk** (loopback request): Resume behaves as today and opens a terminal. If the
  session is live with a link, the row also shows **Open in Claude**.
- **Peers**: forwarded as Resume is today. The owning peer launches and waits, and its JSON
  answer carries `remote_url` back.
- tmux fallback (no `--bg`): the same, with `--remote-control` on the tmux command.

## Part 3: session reviews

### Starting a review

- The session detail page gets **Review this session**. One review runs at a time per machine
  (my call); a second press while one runs shows the running one. The POST starts the review in
  a background task and the page polls (htmx, every 2 s) until it finishes.
- CLI: `vigil review <id> [--json]` runs a review and prints it. Apply and launch actions stay
  in the web UI (my call).
- A peer's session is reviewed by the peer, forwarded like Resume. Every action on a peer
  session's suggestions is forwarded too (decided with the author): Resume with these, Start new
  session with these, Always use (changes the peer's config), Show change and Apply (the peer
  computes the diff and hash and does the write, and the hash check happens on the peer).
  Each forwarded action is its own POST endpoint under `/peers/{name}/…`, mirroring the local
  one, and the peer applies exactly the same allow-lists and refusals as for a local request.

### Step 1: numbers vigil computes itself (no Claude)

From the transcript, stored with the review:

- Totals and per-turn series of input, output, cache-write and cache-read tokens, and cache-hit
  share.
- The ten largest tool results by size: tool name and size only, no content.
- Number of compactions, number of turns, wall-clock span, models used.
- Flags the session was launched with, from launches vigil made itself only. Shell-history
  launches carry no session ID, and matching them by time would be a guess, so they are not
  used here (my call). The prompt says the launch flags may be incomplete.

These show on their own as a "Where the tokens went" panel even if the Claude step fails.

### Step 2: Claude's review

vigil runs, with cwd set to the session's cwd:

```
claude -p --resume <id> --fork-session --no-session-persistence --tools "" \
  --strict-mcp-config --output-format json --json-schema <schema> \
  --max-budget-usd <review.max_budget_usd> [--model <review.model>] "<review prompt>"
```

- The review prompt includes the step 1 numbers, the catalog of the installed version (flags
  and subcommands with descriptions), and the author's current `resume.default_flags`. It asks
  for suggestions tied to evidence in this session, ranked by expected benefit, at most ten.
- The fork is not persisted and has no tools, so the reviewed session is untouched and nothing
  new appears in the index. The builder verifies this on the installed version (see "Left to
  the builder"). `--strict-mcp-config` with no `--mcp-config` loads no MCP server: without it
  a project's `.mcp.json` servers were started for the review (found on a real machine;
  approved by the author).
- Config: `[review]` with `max_budget_usd` (default 3.00) and `model` (default `sonnet`), decided
  with the author after a real run: one Opus turn on a small session cost $1.07 at list price,
  mostly Claude Code's own system prompt, and the budget is only checked after a turn, so an
  answer that arrives over budget is kept with a note.
- Output schema, one list `suggestions`, each item:
  - `kind`: `flag` | `file` | `habit`
  - `title` (≤ 80 chars), `why` (≤ 500 chars), `evidence` (≤ 500 chars: turn number or short
    quote from the session)
  - `flag`: `name`, optional `value`
  - `file`: `target` (`project_claude_md` | `project_settings` | `user_claude_md` |
    `user_settings`), `text` for CLAUDE.md, `settings` (JSON object) for settings
  - plus top-level `token_advice`: a short list of `title` / `why` items about efficiency
- vigil validates the output against the schema and drops invalid items rather than failing the
  whole review. All text fields are truncated to 500 characters.

### Storage

New migration: `reviews` table (id, session_id, machine, created_at, claude version, model,
cost in USD as reported, status, numbers JSON, suggestions JSON, error). Reviews are kept until
the session is pruned from the index. The detail page lists past reviews, newest first.

### Acting on suggestions

Each suggestion is a card: title, why, evidence, and buttons that depend on its kind.

**`flag`**: if the flag and value pass the allow-list:

- **Resume with these**: Resume as today, with the suggested flags added for this launch only.
- **Start new session with these**: `claude --bg [flags]` in the session's cwd (tmux fallback
  as for Resume, where `claude --session-id <new uuid> [flags]` runs under an id vigil
  chooses). `claude --bg` ignores `--session-id` and picks the id itself; vigil finds it as for
  Resume (part 2), from its output and `claude agents --json`. At the desk it opens a terminal
  attached to it. From a phone it adds `--remote-control` and waits for the link as in part 2,
  finding the transcript by that id. If only its job id is known, it attaches by that and the
  answer is pending; if `claude --bg` printed no job id, the answer says the session started and
  its link is not available yet, with no terminal and no link.
- **Always use**: adds the flag to `resume.default_flags` in vigil's own config file, keeping
  the rest of the file as written (comments included). This is the only write to vigil's config
  from the web.

If it fails the allow-list: a copy button with the command line, and a note saying vigil will
not launch this flag itself.

**Flag allow-list** (in code, my call; one list used everywhere flags are added):

| Flag | Value rule |
|---|---|
| `--remote-control` | none, or a name (printable, ≤ 64 chars) |
| `--name` | printable, ≤ 64 chars |
| `--model` | `[A-Za-z0-9._:\[\]-]+`, ≤ 64 chars |
| `--effort` | one of the choices `claude --help` lists for it |
| `--permission-mode` | one of the `--help` choices except `bypassPermissions` |
| `--agent` | `[A-Za-z0-9_-]+` |
| `--worktree` | none |
| `--chrome`, `--no-chrome`, `--ide`, `--verbose`, `--brief` | none |

A flag on the list that the installed version does not have (not in the catalog) is refused
with that reason. Never on the list: anything `--dangerously-*` or `--allow-dangerously-*`,
`--system-prompt*`, `--append-system-prompt*`, `--mcp-config`, `--strict-mcp-config`,
`--settings`, `--setting-sources`, `--plugin-*`, `--add-dir`, `--tools`, `--allowed*`,
`--disallowed*`, `--permission-prompt-tool`, `--bare`, `--debug*`.

**`file`**, project targets (`project_claude_md`, `project_settings`):

- The project folder is the git top-level of the session's cwd, else the cwd itself. The target
  is `<folder>/CLAUDE.md` or `<folder>/.claude/settings.json`.
- **Show change** renders a diff. **Apply** writes only after that preview. It sends the hash
  of the file as previewed and is refused if the file has changed since.
- `CLAUDE.md`: the text is appended after a blank line. Existing text is never changed. The
  file is created if missing.
- `settings.json`: the suggestion is deep-merged. Objects merge, arrays are unioned, and an
  existing scalar is never overwritten (that key is reported as "already set to X" and
  skipped). If the existing file is not valid JSON, refuse. Written with 2-space indent and a
  trailing newline.
- **Settings keys that may be applied** (my call): `model`, `effortLevel`,
  `permissions.deny`, `permissions.ask`, `includeCoAuthoredBy`, `cleanupPeriodDays`,
  `autoCompact*`. Any other key, including `hooks`, `env`, `statusLine`, `apiKeyHelper`,
  `mcpServers`, `enabledMcpjsonServers`, `permissions.allow` and `permissions.defaultMode`,
  makes the whole suggestion copy-only with a note on why.
- Refuse if the resolved target (after symlinks) is under the Claude home, outside the project
  folder, or not a regular file.
- `user_claude_md` and `user_settings` (files under `~/.claude`): copy-only, with the file path
  shown.

**`habit`** and token advice: text only.

Reviews read a transcript that can contain fetched web pages and other untrusted text, so a
review's suggestions are untrusted too. The allow-lists, the preview before Apply and the
copy-only fallbacks are what keep a steered review from doing harm. The review page states that
suggestions come from Claude reading the session and should be checked.

## Calls made during design (for the author to check)

- Background sessions count as "active" on the sessions page.
- Sessions page uses one row layout at every width. Open/closed state is not remembered.
- Remote link wait: 30 s, configurable. Stop wait for Restart with Remote Control: 10 s.
- vigil adds `--remote-control` to every background session it starts.
- A review's launch flags come only from launches vigil made itself.
- One review at a time per machine.
- Review runs on a non-persisted fork of the session (the full conversation) rather than on a
  digest vigil builds. This costs more tokens on long sessions but lets Claude see what
  happened. If cost turns out to matter, a digest mode can come later.
- `vigil review` exists on the CLI; Apply and launches are web-only.
- The flag allow-list and the settings key allow-list above.
- Project folder is the git top-level of the session cwd.
- "Always use" writes vigil's own config file.

## Left to the builder

- Verify on the installed Claude Code that `--resume` + `--fork-session` +
  `--no-session-persistence` + `-p` leaves no new transcript under the Claude home and does not
  touch the reviewed session's file. Also check whether `--bare` still authenticates with a
  subscription login; if it does, add it (skips hooks and plugins during the review). If no
  combination avoids persisting, stop and raise it before building part 3. Found on a real
  machine: the review runs as built (`--tools ""` leaves only Claude Code's internal
  structured-output tool; no transcript is persisted) and no hook fired, but a project's
  `.mcp.json` server was started for it; the author approved adding `--strict-mcp-config`
  (with no `--mcp-config`, so no MCP server loads).
- Verify `claude --bg` without `--resume` starts a new background session and honors
  `--session-id`. If not, start-new uses the tmux fallback. Found on a real machine: it starts
  one but ignores `--session-id`, so start-new keeps `claude --bg` and finds the new id after
  the start (see "Acting on suggestions").
- Which record marks a compaction in the transcript, if any.
- How to edit vigil's TOML config while preserving comments (e.g. `tomlkit`, a new dependency)
  or a line-based edit of the one key.
- The review prompt wording and the exact JSON schema text.
- Whether part 3 lands as one PR or splits (numbers + review, then actions, then peer
  forwarding).
- How to get a live session's start time for the "link newer than live start" check.
- Tests follow the existing rule: fake `claude`/`tmux`/terminal executables record argv and
  cwd. A fake `claude -p` prints canned JSON. Fixtures with `bridge_status` and `usage` records
  are sanitized (placeholder session IDs and URLs).
