# Changelog

All notable changes to this project are recorded here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add one line per change under
`[Unreleased]`.

## [Unreleased]

### Added
- Package scaffold, TOML config loading with validation, `vigil` CLI skeleton and CI.
- systemd, OpenRC and XDG autostart service files in `contrib/`, with install, run and config documentation.
- Tolerant, incremental transcript parser (`vigil.transcript`) with sanitized real fixtures.
- SQLite index with numbered migrations, incremental transcript scanner, project grouping by git remote, and session queries (`vigil.index`, `vigil.projects`).
- Detect installed `claude` capabilities (`--bg`, `attach`, `agents --json`) and live sessions, with fixtures captured from Claude Code 2.1.312.
- Feature catalog (`vigil.catalog`): flags and subcommands parsed from the installed `claude --help`, stored per Claude Code version (migration 0002).
- Opt-in launch history from shell history (`vigil.history`): bash, zsh and fish history, `claude` invocations only, subcommand and flag names only.
- Resume launcher (`vigil.resume`, `vigil.terminal`): `claude --bg` with tmux fallback, terminal detection (config template, WSL Windows Terminal, `$TERMINAL`, known terminals), refusals for open sessions, missing folders and missing tools.
- CLI commands `vigil scan`, `ls`, `show` and `resume`, with `--json` output (`vigil.serialize.session_to_dict`) that the API reuses.
