"""Claude's review of a session: `claude -p` on a fork of it that is not persisted and has no
tools or MCP servers, run in the session's folder.

    claude -p --resume <id> --fork-session --no-session-persistence --tools "" \\
      --strict-mcp-config --output-format json --json-schema <SCHEMA> \\
      --max-budget-usd <budget> [--model <m>] <prompt>

Checked on Claude Code 2.1.312: this leaves no new transcript under the Claude home and does not
touch the reviewed session's file, so nothing new appears in the index either. Without
`--strict-mcp-config` a project's `.mcp.json` servers were started for the review. `-p` prints one
JSON object: `type` (`result`), `subtype` (`success`, `error_max_budget_usd`, ...), `is_error`,
`result` (Claude's text), `structured_output` (the answer as JSON, with `--json-schema`),
`total_cost_usd`, `errors` (a list of messages) and `modelUsage` (cost per model), among others.

Claude Code checks the budget only after a turn, so a single turn can cost more than the cap and
come back with `is_error` and a non-zero exit even though Claude answered. An answer that can be
used is therefore stored as `done` whatever the exit status, with a note in `error`; only a run
with no usable answer is `failed`.

A review is started in two steps so the web service can answer at once: `begin_review` computes
the numbers and stores a `running` row, `complete_review` runs `claude` (up to `REVIEW_TIMEOUT`)
and stores the outcome. `run_review` does both.
"""

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from vigil.catalog import CatalogEntry
from vigil.config import ReviewConfig
from vigil.index.queries import SessionRow, vigil_launch_flags
from vigil.resume import SESSION_ID, ResumeRefused, _check_cwd
from vigil.review.numbers import SessionNumbers, compute_numbers, wall_clock
from vigil.review.schema import SCHEMA, Advice, Suggestion, clean, parse_output
from vigil.review.store import (
    Review,
    fail,
    fail_if_running,
    finish,
    insert_running,
    running_review,
)

REVIEW_TIMEOUT = 600.0
"""Seconds a review may run before `claude` is stopped and the review fails."""

