# vigil: design

Date: 2026-11-15
Status: draft, awaiting approval (builder check done)
Repo: SDNick484/vigil (public, open source)

## Intent

The author runs many Claude Code sessions across many project folders and loses
track of them: which session ID belongs to which piece of work, which folder
it ran in (`claude --resume <id>` only works from that folder), and where they
left off. The author also suspects they are underusing Claude Code: today they use only
`--remote-control`, `--resume`, and `--name`, and have not explored the rest.

vigil is a small local service with a web UI and a CLI that:

1. indexes every Claude Code session on a machine and shows, per session,
   what it was about, where it ran, when it was last touched, and how it
   ended;
2. resumes a chosen session with one click, in the right folder, in a
   terminal;
3. shows how the user actually uses Claude Code next to what their installed
   version offers, so untried features are visible;
4. later, does the same across their other machines.

Linux first. Gentoo is the primary target, but nothing may be
Gentoo-specific.

## Goals

- Answer "where was I?" in under ten seconds: find the session, see its
  last exchange, resume it.
- One-click resume that lands in a terminal attached to the resumed session,
  in the session's own working directory.
- A usage view built from the user's real transcripts, compared against the
  feature surface of the installed `claude` binary.
- Usable by anyone on Linux who installs it: public repo, no dependency on
  any private system.

## Non-goals (v1)

- claude.ai/code cloud sessions. They never write to `~/.claude`. Design
  leaves room (a "source" field on sessions) but v1 does not fetch them.
- LLM-generated summaries of sessions. v1 shows extracted text only.
- Editing, deleting, or rewriting anything under `~/.claude`.
- macOS and Windows. WSL2 counts as Linux and should work.
- Being a replacement for `claude agents` (the built-in live agent view).

## Anti-goals (would be a failure even if it works)

- Any code or concepts specific to the author's private workstream
  tracker. Such tools integrate by reading vigil's public API.
- A web endpoint that can run arbitrary commands. Resume may only ever run
  `claude` against a session ID that exists in the index.
- Leaking transcript contents off the machine without explicit
  configuration.
- A hardcoded list of Claude Code features that goes stale within a month.

## Constraints and facts found during design

