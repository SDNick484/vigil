"""What Claude is asked to answer with (`SCHEMA`, passed to `claude --json-schema`), and reading
that answer back (`parse_output`).

The answer comes from Claude reading a transcript that can hold fetched web pages and other
untrusted text, so it is untrusted too: every item is checked here, an invalid one is dropped
rather than failing the review, and every text field loses its control characters and is cut to
`MAX_TEXT` characters. What a suggestion may then make vigil do is decided later, by the
allow-lists (`vigil.flags`).
"""

import re
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

MAX_TEXT = 500
"""Most characters kept of any text field (titles: `MAX_TITLE`)."""
MAX_TITLE = 80
MAX_SUGGESTIONS = 10
MAX_ADVICE = 10

Kind = Literal["flag", "file", "habit"]
FileTarget = Literal["project_claude_md", "project_settings", "user_claude_md", "user_settings"]

_SETTINGS_TARGETS = ("project_settings", "user_settings")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "description": "At most ten, the one with the most expected benefit first.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(get_args(Kind))},
                    "title": _text("What to do, in at most 80 characters."),
                    "why": _text("Why it would help, in at most 500 characters."),
                    "evidence": _text(
                        "Where this session shows it: a turn number or a short quote; at most "
                        "500 characters."
                    ),
                    "flag": {
                        "type": "object",
                        "description": "For kind flag: the claude command-line flag.",
                        "properties": {
                            "name": _text("The flag, e.g. --model."),
                            "value": _text("Its value, if it takes one."),
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    "file": {
                        "type": "object",
                        "description": "For kind file: the file to change and what to add.",
                        "properties": {
                            "target": {"type": "string", "enum": list(get_args(FileTarget))},
                            "text": _text("For a CLAUDE.md target: the text to append."),
                            "settings": {
                                "type": "object",
                                "description": "For a settings target: the JSON to merge in.",
                            },
                        },
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                },
                "required": ["kind", "title", "why", "evidence"],
                "additionalProperties": False,
            },
        },
        "token_advice": {
            "type": "array",
            "description": "A few short points on using fewer tokens in sessions like this one.",
            "items": {
                "type": "object",
                "properties": {
                    "title": _text("The point, in at most 80 characters."),
                    "why": _text("Why, in at most 500 characters."),
                },
                "required": ["title", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suggestions", "token_advice"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Suggestion:
    """One suggestion. `flag_*` are set for kind `flag` (`flag_name` always), `file_*` for kind
    `file` (`file_target` always, with `file_text` for a CLAUDE.md target and `file_settings`
    for a settings target)."""

    kind: Kind
    title: str
    why: str
    evidence: str
    flag_name: str | None
    flag_value: str | None
    file_target: FileTarget | None
    file_text: str | None
    file_settings: dict[str, Any] | None


@dataclass(frozen=True)
class Advice:
    """A point about using fewer tokens; text only."""

    title: str
    why: str


def clean(text: str, limit: int = MAX_TEXT) -> str:
    """`text` without control characters (C0, DEL and C1, except newline and tab: they could
    drive a terminal that prints it), cut to `limit` characters."""
    return _CONTROL.sub("", text)[:limit]


def _cut(value: Any, limit: int = MAX_TEXT) -> str | None:
    """`value` cleaned and cut to `limit` characters, or None when it is not a string."""
    return clean(value, limit) if isinstance(value, str) else None


def _cut_leaves(value: Any) -> Any:
    """`value` (parsed JSON) with every string in it, keys included, cleaned and cut to
    `MAX_TEXT` characters."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(str(key)): _cut_leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cut_leaves(item) for item in value]
    return value


def _object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _suggestion(item: Any) -> Suggestion | None:
    """`item` as a Suggestion, or None when it is not a valid one."""
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    title, why = _cut(item.get("title"), MAX_TITLE), _cut(item.get("why"))
    if kind not in get_args(Kind) or not title or not why:
        return None
    evidence = _cut(item.get("evidence")) or ""
    flag_name = flag_value = file_target = file_text = None
    file_settings: dict[str, Any] | None = None
    if kind == "flag":
        flag = _object(item.get("flag"))
        flag_name, flag_value = _cut(flag.get("name")), _cut(flag.get("value"))
        if not flag_name:
            return None
    elif kind == "file":
        file = _object(item.get("file"))
        file_target = file.get("target")
        if file_target not in get_args(FileTarget):
            return None
        if file_target in _SETTINGS_TARGETS:
            settings = file.get("settings")
            if not isinstance(settings, dict) or not settings:
                return None
            file_settings = _cut_leaves(settings)
        else:
            file_text = _cut(file.get("text"))
            if not file_text:
                return None
    return Suggestion(
        kind=cast(Kind, kind),
        title=title,
        why=why,
        evidence=evidence,
        flag_name=flag_name,
        flag_value=flag_value,
        file_target=cast(FileTarget | None, file_target),
        file_text=file_text,
        file_settings=file_settings,
    )


def _advice(item: Any) -> Advice | None:
    if not isinstance(item, dict):
        return None
    title, why = _cut(item.get("title"), MAX_TITLE), _cut(item.get("why"))
    return Advice(title=title, why=why) if title and why else None


def parse_output(data: Any) -> tuple[list[Suggestion], list[Advice]]:
    """The valid suggestions (at most `MAX_SUGGESTIONS`, in the order given) and token advice in
    Claude's answer `data`; anything else in it is ignored."""
    answer = _object(data)
    items = answer.get("suggestions")
    points = answer.get("token_advice")
    suggestions = [s for s in map(_suggestion, items if isinstance(items, list) else []) if s]
    advice = [a for a in map(_advice, points if isinstance(points, list) else []) if a]
    return suggestions[:MAX_SUGGESTIONS], advice[:MAX_ADVICE]