_DESCRIPTION_CHARS = 200
"""Most characters of a catalog entry's description put in the prompt."""
_TOP_TURNS = 10
"""How many of the turns with the most input-side tokens the prompt lists."""
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n?\s*```", re.DOTALL)


class ReviewRefused(Exception):
    """A review cannot start; `reason` is user-facing text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReviewBusy(Exception):
    """A review is already running on this machine; `review_id` is it, when known."""

    def __init__(self, review_id: int | None) -> None:
        super().__init__("a review is already running")
        self.review_id = review_id


STOPPED = "vigil stopped during the review"
"""The error of a review whose `claude` was stopped because the service was shutting down."""

STOP_WAIT = 5.0
"""Seconds `ReviewSlot.stop` waits for `claude` to exit after SIGTERM before it kills it."""


class ReviewSlot:
    """One review at a time in this process: `try_start` takes the slot (False when it is
    taken), `finish` gives it back, from any thread. While the review's `claude` runs, the slot
    holds it, so `stop` can end it when the service shuts down."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._review_id: int | None = None
        self._stopping = False

    def try_start(self) -> bool:
        return self._lock.acquire(blocking=False)

    def finish(self) -> None:
        self._lock.release()

    def attach(self, review_id: int, process: subprocess.Popen[str]) -> bool:
        """Note that `process` is running review `review_id`. False when the slot is stopping:
        the caller must not let it run."""
        with self._guard:
            if self._stopping:
                return False
            self._process, self._review_id = process, review_id
            return True

    def detach(self) -> None:
        with self._guard:
            self._process = self._review_id = None

    @property
    def stopping(self) -> bool:
        with self._guard:
            return self._stopping

    def stop(self, wait: float = STOP_WAIT) -> int | None:
        """Stop for good: terminate the running review's `claude` (SIGTERM, then SIGKILL after
        `wait` seconds) and keep any later one from starting. Returns the id of the review that
        was running, or None."""
        with self._guard:
            self._stopping = True
            process, review_id = self._process, self._review_id
        if process is not None:
            _end(process, wait)
        return review_id


def _end(process: subprocess.Popen[str], wait: float) -> None:
    """Terminate `process`, and kill it if it has not exited within `wait` seconds."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def review_argv(session_id: str, cfg: ReviewConfig, schema_json: str, prompt: str) -> list[str]:
    """The review command. The prompt is the one positional, last; every flag before it takes
    exactly one value or none, so none of them can take the prompt as its value.
    `--strict-mcp-config` with no `--mcp-config` loads no MCP server, not even the project's
    (`.mcp.json`), which Claude Code would otherwise start for the review."""
    return [
        "claude",
        "-p",
        "--resume",
        session_id,
        "--fork-session",
        "--no-session-persistence",
        "--tools",
        "",
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--max-budget-usd",
        f"{cfg.max_budget_usd:.2f}",
        *(["--model", cfg.model] if cfg.model else []),
        prompt,
    ]


def _numbers_text(numbers: SessionNumbers) -> str:
    t = numbers.totals
    lines = [
        f"- Turns (API responses): {len(numbers.turns):,}; compactions: {numbers.compactions}",
        f"- Tokens: input {t.input:,}, output {t.output:,}, cache written {t.cache_write:,}, "
        f"cache read {t.cache_read:,}; cache hits {numbers.cache_hit_share:.0%} of input-side "
        "tokens",
        f"- Models: {', '.join(numbers.models) or 'unknown'}",
        f"- Wall-clock span: {wall_clock(numbers) or 'unknown'}",
    ]
    if numbers.largest_tool_outputs:
        outputs = "; ".join(f"{o.tool} {o.size:,}" for o in numbers.largest_tool_outputs)
        lines.append(f"- Largest tool outputs (characters): {outputs}")
    heavy = sorted(
        numbers.turns, key=lambda u: u.input + u.cache_write + u.cache_read, reverse=True
    )[:_TOP_TURNS]
    if heavy:
        turns = "; ".join(
            f"turn {u.turn}: input {u.input:,}, cache written {u.cache_write:,}, "
            f"cache read {u.cache_read:,}, output {u.output:,}"
            for u in heavy
        )
        lines.append(f"- Turns with the most input-side tokens: {turns}")
    return "\n".join(lines)


def _catalog_text(catalog: Sequence[CatalogEntry]) -> str:
    lines = []
    for entry in catalog:
        if entry.parent is not None:
            continue
        description = " ".join(entry.description.split())[:_DESCRIPTION_CHARS]
        if entry.kind == "flag":
            short = f" ({entry.short})" if entry.short else ""
            lines.append(f"- {entry.name}{short}: {description}")
        else:
            lines.append(f"- subcommand {entry.name}: {description}")
    return "\n".join(lines) or "(not available: the installed claude's --help could not be read)"


def build_prompt(
    numbers: SessionNumbers, catalog: list[CatalogEntry], default_flags: tuple[str, ...]
) -> str:
    """The review prompt (`prompt.txt`) filled in with the session's numbers, its launch flags
    (which may be incomplete), the author's default resume flags and the installed version's
    top-level flags and subcommands."""
    template = resources.files("vigil.review").joinpath("prompt.txt").read_text(encoding="utf-8")
    return (
        Template(template)
        .substitute(
            numbers=_numbers_text(numbers),
            launch_flags=", ".join(numbers.launch_flags) or "(none recorded)",
            default_flags=shlex.join(default_flags) if default_flags else "(none)",
            catalog=_catalog_text(catalog),
        )
        .strip()
    )


def check_reviewable(row: SessionRow, env: Mapping[str, str] | None = None) -> str:
    """The folder to review `row` in. Raises ReviewRefused when its id is not a session id (it
    goes into an argv), its folder is gone (`claude --resume` needs it) or `claude` is not on
    PATH."""
    if not SESSION_ID.fullmatch(row.session_id):
        raise ReviewRefused(f"not a valid session id: {row.session_id!r}")
    try:
        cwd = _check_cwd(row.cwd)
    except ResumeRefused as refused:
        raise ReviewRefused(refused.reason) from refused
    path = (os.environ if env is None else env).get("PATH")
    if shutil.which("claude", path=path) is None:
        raise ReviewRefused("cannot review: `claude` is not on PATH")
    return cwd


def busy_review(conn: sqlite3.Connection) -> Review | None:
    """A review running now on this machine (in this process or another), judged by the index:
    the newest `running` row, unless it is older than a review can run (left by a process that
    died)."""
    running = running_review(conn)
    oldest = datetime.now(UTC) - timedelta(seconds=REVIEW_TIMEOUT + 60)
    return running if running is not None and running.created_at > oldest else None


def claim_slot(slot: ReviewSlot, conn: sqlite3.Connection) -> None:
    """Take `slot` for a new review. Raises ReviewBusy, leaving the slot as it was, when a review
    is running in this process or (`busy_review`) in another."""
    if not slot.try_start():
        running = running_review(conn)
        raise ReviewBusy(running.id if running is not None else None)
    other = busy_review(conn)
    if other is not None:
        slot.finish()
        raise ReviewBusy(other.id)


@dataclass(frozen=True)
class PendingReview:
    """A review stored as `running`, with the command that will carry it out."""

    review_id: int
    argv: list[str]
    cwd: str
    budget_usd: float
    model: str | None


def begin_review(
    row: SessionRow,
    cfg: ReviewConfig,
    catalog: list[CatalogEntry],
    default_flags: tuple[str, ...],
    conn: sqlite3.Connection,
    *,
    claude_version: str | None = None,
) -> PendingReview:
    """Compute `row`'s numbers, build the command and store the review as `running`. Raises
    ReviewRefused (nothing stored) when it cannot run: see `check_reviewable`, or the transcript
    cannot be read."""
    cwd = check_reviewable(row)
    flags = vigil_launch_flags(conn, row.session_id)
    try:
        numbers = compute_numbers(Path(row.transcript_path), flags)
    except OSError as error:
        raise ReviewRefused(f"cannot read the transcript: {error.strerror or error}") from error
    prompt = build_prompt(numbers, catalog, default_flags)
    argv = review_argv(row.session_id, cfg, json.dumps(SCHEMA), prompt)
    review_id = insert_running(
        conn,
        session_id=row.session_id,
        machine=row.machine,
        numbers=numbers,
        claude_version=claude_version,
        model=cfg.model,
    )
    return PendingReview(review_id, argv, cwd, cfg.max_budget_usd, cfg.model)


@dataclass(frozen=True)
class _Outcome:
    answer: tuple[list[Suggestion], list[Advice]] | None
    """None when there is no usable answer (then `error` says why)."""
    cost_usd: float | None
    error: str | None
    model: str | None


def _json(text: str) -> Any:
    """`text` parsed as JSON (inside a ``` fence, if it has one), or None."""
    fenced = _FENCE.search(text)
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return None


def _result_object(stdout: str) -> dict[str, Any] | None:
    """The result object `claude -p --output-format json` printed (its last JSON line if there
    are several), or None."""
    data = _json(stdout.strip())
    if data is None:
        lines = [line for line in stdout.splitlines() if line.strip()]
        data = _json(lines[-1]) if lines else None
    return data if isinstance(data, dict) else None


def _usable(answer: Any) -> bool:
    return isinstance(answer, dict) and (
        isinstance(answer.get("suggestions"), list) or isinstance(answer.get("token_advice"), list)
    )


def _answer(result: dict[str, Any]) -> Any:
    """Claude's answer in `result`: `structured_output`, else `result` text parsed as JSON."""
    structured = result.get("structured_output")
    if _usable(structured):
        return structured
    text = result.get("result")
    parsed = _json(text) if isinstance(text, str) else None
    return parsed if _usable(parsed) else None