- Session transcripts live at
  `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, one JSON object per
  line. The record schema is undocumented and changes between Claude Code
  versions. The parser must ignore unknown record types and fields, and must
  take the working directory from the records themselves, not by decoding the
  directory name (the encoding is lossy).
- Claude Code 2.1.x (checked on 2.1.325) has native background sessions:
  `claude --bg --resume <id>` continues a session in the background,
  `claude attach <id|name>` opens it in a terminal, `claude agents --json
  [--all]` lists active sessions as JSON, plus `logs`, `stop`, `respawn`.
  `--resume` accepts a session ID or a session name. Older versions lack
  these; vigil must detect support at runtime.
- `claude --help` and each subcommand's `--help` describe the installed
  version's flags and subcommands. This is the source for "what exists".
- The author's machines: a Linux laptop and a WSL2 desktop, both Gentoo,
  neither always on, reachable over Tailscale. Their init system is OpenRC;
  other users will mostly have systemd.
- The author's other projects use Python with FastAPI, uvicorn, ruff, mypy, pytest,
  and OpenRC init scripts. vigil follows that stack.

## Approaches considered

1. **Local service with a web UI (chosen).** A per-machine Python service
   indexes sessions, serves a localhost web UI and JSON API, and launches
   resumes. Multi-machine later by having one instance aggregate peers over
   Tailscale. A thin CLI shares the same index. Trade-off: something has to
   run in the background; usage charts live in the browser.
2. **Terminal UI (Go or Rust).** No service; reads files on demand and execs
   `claude` on resume. Fastest in a terminal, but multi-machine means SSH to
   each box, no phone view, and cramped usage insights.
3. **Native GTK app.** Most packaging and UI cost for the least reach;
   multi-machine and phone access are awkward.

The author picked 1 because it is the only option that covers one-click launch,
multiple machines, and usage insights without a rewrite.

## Decisions made with the author

- Separate, public, open source app. Integration with the author's private
  tracker happens only through vigil's documented read-only JSON API, which
  that tracker pulls.
- One-click resume that opens a terminal on the right machine, not just a
  copyable command.
- Name: vigil. Repo SDNick484/vigil. CLI command `vigil`.
- Usage insights are in scope: both "what I use" and "what I've never
  tried".
- Launch flags come from shell history, but only `claude` invocations, not
  full history (see Launch history below).
- v1 is localhost only. Remote access (phone, other tools, other machines) is
  v2 and follows quickly, so v1 must not make it hard.

## The design

### Components

One Python package, one process, three faces:

- **Indexer.** Scans `~/.claude/projects/**/*.jsonl`, parses incrementally
  (tracks file size and mtime; re-reads only changed files, and only the
  appended tail when a file grew), and writes to a SQLite index in
  `$XDG_DATA_HOME/vigil/index.db`. Runs at startup and then on file change
  (inotify via `watchfiles`) with a periodic full rescan as a safety net.
- **Web service.** FastAPI on `127.0.0.1:<port>` by default. Server-rendered
  HTML (Jinja2) with htmx for small interactions; no JavaScript build step.
  Also serves the JSON API.
- **CLI.** `vigil serve`, `vigil scan`, `vigil ls [query]`,
  `vigil show <id>`, `vigil resume <id|name>`, `vigil usage`. The CLI reads
  the same index and can work without the service running.

### Session record

Per session, the index holds:

- id, source (`local` in v1), machine (hostname), cwd, git branch and git
  remote URL of the cwd if available, Claude Code version
- name/title if one was set (`-n` or a later rename), first user prompt
  (truncated), last user message and last assistant text (truncated):
  together, the "where I left off" view
- started, last activity, message counts, models used
- status: `running` / `background` / `idle` from `claude agents --json` when
  available, else derived from file activity
- feature usage extracted from the transcript: tool names, slash commands,
  skills, subagents, MCP servers and tools, plan mode, model switches. Which
  of these are recoverable from transcripts must be confirmed against real
  files; record what is found, not what is hoped for.
- vigil-only metadata, stored in vigil's own DB: label, pinned, archived,
  free-text note ("next: ..."). Never written back into `~/.claude`.

Sessions group into **projects** by git remote URL when there is one, else
by cwd. This is the only grouping concept in v1.

**Resume chains** (my call). If continuing a conversation writes a new
session file (to be confirmed against real files), vigil links the files
into a chain using whatever parent/previous-session reference the
transcripts carry. The UI shows one row per chain with the newest ID as the
resume target and the combined history; the API returns each session with a
`chain_id` and `previous_id` so consumers can collapse or not.

### Launch history

Transcripts record what happens inside a session, not the flags `claude`
was started with. To see launch flags, vigil reads shell history, with
these limits:

- Opt-in (`usage.shell_history = true`); off by default.
- Reads bash, zsh (plain and extended format), and fish history files from
  their default locations or configured paths.
- Keeps only lines whose command is `claude` (including via a path, `env`,
  or an alias the user lists in config). Everything else is discarded while
  reading and never stored.
- From each kept line, stores only the subcommand and flag names, plus a
  timestamp when the history format has one. Flag values and prompt text
  are not stored.
- Launches vigil makes itself are recorded directly and counted too.

### Web UI

- **Sessions** (home): grouped by project, newest activity first, with
  search over name, label, note, first prompt, and cwd. Each row: name or
  first prompt, project, branch, last activity, status, Resume button.
- **Session detail:** the record above, the last exchange, the exact
  resume command (copyable), Resume, label/pin/archive/note editing.
- **Usage:** what the user uses (counts and recency per feature, overall and per
  project) beside the feature catalog of the installed version, with
  "never used" called out.

### Resume

`POST /sessions/{id}/resume` (and `vigil resume`):

1. Look up the session in the index. Unknown ID: 404. The command line is
   built from the indexed cwd and ID only; no user-supplied arguments
   except an allow-listed set of extra flags from config (see below).
2. If the session is already running (per `claude agents --json`): if it is
   a background session, skip to attach. If it is open in an ordinary
   terminal, refuse with "already open" and where; never start a second
   process on the same session (my call).
3. If the installed `claude` supports `--bg`: run
   `claude --bg --resume <id> [default flags]` with cwd set to the session's
   cwd. Otherwise fall back to tmux:
   `tmux new-session -d -s vigil-<short-id> -c <cwd> claude --resume <id> ...`.
4. Open a terminal attached to it, using a configured command template
   (e.g. `mate-terminal -- {attach}`), else `$TERMINAL`, else a short list of
   known terminals. `{attach}` is `claude attach <id>` or
   `tmux attach -t vigil-<short-id>`. Under WSL (detected from
   `/proc/version` or `WSL_DISTRO_NAME`), the default is Windows Terminal
   via interop: `wt.exe wsl.exe -d <distro> --cd <cwd> -- {attach}` (my
   call; overridable in config).
5. If no terminal can be opened (no display, service started headless),
   skip step 4 and show the attach command. In v2 the same applies when the
   click comes from another device.

Config `resume.default_flags` (allow-listed: `--remote-control`, `--model`,
`--effort`, `--permission-mode`, `--name`) lets the user always add
`--remote-control`. If the cwd no longer exists, refuse with a clear
message.

### Feature catalog

At startup and when `claude --version` changes, run `claude --help` and
`claude <sub> --help` for each listed subcommand, parse flags and
subcommands, and store them with the version. Slash commands, built-in
tools, and settings keys come from what the installed version exposes where
it can be read without starting a session; anything that cannot be
discovered is simply not listed. No hardcoded feature list. Short "try
this" tips per feature are a later addition, kept as optional data files.

### API (public, versioned)

Read-only JSON under `/api/v1`: `sessions` (filterable by project, cwd,
since, status), `sessions/{id}`, `projects`, `usage`, `catalog`, `health`.
Documented in the README as the integration point for other tools
(the author's private tracker being one). Resume is not part of the read API.

### Security (v1)

- v1 binds to 127.0.0.1 only and refuses any other bind address.
- Every request's `Host` must be a loopback name (defends against DNS
  rebinding). State-changing endpoints (resume, metadata edits) are POST
  only and check `Origin` (defends against cross-site requests from the
  browser).
- Keep auth as one middleware layer so v2 can add token and cookie login
  without touching route handlers.
- vigil only reads `~/.claude`. Transcript text in the index is limited to
  truncated snippets; full transcripts are read from disk on demand and
  never cached elsewhere.

### v2: remote access and multiple machines

Remote access comes first in v2: an optional non-loopback bind (intended
for a Tailscale address), which then requires a configured token. API
clients send it as a bearer token; browsers exchange it once at a login
page for an HttpOnly, SameSite=Strict session cookie so pages and htmx
requests work.

Every machine runs vigil. One instance can be configured with `peers`
(Tailscale hostnames plus tokens); it pulls each peer's `/api/v1` and shows
a merged list with a machine column. Resume on a peer's session is
forwarded to that peer's resume endpoint, which launches there. Offline
peers show their last-known sessions as stale. No central server is
required.

### Packaging and running

- `pip`/`pipx` installable. PyPI name `vigil` is likely taken; the
  distribution name is the builder's call (e.g. `vigil-sessions`), import
  name and command stay `vigil`.
- `contrib/` ships an OpenRC user service script, a systemd user unit (for
  other distros), and an XDG autostart `.desktop` file as the universal
  fallback.
- A Gentoo ebuild goes in the author's overlay, not in this repo.
- License: MIT (my call; easy to change before first release).

### Quality bar

- ruff, mypy (strict), pytest; GitHub Actions CI on push and PR.
- Parser tests run against sanitized fixture transcripts covering more than
  one Claude Code version, including malformed and truncated lines.
- Resume is tested with a fake `claude` and fake terminal on PATH; no test
  launches a real session.

## Calls made during design (for the author to check)

- Python 3.14+ (the author's choice), FastAPI, Jinja2 + htmx, SQLite, `watchfiles`.
- Grouping by git remote, else cwd.
- Notes/labels live in vigil's DB, never in `~/.claude`.
- tmux as the fallback for Claude Code versions without `--bg`.
- MIT license.
- Port: builder's choice of a fixed default, configurable.
- Under WSL, open Windows Terminal (`wt.exe`) by default.
- Refuse to resume a session already open in an ordinary terminal.
- Collapse resume chains into one row in the UI; expose the links in the
  API.
- Launch history stores flag names only, never values or prompts.

## Left to the builder

- Exact transcript fields and record types (from real fixtures).
- Distribution name, default port, terminal auto-detect list.
- Visual design of the pages, within "plain, fast, readable".
- Phase order inside the plan, as long as v1 (local index, web UI, CLI,
  resume, usage, launch history) ships before v2 (remote access, peers).
