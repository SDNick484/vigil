# CLAUDE.md

vigil: a local service, web UI and CLI that index, resume and report on Claude Code sessions.
Design: `docs/superpowers/specs/2026-11-15-vigil-design.md`. Plan: `docs/superpowers/plans/2026-11-15-vigil.md`.

## Verification gate

CI runs exactly this; run it before every commit:

    ruff check . && ruff format --check . && mypy && pytest

## Global Constraints

- `requires-python = ">=3.14"`; CI runs on Python 3.14 only.
- Distribution name `vigil-sessions`; import package `vigil`; console script `vigil`. src layout (`src/vigil/`).
- License MIT.
- Linux only. WSL2 counts as Linux.
- vigil never writes, renames or deletes anything under the Claude home (`~/.claude` or the configured `claude_home`). Tests assert this with a read-only fixture tree.
- No code or concepts specific to any private tool; other tools integrate through the public API.
- v1 binds to `127.0.0.1` only; any other bind address is a startup error.
- Resume may only run `claude` (or `tmux` in fallback) against a session ID present in the index. The only extra flags are those in `resume.default_flags`, allow-listed to `--remote-control`, `--model`, `--effort`, `--permission-mode`, `--name`.
- Indexed transcript text is truncated to 500 characters per field; full transcripts are read from disk on demand and never copied elsewhere.
- Shell history: opt-in (`usage.shell_history`, default `false`); keep only `claude` invocations; store subcommand and flag names and a timestamp only, never values or prompts.
- No hardcoded list of Claude Code features; the catalog comes from the installed binary's `--help`.
- Default port `7787`, configurable.
- Config at `$XDG_CONFIG_HOME/vigil/config.toml`; data at `$XDG_DATA_HOME/vigil/index.db`.
- Verification gate (CI runs exactly this): `ruff check . && ruff format --check . && mypy && pytest`.
- Tests never launch a real Claude session or a real terminal; they put fake `claude`, `tmux`, terminal and `wt.exe` executables on `PATH` that record their argv and cwd.
- Every PR updates `CHANGELOG.md` under `[Unreleased]` (one line per change).

## Append points

Add one entry per line; do not rewrite what is there.

- `COMMANDS` in `src/vigil/cli.py`: register a CLI command by appending one line.
- `tests/conftest.py`: add fixtures at the end.
- `CHANGELOG.md`, under `[Unreleased]`: one line per change.
- `src/vigil/index/migrations/`: add a new numbered migration file; never edit an applied one.
- The router list in `src/vigil/web/app.py`: include a new router by appending one line.

## Workflow

- PRs: `Closes #N`, one PR per issue.
- Conventional commit messages.
- The repository is public: no real user names, home paths, hostnames, emails, tokens or real
  session content in any committed file, fixtures included. Use placeholders.