def _reason(result: dict[str, Any]) -> str:
    """What `result` says went wrong."""
    errors = result.get("errors")
    messages = [e for e in errors if isinstance(e, str)] if isinstance(errors, list) else []
    if messages:
        return "; ".join(messages)
    text = result.get("result")
    if isinstance(text, str) and text.strip():
        return text.strip()
    subtype = result.get("subtype")
    return str(subtype) if isinstance(subtype, str) else "no answer"


def _cost(result: dict[str, Any]) -> float | None:
    cost = result.get("total_cost_usd")
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def _models(result: dict[str, Any]) -> str | None:
    usage = result.get("modelUsage")
    names = [name for name in usage if isinstance(name, str)] if isinstance(usage, dict) else []
    return clean(", ".join(names)) or None


def interpret(stdout: str, returncode: int, stderr: str, budget_usd: float) -> _Outcome:
    """What a finished `claude -p` run amounts to (see the module docstring)."""
    result = _result_object(stdout)
    if result is None:
        tail = (stderr.strip() or stdout.strip())[-300:]
        detail = f": {tail}" if tail else ""
        error = f"claude exited with code {returncode} and no result{detail}"
        return _Outcome(None, None, clean(error), None)
    cost, model = _cost(result), _models(result)
    answer = _answer(result)
    if answer is None:
        if result.get("is_error") is True or returncode != 0:
            error = f"claude: {_reason(result)}"
        else:
            error = f"Claude's answer was not in the expected form: {_reason(result)}"
        return _Outcome(None, cost, clean(error), model)
    note: str | None = None
    if result.get("subtype") == "error_max_budget_usd":
        spent = f"${cost:.2f}" if cost is not None else "more"
        note = f"went over the budget ({spent} of ${budget_usd:.2f})"
    elif result.get("is_error") is True:
        note = f"claude reported an error after answering: {_reason(result)}"
    elif returncode != 0:
        note = f"claude exited with code {returncode} after answering"
    return _Outcome(parse_output(answer), cost, clean(note) if note else None, model)


