# vigil

vigil is a local service, web UI and CLI for Claude Code. It indexes every Claude Code session on
a Linux machine, lets you resume one with a click in the right folder, and shows your Claude Code
usage against what the installed version offers. `claude agents` shows live sessions only; vigil
adds history, resume across projects and usage.

## Install

Setting vigil up takes about five minutes. You install it, check that it can read your sessions,
try the web UI, then choose how to keep it running. Every step below runs as your own user, with
no `sudo`.

### What you need

- **Linux.** WSL2 counts.
- **Python 3.14 or newer.** Check with `python3.14 --version`. If it's missing, install it from
  your distribution; on Gentoo that's `emerge --ask dev-lang/python:3.14`. Your system's default
  `python3` can stay as it is.
- **git**, because vigil is installed straight from GitHub until the first release reaches PyPI.
- **Claude Code** (`claude`) on your `PATH`, so that sessions can be resumed. Indexing and
  browsing work without it.

### Step 1: install vigil

Pick **one** of the two ways below. Either way you end up with a `vigil` command in
`~/.local/bin`, in its own private Python environment, so nothing it installs touches your system
Python.

**A. With pipx**, if your distribution has it:

    pipx install --python python3.14 git+https://github.com/SDNick484/vigil

**B. Without pipx**, using a plain virtual environment. This is the way to go on Gentoo and
anywhere else pipx isn't packaged:

    python3.14 -m venv ~/.local/share/vigil-venv
    ~/.local/share/vigil-venv/bin/pip install git+https://github.com/SDNick484/vigil
    mkdir -p ~/.local/bin
    ln -sfn ~/.local/share/vigil-venv/bin/vigil ~/.local/bin/vigil

Don't use `pip install --user` or `sudo pip install` instead. Most distributions block both to
protect the system Python, and the virtual environment above does the same job safely.

The package is called `vigil-sessions` (that's what pip and pipx list); the command is `vigil`.

### Step 2: check that it works

    vigil --version

This prints something like `vigil-sessions 0.1.0`. If you get `vigil: command not found`,
`~/.local/bin` isn't on your `PATH` yet: add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.bashrc` (or your shell's equivalent) and open a new terminal.

Then let vigil read your sessions. It only ever reads `~/.claude` and never changes anything
there:

    vigil scan
    vigil ls

`vigil scan` reports something like `Scanned 53 transcripts: 53 read, 53 sessions updated,
0 errors.` and `vigil ls` lists your most recent sessions with their project, branch and short id.
The first scan of a large history can take a few seconds; after that, only new lines are read.

### Step 3: try the web UI

    vigil serve

Open <http://127.0.0.1:7787> in a browser. You should see your sessions grouped by project, with a
search box and a **Resume** button on each row. Press `Ctrl-C` in the terminal to stop it. vigil
only listens on `127.0.0.1`, so other machines can't reach it (see [Remote access](#remote-access)
if you want them to).

## Run

To have vigil running all the time, start it with your desktop or as a user service. All three
options below run `vigil serve` as you, from `~/.local/bin/vigil`.

| You use | Choose | Resume opens a terminal window? |
| --- | --- | --- |
| A graphical desktop (MATE, GNOME, KDE, Xfce, …) | **XDG autostart** (recommended) | Yes |
| systemd, and want it running without logging in | systemd user service | Usually not: shows the command to paste |
| OpenRC 0.60 or newer, and want it running without logging in | OpenRC user service | No: shows the command to paste |

Resume opens a terminal window only when vigil can see your display. Autostart runs inside your
desktop session, so it can. A user service starts outside it and normally can't, so Resume starts
the session in the background and shows you the `claude attach …` command to run yourself.

### XDG autostart (desktop sessions)

From a clone of this repository (`git clone https://github.com/SDNick484/vigil`), run:

    mkdir -p ~/.config/autostart
    sed "s|^Exec=vigil serve|Exec=$HOME/.local/bin/vigil serve|" \
        contrib/xdg/vigil.desktop > ~/.config/autostart/vigil.desktop

The `sed` writes the full path to `vigil` into the entry. Desktop sessions often don't have
`~/.local/bin` on their `PATH`, because it's usually added in `~/.bashrc`, which they don't read.

