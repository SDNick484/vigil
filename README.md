# vigil

vigil is a local service, web UI and CLI for Claude Code. It indexes every Claude Code session on
a Linux machine, lets you resume one with a click in the right folder, and shows your Claude Code
usage against what the installed version offers. `claude agents` shows live sessions only; vigil
adds history, resume across projects and usage.

## Install

vigil needs Python 3.14 or newer and runs on Linux (WSL2 counts). It also needs Claude Code
(`claude`) on your `PATH` to resume sessions; indexing and browsing work without it.

    pipx install vigil-sessions

Where the system Python is older than 3.14, tell pipx which interpreter to use:

    pipx install --python python3.14 vigil-sessions

Until the first release is on PyPI, install from a clone with `pipx install .`. The package is
called `vigil-sessions`; the command is `vigil`.

## Run

`vigil serve` starts the web UI and JSON API on `http://127.0.0.1:7787`. It binds to loopback only,
and `--port N` (or `port` in the config file) changes the port. Run it in the foreground to try it,
then pick one of the service files in [`contrib/`](contrib/) to keep it running. All of them run
`vigil serve` as your own user and expect `vigil` in `~/.local/bin`, where pipx puts it; edit the
path if yours is elsewhere.

**systemd** (user unit):

    mkdir -p ~/.config/systemd/user
    cp contrib/systemd/vigil.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now vigil.service

**OpenRC** (user service, OpenRC 0.60 or newer):

    mkdir -p ~/.config/rc/init.d
    install -m 755 contrib/openrc/vigil ~/.config/rc/init.d/vigil
    rc-service --user vigil start
    rc-update --user add vigil default

Set `vigil_bin` or `vigil_args` in `~/.config/rc/conf.d/vigil` to change the binary or pass flags.

**XDG autostart** (any desktop that honours it, the fallback when you have neither):

    mkdir -p ~/.config/autostart
    cp contrib/xdg/vigil.desktop ~/.config/autostart/

The service needs `claude` (and `tmux`, if you use the tmux launcher) on its `PATH`. The systemd
and OpenRC files add `~/.local/bin`, `/usr/local/bin`, `/usr/bin` and `/bin`; if `claude` lives
elsewhere, extend the `PATH` line in the file.

## Configuration

vigil reads `$XDG_CONFIG_HOME/vigil/config.toml` (`~/.config/vigil/config.toml` when
`XDG_CONFIG_HOME` is unset). The file is optional and so is every key. An unknown key, a wrong
type or a disallowed value stops vigil with an error that names it. The index is kept in
`$XDG_DATA_HOME/vigil/index.db` (`~/.local/share/vigil/index.db` by default).

| Key | Default | Meaning |
| --- | --- | --- |
| `claude_home` | `"~/.claude"` | Claude Code's data folder. vigil only reads it and never writes there. |
| `port` | `7787` | Port for `vigil serve`, 1 to 65535. |
| `resume.default_flags` | `[]` | Flags added to every resume. Only `--remote-control`, `--model`, `--effort`, `--permission-mode` and `--name` are allowed, each optionally followed by its value, e.g. `["--model", "opus"]`. |
| `resume.terminal_command` | unset | Command template that opens a terminal. Must contain `{attach}`, e.g. `"mate-terminal -- {attach}"`. Unset means `$TERMINAL`, then a list of known terminals. |
| `resume.launcher` | `"auto"` | `"auto"` uses `claude --bg` when the installed `claude` supports it and falls back to tmux; `"bg"` and `"tmux"` force one. |
| `usage.shell_history` | `false` | Read your shell history to see how you launch `claude`. Off unless you turn it on. |
| `usage.history_files` | `[]` | History files to read. Empty means the default bash, zsh and fish locations. A leading `~` is expanded. |
| `usage.claude_aliases` | `[]` | Extra command names to count as `claude`, e.g. `["cl"]`. |

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

## License

MIT. See [LICENSE](LICENSE).
