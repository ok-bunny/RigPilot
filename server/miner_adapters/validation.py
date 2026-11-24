"""Pure scalar, endpoint and version rules shared by Controller and Agent."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any, Iterable
from types import MappingProxyType
from urllib.parse import urlparse

THREE_PART_VERSION = re.compile(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
TWO_PART_VERSION = re.compile(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})([a-z]{1,8})?$")
SUPPORTED_VERSION = re.compile(r"^(?:(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})|(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})[a-z]{0,8})$")

MAX_EXTRA_ARGS_CHARS = 4096
# These flags select the generated job's identity, hardware, API or private
# files. Other software flags, including flags from future releases, pass
# through literally. Aliases are included because CLI parsers accept them too.
EXTRA_ARGS_RESERVED_FLAGS = MappingProxyType({
    "xmrig": frozenset({
        "--config", "-c", "--url", "-o", "--user", "-u", "--pass", "-p",
        "--userpass", "-O", "--algo", "-a", "--coin", "--rig-id",
        "--api-id", "--api-worker-id", "--http-host", "--http-port",
        "--http-access-token", "--http-no-restricted", "--log-file", "-l",
        "--background", "-B", "--no-color", "--threads", "-t",
        "--cpu-max-threads-hint", "--no-cpu", "--opencl", "--cuda",
        "--opencl-devices", "--cuda-devices", "--tls", "--keepalive", "-k",
        "--huge-pages", "--no-huge-pages", "--randomx-1gb-pages",
        "--pause-on-battery",
    }),
    "srbminer": frozenset({
        "--algorithm", "--algorithm-cpu", "--algorithm-gpu", "--pool",
        "--wallet", "--worker", "--password", "--tls", "--keepalive",
        "--tls-cert-sha256", "--api-enable", "--api-disable", "--api-port",
        "--api-ip", "--api-rig-name", "--log-file", "--log-file-mode",
        "--config", "--config-file", "--disable-cpu", "--disable-gpu",
        "--gpu-id", "--gpu-platform", "--gpu-reset-oc", "--gpu-cclock",
        "--gpu-mclock", "--gpu-coffset", "--gpu-moffset", "--gpu-plimit",
        "--gpu-cclock0", "--gpu-mclock0", "--gpu-coffset0", "--gpu-moffset0", "--gpu-plimit0",
        "--gpu-cclock1", "--gpu-mclock1", "--gpu-coffset1", "--gpu-moffset1", "--gpu-plimit1",
        "--gpu-disable-oc", "--disable-gpu-amd", "--disable-gpu-nvidia", "--disable-gpu-intel",
        "--cpu-threads", "--cpu-threads-reduce", "--cpu-threads-intensity",
        "--disable-huge-pages", "--randomx-use-1gb-pages", "--extended-log",
        "-a", "-ag", "-ac", "-t", "-p", "-o", "-u", "--background",
        "--startup-script", "--a0-oc-script", "--a1-oc-script", "--reboot-script-gpu-watchdog",
        "--api-rig-restart-url", "--api-rig-shutdown-url", "--api-miner-restart-url",
    }),
    "pearlminer": frozenset({
        "--proxy", "--address", "--worker", "--stratum-dialect", "-gpu",
        "--gpu", "--devices", "--device", "--config", "--config-file",
        "--log-file", "--api-port", "--api-bind",
    }),
    "seine": frozenset({
        "--mode", "--backend", "--address", "--pool-url", "--pool-worker",
        "--threads", "--cpu-threads", "--cpu-profile", "--ui", "--api-server",
        "--api-bind", "--api-cookie", "--api-token", "--api-allow-unsafe-bind",
        "--telemetry", "--telemetry-url", "--disable-update-check",
        "--self-update", "--auto-update",
        "--service", "--data-dir",
        "--daemon-dir", "--daemon-url", "--daemon-token", "--wallet-password",
        "--bench", "--bench-kind", "--bench-secs", "--bench-rounds",
    }),
})


def _split_extra_args(text: str) -> list[str]:
    """Split quote groups without a shell or POSIX path-unescaping.

    Single quotes are literal groups. Double quotes use the Windows rule for
    backslashes immediately before a double quote: pairs are backslashes,
    and an odd final backslash escapes that quote. All other backslashes are
    literal, including UNC prefixes. No expansion or shell operator exists.
    """
    result: list[str] = []
    token: list[str] = []
    quote: str | None = None
    started = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                token.append(character)
            index += 1
            continue
        if character == "\\":
            end = index
            while end < len(text) and text[end] == "\\":
                end += 1
            count = end - index
            if end < len(text) and text[end] == '"':
                token.append("\\" * (count // 2))
                if count % 2:
                    token.append('"')
                else:
                    quote = None if quote == '"' else '"'
                index = end + 1
            else:
                token.append("\\" * count)
                index = end
            started = True
            continue
        if character == quote:
            quote = None
        elif quote is None and character in {"'", '"'}:
            quote = character
            started = True
        elif quote is None and character.isspace():
            if started:
                result.append("".join(token))
                token = []
                started = False
        else:
            token.append(character)
            started = True
        index += 1
    if quote is not None:
        raise ValueError("extra_args has an unclosed quote")
    if started:
        result.append("".join(token))
    for argument in result:
        if argument.startswith(("—", "–")):
            corrected = "--" + argument[1:]
            raise ValueError(f"extra_args option {argument!r} uses a Unicode dash; use ASCII {corrected!r}")
    return result


def normalize_extra_args(value: object) -> str:
    """Validate editable wire text; explicit blank/null clears old arguments."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("extra_args must be a string")
    if len(value) > MAX_EXTRA_ARGS_CHARS:
        raise ValueError(f"extra_args must be at most {MAX_EXTRA_ARGS_CHARS} characters")
    if any((ord(c) < 32 and c not in "\t\n\r") or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("extra_args contains control characters")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    _split_extra_args(text)
    return text if text.strip() else ""


def parse_extra_args(
    value: object, *, miner_type: str | None = None,
    reserved_flags: Iterable[str] = (),
) -> list[str]:
    """Return literal argv and refuse overrides of the managed job boundary.

    Callers may add reserved flags for a particular runtime. Flag/value tokens
    and ``--flag=value`` are checked, as are compact single-letter aliases.
    Unknown flags and empty quoted values keep their original argv meaning.
    """
    arguments = _split_extra_args(normalize_extra_args(value))
    reserved = set(EXTRA_ARGS_RESERVED_FLAGS.get(miner_type or "", ()))
    reserved.update(reserved_flags)
    for argument in arguments:
        flag = argument.split("=", 1)[0]
        compact = next((item for item in reserved if len(item) == 2
                        and item.startswith("-") and argument.startswith(item)
                        and len(argument) > 2), None)
        # Preserve case-distinct single-letter aliases (XMRig -o versus -O),
        # while protecting long flags against parsers accepting mixed case.
        matched = flag if flag in reserved else next(
            (item for item in reserved if len(item) > 2 and flag.casefold() == item.casefold()),
            None,
        )
        if matched or compact:
            raise ValueError(f"extra_args conflicts with managed parameter {matched or compact or flag}; use the flight sheet fields")
    return arguments

def validate_option_value(spec: Any, value: Any) -> Any:
    """Validate one value; defaults, omission and error wrappers stay with callers."""
    key = spec.name
    if key == "extra_args" and spec.value_type == "string":
        return normalize_extra_args(value)
    if value is None and spec.nullable:
        return None
    if spec.value_type == "boolean":
        if not isinstance(value, bool): raise ValueError(f"{key} must be a boolean")
    elif spec.value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int): raise ValueError(f"{key} must be an integer")
        if spec.minimum is not None and value < spec.minimum: raise ValueError(f"{key} is below its minimum")
        if spec.maximum is not None and value > spec.maximum: raise ValueError(f"{key} is above its maximum")
    elif spec.value_type == "choice":
        if not any(type(value) is type(choice) and value == choice for choice in spec.choices):
            raise ValueError(f"{key} must be one of: " + ", ".join(str(item) for item in spec.choices))
    elif spec.value_type == "string":
        if not isinstance(value, str): raise ValueError(f"{key} must be a string")
        if any(ord(c) < 32 or ord(c) == 127 for c in value): raise ValueError(f"{key} contains control characters")
        minimum = getattr(spec, "min_length", 0)
        maximum = getattr(spec, "max_length", None)
        if len(value) < minimum or maximum is not None and len(value) > maximum:
            raise ValueError(f"{key} has an invalid length")
    else:
        raise ValueError(f"{key} has an unsupported option type")
    return value

