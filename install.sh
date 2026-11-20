#!/bin/sh
# install.sh: install vigil for your own user, and upgrade it when run again.
#
#   curl -fsSL https://raw.githubusercontent.com/SDNick484/vigil/main/install.sh | sh
#
# It creates a private Python environment in ~/.local/share/vigil-venv, installs vigil from
# GitHub into it, and links the `vigil` command into ~/.local/bin. It never uses sudo, never runs
# as root and never edits a shell rc file. Run `sh install.sh --help` for the options.

set -eu

REPO_URL="https://github.com/SDNick484/vigil"
RUN_DOC="$REPO_URL#run"
MIN_MINOR=14
STOP_PATTERN='/vigil serve( |$)'

usage() {
    cat <<'EOF'
Usage: sh install.sh [options]

Installs vigil into ~/.local/share/vigil-venv (or $XDG_DATA_HOME/vigil-venv) and links
~/.local/bin/vigil to it. Running it again upgrades vigil and restarts it if it is running.

Options:
  --ref REF      install this git branch, tag or commit (default: the ref of the last
                 install, else main)
  --yes          do not ask before going ahead (passed on to `vigil setup`)
  --no-setup     do not run `vigil setup` after a first install
  --dry-run      print every action and change nothing
  --uninstall    run `vigil setup --remove`, then remove the environment and the link
  --purge        with --uninstall, also remove vigil's data and config folders
  -h, --help     show this text

It needs Python 3.14 or newer and git, and refuses to run as root. It never touches ~/.claude.
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: %s\n' "$*" >&2; }
die() {
    warn "$*"
    exit 1
}

# Print the command, then run it unless this is a dry run.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        say "would run: $*"
    else
        say "+ $*"
        "$@"
    fi
}

parse_args() {
    REF=""
    ASSUME_YES=0
    NO_SETUP=0
    DRY_RUN=0
    UNINSTALL=0
    PURGE=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)
                [ $# -ge 2 ] || die "--ref needs a value"
                REF=$2
                shift
                ;;
            --ref=*) REF=${1#--ref=} ;;
            --yes | -y) ASSUME_YES=1 ;;
            --no-setup) NO_SETUP=1 ;;
            --dry-run) DRY_RUN=1 ;;
            --uninstall) UNINSTALL=1 ;;
            --purge) PURGE=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                die "unknown option: $1"
                ;;
        esac
        shift
    done
    if [ "$PURGE" = 1 ] && [ "$UNINSTALL" = 0 ]; then
        die "--purge only goes with --uninstall"
    fi
    if [ -n "$REF" ]; then
        check_ref "$REF"
    fi
}

# A ref goes into a pip URL and a TOML string, so allow only what a git ref needs.
check_ref() {
    case "$1" in
        -* | *[!A-Za-z0-9._/+-]*) die "not a usable git ref: $1" ;;
    esac
}

