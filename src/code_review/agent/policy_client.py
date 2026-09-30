"""The ``acp.interfaces.Client`` implementation used while reviewing.

Two jobs:

1. Apply :mod:`code_review.policy` to every ``session/request_permission``.
2. Accumulate the streamed agent output into markdown we can store.

The session_update handler is deliberately defensive. ``acp/connection.py`` swallows
exceptions raised inside client callbacks, so a typo in an attribute path here would look
exactly like "the agent never sent anything". Every body is guarded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp.interfaces import Agent, Client
from acp.schema import (
    AllowedOutcome,
    CreateElicitationResponse,
    CreateTerminalResponse,
    DeniedOutcome,
    ElicitationMode,
    EnvVariable,
    KillTerminalResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)

from .. import policy
from ..policy import Decision, Verdict

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolEvent:
    """One recorded tool call, for the audit log and for permission decisions.

    ``kind`` and ``raw_input`` are retained because ``session/request_permission`` is not
    required to repeat them: the request's ``toolCall`` may carry only an id and a title.
    Without this cache every such request would be refused for a missing kind, which would
    make the agent look broken while the real cause is a thinner notification.
    """

    tool_call_id: str
    title: str | None = None
    kind: str | None = None
    status: str | None = None
    raw_input: Any = None
    decision: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ReviewClientConfig:
    """Everything the client needs that is not a method parameter."""

    repo: Path
    deny_unknown_commands: bool = True
    max_read_bytes: int = 400_000


@dataclass(slots=True)
class ReviewClient(Client):
    """Accumulates a review and enforces the permission policy."""

    config: ReviewClientConfig
    markdown_parts: list[str] = field(default_factory=list)
    tool_events: dict[str, ToolEvent] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    current_message_id: str | None = None

    # -- output collection ------------------------------------------------

    def markdown(self) -> str:
        """The collected agent output as a single markdown document."""
        return "".join(self.markdown_parts).strip()

    def tool_log(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.tool_events.values()]

    def _record(self, **kwargs: Any) -> None:
        try:
            call_id = str(kwargs.get("tool_call_id") or f"call-{len(self.tool_events)}")
            event = self.tool_events.get(call_id)
            if event is None:
                event = ToolEvent(tool_call_id=call_id)
                self.tool_events[call_id] = event
            for key in ("title", "kind", "status", "raw_input"):
                if kwargs.get(key) is not None:
                    setattr(event, key, kwargs[key])
        except Exception:
            log.debug("failed to record tool event", exc_info=True)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Handle one ``session/update`` notification. Must not raise."""
        try:
            variant = getattr(update, "session_update", None)
            if variant == "agent_message_chunk":
                text = self._text_of(getattr(update, "content", None))
                if text:
                    message_id = getattr(update, "message_id", None)
                    if message_id != self.current_message_id and self.markdown_parts:
                        self.markdown_parts.append("\n\n")
                    self.current_message_id = message_id
                    self.markdown_parts.append(text)
            elif variant in ("tool_call", "tool_call_update"):
                self._record(
                    tool_call_id=getattr(update, "tool_call_id", None),
                    title=getattr(update, "title", None),
                    kind=getattr(update, "kind", None),
                    status=getattr(update, "status", None),
                    raw_input=getattr(update, "raw_input", None),
                )
            elif variant == "usage_update":
                self.usage = {
                    "used": getattr(update, "used", None),
                    "size": getattr(update, "size", None),
                    "cost": self._cost_of(getattr(update, "cost", None)),
                }
        except Exception:
            log.debug("session_update handling failed", exc_info=True)

    @staticmethod
    def _text_of(content: Any) -> str:
        if content is None:
            return ""
        text = getattr(content, "text", None)
        return text if isinstance(text, str) else ""

    @staticmethod
    def _cost_of(cost: Any) -> dict[str, Any] | None:
        if cost is None:
            return None
        amount = getattr(cost, "amount", None)
        currency = getattr(cost, "currency", None)
        if amount is None or currency is None:
            return None
        return {"amount": amount, "currency": currency}

    # -- permissions ------------------------------------------------------

    def _option_for(self, options: list[Any], wanted: str) -> str | None:
        """Pick the option id whose kind matches *wanted*.

        The agent chooses which options exist, so we choose among them by kind rather
        than by id or position.
        """
        for option in options or []:
            kind = getattr(option, "kind", None)
            if kind == wanted:
                return getattr(option, "option_id", None)
        return None

    def _respond(
        self,
        options: list[Any],
        decision: Decision,
        tool_call_id: str,
    ) -> RequestPermissionResponse:
        if decision.allowed:
            option_id = self._option_for(options, "allow_once") or self._option_for(
                options, "allow_always"
            )
            rejected = False
        else:
            option_id = self._option_for(options, "reject_once") or self._option_for(
                options, "reject_always"
            )
            rejected = True

        event = self.tool_events.get(tool_call_id)
        if event is not None:
            event.decision = "deny" if rejected else "allow"
            event.reason = decision.reason
        verdict = "denied" if rejected else "allowed"
        log.info("tool call %s %s: %s", tool_call_id, verdict, decision.reason)

        if option_id is None:
            # No option of the needed kind. Refusing the call cannot be expressed, so
            # cancel the turn instead of silently proceeding.
            log.warning("no %s option offered; cancelling turn", "reject" if rejected else "allow")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Deny anything that could mutate the checkout or run a non-git command."""
        try:
            tool_call_id = str(getattr(tool_call, "tool_call_id", "unknown"))

            # A permission request may omit kind and raw_input, so fall back to what the
            # preceding tool_call notification told us. Anything still unknown is denied,
            # which is the correct default but a poor experience, so the cache matters.
            cached = self.tool_events.get(tool_call_id)
            kind = getattr(tool_call, "kind", None) or (cached.kind if cached else None)
            raw_input = getattr(tool_call, "raw_input", None)
            if raw_input is None and cached is not None:
                raw_input = cached.raw_input

            decision = policy.decide(
                kind=kind,
                title=getattr(tool_call, "title", None),
                raw_input=raw_input,
                deny_unknown_commands=self.config.deny_unknown_commands,
            )
        except Exception as exc:
            log.exception("policy evaluation failed; denying by default")
            decision = Decision(Verdict.DENY, f"policy error: {exc}")

        return self._respond(options or [], decision, tool_call_id)

    # -- client filesystem ------------------------------------------------

    def _resolve_in_repo(self, path: str) -> Path:
        """Resolve *path* and confirm it is inside the repository.

        The agent may read repository files through us. Without this check, a prompt
        injection could ask it to read ``~/.ssh/id_rsa``.
        """
        candidate = Path(path)
        resolved = candidate if candidate.is_absolute() else self.config.repo / candidate
        resolved = resolved.resolve()
        root = self.config.repo.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(f"path is outside the repository: {path}")
        return resolved

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        resolved = self._resolve_in_repo(path)
        content = resolved.read_text(encoding="utf-8", errors="replace")
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = max((line or 1) - 1, 0)
            end = start + limit if limit is not None else len(lines)
            content = "".join(lines[start:end])
        encoded = content.encode("utf-8")
        if len(encoded) > self.config.max_read_bytes:
            content = encoded[: self.config.max_read_bytes].decode("utf-8", errors="ignore")
            content += "\n[code-review: read truncated]\n"
        return ReadTextFileResponse(content=content)

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> None:
        raise PermissionError("code-review runs agents read-only; writes are refused")

    # Terminals are never exposed. These raise rather than return an error response, so an
    # agent that ignores the advertised capability gets a loud failure instead of a silent
    # no-op. Signatures match acp.interfaces.Client exactly.

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise PermissionError(
            "code-review does not expose terminals; use the read-only git allowlist"
        )

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        raise PermissionError("code-review does not expose terminals")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse | None:
        raise PermissionError("code-review does not expose terminals")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise PermissionError("code-review does not expose terminals")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise PermissionError("code-review does not expose terminals")

    # -- protocol methods we deliberately do not support ------------------

    def on_connect(self, conn: Agent) -> None:
        # Declared by the ACP Client protocol and never invoked by the SDK. Defined as a
        # plain no-op because the protocol declares it synchronous.
        return None

    async def create_elicitation(
        self, message: str, mode: ElicitationMode, **kwargs: Any
    ) -> CreateElicitationResponse:
        raise PermissionError("code-review runs unattended and cannot ask the user a question")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise PermissionError(f"code-review does not implement the extension {method!r}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None