vigil now starts each time you log in. To start it right away without logging out:

    setsid -f ~/.local/bin/vigil serve >/dev/null 2>&1

To stop it, run `pkill -f "vigil serve"`. To turn autostart off, delete
`~/.config/autostart/vigil.desktop`.

### systemd user service

    mkdir -p ~/.config/systemd/user
    cp contrib/systemd/vigil.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now vigil.service

Check it with `systemctl --user status vigil`, and read its log with `journalctl --user -u vigil`.

### OpenRC user service

    mkdir -p ~/.config/rc/init.d
    install -m 755 contrib/openrc/vigil ~/.config/rc/init.d/vigil
    rc-service --user vigil start
    rc-update --user add vigil default

Set `vigil_bin` or `vigil_args` in `~/.config/rc/conf.d/vigil` to change the binary or pass flags.

Both service files look for `claude` (and `tmux`, if you use the tmux launcher) in
`~/.local/bin`, `/usr/local/bin`, `/usr/bin`, `/bin` and `/opt/bin`, where Gentoo installs
Claude Code. If `claude` is somewhere else (`command -v claude` tells you where), add its folder
to the `PATH` line in the file.

## Upgrade

Fetch the latest version the same way you installed it, then restart vigil.

- **pipx:** `pipx install --force --python python3.14 git+https://github.com/SDNick484/vigil`
- **Virtual environment:**
  `~/.local/share/vigil-venv/bin/pip install --upgrade --force-reinstall git+https://github.com/SDNick484/vigil`

To restart:
- **Autostart:** `pkill -f "vigil serve"`, then start it again with the `setsid` line above.
- **systemd:** `systemctl --user restart vigil`.
- **OpenRC:** `rc-service --user vigil restart`.

The index and your labels, notes and pins are kept.

## Uninstall