setup_paths() {
    [ -n "${HOME:-}" ] || die "HOME is not set"
    case "${XDG_DATA_HOME:-}" in
        /*) DATA_HOME=$XDG_DATA_HOME ;;
        *) DATA_HOME=$HOME/.local/share ;;
    esac
    case "${XDG_CONFIG_HOME:-}" in
        /*) CONFIG_HOME=$XDG_CONFIG_HOME ;;
        *) CONFIG_HOME=$HOME/.config ;;
    esac
    VENV=$DATA_HOME/vigil-venv
    BIN_DIR=$HOME/.local/bin
    LINK=$BIN_DIR/vigil
    MARKER=$VENV/vigil-install.toml
}

refuse_root() {
    if [ "$(id -u)" = 0 ]; then
        die "do not run this as root or with sudo; it installs for your own user. Run it as that user."
    fi
}

py_ok() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" >/dev/null 2>&1
}

find_python() {
    PY=""
    for candidate in python3.15 python3.14 python3; do
        found=$(command -v "$candidate" 2>/dev/null) || continue
        if py_ok "$found"; then
            PY=$found
            return 0
        fi
    done
    warn "vigil needs Python 3.$MIN_MINOR or newer, and none was found."
    warn "Install it with your distribution's package manager, for example:"
    warn "  Gentoo:         emerge --ask dev-lang/python:3.$MIN_MINOR"
    warn "  Debian/Ubuntu:  apt install python3.$MIN_MINOR python3.$MIN_MINOR-venv"
    warn "  Fedora:         dnf install python3.$MIN_MINOR"
    warn "Your system's default python3 can stay as it is. Then run this script again."
    exit 1
}

# Refuse when vigil is already installed some other way.
check_existing() {
    on_path=$(command -v vigil 2>/dev/null || true)
    if [ -n "$on_path" ]; then
        case "$on_path" in
            /*) ;;
            *) on_path="" ;;
        esac
    fi
    if [ -n "$on_path" ] && [ "$(dirname "$on_path")" != "$BIN_DIR" ]; then
        die "a vigil is already on your PATH at $on_path, outside $BIN_DIR, so it was installed another way (a package manager?). Upgrade or remove it with that tool first."
    fi
    if command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q 'vigil-sessions'; then
        die "vigil-sessions is already installed with pipx. Upgrade it with: pipx install --force git+$REPO_URL (or remove it with: pipx uninstall vigil-sessions) and run this script again."
    fi
    if [ -e "$LINK" ] || [ -L "$LINK" ]; then
        if [ ! -L "$LINK" ] || [ "$(readlink "$LINK")" != "$VENV/bin/vigil" ]; then
            die "$LINK exists and is not a link to $VENV/bin/vigil. Move it away if it is safe to, then run this script again."
        fi
    fi
}

recorded_ref() {
    [ -f "$MARKER" ] || return 0
    value=$(sed -n 's/^ref = "\([^"]*\)"$/\1/p' "$MARKER" | head -n 1)
    case "$value" in
        '' | -* | *[!A-Za-z0-9._/+-]*) ;;
        *) printf '%s' "$value" ;;
    esac
}

write_marker() {
    stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [ "$DRY_RUN" = 1 ]; then
        say "would write $MARKER (ref = \"$REF\", source = \"install.sh\", installed_at = \"$stamp\")"
        return 0
    fi
    {
        # shellcheck disable=SC2016
        printf '# Written by install.sh; `vigil upgrade` reads it.\n'
        printf 'ref = "%s"\n' "$REF"
        printf 'source = "install.sh"\n'
        printf 'installed_at = "%s"\n' "$stamp"
    } >"$MARKER"
    say "Wrote $MARKER."
}

# Run `vigil <args>` with its input from a terminal, because the script itself may be what
# stdin is (curl | sh) and vigil's questions must not read it.
vigil_cmd() {
    if [ "$ASSUME_YES" = 1 ]; then
        "$LINK" "$@" --yes </dev/null
    elif (: </dev/tty) 2>/dev/null; then
        "$LINK" "$@" </dev/tty
    else
        say "No terminal to ask on, so vigil $* was not run. Run it yourself: vigil $*"
        return 0
    fi
}

setup_available() {
    "$LINK" setup --help >/dev/null 2>&1
}

# The lines printed below are meant to be copied literally, so single quotes are right.
# shellcheck disable=SC2016
path_hint() {
    case ":${PATH:-}:" in
        *":$BIN_DIR:"*) return 0 ;;
    esac
    shell_name=$(basename "${SHELL:-sh}")
    say ""
    say "$BIN_DIR is not on your PATH, so a new terminal will not find \`vigil\`."
    case "$shell_name" in
        fish)
            say "Add this line to ~/.config/fish/config.fish:"
            say '  fish_add_path "$HOME/.local/bin"'
            ;;
        zsh)
            say "Add this line to ~/.zshrc:"
            say '  export PATH="$HOME/.local/bin:$PATH"'
            ;;
        bash)
            say "Add this line to ~/.bashrc:"
            say '  export PATH="$HOME/.local/bin:$PATH"'
            ;;
        *)
            say "Add this line to your shell's startup file (for bash, ~/.bashrc):"
            say '  export PATH="$HOME/.local/bin:$PATH"'
            ;;
    esac
    say "This script does not edit shell files."
}

is_running() {
    command -v pgrep >/dev/null 2>&1 && pgrep -f -- "$STOP_PATTERN" >/dev/null 2>&1
}

# After an upgrade: restart vigil if it is running. A service that `vigil setup` installed is
# restarted by its own tool, which setup does not wrap, so say the command instead.
restart_vigil() {
    if ! is_running; then
        say "vigil is not running; the new version starts the next time it does."
        return 0
    fi
    if [ -f "$CONFIG_HOME/systemd/user/vigil.service" ]; then
        say "vigil runs as a systemd user service. Restart it to use the new version:"
        say "  systemctl --user restart vigil.service"
    elif [ -f "$CONFIG_HOME/rc/init.d/vigil" ]; then
        say "vigil runs as an OpenRC user service. Restart it to use the new version:"
        say "  rc-service --user vigil restart"
    elif [ "$DRY_RUN" = 1 ]; then
        say "would run: pkill -f -- '$STOP_PATTERN'"
        say "would run: setsid -f $LINK serve"
    else
        say "Restarting vigil."
        pkill -f -- "$STOP_PATTERN" || true
        sleep 1
        if command -v setsid >/dev/null 2>&1; then
            setsid -f "$LINK" serve >/dev/null 2>&1 </dev/null
        else
            say "setsid was not found, so start vigil yourself: vigil serve"
        fi
    fi
}

install() {
    find_python
    check_existing
    command -v git >/dev/null 2>&1 ||
        die "git is needed, because vigil is installed straight from GitHub. Install git and run this script again."

    if [ -x "$VENV/bin/vigil" ]; then
        FIRST=0
    else
        FIRST=1
    fi
    if [ -z "$REF" ]; then
        REF=$(recorded_ref)
        if [ -n "$REF" ]; then
            say "Using ref $REF, recorded by the last install (give --ref to change it)."
        else
            REF=main
        fi
    fi

    say "Python:      $PY"
    say "Environment: $VENV"
    say "Link:        $LINK"
    say "Source:      git+$REPO_URL@$REF"
    if [ "$FIRST" = 1 ]; then
        say "This is a first install."
    else
        say "vigil is already installed there; this upgrades it."
    fi
    say ""

    if [ -d "$VENV" ] && [ -x "$VENV/bin/python" ]; then
        py_ok "$VENV/bin/python" ||
            die "$VENV was made with a Python older than 3.$MIN_MINOR. Run this script with --uninstall, then again."
    else
        run mkdir -p "$DATA_HOME"
        run "$PY" -m venv "$VENV" ||
            die "could not create the environment (on Debian or Ubuntu the python3.$MIN_MINOR-venv package is needed)."
    fi
    run "$VENV/bin/python" -m pip install --upgrade "git+$REPO_URL@$REF" ||
        die "pip could not install vigil from $REPO_URL@$REF."
    write_marker
    run mkdir -p "$BIN_DIR"
    run ln -sfn "$VENV/bin/vigil" "$LINK"
    path_hint

    if [ "$DRY_RUN" = 1 ]; then
        if [ "$FIRST" = 1 ]; then
            if [ "$NO_SETUP" = 0 ]; then
                say "would run: vigil setup$([ "$ASSUME_YES" = 1 ] && printf ' --yes')"
            fi
        else
            restart_vigil
        fi
        say ""
        say "Dry run: nothing was changed."
        return 0
    fi

    say ""
    say "Installed: $("$LINK" --version 2>/dev/null || echo vigil)"
    if [ "$FIRST" = 0 ]; then
        restart_vigil
    elif [ "$NO_SETUP" = 1 ]; then
        say "Skipped \`vigil setup\`. To keep vigil running, run it later: vigil setup"
    elif setup_available; then
        say ""
        vigil_cmd setup || say "vigil setup did not finish; run it again when you like: vigil setup"
    else
        say "This vigil has no \`vigil setup\` command. To start it, see: $RUN_DOC"
    fi
}

uninstall() {
    if [ ! -f "$MARKER" ] && [ ! -x "$VENV/bin/vigil" ]; then
        say "No vigil environment at $VENV; nothing to remove there."
    else
        if [ -x "$VENV/bin/vigil" ] && [ "$(readlink "$LINK" 2>/dev/null || true)" = "$VENV/bin/vigil" ]; then
            if [ "$DRY_RUN" = 1 ]; then
                say "would run: vigil setup --remove"
            elif setup_available; then
                vigil_cmd setup --remove || say "vigil setup --remove did not finish; see \`vigil doctor\`."
            else
                say "This vigil has no \`vigil setup\` command, so any autostart entry you made by hand stays."
            fi
        fi
        run rm -rf "$VENV"
    fi
    if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$VENV/bin/vigil" ]; then
        run rm -f "$LINK"
    elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
        say "Leaving $LINK: it is not the link this script made."
    fi
    if [ "$PURGE" = 1 ]; then
        run rm -rf "$DATA_HOME/vigil" "$CONFIG_HOME/vigil"
    else
        say "Kept vigil's data ($DATA_HOME/vigil) and config ($CONFIG_HOME/vigil); --purge removes them."
    fi
    say "Your Claude Code sessions in ~/.claude were not touched."
    if [ "$DRY_RUN" = 1 ]; then
        say ""
        say "Dry run: nothing was changed."
    fi
}

main() {
    parse_args "$@"
    setup_paths
    refuse_root
    if [ "$UNINSTALL" = 1 ]; then
        uninstall
    else
        install
    fi
}

# The whole script is read before anything runs, so a download cut short does nothing.
main "$@"
