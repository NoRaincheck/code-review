"""A fake ACP agent, so the ACP integration is testable with no vendor CLI and no network.

Implements just enough of the protocol to answer ``initialize``, ``session/new`` and
``session/prompt`` over stdio, and to optionally request permission and call tools. That
covers every path ``runner.py`` takes.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = 1


class FakeAgent:
    """Drives a scripted conversation with a code-review ACP client."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.prompts: list[str] = []
        self.cwd: str | None = None
        self._perm_counter = 0

    # -- protocol plumbing ------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def respond(self, request_id: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def notify(self, session_id: str, update: dict[str, Any]) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    # -- lifecycle --------------------------------------------------------

    def initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        self.respond(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False},
                "agentInfo": {"name": "fake-agent", "version": self.script.get("version", "9.9.9")},
            },
        )

    def new_session(self, request_id: Any, params: dict[str, Any]) -> None:
        self.cwd = params.get("cwd")
        failure = self.script.get("fail_session")
        if failure:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": failure
                        if isinstance(failure, str)
                        else "Authentication required",
                    },
                }
            )
            return
        self.respond(request_id, {"sessionId": "sess_fake_1"})

    # -- the review turn --------------------------------------------------

    def prompt(self, request_id: Any, params: dict[str, Any]) -> None:
        session_id = params["sessionId"]
        text = "".join(
            block.get("text", "")
            for block in params.get("prompt", [])
            if block.get("type") == "text"
        )
        self.prompts.append(text)

        if self.script.get("crash"):
            sys.exit(1)

        for tool_call in self.script.get("tools", []):
            if not self.run_tool(session_id, request_id, tool_call):
                # The client cancelled the turn outright; nothing more is sent.
                return

        for chunk in self.script.get("chunks", ["No issues found."]):
            self.notify(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk},
                },
            )

        if self.script.get("usage"):
            self.notify(
                session_id,
                {
                    "sessionUpdate": "usage_update",
                    "used": 1234,
                    "size": 200000,
                    "cost": {"amount": 0.02, "currency": "USD"},
                },
            )

        self.respond(
            request_id,
            {
                "stopReason": self.script.get("stop_reason", "end_turn"),
            },
        )

    def run_tool(self, session_id: str, request_id: Any, tool: dict[str, Any]) -> bool:
        """Emit one tool call. Returns False only if the client cancelled the turn.

        A rejection (``reject_once``) is an ordinary refusal of that one call: the agent
        notes the refusal and keeps going, which is what real agents do. Only a cancelled
        permission request ends the turn, because that is the client declining to choose
        any option at all.
        """
        call_id = tool.get("id", "call_1")
        self.notify(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": tool.get("title", "Run something"),
                "kind": tool.get("kind", "execute"),
                "status": "pending",
                "rawInput": tool.get("raw_input"),
            },
        )

        outcome = "allow"
        if tool.get("request_permission", True):
            outcome = self.request_permission(session_id, call_id, tool)
            if outcome == "cancel":
                self.respond(request_id, {"stopReason": "cancelled"})
                return False

        if outcome == "allow":
            self.notify(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": tool.get("result", "ok")},
                        }
                    ],
                },
            )
        else:
            # Refused. Mark the call failed and carry on without it.
            self.notify(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "failed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": "permission denied"},
                        }
                    ],
                },
            )
        return True

    def request_permission(self, session_id: str, call_id: str, tool: dict[str, Any]) -> str:
        kinds = tool.get("permission_options")
        if kinds is None:
            kinds = ["allow_once", "reject_once"]

        # A unique id per call: otherwise a response to an earlier permission request
        # would be read here, and the client would think its answer went to this one.
        request_id = f"perm_{call_id}_{self._perm_counter}"
        self._perm_counter += 1

        options = [{"optionId": kind, "name": kind, "kind": kind} for kind in kinds]
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": call_id,
                        "title": tool.get("title", "t"),
                        "rawInput": tool.get("raw_input"),
                    },
                    "options": options,
                },
            }
        )

        # Read replies until ours arrives, discarding interleaved session/update frames.
        while True:
            line = sys.stdin.readline()
            if not line:
                return "cancel"
            reply = json.loads(line)
            if reply.get("id") != request_id:
                continue
            outcome = reply.get("result", {}).get("outcome", {})
            if outcome.get("outcome") == "cancelled":
                return "cancel"
            chosen = outcome.get("optionId", "")
            return "allow" if chosen.startswith("allow") else "deny"

    # -- dispatch ---------------------------------------------------------

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            params = message.get("params", {})

            if method == "initialize":
                self.initialize(request_id, params)
            elif method == "session/new":
                self.new_session(request_id, params)
            elif method == "session/prompt":
                self.prompt(request_id, params)
            elif method == "session/cancel":
                self.respond(request_id, {}) if request_id else None
            elif request_id is not None:
                self.respond(request_id, {})


def main() -> None:
    script: dict[str, Any] = {}
    script_path = sys.argv[1] if len(sys.argv) > 1 else None
    if script_path:
        with open(script_path, encoding="utf-8") as handle:
            script = json.load(handle)
    else:
        # Default: echo a minimal review without needing a script file.
        script = {"chunks": ["No issues found."]}
    FakeAgent(script).run()


if __name__ == "__main__":
    main()