1. Stop vigil (see **To restart** under [Upgrade](#upgrade)) and remove the autostart entry or
   service file you installed.
2. Remove the program:
   - **pipx:** `pipx uninstall vigil-sessions`
   - **Virtual environment:** `rm -r ~/.local/share/vigil-venv ~/.local/bin/vigil`
3. If you want, remove its data and settings: `rm -r ~/.local/share/vigil ~/.config/vigil`.

Your Claude Code sessions in `~/.claude` are untouched: vigil never writes there.

## Troubleshooting

- **`vigil: command not found`.** `~/.local/bin` isn't on your `PATH`; see
  [Step 2](#step-2-check-that-it-works).
- **Resume says `claude` is not on PATH.** The service can't see Claude Code. Find it with
  `command -v claude`, then add that folder to the service file's `PATH` line (Gentoo's
  `/opt/bin` is already there), or use XDG autostart, which inherits your login `PATH`.
- **Resume shows a command but no window opens.** vigil can't see your display, which is normal
  for a user service. Run the command it shows, or switch to XDG autostart. To choose the
  terminal yourself, set `resume.terminal_command` (see [Configuration](#configuration)).
- **`address already in use` from `vigil serve`.** vigil (or something else) is already using port
  7787. Run `pkill -f "vigil serve"` to stop vigil, or set `port` in the config file.
- **A session is listed under the wrong name after an upgrade.** Names are worked out when a
  transcript is read, and old entries keep the name they were given. To rebuild the index, stop
  vigil, run `rm ~/.local/share/vigil/index.db` and then `vigil scan`. This also clears labels,
  notes and pins.

## Configuration

vigil reads `$XDG_CONFIG_HOME/vigil/config.toml` (`~/.config/vigil/config.toml` when
`XDG_CONFIG_HOME` is unset). The file is optional and so is every key. An unknown key, a wrong
type or a disallowed value stops vigil with an error that names it. The index is kept in
`$XDG_DATA_HOME/vigil/index.db` (`~/.local/share/vigil/index.db` by default).

| Key | Default | Meaning |
| --- | --- | --- |
| `claude_home` | `"~/.claude"` | Claude Code's data folder. vigil only reads it and never writes there. |
| `port` | `7787` | Port for `vigil serve`, 1 to 65535. |
| `resume.default_flags` | `[]` | Flags added to every resume (vigil always adds `--remote-control` itself). Only `--remote-control`, `--name`, `--model`, `--effort`, `--permission-mode` (not `bypassPermissions`), `--agent`, `--worktree`, `--chrome`, `--no-chrome`, `--ide`, `--verbose` and `--brief` are allowed, each with a value its rule accepts, e.g. `["--model", "opus"]`. |
| `resume.terminal_command` | unset | Command template that opens a terminal. Must contain `{attach}`, e.g. `"mate-terminal -- {attach}"`. Unset means `$TERMINAL`, then a list of known terminals. |
| `resume.launcher` | `"auto"` | `"auto"` uses `claude --bg` when the installed `claude` supports it and falls back to tmux; `"bg"` and `"tmux"` force one. |
| `resume.remote_link_timeout` | `30` | Seconds, 1 to 300, to wait for a session's Remote Control link after Resume starts it for a request from another device. |
| `usage.shell_history` | `false` | Read your shell history to see how you launch `claude`. Off unless you turn it on. |
| `usage.history_files` | `[]` | History files to read. Empty means the default bash, zsh and fish locations. A leading `~` is expanded. |
| `usage.claude_aliases` | `[]` | Extra command names to count as `claude`, e.g. `["cl"]`. |
| `review.max_budget_usd` | `3.0` | Most a [session review](#session-reviews) may spend, in USD, 0.05 to 50 (`claude --max-budget-usd`). Claude Code checks it after each turn, so a review can go a little over. A review typically costs about $0.20 to $1 with Sonnet; Claude Code's own system prompt is most of that. |
| `review.model` | `"sonnet"` | Model for session reviews (`claude --model`); `""` uses Claude Code's default. Same rule as `--model` in `resume.default_flags`. |
| `server.bind` | `"127.0.0.1"` | IP address `vigil serve` listens on (`--host` overrides it). Any other address needs `server.token_file`; see [Remote access](#remote-access). |
| `server.token_file` | unset | File holding the access token, which turns remote access on. Must be an absolute path (a leading `~` is expanded) to a file you own with mode 0600 (0400 is fine too). |
| `server.allowed_hosts` | `[]` | Extra host names remote clients may reach vigil by (the `Host` header, without port; IPv6 in brackets), e.g. a Tailscale name. Needs `server.token_file`. |
| `machine` | host name | What the machine column calls this machine when [peers](#peers) are listed. 1 to 63 printable characters. |
| `peers` | `[]` | Other machines' vigil services whose sessions this one lists, as `[[peers]]` tables; see [Peers](#peers). |
| `peers[].name` | required | The peer's name in the machine column and in URLs: 1 to 63 letters, digits, `-` and `_`. Unique, and not the same as `machine`. |
| `peers[].url` | required | The peer's vigil, `http(s)://host[:port]` with no path, e.g. `"http://desktop.example-tailnet.ts.net:7787"`. |
| `peers[].token_file` | required | File holding that peer's access token (its own `server.token_file` token). Same rules: an absolute path (a leading `~` is expanded) to a file you own with mode 0600 or 0400. |

Example:

    port = 7787

    [resume]
    default_flags = ["--remote-control"]
    terminal_command = "mate-terminal -- {attach}"

    [usage]
    shell_history = true
    claude_aliases = ["cl"]

### Shell history

The usage report can show which `claude` flags and subcommands you launch with, but only if you
opt in with `usage.shell_history = true` as above. vigil then reads your bash, zsh (plain and
extended format) and fish history, keeps only lines that run `claude` (directly, by path, through
`env`, or through an alias listed in `usage.claude_aliases`) and stores the subcommand and flag
names with a timestamp. Flag values and prompt text are never stored, and every other line is
discarded as it is read. To read history from somewhere else, list the files in
`usage.history_files`.

## API

`vigil serve` also serves a JSON API. It is the integration point for other tools: read sessions
from it rather than from vigil's database, whose layout may change. The read-only API lives under
`/api/v1`; fields are only ever added there, never renamed or removed. FastAPI's generated
reference is at `/docs` while the service runs.

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/sessions` | Sessions, newest activity first, one per resume chain (its newest session). |
| `GET /api/v1/sessions/{id}` | One session plus `chain` (every session of its resume chain, oldest first) and `resume_command`. `{id}` may also be a unique id prefix or the session's exact title. 404 if nothing matches. |
| `GET /api/v1/projects` | Projects: `project_key`, `label`, `session_count`, `last_activity_at`. |
| `GET /api/v1/health` | `status` (`"ok"`), `version` (vigil's), `claude_version` (null when `claude` is not found), `indexed_sessions`, `last_scan_at` (null until the service has scanned). |
| `GET /api/v1/sessions/{id}/reviews` | The session's [reviews](#session-reviews), newest first: `id`, `status` (`running`, `done` or `failed`), `created_at`, `finished_at`, `cost_usd` (as Claude Code reported it), `model`, `claude_version`, `numbers` (the token numbers the review was given), `suggestions`, `advice` (token advice) and `error` (why it failed, or a note such as going over the budget). |
| `GET /api/v1/peers` | The configured [peers](#peers): `name`, `url`, `fetched_at`, `stale` (it could not be reached), `error` (why), `session_count`. Their sessions are not in `/api/v1/sessions`, which lists this machine's only. |

`GET /api/v1/sessions` takes these query parameters, all optional:

| Parameter | Meaning |
| --- | --- |
| `q` | Words that must all appear in the name, label, note, first prompt or folder. |
| `project` | Only this project (a `project_key`). |
| `cwd` | Only sessions whose working folder is exactly this. |
| `since` | Only sessions active at or after this ISO 8601 time. |
| `status` | `running`, `background`, `idle` or `unknown`. |
| `all` | `true` to include archived sessions and every session of a chain. |
| `limit` | At most this many (default 200). |

Each session has the same fields as `vigil ls --json` (and `vigil show --json` for one session):

| Field | Meaning |
| --- | --- |
| `id`, `short_id` | The session id and its first 8 characters. |
| `name` | vigil's label if set, else Claude Code's title, else null. |
| `title`, `label`, `note` | Claude Code's title; vigil's own label and note. |
| `pinned`, `archived` | vigil's own flags. |
| `cwd`, `git_branch`, `machine`, `version` | Working folder, git branch, host name, Claude Code version. |
| `first_prompt`, `last_user`, `last_assistant` | Where you left off; each cut to 500 characters. |
| `started_at`, `last_activity_at` | ISO 8601 times in UTC. |
| `message_count`, `models` | Messages in the transcript; models used, sorted. |
| `previous_id`, `chain_id` | The session this one continued, if known; the id shared by a resume chain. |
| `project_key`, `project_label` | The project: git remote URL, else the folder. |
| `transcript_path` | The transcript file. |
| `remote_url`, `remote_url_at` | The session's Claude Remote Control link (null if none) and when it was written. |
| `status` | `running` (open in a terminal), `background`, `idle`, or `unknown` when the installed `claude` cannot list live sessions. Refreshed at most every 5 seconds. |

These actions change things. They are POST only, sit outside `/api/v1`, and are for vigil's own
pages:

- `POST /sessions/{id}/resume` resumes the session in its folder and opens a terminal on it when
  one is available. It answers `{"ok": true, "attach": "<command>", "opened_terminal": <bool>}`,
  or 409 with `{"ok": false, "reason": "<why>"}` when resume is refused (already open in a
  terminal, folder gone, no `claude --bg` and no tmux), or 404 for an unknown session.
- `POST /sessions/{id}/meta` sets vigil's own `label`, `note`, `pinned` and `archived` from form
  fields and returns the updated session. Only the fields sent change; an empty `label` or `note`
  clears it.
- `POST /sessions/{id}/review` starts a [review](#session-reviews) of the session and answers
  202 with `{"ok": true, "review_id": <id>}` at once; the review runs in the background. 409 with
  `{"ok": false, "reason": "a review is already running", "review_id": <id>}` while another runs,
  or with the reason when the session's folder is gone or `claude` is missing.
- `POST /reviews/{rid}/suggestions/{i}/resume`, `/start-new` and `/always-use` act on flag
  suggestion `i` (counted from 0) of review `rid`: [see below](#session-reviews). The flag is the
  stored one; nothing else is sent. The first two answer like a resume; `always-use` answers
  `{"ok": true, "default_flags": [...]}`. 409 with the reason when the flag is refused.
- `POST /peers/{name}/sessions/{id}/resume` resumes a session of a [peer](#peers) on that peer, and
  answers like the resume above plus `"machine": "<name>"`. The peer's own 404 and 409 come back
  with its reason; 502 when the peer cannot be reached or refuses the request.

The service only answers requests whose `Host` is `127.0.0.1`, `localhost` or `[::1]` (400
otherwise), which stops other web sites reaching it through DNS rebinding. A POST must carry an
`Origin` header naming the same host and port it was sent to (403 otherwise), so other sites cannot
make your browser resume sessions or edit notes. Scripts posting from outside a browser add the
header themselves:

    curl -X POST -H 'Origin: http://127.0.0.1:7787' http://127.0.0.1:7787/sessions/<id>/resume

While it runs, the service watches `~/.claude/projects` and indexes changed transcripts within a
second or two, with a full rescan every 10 minutes as a safety net.

## Session reviews

**Review this session** on a session's page (or `vigil review <id>`, which waits and prints the
result; `--json` prints it as the API serves it) asks Claude what would make sessions like this
one go better or cost less. vigil runs, in the session's folder:

    claude -p --resume <id> --fork-session --no-session-persistence --tools "" \
      --output-format json --json-schema <schema> --max-budget-usd <review.max_budget_usd> \
      [--model <review.model>] "<review prompt>"

Claude reads a fork of the whole session that is not saved and has no tools, so the session itself
is untouched and nothing new appears under `~/.claude`. The prompt carries the token numbers vigil
computed from the transcript, the flags vigil launched the session with (only launches vigil made,
so they may be incomplete), your `resume.default_flags` and the flags and subcommands of the
installed `claude`. Claude answers with at most ten suggestions, each a flag, a change to
CLAUDE.md or settings, or a habit, tied to evidence in the session, plus a few points of token
advice. Every text field is cut to 500 characters and items that do not fit the schema are
dropped. One review runs at a time; reviews are kept until their session leaves the index.

Suggestions come from Claude reading a transcript that can hold fetched web pages and other text
you did not write, so check them before acting on them.

A flag suggestion the allow-list accepts (the one for `resume.default_flags`, and offered by the
installed `claude`) has three buttons:

- **Resume with these** resumes the session with the flag added for that launch only. A session
  that is already running is joined or attached to as it is.
- **Start new session with these** starts a new session in the reviewed session's folder,
  `claude --bg --session-id <new id> --remote-control [default flags] <flag>` (tmux when there is
  no `--bg`). At the desk it opens a terminal; from another device it waits for the Remote Control
  link, like Resume.
- **Always use** adds the flag to `resume.default_flags` in vigil's config file, replacing the
  value of a flag already there. The rest of the file, comments included, is kept as written, and
  the file is only replaced when the result is a valid config. This is the only write to vigil's
  config from the web.

Any other flag gets its command line to copy and run yourself; vigil will not launch it.

## Remote access

By default vigil only listens on `127.0.0.1` and needs no login. To reach it from your other
devices (over Tailscale, say), give it an access token and an address to listen on. Without a
token, `vigil serve` refuses any address but `127.0.0.1`, and a request relayed by a reverse proxy
is refused (403).

Make a token (32 characters or more, printable ASCII, no spaces) in a file only you can read:

    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/vigil/token)

Then, in the config file (the address and name are examples; use your machine's):

    [server]
    bind = "100.64.0.1"
    token_file = "~/.config/vigil/token"
    allowed_hosts = ["mymachine.example-tailnet.ts.net"]

`vigil serve` checks the token file when it starts and stops with an error if it is missing,
readable by anyone else, or holds no usable token. It reads the token once, so restart the
service after changing it. `bind = "0.0.0.0"` (or `"::"`) listens on every interface.

**Local and remote requests.** vigil decides who is asking from the address the connection
really comes from, never from headers a client can write. A request is local when it comes from a
loopback address, names a loopback host (`127.0.0.1`, `localhost`, `[::1]`) and carries no proxy
headers (`Forwarded`, `X-Forwarded-*`, `X-Real-IP` and similar). Local requests work exactly as
without remote access: no token, and a POST needs a same-origin `Origin`. Everything else is
remote, and needs the token. Remote requests may use the loopback names, `server.allowed_hosts`
and the bind address (unless it is `0.0.0.0` or `::`) as `Host`; any other host gets 400, which
keeps the DNS rebinding defence.

**Scripts and other tools** send the token with every request, and need no `Origin` header:

    curl -H "Authorization: Bearer $(cat ~/.config/vigil/token)" http://100.64.0.1:7787/api/v1/sessions

**Browsers** are sent to `/login`, where you paste the token once. vigil then sets a session
cookie (`vigil_session`) that lasts 30 days: HttpOnly, SameSite=Strict, and Secure when the page
came over HTTPS. Because it is SameSite=Strict, following a link to vigil from another site shows
the login page; open vigil directly instead. A POST made with the cookie must still carry this
service's own `Origin`, as vigil's pages do. **Log out** in the header clears the cookie. The
cookie is signed with a random key that vigil creates in its data folder
(`$XDG_DATA_HOME/vigil/session.key`, mode 0600); delete it, or change the token, to log every
browser out.

**Resume** from a remote request never opens a terminal on the machine running vigil; it starts
the session and answers with the attach command (e.g. `claude attach <id>`) to run there, for
instance over SSH.

**Behind a reverse proxy** (nginx, Caddy and the like), point the proxy at
`http://127.0.0.1:7787`, have it pass the original `Host` and add that name to
`server.allowed_hosts`. vigil treats every request carrying proxy headers as remote, so the token
is required even though the proxy connects from loopback. Make sure your proxy adds
`X-Forwarded-For` (most do by default): a proxy that rewrites `Host` to a loopback name and adds
no proxy headers is indistinguishable from a local client. A TLS proxy should also send
`X-Forwarded-Proto: https`, so the cookie is marked Secure. vigil itself speaks plain HTTP: over
Tailscale the connection is encrypted already; anywhere else, put an HTTPS proxy in front, or the
token crosses the network in the clear.

## Peers

With vigil on several machines, one of them can list every machine's sessions in one place. Turn
on [remote access](#remote-access) on each machine to be listed (a token and an address the
others can reach, with that address or name in its `server.allowed_hosts`), then tell the
machine you browse from about them:

    machine = "laptop"

    [[peers]]
    name = "desktop"
    url = "http://desktop.example-tailnet.ts.net:7787"
    token_file = "~/.config/vigil/desktop.token"

Copy each peer's token into its own file on this machine, readable by you only
(`chmod 600`). The sessions page then shows a **Machine** column: this machine's sessions under
`machine` (or its host name), each peer's under the peer's `name`. Search, the project filter and
"include archived" apply to every machine. A peer's session opens a detail page here, built from
what the peer sent; its label and note are edited on the peer itself.

- **Fetching.** vigil asks each peer for its sessions (`GET /api/v1/sessions`) when a page lists
  them, all peers at once, and reuses the answer for 10 seconds. Connecting to a peer, and waiting for it to answer,
  each time out after 3 seconds.
- **Offline peers.** The last sessions fetched from each peer are kept in the data folder
  (`$XDG_DATA_HOME/vigil/peers/<name>.json`, mode 0600; it holds no token). When a peer cannot be
  reached, its last-known sessions are shown marked **stale**, with the reason and when they were
  fetched, and it is tried again after 30 seconds.
- **Resume** on a peer's session is forwarded to that peer (`POST /sessions/{id}/resume` with its
  token), which starts the session there. Since the request comes from another machine, the peer
  opens no terminal; the page shows the attach command to run on that machine (over SSH, say).
- **Tokens.** A peer's token is read from its `token_file` when needed and sent only to that
  peer's `url`, in an `Authorization: Bearer` header. vigil does not follow redirects, and ignores
  proxy settings and `.netrc` for these requests, so the token cannot be passed on to another
  host. Anyone who can use this vigil (from this machine, or remotely with its token) can browse
  the peers' sessions and resume them through it.

Peers do not list each other's peers: each one serves only its own sessions, so two machines may
list each other. Over plain `http://` the token crosses the network in the clear, which is fine
over Tailscale (encrypted already); anywhere else, use an `https://` URL.

## License

MIT. See [LICENSE](LICENSE).
