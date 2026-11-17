"""The one allow-list for flags vigil puts on a `claude` command line.

`resume.default_flags` in the config, the resume launcher and the flags a session review
suggests all go through `check`. A flag is allowed only when it is in `RULES` and its value
passes that flag's rule; anything else (`--dangerously-*`, `--system-prompt*`, `--mcp-config`,
`--settings`, `--add-dir`, `--tools`, ...) is refused simply by not being listed.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Literal

from vigil.catalog import CatalogEntry

Rule = Literal["none", "optional_name", "name", "model", "choice", "choice_no_bypass", "ident"]

RULES: Mapping[str, Rule] = {
    "--remote-control": "optional_name",
    "--name": "name",
    "--model": "model",
    "--effort": "choice",
    "--permission-mode": "choice_no_bypass",
    "--agent": "ident",
    "--worktree": "none",
    "--chrome": "none",
    "--no-chrome": "none",
    "--ide": "none",
    "--verbose": "none",
    "--brief": "none",
}

_MAX_NAME = 64
_MODEL = re.compile(r"[A-Za-z0-9._:\[\]-]{1,64}")
_IDENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WORD = re.compile(r"[A-Za-z]{1,32}")
_BYPASS = "bypassPermissions"

# `(choices: "a", "b")` as Commander prints a restricted option, and the bare `(a, b, c)` form
# some options use for the same thing.
_CHOICES = re.compile(r'\(choices:\s*((?:"[^"]*"\s*,?\s*)+)\)')
_QUOTED = re.compile(r'"([^"]*)"')
_BARE_CHOICES = re.compile(r"\(\s*([A-Za-z][\w-]*(?:\s*,\s*[A-Za-z][\w-]*)+)\s*\)")


class FlagRefused(Exception):
    """A flag, or its value, is not allowed; `reason` is user-facing text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def choices_from_help(description: str) -> tuple[str, ...]:
    """The values a flag's catalog description lists as its choices, or () when it lists none.

    Reads `(choices: "a", "b")` and the bare `(a, b, c)` form; the description may be wrapped
    (the catalog joins wrapped lines with single spaces, but any whitespace is accepted).
    """
    text = " ".join(description.split())
    found = _CHOICES.search(text)
    if found:
        return tuple(_QUOTED.findall(found.group(1)))
    bare = _BARE_CHOICES.search(text)
    if bare:
        return tuple(item.strip() for item in bare.group(1).split(","))
    return ()


def _listed(flag: str, catalog: Sequence[CatalogEntry]) -> CatalogEntry:
    for entry in catalog:
        if entry.kind == "flag" and entry.parent is None and entry.name == flag:
            return entry
    raise FlagRefused(f"{flag} is not offered by the installed claude")


def _check_name(flag: str, value: str) -> None:
    if not value or len(value) > _MAX_NAME or not value.isprintable() or value.startswith("-"):
        raise FlagRefused(
            f"{flag}: the value must be 1 to {_MAX_NAME} printable characters and not start with -"
        )


def _check_choice(
    flag: str, value: str, rule: Rule, catalog: Sequence[CatalogEntry] | None
) -> None:
    no_bypass = rule == "choice_no_bypass"
    if no_bypass and value.casefold() == _BYPASS.casefold():
        raise FlagRefused(f"{flag} {_BYPASS} is not allowed")
    choices: tuple[str, ...] = ()
    if catalog is not None:
        choices = choices_from_help(_listed(flag, catalog).description)
        if no_bypass:
            choices = tuple(c for c in choices if c.casefold() != _BYPASS.casefold())
    if choices:
        if value not in choices:
            raise FlagRefused(f'{flag}: "{value}" is not one of {", ".join(choices)}')
    elif not _WORD.fullmatch(value):
        raise FlagRefused(f"{flag}: {value!r} is not a valid value")


def _check_value(flag: str, rule: Rule, value: str, catalog: Sequence[CatalogEntry] | None) -> None:
    if rule == "optional_name" or rule == "name":
        _check_name(flag, value)
    elif rule == "model":
        if value.startswith("-") or not _MODEL.fullmatch(value):
            raise FlagRefused(f"{flag}: {value!r} is not a valid model name")
    elif rule == "ident":
        if value.startswith("-") or not _IDENT.fullmatch(value):
            raise FlagRefused(f"{flag}: {value!r} is not a valid name")
    else:
        _check_choice(flag, value, rule, catalog)


def check(tokens: Sequence[str], catalog: Sequence[CatalogEntry] | None = None) -> list[str]:
    """`tokens` (flags, each with its value as the next token or as `--flag=value`) checked
    against `RULES`; returns them normalized to the separate-token form.

    With `catalog` (the installed claude's flags), a flag the installed version does not list is
    refused, and `choice` rules take their values from its help. Without it any letters pass for
    a choice (`bypassPermissions` is refused either way). Raises FlagRefused.
    """
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-"):
            raise FlagRefused(f"value without a flag: {token}")
        flag, equals, value = token.partition("=")
        rule = RULES.get(flag)
        if rule is None:
            raise FlagRefused(f"flag not allowed: {flag}")
        if catalog is not None:
            _listed(flag, catalog)
        has_value = bool(equals)
        if rule == "none":
            if has_value:
                raise FlagRefused(f"{flag} takes no value")
            result.append(flag)
            continue
        if not has_value and index < len(tokens) and not tokens[index].startswith("-"):
            value = tokens[index]
            index += 1
            has_value = True
        if not has_value:
            if rule == "optional_name":
                result.append(flag)
                continue
            raise FlagRefused(f"{flag} needs a value")
        _check_value(flag, rule, value, catalog)
        result.extend([flag, value])
    return result


def parse_suggested(name: str, value: str | None) -> list[str]:
    """The tokens for a suggested flag: `[name]`, or `[name, value]` when it has a value. Not
    checked; pass the result to `check`."""
    if value is None or value == "":
        return [name]
    return [name, value]


def _groups(tokens: Sequence[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    for token in tokens:
        if token.startswith("-") or not groups:
            groups.append([token])
        else:
            groups[-1].append(token)
    return groups


def merge(base: Sequence[str], extra: Sequence[str]) -> list[str]:
    """`base` with `extra` merged in, both as `check` returns them: a flag `base` already has takes
    its value (or its lack of one) from `extra` where it stands; the other flags of `extra` follow,
    in order."""
    added = {group[0]: group for group in _groups(extra)}
    merged: list[str] = []
    for group in _groups(base):
        merged.extend(added.pop(group[0], group))
    for group in added.values():
        merged.extend(group)
    return merged
