import re
import sqlite3
from html.parser import HTMLParser

from fastapi.testclient import TestClient
from markupsafe import escape

from tests.conftest import VigilEnv
from vigil.index.db import connect
from vigil.index.queries import get_session
from vigil.index.scanner import link_chains

NAMED = "e47d2def-0bb3-4d50-9f9c-8e38e4cdcfba"  # titled "fixture-named", in fixture-repo
PLAIN = "7fb815ab-28b5-4123-85d0-64e6fd3b4141"
FORKED = "68e04fa1-b95c-4610-8221-261395d0ca5d"
HANDMADE = "11111111-1111-4111-8111-111111111111"  # the only session in broken-repo

HX = {"HX-Request": "true"}
ARCHIVE = "INSERT INTO session_meta (session_id, archived, updated_at) VALUES (?, 1, '')"


class Rows(HTMLParser):
    """The project headings (the link in each `<h2>`) and the ids of the session rows, in page
    order."""

    def __init__(self) -> None:
        super().__init__()
        self.sequence: list[tuple[str, str]] = []
        self._in_h2 = False
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h2":
            self._in_h2 = True
        if tag == "a":
            self._in_link = True
        if tag == "tr" and (attributes.get("id") or "").startswith("row-"):
            self.sequence.append(("row", (attributes["id"] or "")[4:]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_h2 = False
        if tag == "a":
            self._in_link = False

    def handle_data(self, data: str) -> None:
        if self._in_h2 and self._in_link and data.strip():
            self.sequence.append(("project", data.strip()))


def parse(html: str) -> list[tuple[str, str]]:
    rows = Rows()
    rows.feed(html)
    return rows.sequence


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
    assert "1 session" in response.text

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
    row = re.search(rf'<tr[^>]*id="row-{NAMED}".*?</tr>', page, re.S)
    assert row is not None
    post = re.search(r'hx-post="([^"]+)"', row.group(0))
    target = re.search(r'hx-target="#([^"]+)"', row.group(0))
    assert post is not None and target is not None
    assert f'id="{target.group(1)}"' in row.group(0)

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
