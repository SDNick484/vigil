#!/bin/sh
# install.sh: install a vigil hub on Alpine Linux (OpenRC), and upgrade it when run again.
#
#   doas sh deploy/alpine/install.sh            (from a checkout of this repository)
#   doas sh deploy/alpine/install.sh --from 'vigil-sessions @ git+https://github.com/SDNick484/vigil'
#
# Run it as root. Alpine's stable Python is older than the 3.14 vigil needs, so it installs uv,
# lets uv fetch a standalone CPython 3.14 for musl into /opt/vigil/python, and builds a venv in
# /opt/vigil/venv. Config goes in /etc/vigil, data in /var/lib/vigil, both kept when it runs again.
# Run `sh install.sh --help` for the options.

set -eu

PREFIX_DIR=/opt/vigil
SERVICE_USER=vigil
SERVICE_GROUP=vigil
PYTHON_VERSION=3.14
PACKAGE=vigil-sessions

usage() {
    emit <<'EOF2'
Usage: sh install.sh [options]

Installs vigil as an OpenRC service run by the system user "vigil". Running it again upgrades
the code and keeps /etc/vigil/config.toml and everything in /var/lib/vigil.

Options:
  --from SPEC    what to install: a folder holding a vigil checkout, or a pip requirement
                 such as 'vigil-sessions @ git+https://github.com/SDNick484/vigil'
                 (default: the checkout this script sits in)
  --dry-run      print every action and change nothing
  -h, --help     show this text

Set ROOT to a folder to install under it instead of / (for a staging tree; the service
paths inside the files are not rewritten). It needs root, apk, and a network connection.
EOF2
}

say() { printf '%s\n' "$*"; }
# Copy standard input to standard output with shell built-ins only (no `cat`).
emit() {
    while IFS= read -r line; do
        printf '%s\n' "$line"
    done
}
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
    FROM=""
    DRY_RUN=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --from)
                [ $# -ge 2 ] || die "--from needs a value"
                FROM="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
}

# Where this script sits, and the two files it installs from there.
locate() {
    case "$0" in
        */*) here="${0%/*}" ;;
        *) here="." ;;
    esac
    HERE=$(cd "$here" && pwd) || die "cannot find the folder of $0"
    INITD_SRC="$HERE/vigil.initd"
    CONFIG_SRC="$HERE/config.example.toml"
    [ -f "$INITD_SRC" ] || die "missing $INITD_SRC"
    [ -f "$CONFIG_SRC" ] || die "missing $CONFIG_SRC"
    if [ -z "$FROM" ]; then
        FROM=$(cd "$HERE/../.." && pwd) || die "cannot find the checkout above $HERE"
    fi
}

check_root() {
    [ "$DRY_RUN" = 1 ] && return 0
    [ -z "$ROOT" ] || return 0
    [ "$(id -u)" = 0 ] || die "run this as root (it creates a system user and writes under /etc, /opt and /var/lib)"
}

create_user() {
    if [ "$DRY_RUN" = 1 ] || ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        run addgroup -S "$SERVICE_GROUP"
    fi
    if [ "$DRY_RUN" = 1 ] || ! id "$SERVICE_USER" >/dev/null 2>&1; then
        run adduser -S -D -H -G "$SERVICE_GROUP" "$SERVICE_USER"
    fi
}

install_python() {
    run apk add --no-cache uv
    run env UV_PYTHON_INSTALL_DIR="$ROOT$PREFIX_DIR/python" uv python install "$PYTHON_VERSION"
    run env UV_PYTHON_INSTALL_DIR="$ROOT$PREFIX_DIR/python" \
        uv venv --python "$PYTHON_VERSION" --allow-existing "$ROOT$PREFIX_DIR/venv"
}

install_vigil() {
    # A folder is installed as it is; anything else is a pip requirement. The package is
    # reinstalled so that running this again picks up new code even at the same version.
    if [ -d "$FROM" ]; then
        run uv pip install --python "$ROOT$PREFIX_DIR/venv/bin/python" \
            --reinstall-package "$PACKAGE" "$FROM"
    else
        run uv pip install --python "$ROOT$PREFIX_DIR/venv/bin/python" \
            --upgrade --reinstall-package "$PACKAGE" "$FROM"
    fi
}

install_files() {
    run install -d -m 0755 "$ROOT/etc/init.d"
    run install -m 0755 "$INITD_SRC" "$ROOT/etc/init.d/vigil"
    run install -d -m 0750 -o root -g "$SERVICE_GROUP" "$ROOT/etc/vigil"
    run install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$ROOT/var/lib/vigil"
    if [ "$DRY_RUN" = 1 ] || [ ! -e "$ROOT/etc/vigil/config.toml" ]; then
        run install -m 0640 -o root -g "$SERVICE_GROUP" "$CONFIG_SRC" "$ROOT/etc/vigil/config.toml"
    else
        say "keeping the existing $ROOT/etc/vigil/config.toml"
    fi
}

next_steps() {
    emit <<EOF2

vigil is installed in $PREFIX_DIR. Next:

  1. Edit /etc/vigil/config.toml: your hub's public name, your machines, and your Cloudflare
     Access team domain and AUD tag. Every value in it is a placeholder.
  2. Make the read token the example names, readable by the service only:
         (umask 077; tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40 > /etc/vigil/read.token)
         chown $SERVICE_USER:$SERVICE_GROUP /etc/vigil/read.token
     (or delete the [[server.tokens]] table).
  3. Start it, and start it at boot:
         rc-update add vigil default
         rc-service vigil start
  4. Check it: curl -s 127.0.0.1:7787/api/v1/health
EOF2
}

main() {
    parse_args "$@"
    ROOT="${ROOT:-/}"
    ROOT="${ROOT%/}" # empty means the real root: "$ROOT/etc" is then /etc
    locate
    check_root
    [ "$DRY_RUN" = 1 ] && say "dry run: nothing will be changed"
    say "installing $FROM"
    create_user
    install_python
    install_vigil
    install_files
    next_steps
}

main "$@"
