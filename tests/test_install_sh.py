"""install.sh, run for real with `sh` against a temporary tree.

HOME and the XDG folders point into the tree, and PATH holds only recording fakes (python, pip,
pipx, git, id, pgrep, pkill, setsid, sleep and the vigil the fake pip "installs") plus thin
wrappers for the few coreutils the script uses. Nothing real is installed, started or stopped.
"""

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
SH = shutil.which("sh") or "/bin/sh"
TOOLS = (
    "mkdir",
    "ln",
    "rm",
    "date",
    "dirname",
    "basename",
    "readlink",
    "sed",
    "head",
    "grep",
    "cat",
    "chmod",
)

LOGGER = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "{log}"
"""

VIGIL_STUB = """#!/bin/sh
printf '%s\\n' "vigil $*" >> "{log}"
case "$1" in
    --version) echo "vigil-sessions 0.0.0" ;;
    setup) [ "$2" = --help ] && [ -e "{tree}/no-setup" ] && exit 2 ;;
esac
exit 0
"""

VENV_PYTHON = """#!/bin/sh
printf '%s\\n' "venv-python $*" >> "{log}"
case "$1" in
    -c) exit 0 ;;
    -m) shift; [ "$1" = pip ] && shift && exec "$(dirname "$0")/pip" "$@" ;;
esac
exit 0
"""

PIP_STUB = """#!/bin/sh
printf '%s\\n' "pip $*" >> "{log}"
cat > "$(dirname "$0")/vigil" <<'VIGIL'
{vigil}
VIGIL
chmod +x "$(dirname "$0")/vigil"
"""

PYTHON_STUB = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "{log}"
case "$1" in
    -c) exit {version_exit} ;;
    -m)
        if [ "$2" = venv ]; then
            mkdir -p "$3/bin"
            cat > "$3/bin/python" <<'PYTHON'
{venv_python}
PYTHON
            cat > "$3/bin/pip" <<'PIP'
{pip}
PIP
            chmod +x "$3/bin/python" "$3/bin/pip"
        fi
        ;;
esac
exit 0
"""


@dataclass
class Result:
    code: int
    out: str
    err: str


class Tree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.data = self.home / ".local" / "share"
        self.config = self.home / ".config"
        self.bin = self.home / ".local" / "bin"
        self.fake = root / "fakebin"
        self.sys = root / "sysbin"
        self.log = root / "calls.log"
        for folder in (self.home, self.fake, self.sys):
            folder.mkdir(parents=True)
        self.log.write_text("")
        for tool in TOOLS:
            real = shutil.which(tool)
            assert real is not None, tool
            self.stub(self.sys, tool, f'#!/bin/sh\nexec {real} "$@"\n')
        self.python("python3.14")
        self.recorder("id", 'echo "${FAKE_UID:-1000}"')
        self.recorder("git")
        self.recorder("sleep")
        self.recorder("pkill")
        self.recorder("setsid")
        self.recorder("pgrep", f'[ -e "{root}/running" ]')

    @property
    def venv(self) -> Path:
        return self.data / "vigil-venv"

    @staticmethod
    def stub(folder: Path, name: str, text: str) -> Path:
        path = folder / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def recorder(self, name: str, tail: str = "exit 0", folder: Path | None = None) -> Path:
        text = LOGGER.format(name=name, log=self.log) + tail + "\n"
        return self.stub(folder or self.fake, name, text)

    def python(self, name: str, *, version_ok: bool = True) -> None:
        vigil = VIGIL_STUB.format(log=self.log, tree=self.root)
        pip = PIP_STUB.format(log=self.log, vigil=vigil)
        venv_python = VENV_PYTHON.format(log=self.log)
        text = PYTHON_STUB.format(
            name=name,
            log=self.log,
            version_exit=0 if version_ok else 1,
            venv_python=venv_python,
            pip=pip,
        )
        self.stub(self.fake, name, text)

    def calls(self, prefix: str = "") -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith(prefix)]

    def clear_calls(self) -> None:
        self.log.write_text("")

    def snapshot(self) -> list[str]:
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def run(
        self,
        *args: str,
        extra_path: tuple[Path, ...] = (),
        env: dict[str, str] | None = None,
        home_bin_on_path: bool = True,
    ) -> Result:
        folders = [self.fake, *extra_path, self.sys]
        if home_bin_on_path:
            folders.append(self.bin)
        full = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": os.pathsep.join(str(f) for f in folders),
            "SHELL": "/bin/bash",
            **(env or {}),
        }
        # A new session has no controlling terminal, so /dev/tty cannot be opened, whatever
        # terminal pytest itself runs in.
        done = subprocess.run(
            [SH, str(SCRIPT), *args],
            env=full,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=60,
        )
        return Result(done.returncode, done.stdout, done.stderr)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


