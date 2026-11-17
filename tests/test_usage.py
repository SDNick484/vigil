"""Tests for the usage report: the aggregation, `vigil usage`, the API and the page. Only fake
`claude` executables are ever run (see FakeBin)."""

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_URL, FakeBin, VigilEnv
from tests.test_catalog import install_claude
from vigil import claude_cli, cli
from vigil.catalog import refresh_catalog
from vigil.config import load_config
from vigil.index.db import connect
from vigil.index.queries import record_launch
from vigil.usage import FeatureUse, build_report
from vigil.web.app import create_app

FIXTURE_REPO = "/home/user/src/fixture-repo"
BROKEN_REPO = "/home/user/src/broken-repo"
AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    claude_cli.clear_cache()
    yield
    claude_cli.clear_cache()


@pytest.fixture
def catalog_db(index_db: sqlite3.Connection, fake_bin: FakeBin) -> sqlite3.Connection:
    """The fixture index plus the catalog of a fake claude 2.1.312."""
    install_claude(fake_bin)
    assert refresh_catalog(index_db) is not None
    return index_db


def launch(
    conn: sqlite3.Connection,
    flags: list[str],
    subcommand: str | None = None,
    at: datetime = AT,
    source: str = "shell",
) -> None:
    record_launch(
        conn,
        source="vigil" if source == "vigil" else "shell",
        subcommand=subcommand,
        flags=flags,
        at=at,
        session_id=None,
    )


def by_name(uses: list[FeatureUse], kind: str) -> dict[str, FeatureUse]:
    return {u.name: u for u in uses if u.kind == kind}


def never(conn: sqlite3.Connection) -> set[str]:
    return {e.name for e in build_report(conn).never_used}


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_feature_counts_from_fixtures(index_db: sqlite3.Connection) -> None:
    report = build_report(index_db)
    tools = by_name(report.features, "tool")
    assert tools["Bash"].count == 2  # one in the fixture repo, one in the hand-made transcript
    assert tools["Read"].count == 1
    assert by_name(report.features, "slash_command")["/greet"].count == 1
    assert by_name(report.features, "skill")["fixture-skill"].count == 1
    assert by_name(report.features, "mcp_server")["fixture"].count == 1
    # Most used first, and every feature knows when it was last used.
    counts = [u.count for u in report.features]
    assert counts == sorted(counts, reverse=True)
    assert all(u.last_used_at is not None for u in report.features)
    assert report.launch_flags == []
    assert report.never_used == []  # no catalog yet: nothing was compared
    assert not report.catalog_available


def test_never_used_excludes_used_flags(catalog_db: sqlite3.Connection) -> None:
    assert {"--resume", "--name", "--bg", "attach"} <= never(catalog_db)
    launch(catalog_db, ["--resume", "--name"])
    launch(catalog_db, [], subcommand="attach")
    unused = never(catalog_db)
    assert "--resume" not in unused
    assert "--name" not in unused
    assert "attach" not in unused
    assert "--bg" in unused
    assert "agents" in unused
    # Only top-level flags and subcommands are compared, never subcommand_flag entries.
    assert all(e.kind in ("flag", "subcommand") for e in build_report(catalog_db).never_used)


def test_short_flag_counts_as_long(catalog_db: sqlite3.Connection) -> None:
    launch(catalog_db, ["-n", "--name", "-r"])
    report = build_report(catalog_db)
    flags = by_name(report.launch_flags, "flag")
    assert flags["--name"].count == 1  # one launch, however many spellings it used
    assert "-n" not in flags
    assert "--resume" in flags
    assert "--name" not in never(catalog_db)
    assert "--resume" not in never(catalog_db)


def test_short_flag_stays_as_typed_without_a_catalog(index_db: sqlite3.Connection) -> None:
    launch(index_db, ["-n"])
    assert by_name(build_report(index_db).launch_flags, "flag").keys() == {"-n"}


def test_launch_counts_and_recency(catalog_db: sqlite3.Connection) -> None:
    launch(catalog_db, ["--bg"], at=AT)
    launch(catalog_db, ["--bg"], at=datetime(2026, 11, 5, tzinfo=UTC))
    launch(catalog_db, ["--json"], subcommand="agents")
    uses = build_report(catalog_db).launch_flags
    bg = by_name(uses, "flag")["--bg"]
    assert (bg.count, bg.last_used_at) == (2, datetime(2026, 11, 5, tzinfo=UTC))
    assert by_name(uses, "subcommand")["agents"].count == 1
    # A subcommand's flag is its own entry; it does not mark a top-level flag of that name used.
    assert by_name(uses, "flag")["agents --json"].count == 1
    assert "--json" not in by_name(uses, "flag")
    assert uses == sorted(uses, key=lambda u: (-u.count, u.kind, u.name))


def test_since_limits_features_and_launches(catalog_db: sqlite3.Connection) -> None:
    launch(catalog_db, ["--bg"], at=AT)
    everything = build_report(catalog_db)
    later = build_report(catalog_db, since=datetime(2030, 1, 1, tzinfo=UTC))
    assert everything.features and everything.launch_flags
    assert later.features == [] and later.launch_flags == []
    assert "--bg" in {e.name for e in later.never_used}


