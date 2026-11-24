"""Bounded serial-console transport and authenticated verifier results."""
from __future__ import annotations

import socket
import time


def contains_exact_line(payload: bytes, marker: bytes) -> bool:
    # The last fragment is still being received until a line terminator arrives.
    lines = payload.replace(b"\r", b"\n").split(b"\n")[:-1]
    return any(line.strip() == marker for line in lines)


def contains_marker_line(payload: bytes, marker: bytes) -> bool:
    # Serial smoke-marker matching for boot logs.  Depending on the systemd
    # release, StandardOutput=journal+console renders the console copy of a
    # service line either bare or with the kernel timestamp plus
    # "unit[pid]:" journal prefix (observed on the 2026-11-01 noble builder:
    # "[   20.199061] rigpilotos-smoke-ready[400]: MARKER").  Accept exactly
    # those two shapes; arbitrary prefixes or suffixes still do not count so
    # a partial readiness line can never satisfy the gate.
    lines = payload.replace(b"\r", b"\n").split(b"\n")[:-1]
    for line in lines:
        stripped = line.strip()
        if stripped == marker:
            return True
        if stripped.startswith(b"[") and stripped.endswith(b": " + marker):
            return True
    return False


def contains_failure_line(payload: bytes, marker: bytes) -> bool:
    lines = payload.replace(b"\r", b"\n").split(b"\n")[:-1]
    return any(line.strip() == marker or line.strip().startswith(marker + b" ") for line in lines)


def verified_result(payload: bytes, success: bytes, failed: bytes) -> bool:
    return contains_exact_line(payload, success) and not contains_failure_line(payload, failed)


def connect_serial(port: int, deadline: float) -> socket.socket:
    last_error = None
    while time.monotonic() < deadline:
        stream = socket.socket()
        stream.settimeout(1.0)
        try:
            stream.connect(("127.0.0.1", port))
            return stream
        except OSError as error:
            last_error = error
            stream.close()
            time.sleep(0.25)
    raise RuntimeError(f"serial connection failed: {last_error}")


def _read_until(stream, accepts, deadline: float, context: str) -> bytes:
    received = bytearray()
    while time.monotonic() < deadline:
        try:
            chunk = stream.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            raise RuntimeError("serial connection closed")
        received.extend(chunk)
        payload = bytes(received)
        if accepts(payload):
            return payload
        if len(received) > 4 * 1024 * 1024:
            del received[:len(received) - 2 * 1024 * 1024]
    tail = bytes(received[-8000:]).decode("utf-8", errors="replace")
    raise RuntimeError(f"timeout waiting for {context}\n{tail}")


def wait_for(stream: socket.socket, needle: bytes, deadline: float) -> bytes:
    return _read_until(stream, lambda payload: needle in payload, deadline, repr(needle))


def wait_for_result(stream: socket.socket, success: bytes, failed: bytes, deadline: float) -> bytes:
    def completed(payload):
        return contains_exact_line(payload, success) or contains_failure_line(payload, failed)
    return _read_until(stream, completed, deadline, "verification result")


def authenticate(stream, password: str, deadline: float, *, username="user", sudo=False, capture=None, wait=wait_for):
    """Authenticate after the caller's login-prompt wait; never log the password."""
    def receive(marker):
        payload = wait(stream, marker, deadline)
        if capture is not None:
            capture.extend(payload)
        return payload
    stream.sendall(username.encode("utf-8") + b"\n")
    receive(b"Password:")
    stream.sendall(password.encode("utf-8") + b"\n")
    authentication = receive(b"user@rigpilot:")
    if b"Login incorrect" in authentication:
        raise RuntimeError("serial-console authentication was rejected")
    if sudo:
        stream.sendall(b"sudo -S -v\n")
        receive(b"password for user:")
        stream.sendall(password.encode("utf-8") + b"\n")
        receive(b"user@rigpilot:")
    return authentication
