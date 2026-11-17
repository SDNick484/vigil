# Captured `claude` output

Real output from Claude Code **2.1.312** (`claude --version` prints `2.1.312 (Claude Code)`), captured
on Linux. Used by `tests/test_claude_cli.py`, and reused by the feature catalog (Task 8) to parse
the full flag and subcommand list.

| File | Command | Notes |
| --- | --- | --- |
| `claude_help.txt` | `claude --help` | Complete and unmodified. |
| `claude_agents_help.txt` | `claude agents --help` | Complete and unmodified. |
| `claude_version.txt` | `claude --version` | Unmodified. |
| `claude_agents_json_all.json` | `claude agents --json --all` | Sanitized, see below. |

The help files contain no paths, user names or hostnames (checked), so they are verbatim.

## `claude_agents_json_all.json`

Captured with `--cwd <throwaway repo>` so only the capture session was listed (the real install
also lists the owner's sessions; none of those were read). The entry came from a short
`claude -p` run that was still executing; it is the only shape observed. Sanitized: `cwd`
replaced with `/home/user/projects/demo`, `sessionId` replaced with a placeholder UUID, `name`
and `pid` replaced. `startedAt` (epoch milliseconds) and `status` are as captured.

Fields seen: `pid`, `cwd`, `kind`, `startedAt`, `sessionId`, `name`, `status`.

### Unverified

- Only `"kind": "interactive"` was captured. A `claude --bg` session could not be started
  non-interactively: `claude --bg` refuses with "Workspace not trusted" until the trust prompt is
  accepted in an interactive session, and accepting it would change the owner's Claude config. So
  the exact `kind` value for a background session (assumed `"background"`) and the `status`
  values other than `busy` are **unverified**. The parser maps `"interactive"` to interactive and
  every other `kind` to background, and ignores `status`.
- `name` is `null`/absent when the session has no name: assumed, not observed.
- A completed session listed by `--all` is not captured; `live_sessions` does not pass `--all`.
