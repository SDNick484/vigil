import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient
from markupsafe import escape

from tests.conftest import VigilEnv
from vigil.claude_cli import Capabilities, LiveSession
from vigil.index.db import connect
from vigil.index.queries import SessionRow, get_session
from vigil.index.scanner import link_chains
from vigil.peers import PeerSession
from vigil.web.pages import StatusCounts, count_statuses, group_by_project, split_groups

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"  # titled "fixture-named", in fixture-repo
PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
HANDMADE = "11111111-1111-4111-8111-111111111111"  # the only session in broken-repo

HX = {"HX-Request": "true"}
CAPS = Capabilities(version="2.1.312", has_bg=True, has_attach=True, has_agents_json=True)
ARCHIVE = "INSERT INTO session_meta (session_id, archived, updated_at) VALUES (?, 1, '')"


class Rows(HTMLParser):
    """The project labels (the element with class `label` in each project summary) and the ids of
    the session rows, in page order."""

    def __init__(self) -> None:
        super().__init__()
        self.sequence: list[tuple[str, str]] = []
        self._in_label = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "label" in (attributes.get("class") or "").split():
            self._in_label = True
        if tag == "li" and (attributes.get("id") or "").startswith("row-"):
            self.sequence.append(("row", (attributes["id"] or "")[4:]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._in_label = False

    def handle_data(self, data: str) -> None:
        if self._in_label and data.strip():
            self.sequence.append(("project", data.strip()))


def parse(html: str) -> list[tuple[str, str]]:
    rows = Rows()
    rows.feed(html)
    return rows.sequence


def row_html(page: str, row_id: str) -> str:
    match = re.search(rf'<li [^>]*id="{re.escape(row_id)}".*?</li>', page, re.S)
    assert match, f"no row {row_id}"
    return match.group(0)


def row_ids(html: str) -> list[str]:
    return [value for kind, value in parse(html) if kind == "row"]


def stored(env: VigilEnv, session_id: str) -> dict[str, str]:
    conn = connect(env.index)
    row = get_session(conn, session_id)
    conn.close()
    assert row is not None
    return {
        "last_user": row.last_user or "",
        "last_assistant": row.last_assistant or "",
        "cwd": row.cwd or "",
    }


def sql(env: VigilEnv, statement: str, *params: object) -> None:
    conn = sqlite3.connect(env.index)
    with conn:
        conn.execute(statement, params)
    conn.close()


def test_index_groups_by_project(vigil_env: VigilEnv, web_client: TestClient) -> None:
    response = web_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = parse(response.text)

    headings = [value for kind, value in page if kind == "project"]
    assert headings == ["fixture-repo", "broken-repo"]  # the group with the newest session first
    assert len(row_ids(response.text)) == 7
    # Rows sit under their own project's heading, newest activity first.
    broken_at = page.index(("project", "broken-repo"))
    assert [value for kind, value in page[broken_at:] if kind == "row"] == [HANDMADE]
    api_order = [s["id"] for s in web_client.get("/api/v1/sessions").json()]
    fixture_group = [value for kind, value in page[:broken_at] if kind == "row"]
    assert fixture_group == [i for i in api_order if i != HANDMADE]

    # Each row links to its detail page and has a Resume form posting to the resume endpoint.
    assert f'href="/sessions/{NAMED}"' in response.text
    assert f'hx-post="/sessions/{NAMED}/resume"' in response.text
    assert "fixture-named" in response.text


def test_index_filters_by_project_and_archived(vigil_env: VigilEnv, web_client: TestClient) -> None:
    broken = web_client.get("/", params={"project": "/home/user/src/broken-repo"})
    assert row_ids(broken.text) == [HANDMADE]
    sql(vigil_env, ARCHIVE, NAMED)
    assert NAMED not in row_ids(web_client.get("/").text)
    assert NAMED in row_ids(web_client.get("/", params={"all": "true"}).text)


def test_search_filters_rows(web_client: TestClient) -> None:
    response = web_client.get("/", params={"q": "fixture named"}, headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text  # just the results, for htmx to swap in
    assert row_ids(response.text) == [NAMED]
    assert "across 1 project" in response.text and "<strong>1</strong> idle" in response.text

    nothing = web_client.get("/", params={"q": "nothing-matches"}, headers=HX)
    assert row_ids(nothing.text) == []
    assert "No sessions match." in nothing.text

    # The same query without htmx is the whole page, with the box holding the query.
    full = web_client.get("/", params={"q": "fixture named"})
    assert "<html" in full.text and row_ids(full.text) == [NAMED]
    assert 'value="fixture named"' in full.text
    # The search box filters live through htmx.
    assert 'hx-get="/"' in full.text and 'hx-target="#results"' in full.text


def test_detail_shows_last_exchange_and_command(
    vigil_env: VigilEnv, web_client: TestClient
) -> None:
    response = web_client.get(f"/sessions/{NAMED}")
    assert response.status_code == 200
    data = stored(vigil_env, NAMED)
    assert data["last_user"] and data["last_assistant"]
    assert str(escape(data["last_user"])) in response.text
    assert str(escape(data["last_assistant"])) in response.text
    command = f"cd /home/user/src/fixture-repo && claude --resume {NAMED}"
    assert f'<code id="command">{escape(command)}</code>' in response.text
    assert f'data-copy="{escape(command)}"' in response.text  # the Copy button
    assert f'hx-post="/sessions/{NAMED}/resume"' in response.text
    assert f'hx-post="/sessions/{NAMED}/meta"' in response.text
    assert "fixture-named" in response.text

    by_prefix = web_client.get(f"/sessions/{NAMED[:8]}")
    assert f"claude --resume {NAMED}" in by_prefix.text


def test_detail_unknown_session_is_404_page(web_client: TestClient) -> None:
    response = web_client.get("/sessions/no-such-session")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "no-such-session" in response.text


def test_detail_lists_chain(vigil_env: VigilEnv, web_client: TestClient) -> None:
    assert "<h2>Chain</h2>" not in web_client.get(f"/sessions/{FORKED}").text
    conn = connect(vigil_env.index)
    with conn:
        conn.execute(
            "UPDATE sessions SET previous_session_id = ? WHERE session_id = ?", (PLAIN, FORKED)
        )
        link_chains(conn)
    conn.close()

    page = web_client.get(f"/sessions/{FORKED}").text
    assert "<h2>Chain</h2>" in page
    assert f'<a href="/sessions/{PLAIN}">{PLAIN[:8]}</a>' in page
    assert page.index(PLAIN[:8]) < page.index(f"<strong>{FORKED[:8]}</strong>")  # oldest first
    # The list shows the chain once, by its newest session.
    ids = row_ids(web_client.get("/").text)
    assert FORKED in ids and PLAIN not in ids


def test_resume_button_shows_refusal_text(vigil_env: VigilEnv, web_client: TestClient) -> None:
    """What a click does: htmx posts the form to the resume endpoint and the page's handler shows
    the reason from the 409 in the row's result element."""
    page = web_client.get("/").text
    row = row_html(page, f"row-{NAMED}")
    post = re.search(r'hx-post="([^"]+)"', row)
    target = re.search(r'hx-target="#([^"]+)"', row)
    assert post is not None and target is not None
    assert f'id="{target.group(1)}"' in row

    # No claude on PATH: the session's folder does not exist here either, so resume refuses.
    response = web_client.post(post.group(1))
    assert response.status_code == 409
    reason = response.json()["reason"]
    assert response.json()["ok"] is False and reason
    # The handler turns a JSON answer into text in the target; it reads `reason` from it.
    assert "htmx:beforeSwap" in page and "data.reason" in page and "textContent" not in reason


def test_edit_form_round_trips_through_meta_endpoint(
    vigil_env: VigilEnv, web_client: TestClient
) -> None:
    page = web_client.get(f"/sessions/{NAMED}").text
    form = re.search(r'<form class="meta".*?</form>', page, re.S)
    assert form is not None
    action = re.search(r'hx-post="([^"]+)"', form.group(0))
    assert action is not None
    # The form sends hidden "false" before each checkbox, so an unchecked box means false.
    data = {"label": "Tidy <b>", "note": "a note", "pinned": ["false", "true"], "archived": "false"}
    response = web_client.post(action.group(1), data=data)
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Tidy <b>" and response.json()["pinned"] is True

    # Values come back escaped, in the form and in the list.
    page = web_client.get(f"/sessions/{NAMED}").text
    assert "Tidy &lt;b&gt;" in page and "<b>" not in page.replace("<b>Tidy", "")
    assert re.search(r'name="pinned" value="true" checked', page)
    assert "Tidy &lt;b&gt;" in web_client.get("/").text


def test_no_external_urls_in_pages(vigil_env: VigilEnv, web_client: TestClient) -> None:
    ids = row_ids(web_client.get("/", params={"all": "true"}).text)
    assert len(ids) == 7
    pages = [web_client.get("/", params={"all": "true"}).text]
    pages += [web_client.get(f"/sessions/{i}").text for i in ids]
    pages.append(web_client.get("/sessions/missing").text)
    for html in pages:
        assert "http://" not in html and "https://" not in html
        # Everything a page loads is served by vigil itself.
        for url in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert url.startswith("/"), url


def test_static_files_served_and_contained(web_client: TestClient) -> None:
    css = web_client.get("/static/vigil.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    script = web_client.get("/static/htmx.min.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert script.text.startswith("/* htmx 2.")  # the version is noted in a comment
    assert "htmx" in script.text and len(script.text) > 40_000  # the real library, minified
    assert web_client.get("/static/missing.css").status_code == 404
    assert web_client.get("/static/%2e%2e/pages.py").status_code == 404
    assert web_client.get("/static/../pages.py").status_code == 404


def make_row(project: str, status: str, session_id: str, **fields: object) -> SessionRow:
    return SessionRow(
        session_id=session_id,
        cwd=f"/home/user/src/{project}",
        git_branch=None,
        version=None,
        title=None,
        first_prompt=None,
        last_user=None,
        last_assistant=None,
        started_at=None,
        last_activity_at=None,
        message_count=1,
        previous_session_id=None,
        machine="laptop",
        project_key=f"/home/user/src/{project}",
        project_label=project,
        chain_id=session_id,
        transcript_path="/nonexistent",
        status=status,  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )


def make_live(client: TestClient, sessions: dict[str, str]) -> None:
    """Make the service see `sessions` (ID to `background` or `interactive`) as live."""
    live = {i: LiveSession(i, kind, None, None) for i, kind in sessions.items()}  # type: ignore[arg-type]
    client.app.state.vigil.live.get = lambda **_: (CAPS, live)  # type: ignore[attr-defined]


def test_split_groups_active_first() -> None:
    rows = [
        make_row("a", "idle", "a1"),
        make_row("b", "idle", "b1"),
        make_row("a", "background", "a2"),
        make_row("c", "running", "c1"),
        make_row("b", "idle", "b2"),
    ]
    groups = group_by_project(rows)
    assert [g.label for g in groups] == ["a", "b", "c"]
    assert [(g.active, g.idle_count) for g in groups] == [(True, 1), (False, 2), (True, 0)]
    active, quiet = split_groups(groups)
    assert [g.label for g in active] == ["a", "c"]
    assert [g.label for g in quiet] == ["b"]


def test_stale_peer_session_is_quiet() -> None:
    stale = PeerSession(
        **{**make_row("a", "running", "a1").__dict__, "peer": "peer-b", "stale": True}
    )
    (group,) = group_by_project([stale])
    assert not group.active and group.idle_count == 1
    assert count_statuses([stale]) == StatusCounts(0, 0, 1, 1)


def test_count_statuses() -> None:
    rows = [
        make_row("a", "running", "a1"),
        make_row("a", "running", "a2"),
        make_row("b", "background", "b1"),
        make_row("b", "idle", "b2"),
        make_row("b", "idle", "b3"),
        make_row("c", "idle", "c1"),
        make_row("c", "unknown", "c2"),
    ]
    assert count_statuses(rows) == StatusCounts(2, 1, 4, 3)


def test_sessions_page_active_open_quiet_closed(
    vigil_env: VigilEnv, web_client: TestClient
) -> None:
    # Archive all but two of fixture-repo's sessions: the live one and one idle one.
    sql(
        vigil_env,
        "INSERT INTO session_meta (session_id, archived, updated_at) "
        "SELECT session_id, 1, '' FROM sessions WHERE session_id NOT IN (?, ?, ?)",
        NAMED,
        PLAIN,
        HANDMADE,
    )
    make_live(web_client, {NAMED: "background"})
    page = web_client.get("/").text
    assert page.count('<details class="project" open>') == 1
    assert page.count('<details class="project">') == 1
    active_at = page.index('<details class="project" open>')
    quiet_at = page.index('<details class="project">')
    assert active_at < quiet_at
    assert "fixture-repo" in page[active_at:quiet_at] and "broken-repo" in page[quiet_at:]
    assert "Active now" in page[:active_at] and "Quiet" in page[active_at:quiet_at]

    # Only the active project's idle row folds; the quiet project's row does not.
    assert 'class="session folded"' in row_html(page, f"row-{PLAIN}")
    assert "folded" not in row_html(page, f"row-{NAMED}")
    assert "folded" not in row_html(page, f"row-{HANDMADE}")
    assert page.count('class="more"') == 1
    assert "Show 1 idle session</button>" in page
    assert "1 session" in page[quiet_at:]  # the quiet project's one-line summary


def test_idle_fold_pluralises(web_client: TestClient) -> None:
    make_live(web_client, {NAMED: "interactive"})
    page = web_client.get("/").text
    idle = [i for i in row_ids(page) if i not in (NAMED, HANDMADE)]
    assert len(idle) > 1
    assert f"Show {len(idle)} idle sessions</button>" in page


def test_search_opens_matching_projects(web_client: TestClient) -> None:
    # Nothing is live, so fixture-repo is a quiet project; a search opens it all the same.
    quiet = web_client.get("/").text
    assert '<details class="project">' in quiet and '<details class="project" open>' not in quiet
    page = web_client.get("/", params={"q": "fixture"}).text
    assert page.count('<details class="project" open>') == 1  # broken-repo has no match
    assert '<details class="project">' not in page
    assert len(row_ids(page)) > 1

    # An active project opens with every session unfolded.
    make_live(web_client, {NAMED: "background"})
    folded = web_client.get("/").text
    assert re.search(r'class="[^"]*\bfolded\b', folded) and 'class="more"' in folded
    page = web_client.get("/", params={"q": "fixture"}).text
    assert not re.search(r'class="[^"]*\bfolded\b', page) and 'class="more"' not in page


def test_only_this_project_link(web_client: TestClient) -> None:
    key = "/home/user/src/broken-repo"
    page = web_client.get("/").text
    assert 'href="/?project=/home/user/src/broken-repo">Only this project</a>' in page
    # It sits in the card body, not in the summary, so tapping the summary still toggles.
    card = page[page.index("broken-repo</span>") :]
    assert card.index("</summary>") < card.index("Only this project")
    filtered = web_client.get("/", params={"project": key}).text
    assert "Only this project" not in filtered and "show all" in filtered


def test_summary_line(web_client: TestClient) -> None:
    make_live(web_client, {NAMED: "background", FORKED: "interactive", PLAIN: "interactive"})
    page = web_client.get("/").text
    total = len(row_ids(page))
    assert "<strong>2</strong> running" in page
    assert "<strong>1</strong> background" in page
    assert f"<strong>{total - 3}</strong> idle" in page
    assert "across 2 projects" in page
    assert "Quiet" in page  # broken-repo


LINK = "https://claude.ai/code/session_PLACEHOLDER01"


def test_open_link_on_live_row_and_detail_page(vigil_env: VigilEnv, web_client: TestClient) -> None:
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, "2026-11-17T11:05:00.000000+00:00", NAMED)  # fmt: skip
    sql(vigil_env, "UPDATE sessions SET remote_url = ?, remote_url_at = ? WHERE session_id = ?",
        LINK, "2026-11-17T11:05:00.000000+00:00", FORKED)  # fmt: skip
    # Idle: a link from an earlier run is not offered.
    page = web_client.get("/").text
    assert LINK not in page
    assert LINK not in web_client.get(f"/sessions/{NAMED}").text

    make_live(web_client, {NAMED: "background"})
    page = web_client.get("/").text
    # At the desk (a loopback request) it is "Open in Claude", a plain link, beside Resume.
    assert f'<a class="open" href="{LINK}" rel="noopener">Open in Claude</a>' in row_html(
        page, f"row-{NAMED}"
    )
    assert "Resume</button>" in row_html(page, f"row-{NAMED}")
    detail = web_client.get(f"/sessions/{NAMED}").text
    assert f'<a class="open" href="{LINK}" rel="noopener">Open in Claude</a>' in detail
    # Only the live session with a link gets one (FORKED has a link but is idle).
    assert page.count(LINK) == 1


def test_resume_answer_handler_follows_link_and_offers_restart(web_client: TestClient) -> None:
    page = web_client.get("/").text
    for text in (
        "data.remote_url",
        "window.location.href = link",
        "data.at_desk === true",  # at the desk the link is shown, not followed
        "data.pending",
        "the Remote Control link is not available yet",
        "data.copy_of",  # a background session that keeps its own options came back as a copy
        "this background session keeps its own options.",
        "claude attach ([0-9a-f]{8})",  # a copy whose full id is unknown: its job id
        "data.restartable",
        "Restart with Remote Control",
        "restart-confirm",
        "button.restart",
    ):
        assert text in page, text


CONFIRM_COPY = "Restarting stops this session and interrupts anything it is doing."
BUSY_COPY = "This session is working right now. Restarting stops it mid-task."


def make_busy(client: TestClient, session_id: str, *, busy: bool) -> None:
    live = {session_id: LiveSession(session_id, "background", None, None, busy=busy)}
    client.app.state.vigil.live.get = lambda **_: (CAPS, live)  # type: ignore[attr-defined]


def test_restart_confirm_step_on_background_row_and_detail(web_client: TestClient) -> None:
    # Nothing to restart on an idle session.
    assert "restart-remote" not in row_html(web_client.get("/").text, f"row-{NAMED}")
    assert "/restart-remote" not in web_client.get(f"/sessions/{NAMED}").text.split("<script>")[0]

    make_busy(web_client, NAMED, busy=False)
    page = web_client.get("/").text
    row = row_html(page, f"row-{NAMED}")
    assert f'action="/sessions/{NAMED}/restart-remote"' in row
    assert 'name="confirm" value="yes"' in row
    assert CONFIRM_COPY in row and BUSY_COPY not in row
    assert ">Restart</button>" in row and ">Cancel</button>" in row
    assert 'class="restart-confirm" hidden' in row  # opened by the answer's restart button
    detail = web_client.get(f"/sessions/{NAMED}").text
    assert f'action="/sessions/{NAMED}/restart-remote"' in detail
    assert CONFIRM_COPY in detail and BUSY_COPY not in detail

    # A working session gets the plain warning.
    make_busy(web_client, NAMED, busy=True)
    assert BUSY_COPY in row_html(web_client.get("/").text, f"row-{NAMED}")
    assert BUSY_COPY in web_client.get(f"/sessions/{NAMED}").text
    # Only that session's: another background row beside it has none.
    live = {
        NAMED: LiveSession(NAMED, "background", None, None, busy=True),
        FORKED: LiveSession(FORKED, "background", None, None, busy=False),
    }
    web_client.app.state.vigil.live.get = lambda **_: (CAPS, live)  # type: ignore[attr-defined]
    page = web_client.get("/").text
    assert BUSY_COPY in row_html(page, f"row-{NAMED}")
    assert BUSY_COPY not in row_html(page, f"row-{FORKED}")


def test_detail_shows_token_panel(vigil_env: VigilEnv, web_client: TestClient) -> None:
    usage = Path(__file__).parent / "fixtures" / "transcripts" / "handmade" / "usage.jsonl"
    sql(
        vigil_env, "UPDATE sessions SET transcript_path = ? WHERE session_id = ?", str(usage), NAMED
    )
    page = web_client.get(f"/sessions/{NAMED}").text
    assert "<h2>Where the tokens went</h2>" in page
    assert '<span class="tabular-nums">180</span>' in page  # total output tokens
    assert "64%" in page  # cache hits: 4500 of 7018 input-side tokens
    assert "Bash" in page and "40,000" in page  # the largest tool output
    # The rest of what a review is given: models, wall-clock span, launch flags, per-turn series.
    panel = page.split("<h2>Where the tokens went</h2>")[1].split("</section>")[0]
    assert "claude-sonnet-5-5" in panel and "claude-opus-5-5" in panel
    assert "<dt>Wall-clock span</dt><dd>0 min</dd>" in panel
    assert "<dt>Launch flags</dt>" in panel and "none recorded" in panel
    assert '<table class="turns">' in panel and "<th>Cache read</th>" in panel

    # No panel when the transcript cannot be read, or for a session without usage.
    sql(vigil_env, "UPDATE sessions SET transcript_path = '/nonexistent.jsonl' WHERE 1")
    assert "Where the tokens went" not in web_client.get(f"/sessions/{NAMED}").text
