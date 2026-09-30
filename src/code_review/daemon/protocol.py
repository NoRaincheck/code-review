"""Newline-delimited JSON framing for the daemon socket.

Requests are one JSON object per line, responses likewise. A socket that is framed this
way survives partial reads and never needs a length prefix, which is all a local
Unix-domain control channel requires.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

MAX_LINE_BYTES = 1_000_000


class ProtocolError(Exception):
    """A peer sent something that is not a valid frame."""


@dataclass(frozen=True, slots=True)
class Request:
    """A command sent to the daemon."""

    op: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> Request:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: object) -> Request:
        if not isinstance(data, dict):
            raise ProtocolError("request must be a JSON object")
        op = data.get("op")
        if not isinstance(op, str) or not op:
            raise ProtocolError("request requires a string 'op'")
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("'params' must be an object")
        return cls(op=op, params=params)


@dataclass(frozen=True, slots=True)
class Response:
    """A reply from the daemon."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException) -> Response:
        return cls(ok=False, error=f"{type(exc).__name__}: {exc}")


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


async def write_message(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(encode_message(payload))
    await writer.drain()


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one frame. Returns None at a clean end of stream."""
    try:
        line = await reader.readline()
    except ValueError as exc:
        raise ProtocolError(f"frame too large (limit {MAX_LINE_BYTES} bytes)") from exc
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError(f"frame too large (limit {MAX_LINE_BYTES} bytes)")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid frame: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("frame must be a JSON object")
    return data
