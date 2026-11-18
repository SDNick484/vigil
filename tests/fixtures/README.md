# Test fixtures

## `transcripts/2.1.312/`: real transcripts, sanitized

Recorded on 2026-11-17 with **Claude Code 2.1.312** (`claude --version`), Linux, by running the
installed `claude` non-interactively. Each file is a real session transcript, copied from
`~/.claude/projects/<encoded scratch path>/` and passed through `sanitize.py`. File names are the
session ids, as Claude Code writes them.

| File | Session |
| --- | --- |
| `7fb815ab-….jsonl` | `-p` with Haiku, then `-p --resume <id>` with Sonnet. The resume appended to the same file under the same id. |
| `e47d2def-….jsonl` | `-p -n fixture-named`: `custom-title` and `agent-name` records. |
| `68e04fa1-….jsonl` | `-p --resume 7fb815ab… --fork-session`: a new id whose file starts with copies of the earlier messages. |
| `c8232e85-….jsonl` | `-p "/greet"`: a project slash command that runs Bash, Read and the `fixture-skill` skill. |
| `7bf4d32d-….jsonl` | `-p` calling an MCP tool (`mcp__fixture__echo`) and a background `general-purpose` subagent. |
| `7bf4d32d-…/subagents/agent-a6baffeca9ef6541c.jsonl` (+ `.meta.json`) | That subagent's own transcript. |
| `f26f1b2c-….jsonl` | `-p --permission-mode plan`. |

### How they were made

The owner chose to run `claude` with the real `HOME` (a scratch `HOME` is not logged in) from a
throwaway git repo, so the sessions landed in their own `~/.claude/projects/` folder and only those
files were read. `<scratch>` below is that throwaway repo; `<worktree>` is the vigil checkout it
lived under. The repo held one commit with a `README.md`, a project command and a project skill:

- `.claude/commands/greet.md`: "Do these steps in order and keep every reply short: 1. Run `ls`
  with the Bash tool. 2. Read README.md with the Read tool. 3. Invoke the fixture-skill skill with
  the Skill tool. Then reply with exactly: done"
- `.claude/skills/fixture-skill/SKILL.md`: a skill whose body is "When this skill runs, reply with
  exactly: skill ran"
- `mcp.json`: one stdio server named `fixture`, a 20-line Python script answering `initialize`,
  `tools/list` (one tool, `echo`) and `tools/call` (returns its `text` argument).

Every `claude` call ran from `<scratch>` with the parent Claude Code session's `CLAUDE*`
environment variables removed (`env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID ...`), with `</dev/null`
on stdin, and with these flags added, to keep the owner's user settings, hooks and MCP servers out:

    claude --setting-sources project,local --strict-mcp-config --max-budget-usd 0.50 ...

The sessions, in order (`... ` is the prefix above):

    claude ... -p --model haiku --output-format json "Reply with exactly: hello fixture"
    claude ... -p --model sonnet --resume 7fb815ab-28b5-4123-85d0-64e6fd3b4141 --output-format json "Reply with exactly: resumed fixture"
    claude ... -p --model haiku -n fixture-named --output-format json "Reply with exactly: named fixture"
    claude ... -p --model haiku --resume 7fb815ab-28b5-4123-85d0-64e6fd3b4141 --fork-session --output-format json "Reply with exactly: forked fixture"
    claude ... -p --model haiku --allowedTools "Bash(ls*)" Read Skill --output-format json "/greet"
    claude ... -p --model haiku --mcp-config <scratch>/../mcp.json --allowedTools mcp__fixture__echo Agent --output-format json "First call the echo tool of the fixture MCP server with text ping. Then use the Agent tool with subagent_type general-purpose and model haiku, asking it to reply with exactly: pong. Then reply with exactly: finished"
    claude ... -p --model haiku --permission-mode plan --output-format json "Plan how to add a LICENSE file to this repo, in two short steps. Do not explore."

(The plan-mode session answered with Sonnet even though `--model haiku` was given; the transcript
records what happened.) The raw files were copied out and sanitized with:

    cp -r ~/.claude/projects/<encoded scratch path>/. <raw>/
    python tests/fixtures/sanitize.py <raw> tests/fixtures/transcripts/2.1.312 \
        --replace "<scratch>=/home/user/src/fixture-repo" \
        --replace "<worktree>=/home/user/src/vigil" \
        --replace "<organization uuid>=00000000-0000-0000-0000-000000000000"

`sanitize.py` replaces those paths (and their encoded project-folder forms), the home directory,
the login name, the host name and e-mail addresses, and redacts the content of system-prompt
snapshots, API request shapes and blobs, skill and agent listings, loaded `CLAUDE.md` files, session
context and credential attachments, keeping their record types and keys. Afterwards the fixture
folder was searched for the real home directory path, login name, host name, e-mail address and
organization id; none remain.

### What they show (and what the parser relies on)

- Record types seen: `user`, `assistant`, `attachment`, `queue-operation`, `last-prompt`,
  `custom-title`, `agent-name`, `mode`, `tools-used`, `cost-state`, `atis-latch`, `api-request`,
  `api-request-shape`, `api-request-blob`. The parser uses `user`, `assistant` and `custom-title`
  and reads `sessionId`, `cwd`, `gitBranch`, `version` and `timestamp` from any record.
- **No record names a previous session.** `-p --resume <id>` continues the same file; a fork gets a
  new file whose copied messages keep their original `uuid`s but carry the new `sessionId`. So
  `previous_session_id` is always `None` for these files, and resume chains rely on Task 3's
  fallback (each session is its own chain).
- Subagent transcripts live in `<session-id>/subagents/agent-*.jsonl`, carry the **parent's**
  `sessionId`, and hold only `isSidechain: true` records. A scanner should not treat them as
  sessions of their own.
- A slash command prompt is a `user` record whose content is
  `<command-message>greet</command-message>\n<command-name>/greet</command-name>`, followed by an
  `isMeta: true` record with the expanded command body.
- Plan mode shows as `permissionMode: "plan"` on the user record (and a `plan_mode` attachment).
- Task notifications (a background subagent finishing) are `user` records with
  `promptSource: "system"`.

The module docstring of `src/vigil/transcript.py` lists the exact field and feature mapping.

## `transcripts/handmade/`: a deliberately broken file

`11111111-1111-4111-8111-111111111111.jsonl` is written by hand (record shapes copied from the real
fixtures) to test tolerance. In order, its lines are: a user prompt containing the invalid UTF-8
bytes `\xff\xfe`; a line that is not valid JSON; a JSON array; a record of an unknown type; an
assistant reply with a Bash tool use; a user record whose `content` is a number; and a final user
record cut off mid-line with no trailing newline (Claude Code mid-write).

`remote-control.jsonl` (session `22222222-2222-4222-8222-222222222222`) is also hand-written: a
user prompt, an assistant reply, two `bridge_status` system records with placeholder Remote Control
links (`session_PLACEHOLDER01`, older, and `02`, newer) and one whose `url` is not a claude.ai link.
The shared helpers in `tests/conftest.py` leave it out of the fixture tree; the tests that use it
copy it themselves.