def normalize_pool_text(value: Any) -> str:
    if not isinstance(value, str): raise ValueError("pool_url must be a string")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("pool_url contains whitespace or control characters")
    text = value.strip()
    if any(c.isspace() for c in text): raise ValueError("pool_url contains whitespace or control characters")
    return text

@dataclass(frozen=True, slots=True)
class PoolEndpoint:
    url: str
    scheme: str
    host: str
    port: int
    tls: bool

def parse_pool_endpoint(value: Any, allowed_schemes: Iterable[str]) -> PoolEndpoint:
    text = normalize_pool_text(value)
    try:
        parsed = urlparse(text)
        scheme, host, port = parsed.scheme.lower(), parsed.hostname, parsed.port
        username, password = parsed.username, parsed.password
    except (ValueError, TypeError) as exc:
        raise ValueError("pool_url is malformed") from exc
    if scheme not in set(allowed_schemes): raise ValueError("pool_url uses an unsupported protocol")
    if not host or username is not None or password is not None: raise ValueError("pool_url must contain a host and no credentials")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("pool_url must not contain a path, query, or fragment")
    if port is None or not 1 <= port <= 65535: raise ValueError("pool_url must include a valid port")
    return PoolEndpoint(text, scheme, host, port, scheme in {"stratum+ssl", "stratum+tls"})
