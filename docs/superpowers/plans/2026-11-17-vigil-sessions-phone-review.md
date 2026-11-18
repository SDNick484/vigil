# vigil: sessions page, joining from a phone, session reviews — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is filed as one `Prompt:` issue and lands as one PR.

**Goal:** A sessions page that shows what is active at a glance on a phone; one-tap joining of a session from a phone through Claude's Remote Control; and on-demand Claude reviews of a session whose suggestions can be acted on with one tap.

**Architecture:** Part 1 reshapes the sessions page (pure template and grouping work). Part 2 indexes the Remote Control link from transcripts and teaches resume to wait for it on remote requests, plus a confirmed stop-and-restart. Part 3 adds a flag allow-list module, a token-numbers module, a review runner around `claude -p` on a non-persisted fork, review storage, suggestion actions (launch, default flags, project file apply), and peer forwarding of all of it.

**Tech Stack:** as today (Python 3.14, FastAPI, Jinja2, htmx, SQLite, httpx, pytest, ruff, mypy --strict); adds `tomlkit` (runtime) in Task 8 to edit vigil's config without losing comments.

**Spec:** `docs/superpowers/specs/2026-11-17-vigil-sessions-phone-review-design.md`

## Global Constraints

Everything in `CLAUDE.md` "Global Constraints" still holds, with these changes made by the task named (each task edits that CLAUDE.md line in the same PR):

- vigil never writes, renames or deletes anything under the Claude home (`~/.claude` or the configured `claude_home`). Unchanged.
- Indexed transcript text is truncated to 500 characters per field. Review text fields are truncated to 500 characters too.
- Flags vigil adds to any `claude` launch pass one allow-list in `vigil.flags` (Task 5): `--remote-control`, `--name`, `--model`, `--effort`, `--permission-mode` (not `bypassPermissions`), `--agent`, `--worktree`, `--chrome`, `--no-chrome`, `--ide`, `--verbose`, `--brief`, each with its value rule from the spec.
- vigil adds `--remote-control` to every session it starts (Task 3).
- vigil may run `claude stop <id>` only for "Restart with Remote Control", after a confirm (Task 4).
- vigil may run `claude -p --resume <id> --fork-session --no-session-persistence --tools ""` for an indexed session, only when the author presses Review (Task 7).
- vigil may start a new session only in the cwd of an indexed session (Task 8).
- vigil may write only `CLAUDE.md` and `.claude/settings.json` in a project folder, only on a confirmed Apply (Task 9).
- Peers forward Resume, Restart with Remote Control, Review and every suggestion action, and apply the same checks as for a local request (Tasks 4, 10).
- Tests never launch a real Claude session; fake `claude` executables print canned output. Fixtures use placeholder IDs and `https://claude.ai/code/session_PLACEHOLDER…` URLs.
- Every PR updates `CHANGELOG.md` under `[Unreleased]` (one line per change).

## Review Focus

1. **A stale Remote Control link from an earlier run.** A session that had Remote Control last week is resumed from a phone: vigil must wait for a `bridge_status` newer than the launch, never send the phone to the old link. Owned by Task 3 (`test_remote_resume_ignores_older_link`).
2. **Two taps from a phone in a row.** The second tap finds the session already in the background: with a link newer than its live start it answers with that link; without one it offers the hint and Restart, and never starts a second process. Owned by Task 3 (`test_second_remote_tap_opens_existing_link`).
3. **A review that runs out of budget or fails** (`claude -p` exits non-zero or reports `is_error`): the review is stored as `failed` with the reason, the numbers panel still shows, and Review can be pressed again. Owned by Task 7 (`test_review_failure_is_stored`).
4. **Apply when the target is missing, a symlink, or its folder is missing.** `CLAUDE.md` missing is created; `.claude/` missing is created; a symlink resolving outside the project folder or into the Claude home is refused and nothing is written. Owned by Task 9 (`test_apply_refuses_symlink_out`, `test_apply_creates_missing_dot_claude`).
5. **"Always use" with a config file that is missing or has comments.** A missing file is created with just `[resume] default_flags`; an existing one keeps its comments and other keys, and the service picks up the change without a restart. Owned by Task 8 (`test_always_use_keeps_comments`, `test_always_use_creates_file`).

---

## Part 1: the sessions page

### Task 1: Active and quiet project groups, phone-first rows

**Files:**
- Modify: `src/vigil/web/pages.py` (`ProjectGroup`, `group_by_project`, `sessions_page` context)
- Modify: `src/vigil/web/templates/sessions.html`, `src/vigil/web/templates/_session_row.html`, `src/vigil/web/static/vigil.css`, `src/vigil/web/templates/base.html` (fold script)
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces:
  - `ProjectGroup` gains `active: bool` (any session `running`/`background` and not stale) and `idle_count: int` (sessions that are not active).
  - `group_by_project(rows: list[SessionRow]) -> list[ProjectGroup]` keeps newest-first order; `split_groups(groups: list[ProjectGroup]) -> tuple[list[ProjectGroup], list[ProjectGroup]]` returns `(active, quiet)` preserving order.
  - `StatusCounts` (frozen dataclass): `running: int`, `background: int`, `idle: int`, `projects: int`; `count_statuses(rows: list[SessionRow]) -> StatusCounts` (`idle` counts every non-active row, `unknown` included; a stale peer row is never active).
  - Template context adds `active`, `quiet`, `counts`, `searching: bool` (`q` non-empty).