def test_per_project_filter(index_db: sqlite3.Connection) -> None:
    overall = by_name(build_report(index_db).features, "tool")
    fixture = by_name(build_report(index_db, project_key=FIXTURE_REPO).features, "tool")
    broken = by_name(build_report(index_db, project_key=BROKEN_REPO).features, "tool")
    assert broken.keys() == {"Bash"} and broken["Bash"].count == 1
    assert fixture["Bash"].count == overall["Bash"].count - 1
    assert "Read" in fixture
    assert build_report(index_db, project_key="/no/such/project").features == []


def test_history_disabled_message(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    install_claude(vigil_env.bin)
    code, out, _ = run(capsys, "usage")
    assert code == 0
    assert "usage.shell_history = true" in out
    assert "Never tried" in out
    assert "--bg" in out  # the catalog of the fake claude was refreshed
    assert "Bash" in out


def test_cli_json_and_project(vigil_env: VigilEnv, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "usage", "--json", "--project", BROKEN_REPO)
    assert code == 0
    data = json.loads(out)
    assert set(data) == {
        "features",
        "launch_flags",
        "never_used",
        "history_enabled",
        "catalog_available",
    }
    assert {(f["kind"], f["name"]) for f in data["features"]} == {
        ("model", "claude-haiku-5-5"),
        ("tool", "Bash"),
    }
    assert data["history_enabled"] is False and data["never_used"] == []


def enable_history(env: VigilEnv, tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.write_text("ls -la\nclaude -n work --bg\nclaude attach\n")
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "vigil" / "config.toml"
    config.write_text(
        f'claude_home = "{env.claude_home}"\n[usage]\nshell_history = true\n'
        f'history_files = ["{history}"]\n'
    )


def test_cli_imports_history_when_enabled(
    vigil_env: VigilEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_claude(vigil_env.bin)
    enable_history(vigil_env, tmp_path)
    code, out, _ = run(capsys, "usage", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["history_enabled"] is True
    launched = {(u["kind"], u["name"]) for u in data["launch_flags"]}
    assert launched == {("flag", "--name"), ("flag", "--bg"), ("subcommand", "attach")}
    never_used = {e["name"] for e in data["never_used"]}
    assert "--bg" not in never_used and "--name" not in never_used and "agents" in never_used
    _, text, _ = run(capsys, "usage")
    assert "usage.shell_history" not in text  # enabled: no how-to-enable message


def test_usage_api_and_page(web_client: TestClient, vigil_env: VigilEnv) -> None:
    install_claude(vigil_env.bin)
    data = web_client.get("/api/v1/usage").json()
    assert {f["name"] for f in data["features"]} >= {"Bash", "Read", "/greet"}
    assert data["history_enabled"] is False and data["catalog_available"] is True
    assert "--bg" in {e["name"] for e in data["never_used"]}
    scoped = web_client.get("/api/v1/usage", params={"project": BROKEN_REPO}).json()
    assert {f["name"] for f in scoped["features"]} == {"claude-haiku-5-5", "Bash"}
    future = web_client.get("/api/v1/usage", params={"since": "2030-01-01T00:00:00Z"}).json()
    assert future["features"] == []

    catalog = web_client.get("/api/v1/catalog").json()
    names = {(e["kind"], e["name"]) for e in catalog}
    assert {("flag", "--resume"), ("subcommand", "attach")} <= names
    name_entry = next(e for e in catalog if e["name"] == "--name")
    assert name_entry["short"] == "-n" and name_entry["description"]

    page = web_client.get("/usage")
    assert page.status_code == 200
    html = page.text
    for heading in ("Inside sessions", "How you launch claude", "Never tried"):
        assert heading in html
    assert "usage.shell_history = true" in html
    assert "fixture-skill" in html and "--bg" in html
    assert 'href="/usage"' in web_client.get("/").text  # in the nav
    assert "fixture-repo" in html  # the project filter
    assert "Launches are not tied" in web_client.get("/usage", params={"project": BROKEN_REPO}).text


def test_page_without_claude_or_history(web_client: TestClient) -> None:
    html = web_client.get("/usage").text
    assert "No catalog" in html
    assert "usage.shell_history = true" in html
    assert web_client.get("/api/v1/catalog").json() == []


def test_page_imports_history_when_enabled(vigil_env: VigilEnv, tmp_path: Path) -> None:
    install_claude(vigil_env.bin)
    enable_history(vigil_env, tmp_path)
    app = create_app(
        load_config(), conn_factory=lambda: connect(vigil_env.index), start_watcher=False
    )
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        client.get("/usage")  # imports
        html = client.get("/usage").text
        again = client.get("/api/v1/usage").json()
    assert "usage.shell_history" not in html
    assert "<code>--bg</code>" in html
    # A second import reads only what was appended, so nothing is counted twice.
    assert {u["name"]: u["count"] for u in again["launch_flags"]}["--bg"] == 1
