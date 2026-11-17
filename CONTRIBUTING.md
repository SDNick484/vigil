# Contributing

Python 3.14 on Linux (WSL2 counts). Set up a virtualenv and install with dev extras:

    python3.14 -m venv .venv
    .venv/bin/pip install -e ".[dev]"

The verification gate, which CI runs exactly:

    ruff check . && ruff format --check . && mypy && pytest

Rules of the road:

- One PR per issue, with `Closes #N` in the description.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Every PR adds one line under `[Unreleased]` in `CHANGELOG.md`.
- vigil never writes under the Claude home (`~/.claude`). Tests never launch a real Claude session
  or terminal; use the `fake_bin` fixture, which puts recording fakes on `PATH`.
- The repository is public: no real names, home paths, hostnames, emails, tokens or session
  content in any committed file, fixtures included. Use placeholders.

See `CLAUDE.md` for the full constraints and the places where new code is appended.