@pytest.fixture
def installed(tree: Tree) -> Tree:
    assert tree.run("--yes").code == 0
    tree.clear_calls()
    return tree


# The script itself ---------------------------------------------------------------------------


def test_shellcheck_is_clean() -> None:
    """Fails, never skips, when shellcheck is missing: CI installs it, and so does `.[dev]`."""
    found = shutil.which("shellcheck") or str(Path(sys.executable).parent / "shellcheck")
    assert Path(found).exists(), "shellcheck is not installed; install it to run this gate"
    done = subprocess.run([found, "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_is_posix_sh_and_syntax_checks() -> None:
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "set -eu" in text
    assert "sudo" not in text.replace("never uses sudo", "").replace("or with sudo", "")
    assert subprocess.run([SH, "-n", str(SCRIPT)]).returncode == 0


def test_help_changes_nothing(tree: Tree) -> None:
    result = tree.run("--help")
    assert result.code == 0 and "--uninstall" in result.out
    assert tree.calls() == [] and tree.snapshot() == []


def test_unknown_option_and_bad_ref(tree: Tree) -> None:
    assert tree.run("--frobnicate").code == 1
    for ref in ("-x", "a b", "main;rm", '"q"'):
        assert tree.run("--ref", ref).code == 1
    assert tree.run("--purge").code == 1
    assert tree.snapshot() == []


# Refusals ------------------------------------------------------------------------------------


def test_picks_the_newest_python(tree: Tree) -> None:
    tree.python("python3.15")
    tree.python("python3", version_ok=False)
    assert tree.run("--no-setup").code == 0
    assert any(c.startswith("python3.15 -m venv") for c in tree.calls())
    assert not any(c.startswith("python3.14 -m venv") for c in tree.calls())


def test_skips_a_python3_that_is_too_old(tree: Tree) -> None:
    (tree.fake / "python3.14").unlink()
    tree.python("python3", version_ok=False)
    result = tree.run()
    assert result.code == 1
    assert "emerge --ask dev-lang/python:3.14" in result.err
    assert tree.snapshot() == []


def test_uses_python3_when_it_is_new_enough(tree: Tree) -> None:
    (tree.fake / "python3.14").unlink()
    tree.python("python3")
    assert tree.run("--no-setup").code == 0
    assert any(c.startswith("python3 -m venv") for c in tree.calls())


def test_refuses_root(tree: Tree) -> None:
    result = tree.run("--yes", env={"FAKE_UID": "0"})
    assert result.code == 1 and "root" in result.err
    assert tree.snapshot() == []
    assert not any(c.startswith("python") for c in tree.calls())
    assert tree.run("--uninstall", env={"FAKE_UID": "0"}).code == 1


def test_refuses_an_existing_pipx_install(tree: Tree) -> None:
    tree.recorder("pipx", "echo '   package vigil-sessions 0.1.0, installed using Python 3.14'")
    result = tree.run()
    assert result.code == 1 and "pipx" in result.err
    assert tree.snapshot() == []
    assert not any(c.startswith("pip ") for c in tree.calls())


def test_pipx_without_vigil_is_fine(tree: Tree) -> None:
    tree.recorder("pipx", "echo '   package other 1.0'")
    assert tree.run("--no-setup").code == 0


def test_refuses_a_vigil_outside_local_bin(tree: Tree, tmp_path: Path) -> None:
    elsewhere = tmp_path / "usr-bin"
    elsewhere.mkdir()
    tree.recorder("vigil", folder=elsewhere)
    result = tree.run(extra_path=(elsewhere,))
    assert result.code == 1 and str(elsewhere / "vigil") in result.err
    assert tree.snapshot() == []


def test_refuses_to_replace_a_link_it_did_not_make(tree: Tree) -> None:
    tree.bin.mkdir(parents=True)
    (tree.bin / "vigil").write_text("not ours")
    result = tree.run(home_bin_on_path=False)
    assert result.code == 1 and "not a link" in result.err
    assert (tree.bin / "vigil").read_text() == "not ours"


def test_needs_git(tree: Tree) -> None:
    (tree.fake / "git").unlink()
    result = tree.run()
    assert result.code == 1 and "git" in result.err


# Install -------------------------------------------------------------------------------------


def test_fresh_install_writes_the_marker_and_the_link(tree: Tree) -> None:
    result = tree.run("--yes")
    assert result.code == 0, result.err
    link = tree.bin / "vigil"
    assert link.is_symlink() and os.readlink(link) == str(tree.venv / "bin" / "vigil")
    marker = tomllib.loads((tree.venv / "vigil-install.toml").read_text())
    assert marker["ref"] == "main" and marker["source"] == "install.sh"
    assert marker["installed_at"].endswith("Z") and "T" in marker["installed_at"]
    assert any(
        c.startswith(
            "pip install --upgrade --force-reinstall git+https://github.com/SDNick484/vigil@main"
        )
        for c in tree.calls()
    )
    assert tree.calls("vigil setup --help") and tree.calls("vigil setup --yes")


def test_setup_without_yes_needs_a_terminal(tree: Tree) -> None:
    result = tree.run()
    assert result.code == 0
    assert "No terminal" in result.out
    assert not tree.calls("vigil setup --yes")
    assert [c for c in tree.calls("vigil setup") if c != "vigil setup --help"] == []


def test_no_setup_skips_it(tree: Tree) -> None:
    assert tree.run("--yes", "--no-setup").code == 0
    assert tree.calls("vigil setup") == []


def test_without_a_setup_command_it_points_at_the_readme(tree: Tree) -> None:
    (tree.root / "no-setup").write_text("")
    result = tree.run("--yes")
    assert result.code == 0
    assert "github.com/SDNick484/vigil#run" in result.out
    assert tree.calls("vigil setup --yes") == []


def test_ref_is_used_recorded_and_kept(tree: Tree) -> None:
    assert tree.run("--no-setup", "--ref", "v1.2").code == 0
    assert tree.calls("pip install")[0].endswith("vigil@v1.2")
    assert tomllib.loads((tree.venv / "vigil-install.toml").read_text())["ref"] == "v1.2"
    tree.clear_calls()
    assert tree.run().code == 0
    assert tree.calls("pip install")[0].endswith("vigil@v1.2")
    tree.clear_calls()
    assert tree.run("--ref=main").code == 0
    assert tree.calls("pip install")[0].endswith("vigil@main")


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("/bin/bash", "~/.bashrc"),
        ("/usr/bin/zsh", "~/.zshrc"),
        ("/usr/bin/fish", "~/.config/fish/config.fish"),
        ("/bin/dash", "your shell's startup file"),
    ],
)
def test_path_hint_names_the_rc_file(tree: Tree, shell: str, expected: str) -> None:
    result = tree.run("--no-setup", env={"SHELL": shell}, home_bin_on_path=False)
    assert result.code == 0
    assert expected in result.out and "is not on your PATH" in result.out
    assert (".local/bin" in result.out) and "sudo" not in result.out
    assert not (tree.home / ".bashrc").exists() and not (tree.home / ".zshrc").exists()


