# Installer, upgrade and Gentoo ebuild: design

Date: 2026-11-17
Status: approved

## Intent

Installing vigil today is several manual steps: make a virtual environment, pip-install from git,
link the command into `~/.local/bin`, then set up autostart or a service. Upgrading means
remembering a long pip line. `vigil setup` and `vigil doctor` (#31) cover what happens after the
package is installed; this covers getting it installed and keeping it current.

## Goals

- One command installs vigil for a general Linux user, and the same command upgrades it.
- `vigil upgrade` upgrades whatever kind of install it finds, then restarts vigil.
- Gentoo users install and upgrade with `emerge`, from the author's overlay.

## Non-goals

- Editing shell rc files, using `sudo`, or installing system-wide from the script.
- Tagged releases or PyPI publishing (the ebuild is live, `-9999`, until there are tags).
- Packages for other distributions.

## Approaches

1. **Script plus ebuild (chosen).** A POSIX `install.sh` for everyone; a live ebuild for Gentoo.
   They do not conflict: the script installs per user into a venv, the ebuild system-wide.
2. Script only. Simpler, but Gentoo users would manage a venv by hand outside portage.
3. Ebuild only. Most native for Gentoo, no help for anyone else.

## Design

### `install.sh` (repo root, POSIX `sh`)

- Finds Python 3.14 or newer (`python3.15`, `python3.14`, then `python3` if new enough); refuses
  to run as root.
- Creates or reuses `~/.local/share/vigil-venv` (the path the README already uses), pip-installs
  `git+https://github.com/SDNick484/vigil@<ref>` (default `main`, `--ref` to choose), and records
  the ref in `~/.local/share/vigil-venv/vigil-install.toml` for `vigil upgrade`.
- Links `~/.local/bin/vigil`. If `~/.local/bin` is not on `PATH`, prints the line to add and where.
- Hands off to `vigil setup` when that command exists, else prints the manual Run steps.
- Re-running upgrades in place and restarts vigil. `--uninstall` runs `vigil setup --remove` when
  available, then removes the venv and the link (data and config only with `--purge`).
- Refuses, with a message, if vigil is already installed by pipx or a package manager.

### `vigil upgrade`

Detects how the running vigil was installed: the script's venv (marker file present), another
venv, pipx (`pipx list` shows `vigil-sessions`), or a system package (`sys.prefix` under `/usr`).
venv: pip upgrade from the recorded ref (`--ref` overrides). pipx: `pipx install --force`. System
package: refuse and name the package manager command (`emerge -u dev-util/vigil` on Gentoo). Then
restart vigil the way `vigil setup` installed it. `--no-restart` and `--dry-run`.

### Live ebuild `dev-util/vigil-9999`

In `packaging/gentoo/dev-util/vigil/` upstream, copied unchanged into the overlay. `distutils-r1`
with `DISTUTILS_USE_PEP517=hatchling`, `git-r3`, `PYTHON_COMPAT=( python3_{14..15} )`, RDEPEND
from `pyproject.toml`, `distutils_enable_tests pytest`. Installs `contrib/` under
`/usr/share/vigil/` rather than as a system-wide autostart; each user opts in with `vigil setup`.
No KEYWORDS (live). A copy goes into the author's overlay with a README row.
