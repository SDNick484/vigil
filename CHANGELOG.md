# Changelog

All notable changes to this project are recorded here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add one line per change under
`[Unreleased]`.

## [Unreleased]

### Added
- `vigil upgrade [--ref REF] [--dry-run] [--no-restart]`: upgrade vigil the way it was installed (`install.sh` venv, other venv, pipx; refuses a system package and names `emerge -u dev-util/vigil` on Gentoo), then restart the `vigil setup` service.
- `install.sh`: POSIX installer and upgrader (private venv, `~/.local/bin` link, `vigil-install.toml`, hands off to `vigil setup`), with `--ref`, `--yes`, `--no-setup`, `--dry-run`, `--uninstall` and `--purge`; shellcheck runs in CI.
- Live Gentoo ebuild `dev-util/vigil-9999` in `packaging/gentoo`, with a test that keeps its `RDEPEND` in step with `pyproject.toml`, and a CI syntax check.
- Design for the install script, `vigil upgrade` and the live Gentoo ebuild.
- Package scaffold, TOML config loading with validation, `vigil` CLI skeleton and CI.
- systemd, OpenRC and XDG autostart service files in `contrib/`, with install, run and config documentation.
- Tolerant, incremental transcript parser (`vigil.transcript`) with sanitized real fixtures.
- SQLite index with numbered migrations, incremental transcript scanner, project grouping by git remote, and session queries (`vigil.index`, `vigil.projects`).
- Detect installed `claude` capabilities (`--bg`, `attach`, `agents --json`) and live sessions, with fixtures captured from Claude Code 2.1.312.
- Feature catalog (`vigil.catalog`): flags and subcommands parsed from the installed `claude --help`, stored per Claude Code version (migration 0002).
- Opt-in launch history from shell history (`vigil.history`): bash, zsh and fish history, `claude` invocations only, subcommand and flag names only.
- Resume launcher (`vigil.resume`, `vigil.terminal`): `claude --bg` with tmux fallback, terminal detection (config template, WSL Windows Terminal, `$TERMINAL`, known terminals), refusals for open sessions, missing folders and missing tools.
- CLI commands `vigil scan`, `ls`, `show` and `resume`, with `--json` output (`vigil.serialize.session_to_dict`) that the API reuses.
- Web service (`vigil serve`): JSON API under `/api/v1`, resume and metadata POST actions, a loopback `Host` and same-origin guard, and a transcript watcher that keeps the index current.
- Web pages: sessions list grouped by project with live search, session detail with last exchange, resume command, chain and label/note/pin/archive editing; htmx vendored, no external requests.
- Usage report (`vigil usage`, `GET /api/v1/usage`, `GET /api/v1/catalog`, `/usage` page): what you use inside sessions, how you launch `claude` (short flags counted as their long names), and which flags and subcommands of the installed version you have never tried.
- Remote access (v2): optional non-loopback bind (`server.bind`) behind a token (`server.token_file`, mode 0600), bearer auth for API clients, a `/login` page with a signed HttpOnly, SameSite=Strict session cookie for browsers, `server.allowed_hosts`, and no terminal for remote resume.
- Peers (v2): `[[peers]]` and `machine` in the config, a merged sessions list with a machine column, stale last-known sessions for offline peers (cached in the data dir), peer session pages, Resume forwarded to the owning peer (`POST /peers/{name}/sessions/{id}/resume`), and `GET /api/v1/peers`; httpx is now a runtime dependency.
- Sessions page: active projects open on top, quiet projects collapsed to one line, idle sessions folded, a status summary, and phone-first rows.
- Index each session's Remote Control link (`remote_url`) and whether a live session is busy.
- Join from a phone: Resume from another device opens the session's Remote Control link; Open button for live sessions with a link.
- Restart with Remote Control for background sessions without a link, after a confirm warning.
- One flag allow-list with value rules (`vigil.flags`), used by config and every launch.
- Session page: where the tokens went (per-turn tokens, cache hits, largest tool outputs, compactions).
- Session reviews: Claude reads a non-persisted fork of a session and suggests flags, project file changes and habits; stored per session; `vigil review`.
- Act on flag suggestions: resume or start a new session with them, or add them to `resume.default_flags`.
- Apply project file suggestions to `CLAUDE.md` and `.claude/settings.json` after a preview.
- Reviews and their actions work on peer sessions, run by the peer.
- `vigil setup`: picks and installs XDG autostart, a systemd user unit or an OpenRC user service (`--method`, `--dry-run`, `--yes`, `--no-start`), starts vigil and checks that it answers, and `--remove` undoes only what it wrote; the service templates now ship inside the package (`contrib/` keeps identical copies).
- `vigil doctor`: read-only checks (Python, `vigil` and `claude` on `PATH`, Claude home, index, config, display and terminal, port, autostart or service) with a fix for each problem; `--json`; exits 1 on a problem, and a broken config is reported as a problem instead of stopping it.

### Changed
- Install guide rewritten step by step: install without pipx (a virtual environment), checks after each step, which way to keep vigil running, upgrade, uninstall and troubleshooting; the service files also look for `claude` in `/opt/bin`.
- `tomlkit` is now a runtime dependency, and the first scan after upgrading re-reads every transcript once (migration 0004).

### Fixed
- A session started with a local command such as `/model` (or a `!` bash-mode command) is named after the first typed prompt, and command output is no longer taken for a prompt.
- Resume, Restart with Remote Control and the attach command shown pass `claude attach` and `claude stop` the 8-character job id they require, not the full session id they refuse (Resume's attach command had been wrong since v1).
- After `claude --bg`, vigil follows the session that actually started (its job id from the output, its full id from `claude agents --json`): a copy of a background session resumed with flags (the answer says so with `copy_of`), or a new session's own id, since `--bg` ignores `--session-id`.
- Reviews load no MCP servers (`--strict-mcp-config`); a project's `.mcp.json` servers had been started for each review.