- [ ] **Step 1: Write failing tests** in `tests/test_pages.py`:
  - `test_split_groups_active_first`: rows for projects A (one `background`), B (all `idle`), C (one `running`) → active `[A, C]` in input order, quiet `[B]`.
  - `test_stale_peer_session_is_quiet`: a `PeerSession` with `status="running"`, `stale=True` → its project is quiet.
  - `test_count_statuses`: 2 running, 1 background, 3 idle, 1 unknown across 3 projects → `StatusCounts(2, 1, 4, 3)`.
  - `test_sessions_page_active_open_quiet_closed`: rendered `/` has `<details class="project" open>` for the active project and `<details class="project">` (no `open`) for the quiet one; the active project's idle row has class `folded`; the page contains `Show 1 idle session`.
  - `test_search_opens_matching_projects`: `/?q=<word>` renders every project `open` and no row with class `folded`.
  - `test_summary_line`: page contains `2</strong> running` style counts matching the fixture.
- [ ] **Step 2: Run** `pytest tests/test_pages.py -v`; the new tests fail.
- [ ] **Step 3: Implement** the dataclass fields, `split_groups`, `count_statuses` in `pages.py`, and the context keys.
- [ ] **Step 4: Templates and CSS** following the approved mockup (spec Part 1): summary line; "Active now" and "Quiet" section labels (omit a section with no groups); `<details class="project">` per group with `<summary>` showing label, active pills (`N running`, `N background`) or `N sessions · <last activity>`, and the project path; session rows as `<li>` two-line items (name + badges + note; status pill, branch in `<code>`, last activity, machine when peers are configured; actions right) replacing the `<table>`; idle rows in active groups get class `folded` and a `<button type="button" class="more">Show N idle session(s)</button>`; while `searching`, all groups are `open` and nothing is folded. Remove the `@media (max-width: 40rem)` column-hiding rule. Touch targets ≥ 2.5rem tall. The fold script lives in `base.html` and binds with event delegation on `document` so htmx-swapped results work.
- [ ] **Step 5: Run** `pytest tests/test_pages.py -v`; all pass. Then the full gate.
- [ ] **Step 6: CHANGELOG** line under Added: "Sessions page: active projects open on top, quiet projects collapsed to one line, idle sessions folded, a status summary, and phone-first rows."
- [ ] **Step 7: Commit** `feat(web): group sessions page into active and quiet projects`

---

## Part 2: joining from a phone

### Task 2: Index the Remote Control link and live-session detail

**Files:**
- Create: `src/vigil/index/migrations/0004_remote_url.sql`
- Modify: `src/vigil/transcript.py`, `src/vigil/index/scanner.py`, `src/vigil/index/queries.py`, `src/vigil/serialize.py`, `src/vigil/peers.py` (`session_from_dict`), `src/vigil/claude_cli.py`, `src/vigil/web/api.py` (`SessionOut`)
- Create: `tests/fixtures/transcripts/handmade/remote-control.jsonl`
- Test: `tests/test_transcript.py`, `tests/test_scanner.py`, `tests/test_claude_cli.py`, `tests/test_peers.py`, `tests/test_api.py`

**Interfaces:**
- Produces:
  - Migration 0004: `ALTER TABLE sessions ADD COLUMN remote_url TEXT; ALTER TABLE sessions ADD COLUMN remote_url_at TEXT;`
  - `SessionFacts.remote_url: str | None`, `SessionFacts.remote_url_at: datetime | None` (newest `bridge_status` by timestamp wins).
  - `vigil.transcript.REMOTE_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,128}")`; only a `fullmatch` is kept.
  - `vigil.transcript.find_remote_url(path: Path, after: datetime) -> str | None`: reads the whole file, returns the newest valid `bridge_status` URL whose `timestamp` is strictly after `after`, else None.
  - `SessionRow.remote_url: str | None = None`, `SessionRow.remote_url_at: datetime | None = None`; `session_to_dict` adds `"remote_url"` and `"remote_url_at"`; `session_from_dict` accepts them (URL must `fullmatch` `REMOTE_URL`, else None).
  - `LiveSession` gains `busy: bool` (agents `status == "busy"`) and `started_at: datetime | None` (agents `startedAt`, epoch ms, UTC).
  - `vigil.serialize.open_link(row: SessionRow, live: dict[str, LiveSession]) -> str | None`: `row.remote_url` when the row is live and `remote_url_at` is at or after the live `started_at` (or `started_at` is None), else None.

- [ ] **Step 1: Fixture** `remote-control.jsonl`: a user and assistant record plus two `{"type":"system","subtype":"bridge_status","url":…,"timestamp":…}` records with placeholder URLs `https://claude.ai/code/session_PLACEHOLDER01` (older) and `…02` (newer), and one with `"url":"https://evil.example/x"`.
- [ ] **Step 2: Write failing tests:**
  - `test_transcript.py::test_bridge_status_newest_valid_url` → facts `remote_url == "https://claude.ai/code/session_PLACEHOLDER02"`, `remote_url_at` is that record's time.
  - `test_transcript.py::test_find_remote_url_after` → with `after` between the two records returns `…02`; with `after` past both returns None.
  - `test_scanner.py::test_remote_url_stored_and_listed` → after `scan`, `get_session(...).remote_url` is `…02`.
  - `test_claude_cli.py::test_live_session_busy_and_started` → agents JSON entry with `"status":"busy","startedAt":1794938034841` parses to `busy=True`, `started_at == datetime(2026, 11, 17, …, tzinfo=UTC)` (compute exact value in the test from the ms).
  - `test_peers.py::test_peer_remote_url_validated` → `remote_url` of `https://evil.example/x` from a peer is dropped to None.
  - `test_api.py::test_session_has_remote_url` → `/api/v1/sessions/{id}` includes `remote_url`.
  - `test_api.py::test_open_link_only_when_live_and_newer` → `open_link` returns None for an idle row and for a link older than `started_at`.
