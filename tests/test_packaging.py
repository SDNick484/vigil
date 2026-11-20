import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EBUILD = ROOT / "packaging" / "gentoo" / "dev-util" / "vigil" / "vigil-9999.ebuild"


def _ebuild_text() -> str:
    return EBUILD.read_text()


def _ebuild_rdepend() -> list[str]:
    match = re.search(r'^RDEPEND="(.*?)"', _ebuild_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, "the ebuild has no RDEPEND"
    return match.group(1).split()


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject_dependencies() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names = set()
    for requirement in data["project"]["dependencies"]:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, requirement
        names.add(_normalize(match.group(0)))
    return names


def test_ebuild_rdepend_matches_pyproject_dependencies() -> None:
    atoms = _ebuild_rdepend()
    pattern = re.compile(r"dev-python/([a-z0-9._-]+)\[\$\{PYTHON_USEDEP\}\]")
    names = []
    for atom in atoms:
        match = pattern.fullmatch(atom)
        assert match is not None, f"not dev-python/<name>[${{PYTHON_USEDEP}}]: {atom}"
        names.append(_normalize(match.group(1)))
    assert len(names) == len(set(names)), "RDEPEND lists a package twice"
    assert set(names) == _pyproject_dependencies()


def test_ebuild_python_compat_includes_python_3_14() -> None:
    match = re.search(r"^PYTHON_COMPAT=\((.*?)\)", _ebuild_text(), re.MULTILINE)
    assert match is not None, "the ebuild has no PYTHON_COMPAT"
    assert "python3_{14..15}" in match.group(1) or "python3_14" in match.group(1).split()


def test_ebuild_is_live_without_keywords() -> None:
    lines = _ebuild_text().splitlines()
    assert not [line for line in lines if re.match(r"\s*KEYWORDS=", line)]
    assert 'EGIT_REPO_URI="https://github.com/SDNick484/vigil.git"' in lines
    assert "inherit distutils-r1 git-r3" in lines


def test_ebuild_installs_contrib_without_enabling_anything() -> None:
    text = _ebuild_text()
    assert "insinto /usr/share/vigil" in text
    assert "doins -r contrib" in text
    # No system-wide autostart entry and no enabled service.
    for forbidden in ("/etc/xdg", "systemd_dounit", "doinitd", "systemctl", "rc-update"):
        assert forbidden not in text
