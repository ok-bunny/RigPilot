"""Shared template spelling and Controller Worker-name resolution.

Apply normalization only to explicit raw template fields, never wallet/pool
values, package scripts or already rendered text. Controller resolves a Worker
name once from the visible rig name; Agent receives that result as literal data.
"""
from __future__ import annotations

import re
from typing import Iterator

DEFAULT_WORKER_TEMPLATE = "%WORKER_NAME%"
DEFAULT_WALLET_WORKER_TEMPLATE = "%WAL%.%WORKER_NAME%"
MAX_WORKER_TEMPLATE_CHARS = 512
MAX_WORKER_NAME_CHARS = 128
HIVEOS_TEMPLATE_TOKENS = frozenset({"WAL", "WORKER_NAME", "COIN", "URL", "URL_HOST", "URL_PORT", "EMAIL"})
_PERCENT_TOKEN = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_URL_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_WORKER_BRACE = re.compile(r"\{(hostname|agent_name|system_hostname|agent_id)\}")
_WORKER_KEYS = {"hostname": "WORKER_NAME", "agent_name": "WORKER_NAME",
                "system_hostname": "system_hostname", "agent_id": "agent_id"}


def iter_hiveos_template_tokens(text: str) -> Iterator[tuple[re.Match[str], str | None]]:
    """Scan original text only; URL %HH escapes precede ASCII named macros."""
    position = 0
    while position < len(text):
        if text[position] == "%":
            escape = _URL_ESCAPE.match(text, position)
            if escape is not None:
                position = escape.end()
                continue
            token = _PERCENT_TOKEN.match(text, position)
            if token is not None:
                name = token[1].upper()
                if name == "WORK_NAME":
                    name = "WORKER_NAME"
                yield token, name if name in HIVEOS_TEMPLATE_TOKENS else None
                position = token.end()
                continue
        position += 1


def normalize_hiveos_template_tokens(text: str) -> str:
    """Canonicalize known input macros without changing values or shell/JSON text."""
    if not isinstance(text, str):
        raise ValueError("template must be a string")
    pieces, position = [], 0
    for token, name in iter_hiveos_template_tokens(text):
        pieces.extend((text[position:token.start()], f"%{name}%" if name else token[0]))
        position = token.end()
    pieces.append(text[position:])
    return "".join(pieces)


def _worker_tokens(text: str):
    position = 0
    while position < len(text):
        escape = _URL_ESCAPE.match(text, position)
        if escape is not None:
            position = escape.end()
            continue
        token = _PERCENT_TOKEN.match(text, position)
        if token is not None:
            if token[1].upper() in {"WORKER_NAME", "WORK_NAME"}:
                yield token, "WORKER_NAME"
            position = token.end()
            continue
        token = _WORKER_BRACE.match(text, position)
        if token is not None:
            yield token, _WORKER_KEYS[token[1]]
            position = token.end()
            continue
        position += 1


def normalize_worker_name_template(text: str) -> str:
    """Pure presentation/input normalization; RigPilot-only fields stay intact."""
    if not isinstance(text, str):
        raise ValueError("worker_name_template must be a string")
    pieces, position = [], 0
    for token, key in _worker_tokens(text):
        pieces.extend((text[position:token.start()], DEFAULT_WORKER_TEMPLATE if key == "WORKER_NAME" else token[0]))
        position = token.end()
    pieces.append(text[position:])
    return "".join(pieces)


def _check_worker_template(text: str) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_WORKER_TEMPLATE_CHARS:
        raise ValueError("worker_name_template must contain 1-512 characters")
    if any(ord(character) < 32 for character in text):
        raise ValueError("worker_name_template contains control characters")


def validate_worker_name_template(text: str) -> str:
    """Validate new inputs and return their standard spelling, without rendering.

    The input budget accommodates canonical expansion of every formerly valid
    128-character legacy template. The resolved Worker limit remains 128.
    Unknown percent text remains literal as before; unsupported braces fail.
    """
    _check_worker_template(text)
    normalized = normalize_worker_name_template(text)
    _check_worker_template(normalized)
    remainder = _WORKER_BRACE.sub("", normalized)
    if "{" in remainder or "}" in remainder:
        raise ValueError("worker_name_template only supports %WORKER_NAME%, {system_hostname}, and {agent_id} (legacy Worker aliases are accepted)")
    return normalized


def worker_template_uses_agent_name(text: str) -> bool:
    """Use the same scanner for rename dependencies as for actual rendering."""
    return isinstance(text, str) and any(key == "WORKER_NAME" for _token, key in _worker_tokens(text))


def render_worker_name_template(
    text: str, *, agent_name: str, system_hostname: str, agent_id: str,
) -> str:
    """Resolve known original spans once. Injected names are never rescanned.

    WORKER_NAME here means the base visible device name, not this template or
    its own final result. In subsequent HiveOS field rendering it means the
    already resolved literal Worker name sent by the Controller.
    """
    _check_worker_template(text)
    values = {"WORKER_NAME": str(agent_name), "system_hostname": str(system_hostname), "agent_id": str(agent_id)}
    pieces, position = [], 0
    for token, key in _worker_tokens(text):
        pieces.extend((text[position:token.start()], values[key]))
        position = token.end()
    pieces.append(text[position:])
    worker = "".join(pieces).strip()
    if not worker or len(worker) > MAX_WORKER_NAME_CHARS:
        raise ValueError("rendered worker name must contain 1-128 characters")
    if any(ord(character) < 32 for character in worker):
        raise ValueError("rendered worker name contains control characters")
    return worker