- [ ] **Step 3: Run** these tests; they fail.
- [ ] **Step 4: Implement** the migration, parser branch for `type == "system" and subtype == "bridge_status"` (before the user/assistant filter), scanner store, query mapping, serializers, `LiveSession` fields, `open_link`.
- [ ] **Step 5: Run** the tests, then the full gate.
- [ ] **Step 6: CHANGELOG** "Index each session's Remote Control link (`remote_url`) and whether a live session is busy."
- [ ] **Step 7: Commit** `feat(index): record the Remote Control link of each session`

### Task 3: Join from a phone

**Files:**
- Create: `src/vigil/remote.py`
- Modify: `src/vigil/resume.py`, `src/vigil/web/api.py` (`resume`, `ResumeOut`, `ForwardedResumeOut`), `src/vigil/peers.py` (`forward_resume`), `src/vigil/config.py` (`ResumeConfig.remote_link_timeout`), `src/vigil/web/templates/_session_row.html`, `session_detail.html`, `base.html` (navigate on `remote_url`), `CLAUDE.md`
- Test: `tests/test_resume.py`, `tests/test_api.py`, `tests/test_peers.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `find_remote_url`, `open_link`, `LiveSession.started_at` (Task 2).
- Produces:
  - `ResumeConfig.remote_link_timeout: float = 30.0` (config key `resume.remote_link_timeout`, 1–300).
  - `vigil.remote.wait_for_remote_url(path: Path, after: datetime, timeout: float, *, poll: float = 0.5, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> str | None`.
  - `plan_resume` always puts `--remote-control` in `start` (after `--resume <id>`, before default flags; not duplicated when already in `default_flags`).
  - `ResumeRefused` gains `restartable: bool = False`. For a live background session with no `open_link` on a remote request (`TERMINAL_ALLOWED` False), `plan_resume` raises `ResumeRefused(reason, restartable=True)` with reason `"running in the background without Remote Control; attach at a desk with `claude attach <id>` and type /remote-control, or restart it with Remote Control (this interrupts it)"`.
  - For a live background session with an `open_link`, the resume endpoint answers without starting anything: `{"ok": True, "attach": …, "opened_terminal": False, "remote_url": <link>}`.
  - The interactive-session refusal text gains: `"; or type /remote-control in that terminal to make it joinable"`.
  - `ResumeOut.remote_url: str | None`; 409 refusals carry `"restartable": bool`. `forward_resume` passes both through.
  - Remote (non-terminal) resume: after `execute`, `wait_for_remote_url(Path(row.transcript_path), launched_at, cfg.remote_link_timeout)`; on None the answer is ok with `remote_url: None` and `"pending": True`.

- [ ] **Step 1: Write failing tests:**
  - `test_resume.py::test_start_always_has_remote_control` → bg plan `start == ["claude","--bg","--resume",SID,"--remote-control"]`; with `default_flags=("--remote-control","--model","opus")` it appears once.
  - `test_resume.py::test_tmux_start_has_remote_control`.
  - `test_resume.py::test_remote_bg_without_link_is_restartable` → `TERMINAL_ALLOWED` False, live background, no link → `ResumeRefused` with `restartable is True`.
  - `test_resume.py::test_interactive_refusal_mentions_remote_control`.
  - `test_resume.py::test_wait_for_remote_url_times_out` and `test_wait_for_remote_url_sees_appended_line` (fake clock and sleep; append a `bridge_status` line between polls).
  - `test_api.py::test_remote_resume_returns_link` → remote client, fake `claude` that appends a `bridge_status` line to the transcript copy when called with `--bg`, answer has `remote_url`.
  - `test_api.py::test_remote_resume_ignores_older_link` (Review Focus 1) → transcript already has a `bridge_status` from before the launch and the fake adds none → `remote_url` is None, `pending` True.
  - `test_api.py::test_second_remote_tap_opens_existing_link` (Review Focus 2) → live background with a newer link → answer has that `remote_url` and the fake `claude` recorded no `--bg` call.
  - `test_peers.py::test_forward_resume_carries_remote_url_and_restartable`.
  - `test_config.py::test_remote_link_timeout_bounds`.
- [ ] **Step 2: Run** them; they fail.
- [ ] **Step 3: Implement** `remote.py`, the resume and endpoint changes, config key. vigil only reads the transcript. In the API tests, the fake `claude` script appends the `bridge_status` line, after the test thaws that one file (`thaw_tree` on its folder); vigil itself never writes it.
- [ ] **Step 4: UI.** Rows and the detail page show **Open** (a plain `<a href>` to `open_link`) when present; label it "Open in Claude" on loopback requests. In `base.html`'s JSON handler: `data.remote_url` → `window.location.href = data.remote_url`; `data.pending` → "Started; the Remote Control link is not available yet" plus a link to the session page; `data.restartable` → show the reason and a "Restart with Remote Control" button (wired in Task 4; hidden until then).
- [ ] **Step 5: Run** the tests and the full gate.
- [ ] **Step 6: CLAUDE.md** add "vigil adds `--remote-control` to every session it starts." **CHANGELOG** "Join from a phone: Resume from another device opens the session's Remote Control link; Open button for live sessions with a link."
- [ ] **Step 7: Commit** `feat(resume): join a session from a phone through Remote Control`

### Task 4: Restart with Remote Control

**Files:**
- Modify: `src/vigil/resume.py`, `src/vigil/web/api.py`, `src/vigil/peers.py` (`forward_restart`, `PeerHub.restart`), templates, `CLAUDE.md`
- Test: `tests/test_resume.py`, `tests/test_api.py`, `tests/test_peers.py`

**Interfaces:**
- Consumes: `LiveSession.busy`, `wait_for_remote_url`, `ResumeRefused.restartable` (Tasks 2–3).
- Produces:
  - `vigil.resume.stop_session(session_id: str, live_probe: Callable[[], dict[str, LiveSession]], *, timeout: float = 10.0, poll: float = 0.5, clock=…, sleep=…) -> None`: runs `["claude", "stop", session_id]` (20 s timeout), then polls `live_probe` until the ID is gone; raises `ResumeRefused("could not stop the session; nothing was restarted")` if `claude stop` exits non-zero or the session is still live after `timeout`.
  - `POST /sessions/{id}/restart-remote` (form field `confirm=yes` required, else 400): refuses 409 unless the session is live with `kind == "background"`; calls `stop_session`, then the remote resume path of Task 3; same answer shape as resume.
  - `POST /peers/{peer}/sessions/{id}/restart-remote` forwards with `confirm=yes`.
  - Confirm copy (in-page, two-step): `"Restarting stops this session and interrupts anything it is doing."`; when `busy`: `"This session is working right now. Restarting stops it mid-task."` Buttons: "Restart" and "Cancel".

- [ ] **Step 1: Write failing tests:** `test_stop_session_waits_until_gone`, `test_stop_session_refuses_when_still_live` (nothing started: fake `claude` has no `--bg` call), `test_stop_session_refuses_on_exit_code`, `test_restart_requires_confirm` (400 without `confirm=yes`), `test_restart_refuses_interactive` (409), `test_restart_remote_returns_link`, `test_forward_restart`.
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement**, with `live_probe` bound to `web.live.get(fresh=True)[1]`.
- [ ] **Step 4: UI**: the "Restart with Remote Control" button from Task 3 opens the confirm step (copy above; the busy line when the row's live session is busy) and posts with `confirm=yes`.
- [ ] **Step 5: Run** tests and the gate.
- [ ] **Step 6: CLAUDE.md** add "vigil may run `claude stop <id>` only for Restart with Remote Control, after the author confirms." **CHANGELOG** "Restart with Remote Control for background sessions without a link, after a confirm warning."
- [ ] **Step 7: Commit** `feat(resume): restart a background session with Remote Control`

---

## Part 3: session reviews

### Task 5: One flag allow-list with value rules

**Files:**
- Create: `src/vigil/flags.py`
- Modify: `src/vigil/config.py` (drop `ALLOWED_FLAGS`, validate with `vigil.flags`), `src/vigil/resume.py` (`_checked_flags` uses `vigil.flags`), `CLAUDE.md`
- Test: `tests/test_flags.py`, `tests/test_config.py`, `tests/test_resume.py`

**Interfaces:**
- Produces:
  - `vigil.flags.FlagRefused(Exception)` with `reason: str`.
  - `vigil.flags.RULES: Mapping[str, Rule]` where `Rule` is `Literal["none", "optional_name", "name", "model", "choice", "choice_no_bypass", "ident"]`: `--remote-control: optional_name`, `--name: name`, `--model: model`, `--effort: choice`, `--permission-mode: choice_no_bypass`, `--agent: ident`, `--worktree/--chrome/--no-chrome/--ide/--verbose/--brief: none`. Patterns: name = printable, 1–64 chars, no leading `-`; model = `[A-Za-z0-9._:\[\]-]{1,64}`; ident = `[A-Za-z0-9_-]{1,64}`.
  - `vigil.flags.choices_from_help(description: str) -> tuple[str, ...]`: parses `(choices: "a", "b")` from a catalog description.
  - `vigil.flags.check(tokens: Sequence[str], catalog: Sequence[CatalogEntry] | None = None) -> list[str]`: tokens like `["--model", "opus", "--worktree"]` or `--model=opus`; raises `FlagRefused` for an unlisted flag, a bad value, a missing required value, `bypassPermissions`, or (with `catalog`) a flag the installed version does not list. `choice` rules need `catalog`; without it any `[A-Za-z]{1,32}` passes and `bypassPermissions` is still refused.
  - `vigil.flags.parse_suggested(name: str, value: str | None) -> list[str]` builds tokens for a suggestion.

- [ ] **Step 1: Write failing tests** in `tests/test_flags.py`: each rule accepts its good example and refuses a bad one; `test_refuses_dangerous` over `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--system-prompt`, `--mcp-config`, `--settings`, `--add-dir`, `--tools`, `--bare`; `test_permission_mode_bypass_refused`; `test_effort_choices_from_catalog` (catalog description `(choices: "low", "medium", "high")` → `high` ok, `extreme` refused); `test_flag_missing_from_catalog_refused`. In `tests/test_config.py`: `test_default_flags_value_checked` (`["--permission-mode","bypassPermissions"]` → `ConfigError`), existing `test_disallowed_default_flag_raises` still passes.
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement** `flags.py`; switch config and resume to it.
- [ ] **Step 4: Run** tests and the gate.
- [ ] **Step 5: CLAUDE.md** replace the five-flag sentence with the new allow-list line from Global Constraints. **CHANGELOG** "One flag allow-list with value rules (`vigil.flags`), used by config and every launch."
- [ ] **Step 6: Commit** `feat: flag allow-list with value rules`

### Task 6: Where the tokens went

**Files:**
- Create: `src/vigil/review/__init__.py`, `src/vigil/review/numbers.py`
- Modify: `src/vigil/web/pages.py` (session page context), `src/vigil/web/templates/session_detail.html`
- Create: `tests/fixtures/transcripts/handmade/usage.jsonl`
- Test: `tests/test_review_numbers.py`, `tests/test_pages.py`

**Interfaces:**
- Produces:
  - `TurnTokens` (frozen): `turn: int`, `input: int`, `output: int`, `cache_write: int`, `cache_read: int`.
  - `ToolOutput` (frozen): `tool: str`, `size: int` (characters of the tool result content).
  - `SessionNumbers` (frozen): `turns: list[TurnTokens]`, `totals: TurnTokens` (`turn=0`), `cache_hit_share: float` (cache_read / (input + cache_write + cache_read), 0.0 when the denominator is 0), `largest_tool_outputs: list[ToolOutput]` (top 10 by size), `compactions: int`, `models: list[str]`, `started_at: datetime | None`, `ended_at: datetime | None`, `launch_flags: list[str]`.
  - `compute_numbers(path: Path, launch_flags: list[str]) -> SessionNumbers`: a turn is one non-sidechain assistant record with `message.usage`; tool name comes from matching `tool_use_id` to the assistant `tool_use` block; a compaction is counted per `system` record with `subtype == "compact_boundary"` (verify the subtype against a real transcript when implementing; count 0 if absent). Non-UTF-8 and bad lines are skipped.
  - `numbers_to_dict(n: SessionNumbers) -> dict[str, object]` and `numbers_from_dict(d) -> SessionNumbers`.
  - `vigil.index.queries.vigil_launch_flags(conn, session_id: str) -> list[str]`: flag names of `launches` rows with `source='vigil'` and that `session_id`, de-duplicated in first-seen order.

- [ ] **Step 1: Fixture** `usage.jsonl`: three assistant turns with `usage`, two tool results of 1 200 and 40 000 chars (placeholder text), one `compact_boundary`, one sidechain assistant record (ignored).
- [ ] **Step 2: Write failing tests:** `test_totals_and_turns`, `test_cache_hit_share`, `test_largest_tool_outputs_named`, `test_compactions_counted`, `test_sidechain_ignored`, `test_numbers_roundtrip`, `test_vigil_launch_flags_only_vigil_source` (a `shell` launch is ignored), `test_pages.py::test_detail_shows_token_panel` (heading `Where the tokens went`, total output tokens rendered with `tabular-nums` class).
- [ ] **Step 3: Run**; they fail.
- [ ] **Step 4: Implement**; the detail page computes numbers on request for local sessions (not for peer sessions until Task 10) and renders totals, cache-hit share as a percentage, compactions, and the largest tool outputs list.
- [ ] **Step 5: Run** tests and the gate.
- [ ] **Step 6: CHANGELOG** "Session page: where the tokens went (per-turn tokens, cache hits, largest tool outputs, compactions)."
- [ ] **Step 7: Commit** `feat(review): compute a session's token numbers`

### Task 7: Run and store a review

**Files:**
- Create: `src/vigil/review/schema.py`, `src/vigil/review/prompt.txt`, `src/vigil/review/runner.py`, `src/vigil/review/store.py`, `src/vigil/index/migrations/0005_reviews.sql`, `src/vigil/commands/review.py`, `src/vigil/web/review.py`, `src/vigil/web/templates/_review.html`
- Modify: `src/vigil/config.py` (`ReviewConfig`), `src/vigil/cli.py` (append to `COMMANDS`), `src/vigil/web/app.py` (append router), `session_detail.html`, `CLAUDE.md`, `README.md` (config keys)
- Test: `tests/test_review_runner.py`, `tests/test_review_store.py`, `tests/test_review_web.py`, `tests/test_commands.py`

**Interfaces:**
- Consumes: `compute_numbers`, `vigil_launch_flags` (Task 6); `current_catalog` (`vigil.catalog`).
- Produces:
  - `ReviewConfig` (frozen): `max_budget_usd: float = 1.0` (0.05–50), `model: str | None = None` (must pass `vigil.flags` `--model` rule). `Config.review: ReviewConfig`.
  - `vigil.review.schema.SCHEMA: dict[str, Any]` (JSON Schema per spec "Output schema"); `Suggestion` (frozen): `kind: Literal["flag","file","habit"]`, `title`, `why`, `evidence: str`, `flag_name: str | None`, `flag_value: str | None`, `file_target: Literal["project_claude_md","project_settings","user_claude_md","user_settings"] | None`, `file_text: str | None`, `file_settings: dict[str, Any] | None`; `Advice` (frozen): `title`, `why`. `parse_output(data: Any) -> tuple[list[Suggestion], list[Advice]]` drops invalid items and truncates each text field to 500 chars; at most 10 suggestions.
  - `vigil.review.runner.review_argv(session_id: str, cfg: ReviewConfig, schema_json: str, prompt: str) -> list[str]` = `["claude","-p","--resume",session_id,"--fork-session","--no-session-persistence","--tools","","--output-format","json","--json-schema",schema_json,"--max-budget-usd",f"{cfg.max_budget_usd:.2f}", *(["--model", cfg.model] if cfg.model else []), prompt]`.
  - `vigil.review.runner.build_prompt(numbers: SessionNumbers, catalog: list[CatalogEntry], default_flags: tuple[str, ...]) -> str` from `prompt.txt` (loaded with `importlib.resources`), which states that launch flags may be incomplete.
  - `vigil.review.runner.run_review(row: SessionRow, cfg: ReviewConfig, catalog, default_flags, conn) -> int` (review id): inserts a `running` row, runs `claude` in `row.cwd` with a 600 s timeout, reads the JSON result (`structured_output` or `result` parsed as JSON; `total_cost_usd`), stores `done` or `failed` with `error`.
  - `ReviewBusy(Exception)`; `vigil.review.runner.ReviewSlot` (one at a time per process: `try_start() -> bool`, `finish()`).
  - Migration 0005: `reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE, machine TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, claude_version TEXT, model TEXT, cost_usd REAL, status TEXT NOT NULL CHECK (status IN ('running','done','failed')), numbers TEXT NOT NULL, suggestions TEXT, advice TEXT, error TEXT)` plus an index on `session_id`. Reviews go when their session leaves the index (the cascade; `foreign_keys` is already on).
  - `vigil.review.store`: `Review` (frozen dataclass mirroring the row with parsed fields), `insert_running(...) -> int`, `finish(conn, id, ...)`, `fail(conn, id, error)`, `get_review(conn, id) -> Review | None`, `list_reviews(conn, session_id) -> list[Review]` newest first, `running_review(conn) -> Review | None`. On service start, any `running` row is marked `failed` with `"vigil restarted during the review"`.
  - Web (`vigil.web.review.router`): `POST /sessions/{id}/review` → 202 `{"ok": true, "review_id"}` and starts a background thread; 409 `{"ok": false, "reason": "a review is already running", "review_id"}` when busy. `GET /sessions/{id}/reviews/{rid}` → the `_review.html` fragment; while `running` the fragment carries `hx-get` itself with `hx-trigger="every 2s"`. Read API: `GET /api/v1/sessions/{id}/reviews`.
  - CLI `vigil review <id> [--json]`: runs synchronously, prints suggestions (title, why, the action as a command line where it is a flag) or the JSON.

- [ ] **Step 0: Verify the fork stays out of the Claude home (manual, once, by the implementer; not a test).** On a machine with Claude Code, pick an idle session, list `~/.claude/projects/<dir>/` and note the session file's size and mtime, run the `review_argv` command with a one-line prompt and `--max-budget-usd 0.05`, and list again. Expected: no new `.jsonl`, and the session file unchanged. Also try adding `--bare`; keep it in `review_argv` only if the run still authenticates. If any run leaves a new file or changes the session file, stop and report to the author before continuing.
- [ ] **Step 1: Write failing tests:**
  - `test_review_argv_exact` (budget `1.00`, no `--model` when unset).
  - `test_parse_output_drops_invalid_and_truncates` (an item with `kind: "other"` is dropped; a 900-char `why` is cut to 500; 12 items → 10).
  - `test_run_review_success` (fake `claude` prints `{"type":"result","is_error":false,"total_cost_usd":0.12,"structured_output":{…}}`; stored `done`, `cost_usd == 0.12`; fake recorded cwd == session cwd).
  - `test_review_failure_is_stored` (Review Focus 3: fake exits 1 with `{"is_error":true,"result":"budget exceeded"}` → `failed`, `error` contains `budget exceeded`; numbers stored).
  - `test_review_slot_one_at_a_time` and `test_post_review_409_when_busy`.
  - `test_running_reviews_failed_on_start`.
  - `test_review_fragment_polls_while_running` (fragment has `hx-trigger="every 2s"` only while `running`).
  - `test_commands.py::test_review_cli_json`.
  - `test_claude_home_untouched_by_review` (the read-only fixture tree is unchanged after a review with the fake).
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement** schema, prompt, runner, store, migration, config, web router, CLI command.
- [ ] **Step 4: UI**: "Review this session" button on the detail page (local sessions only until Task 10); a reviews list, newest first; each review shows its status, cost, the suggestion cards (title, why, evidence; action buttons arrive in Tasks 8–9) and token advice, and the note "Suggestions come from Claude reading this session. Check them before acting."
- [ ] **Step 5: Run** tests and the gate.
- [ ] **Step 6: CLAUDE.md** add the `claude -p` review rule from Global Constraints. **README** document `[review] max_budget_usd`, `model`. **CHANGELOG** "Session reviews: Claude reads a non-persisted fork of a session and suggests flags, project file changes and habits; stored per session; `vigil review`."
- [ ] **Step 7: Commit** `feat(review): run and store session reviews`

### Task 8: Act on flag suggestions

**Files:**
- Create: `src/vigil/config_edit.py`
- Modify: `src/vigil/resume.py` (`extra_flags`, `plan_start_new`), `src/vigil/web/review.py`, `src/vigil/web/__init__.py` (reloadable config), `_review.html`, `pyproject.toml` (`tomlkit`), `CLAUDE.md`
- Test: `tests/test_config_edit.py`, `tests/test_resume.py`, `tests/test_review_web.py`

**Interfaces:**
- Consumes: `vigil.flags.check`, `parse_suggested` (Task 5); `get_review`, `Suggestion` (Task 7); `wait_for_remote_url` (Task 3).
- Produces:
  - `plan_resume(..., extra_flags: Sequence[str] = ())`: the endpoint first runs `vigil.flags.check(tokens, current_catalog(conn))`; `plan_resume` re-checks with `vigil.flags.check(tokens)` (no catalog) and appends them after default flags (a flag already present is replaced by the suggested value).
  - `plan_start_new(cwd: str, flags: Sequence[str], caps, cfg: ResumeConfig, env, new_session_id: str) -> ResumePlan`: `bg` → `["claude","--bg","--session-id",new_id,"--remote-control",*default_flags,*flags]`, attach `["claude","attach",new_id]`; tmux → `["tmux","new-session","-d","-s",tmux_session_name(new_id),"-c",cwd,"claude","--session-id",new_id,"--remote-control",…]`. `cwd` must be the cwd of an indexed session (the endpoint passes the reviewed session's) and pass `_check_cwd`. Verify `claude --bg` without `--resume` honors `--session-id` when implementing; if it does not, `plan_start_new` uses tmux.
  - `vigil.remote.find_transcript(claude_home: Path, session_id: str) -> Path | None` (glob `projects/*/<id>.jsonl`), used to wait for a new session's link.
  - `vigil.config_edit.add_default_flags(path: Path, tokens: Sequence[str]) -> None`: reads with `tomlkit` (missing file → new document), sets `resume.default_flags` to the old list with `tokens` merged (a flag already present gets the new value), validates the result with `load_config` on the rendered text before writing, writes atomically (temp file + `os.replace`).
  - `WebState.reload_config() -> None` re-reads the config file; called after `add_default_flags`.
  - Endpoints, each taking `review_id` and `index` (the suggestion's position) so no flag text comes from the client: `POST /reviews/{rid}/suggestions/{i}/resume`, `/start-new`, `/always-use`. A suggestion whose flag fails `check` answers 409 with the `FlagRefused` reason. Answers match the resume answer shape (`attach`, `opened_terminal`, `remote_url`, `pending`); `always-use` answers `{"ok": true, "default_flags": [...]}`.
  - Copy for a refused flag card: `"vigil will not launch this flag itself. Copy the command to try it:"` followed by the command line in `<code>` and a Copy button.

- [ ] **Step 1: Write failing tests:**
  - `test_config_edit.py::test_always_use_keeps_comments` (Review Focus 5: a file with comments and `port = 7790` keeps both; `default_flags == ["--model","opus"]`).
  - `test_config_edit.py::test_always_use_creates_file`.
  - `test_config_edit.py::test_always_use_replaces_value` (`--model sonnet` then `--model opus` → one `--model opus`).
  - `test_config_edit.py::test_always_use_refuses_invalid` (nothing written when the result fails `load_config`).
  - `test_resume.py::test_extra_flags_appended_and_checked`, `test_start_new_bg_argv`, `test_start_new_tmux_argv`, `test_start_new_refuses_missing_cwd`.
  - `test_review_web.py::test_suggestion_resume_uses_stored_flag` (client sends only ids; fake `claude` sees `--model opus`).
  - `test_review_web.py::test_suggestion_refused_flag_409` (stored suggestion `--dangerously-skip-permissions`).
  - `test_review_web.py::test_start_new_remote_waits_for_link`.
  - `test_review_web.py::test_always_use_reloads_config`.
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement**; add `tomlkit` to `[project] dependencies`.
- [ ] **Step 4: UI**: flag cards get "Resume with these", "Start new session with these", "Always use", or the copy-only block when `vigil.flags.check` refuses the flag (decided at render time).
- [ ] **Step 5: Run** tests and the gate.
- [ ] **Step 6: CLAUDE.md** amend the resume line: "vigil may also start a new session, only in the cwd of an indexed session." **CHANGELOG** "Act on flag suggestions: resume or start a new session with them, or add them to `resume.default_flags`."
- [ ] **Step 7: Commit** `feat(review): act on flag suggestions`

### Task 9: Apply project file suggestions

**Files:**
- Create: `src/vigil/apply.py`
- Modify: `src/vigil/web/review.py`, `_review.html`, `CLAUDE.md`
- Test: `tests/test_apply.py`, `tests/test_review_web.py`

**Interfaces:**
- Consumes: `Suggestion` (Task 7).
- Produces:
  - `SETTINGS_KEYS: frozenset[str]` = `{"model", "effortLevel", "permissions.deny", "permissions.ask", "includeCoAuthoredBy", "cleanupPeriodDays"}` plus any top-level key starting `autoCompact`; a suggestion is appliable only if every leaf path of `file_settings` is one of these or under one of these.
  - `ApplyRefused(Exception)` with `reason`.
  - `project_folder(cwd: str) -> Path`: `git -C <cwd> rev-parse --show-toplevel` (5 s timeout), else `cwd`.
  - `Change` (frozen): `target: Path`, `before: str | None`, `after: str`, `before_hash: str` (sha256 hex of `before` bytes, `"absent"` when missing), `skipped: list[str]` (settings keys already set).
  - `plan_change(s: Suggestion, folder: Path, claude_home: Path) -> Change`: refuses (`ApplyRefused`) a user target, a non-appliable settings key (reason names the key), an existing settings file that is not a JSON object, and a target whose resolved path is under `claude_home`, outside `folder`, or exists and is not a regular file. CLAUDE.md: `after = before.rstrip("\n") + "\n\n" + text.strip() + "\n"` (or `text.strip() + "\n"` when absent). Settings: deep merge (objects merge, lists union in order, existing scalars kept and listed in `skipped`), `json.dumps(indent=2) + "\n"`.
  - `diff_text(c: Change) -> str` (`difflib.unified_diff`).
  - `apply_change(c: Change, claude_home: Path) -> None`: re-reads the target and raises `ApplyRefused("the file changed since the preview; show the change again")` if its hash differs from `before_hash`; re-checks the path rules; creates `.claude/` if needed; writes atomically.
  - Endpoints: `GET /reviews/{rid}/suggestions/{i}/change` → fragment with the diff, the skipped keys, and an Apply form carrying `before_hash`; `POST /reviews/{rid}/suggestions/{i}/apply` with `before_hash` → recomputes the plan from the stored suggestion and compares hashes (409 on mismatch or refusal).

- [ ] **Step 1: Write failing tests** in `tests/test_apply.py`: `test_claude_md_appended`, `test_claude_md_created`, `test_settings_merge_union_and_skip` (existing `{"model":"sonnet","permissions":{"deny":["Bash(rm:*)"]}}` + suggestion `{"model":"opus","permissions":{"deny":["WebFetch"]}}` → model stays `sonnet`, skipped `["model"]`, deny `["Bash(rm:*)","WebFetch"]`), `test_settings_hooks_refused`, `test_settings_permissions_allow_refused`, `test_settings_invalid_json_refused`, `test_apply_refuses_symlink_out` (Review Focus 4), `test_apply_refuses_under_claude_home`, `test_apply_creates_missing_dot_claude` (Review Focus 4), `test_apply_refuses_changed_file`, `test_project_folder_git_toplevel` (a `git init` in tmp, cwd a subfolder). In `tests/test_review_web.py`: `test_show_change_then_apply`, `test_apply_hash_mismatch_409`, `test_user_target_copy_only` (card has no Apply button and shows the path).
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: UI**: project file cards get "Show change" (htmx-loads the diff fragment) and then "Apply"; refused and user-target cards show the text, the target path and a Copy button with the reason.
- [ ] **Step 5: Run** tests and the gate.
- [ ] **Step 6: CLAUDE.md** add "vigil may write only `CLAUDE.md` and `.claude/settings.json` in a project folder, only on a confirmed Apply." **CHANGELOG** "Apply project file suggestions to `CLAUDE.md` and `.claude/settings.json` after a preview."
- [ ] **Step 7: Commit** `feat(review): apply project file suggestions after a preview`

### Task 10: Reviews and actions on peer sessions

**Files:**
- Modify: `src/vigil/peers.py`, `src/vigil/web/review.py`, `src/vigil/web/pages.py` (peer session page), templates, `CLAUDE.md`
- Test: `tests/test_peers.py`, `tests/test_review_web.py`

**Interfaces:**
- Consumes: every endpoint of Tasks 6–9 on the peer side.
- Produces:
  - `vigil.peers.forward(peer: Peer, method: Literal["GET","POST"], path: str, form: Mapping[str, str] | None = None, *, timeout: float, transport=None) -> tuple[int, dict[str, Any] | str]`: one generic forwarder built from `forward_resume`'s request and error handling (502 on unreachable; the peer's 400/404/409 bodies passed through with the reason prefixed by the peer name; HTML fragments returned as text). `forward_resume` becomes a thin wrapper.
  - Local mirror endpoints under `/peers/{peer}/…` for: `sessions/{id}/review` (POST), `sessions/{id}/reviews/{rid}` (GET fragment), `api/v1/sessions/{id}/reviews` (GET), `reviews/{rid}/suggestions/{i}/resume|start-new|always-use|apply` (POST), `reviews/{rid}/suggestions/{i}/change` (GET). Review ids are the peer's own; forwarded fragments are re-rendered locally from the peer's JSON where they contain links (so htmx URLs point at the `/peers/{peer}/…` mirrors), never inserted as raw peer HTML.
  - Review POST timeout: 30 s (the peer answers 202 at once); fragment GET: `PEER_TIMEOUT`.
  - The peer session page shows the token panel (from the peer's `/api/v1/sessions/{id}/numbers`, a new read endpoint added locally in this task) and the reviews section.

- [ ] **Step 1: Write failing tests** with an `httpx.MockTransport` peer: `test_forward_review_start`, `test_forward_review_poll_renders_local_links` (fragment links start with `/peers/desk/`), `test_forward_apply_hash_passed_through`, `test_forward_always_use_changes_peer_not_local` (local config untouched), `test_forward_refusal_prefixed`, `test_forward_unreachable_502`, `test_numbers_endpoint`.
- [ ] **Step 2: Run**; they fail.
- [ ] **Step 3: Implement** `forward`, the mirrors and the numbers endpoint.
- [ ] **Step 4: Run** tests and the gate.
- [ ] **Step 5: CLAUDE.md** add "Peers forward Resume, Restart with Remote Control, Review and every suggestion action, and apply the same checks as for a local request." **CHANGELOG** "Reviews and their actions work on peer sessions, run by the peer."
- [ ] **Step 6: Commit** `feat(peers): forward reviews and suggestion actions`