def _run(pending: PendingReview, slot: ReviewSlot | None) -> tuple[str, str, int] | None:
    """Run the review's `claude` and return `(stdout, stderr, exit code)`; None when it timed out
    (it is killed) or `slot` stopped it. Raises OSError when it cannot be started."""
    process = subprocess.Popen(  # noqa: S603 - argv is built by review_argv
        pending.argv,
        cwd=pending.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        if slot is not None and not slot.attach(pending.review_id, process):
            _end(process, 0)
            return None
        try:
            stdout, stderr = process.communicate(timeout=REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return None
        if slot is not None and slot.stopping:  # ended by `stop`, not by itself
            return None
        return stdout, stderr, process.returncode
    finally:
        if slot is not None:
            slot.detach()
        if process.poll() is None:  # an exception while it ran
            _end(process, 0)


def complete_review(
    pending: PendingReview, conn: sqlite3.Connection, *, slot: ReviewSlot | None = None
) -> None:
    """Run the review's `claude` and store the outcome: `done` with the suggestions, or `failed`
    with why. The row never stays `running`, whatever goes wrong. With `slot`, the process is
    held there while it runs, and a `slot.stop()` ends it and fails the review with `STOPPED`."""
    try:
        try:
            ran = _run(pending, slot)
        except OSError as error:
            fail(conn, pending.review_id, clean(f"could not run claude: {error}"))
            return
        if ran is None and slot is not None and slot.stopping:
            fail_if_running(conn, pending.review_id, STOPPED)
            return
        if ran is None:
            fail(conn, pending.review_id, f"stopped after {REVIEW_TIMEOUT:.0f} s without an answer")
            return
        stdout, stderr, returncode = ran
        outcome = interpret(stdout, returncode, stderr, pending.budget_usd)
        if outcome.answer is None:
            fail(conn, pending.review_id, outcome.error or "no answer", cost_usd=outcome.cost_usd)
            return
        suggestions, advice = outcome.answer
        finish(
            conn,
            pending.review_id,
            suggestions=suggestions,
            advice=advice,
            cost_usd=outcome.cost_usd,
            model=None if pending.model else outcome.model,
            error=outcome.error,
        )
    except BaseException as error:
        fail(conn, pending.review_id, clean(f"the review stopped: {error!r}"))
        raise


def run_review(
    row: SessionRow,
    cfg: ReviewConfig,
    catalog: list[CatalogEntry],
    default_flags: tuple[str, ...],
    conn: sqlite3.Connection,
    *,
    claude_version: str | None = None,
) -> int:
    """Review `row` now, waiting for it (up to `REVIEW_TIMEOUT`); return the review's id. Raises
    ReviewRefused (nothing stored) when it cannot start."""
    pending = begin_review(row, cfg, catalog, default_flags, conn, claude_version=claude_version)
    complete_review(pending, conn)
    return pending.review_id