def test_no_path_hint_when_it_is_on_path(tree: Tree) -> None:
    result = tree.run("--no-setup")
    assert "not on your PATH" not in result.out


# Upgrade -------------------------------------------------------------------------------------


def test_rerun_upgrades_and_restarts(installed: Tree) -> None:
    (installed.root / "running").write_text("")
    result = installed.run("--yes")
    assert result.code == 0, result.err
    calls = installed.calls()
    assert any(c.startswith("pip install --upgrade --force-reinstall git+") for c in calls)
    assert not any(c.startswith("python3.14 -m venv") for c in calls)
    assert any(c.startswith("pkill -f -- ") and "vigil serve" in c for c in calls)
    assert f"setsid -f {installed.bin / 'vigil'} serve" in calls
    assert not installed.calls("vigil setup --yes")


def test_rerun_leaves_a_stopped_vigil_alone(installed: Tree) -> None:
    result = installed.run("--yes")
    assert result.code == 0
    assert installed.calls("pkill") == [] and installed.calls("setsid") == []


@pytest.mark.parametrize(
    ("unit", "command"),
    [
        ("systemd/user/vigil.service", "systemctl --user restart vigil.service"),
        ("rc/init.d/vigil", "rc-service --user vigil restart"),
    ],
)
def test_rerun_names_the_service_restart_instead_of_killing(
    installed: Tree, unit: str, command: str
) -> None:
    (installed.root / "running").write_text("")
    path = installed.config / unit
    path.parent.mkdir(parents=True)
    path.write_text("# unit")
    result = installed.run("--yes")
    assert result.code == 0 and command in result.out
    assert installed.calls("pkill") == [] and installed.calls("setsid") == []


# Dry run -------------------------------------------------------------------------------------


def test_dry_run_changes_nothing(tree: Tree) -> None:
    result = tree.run("--dry-run", "--yes")
    assert result.code == 0
    assert tree.snapshot() == []
    assert (
        "would run:" in result.out and "git+https://github.com/SDNick484/vigil@main" in result.out
    )
    assert "vigil-install.toml" in result.out and "vigil setup" in result.out
    assert not any(c.startswith(("pip", "venv-python")) for c in tree.calls())
    assert not any("-m venv" in c for c in tree.calls())


def test_dry_run_on_an_install_changes_nothing(installed: Tree) -> None:
    (installed.root / "running").write_text("")
    before = installed.snapshot()
    marker = (installed.venv / "vigil-install.toml").read_text()
    result = installed.run("--dry-run")
    assert result.code == 0 and "would run: pkill" in result.out
    assert installed.snapshot() == before
    assert (installed.venv / "vigil-install.toml").read_text() == marker
    assert installed.calls("pip") == [] and installed.calls("pkill") == []


def test_dry_run_uninstall_changes_nothing(installed: Tree) -> None:
    before = installed.snapshot()
    result = installed.run("--uninstall", "--purge", "--dry-run")
    assert result.code == 0 and "would run: rm -rf" in result.out
    assert installed.snapshot() == before
    assert installed.calls("vigil setup --remove") == []


# Uninstall -----------------------------------------------------------------------------------


def _with_user_files(tree: Tree) -> Path:
    (tree.data / "vigil").mkdir(parents=True)
    (tree.data / "vigil" / "index.db").write_text("db")
    (tree.config / "vigil").mkdir(parents=True)
    (tree.config / "vigil" / "config.toml").write_text("")
    claude = tree.home / ".claude" / "projects"
    claude.mkdir(parents=True)
    (claude / "session.jsonl").write_text("{}")
    other = tree.data / "other-tool"
    other.mkdir()
    return other


def test_uninstall_removes_the_venv_and_the_link_only(installed: Tree) -> None:
    other = _with_user_files(installed)
    result = installed.run("--uninstall", "--yes")
    assert result.code == 0, result.err
    assert installed.calls("vigil setup --remove --yes")
    assert not installed.venv.exists() and not (installed.bin / "vigil").is_symlink()
    assert (installed.data / "vigil" / "index.db").exists()
    assert (installed.config / "vigil" / "config.toml").exists()
    assert (installed.home / ".claude" / "projects" / "session.jsonl").exists()
    assert other.exists()
    assert "--purge" in result.out


def test_purge_also_removes_data_and_config_but_never_claude(installed: Tree) -> None:
    other = _with_user_files(installed)
    assert installed.run("--uninstall", "--purge", "--yes").code == 0
    assert not installed.venv.exists()
    assert not (installed.data / "vigil").exists() and not (installed.config / "vigil").exists()
    assert (installed.home / ".claude" / "projects" / "session.jsonl").exists()
    assert other.exists()


def test_uninstall_leaves_a_link_it_did_not_make(installed: Tree) -> None:
    link = installed.bin / "vigil"
    link.unlink()
    link.write_text("someone else's")
    result = installed.run("--uninstall", "--yes")
    assert result.code == 0 and "Leaving" in result.out
    assert link.read_text() == "someone else's"
    assert not installed.venv.exists()


def test_uninstall_without_an_install_removes_nothing(tree: Tree) -> None:
    other = _with_user_files(tree)
    before = tree.snapshot()
    result = tree.run("--uninstall", "--yes")
    assert result.code == 0 and "nothing to remove" in result.out
    assert tree.snapshot() == before and other.exists()
