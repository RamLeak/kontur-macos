"""Safe OpenCode CLI request/event contract for the native job owner.

OpenCode process creation, fixed argv/environment, and cancellation remain
native responsibilities.  The Python helper only validates a typed request and
parses the documented ``opencode run --format json`` event stream.  It never
executes a host command and never reads OpenCode's existing auth/config files.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from offline_ai import (
    ALLOWED_ROUTES,
    MAX_EVENTS,
    MAX_OUTPUT_BYTES,
    AIConfigError,
    AIProtocolError,
    AIToolEvent,
    AIUnavailable,
    ProviderConfig,
    TutorDomainPacket,
    validate_provider_config,
)

MAX_EVENT_LINE_BYTES = 256 * 1024
_FORBIDDEN_EVENT_WORDS = frozenset(
    {
        "tool",
        "tools",
        "shell",
        "bash",
        "exec",
        "command",
        "mcp",
        "web",
        "browser",
        "computer",
        "patch",
        "edit",
        "permission",
        "task",
        "lsp",
        "skill",
    }
)
def _forbidden_event_type(value: str) -> bool:
    parts = {part for part in re.split(r"[.:/_-]+", value.casefold()) if part}
    return bool(parts & _FORBIDDEN_EVENT_WORDS)


def _reject_tool_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_lc = str(key).casefold()
            if key_lc in {
                "tool",
                "tools",
                "tool_call",
                "tool_calls",
                "function_call",
                "function_calls",
                "command",
                "command_execution",
                "shell",
                "bash",
                "exec",
                "mcp",
                "web_search",
                "webfetch",
                "path",
                "file_path",
            }:
                raise AIToolEvent("OpenCode returned a tool event")
            _reject_tool_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_tool_fields(item)


@dataclass(frozen=True, slots=True)
class OpenCodeRequestSpec:
    """Fixed typed reverse-gateway input for the native OpenCode job.

    This deliberately contains no executable path, argv, environment, cwd, or
    shell string.  Native code owns those fixed safety settings.  The Python
    seam only carries the already validated tutor packet, selected provider
    endpoint/model, and an explicit Send-time API key.  Callers must never log
    the serialized object.
    """

    run_id: str
    rendered_prompt: str = field(repr=False)
    provider_model: str
    provider_id: str | None
    endpoint: str | None
    api_key: str | None = field(repr=False)
    effort: str
    tutor_mode: str
    manifest_sha256: str
    prompt_sha256: str
    input_digest: str

    def to_native_job(self) -> dict[str, Any]:
        """Serialize only the fields accepted by the root-owned native job."""

        return {
            "runId": self.run_id,
            "renderedPrompt": self.rendered_prompt,
            "providerModel": self.provider_model,
            "providerId": self.provider_id,
            "endpoint": self.endpoint,
            "apiKey": self.api_key,
            "effort": self.effort,
            "tutorMode": self.tutor_mode,
            "manifestSha256": self.manifest_sha256,
            "promptSha256": self.prompt_sha256,
            "inputDigest": self.input_digest,
            "textOnly": True,
        }


def build_opencode_request(
    packet: TutorDomainPacket,
    config: ProviderConfig,
    *,
    api_key: str | None = None,
) -> OpenCodeRequestSpec:
    """Build the narrow typed request consumed by the native OpenCode job.

    The native implementation maps this object to the documented
    ``opencode run --format json --model provider/model --variant effort``
    invocation and fixed deny-all environment.  Python never supplies process
    controls or an arbitrary executable.
    """

    normalized = validate_provider_config(config)
    if normalized.kind != "opencode_cli":
        raise AIConfigError("provider is not an OpenCode CLI configuration")
    if packet.effort not in {"low", "medium", "high", "xhigh"}:
        raise AIProtocolError("OpenCode effort is invalid")
    model = normalized.model if "/" in normalized.model else f"{normalized.opencode_provider}/{normalized.model}"
    if "/" not in model:
        raise AIConfigError("OpenCode model must use provider/model form")
    if "\x00" in model or any(ord(char) < 32 or ord(char) == 127 for char in model) or len(model.encode("utf-8")) > 512:
        raise AIConfigError("OpenCode model is invalid")
    if api_key is not None and (
        not isinstance(api_key, str)
        or not api_key
        or "\x00" in api_key
        or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
        or len(api_key.encode("utf-8")) > 4_096
    ):
        raise AIConfigError("OpenCode API key is invalid")
    if packet.effort not in {effort for _model, effort in ALLOWED_ROUTES}:
        raise AIProtocolError("OpenCode effort is not supported")
    return OpenCodeRequestSpec(
        run_id=packet.run_id,
        rendered_prompt=packet.rendered_prompt,
        provider_model=model,
        provider_id=normalized.opencode_provider or model.split("/", 1)[0],
        endpoint=normalized.endpoint,
        api_key=api_key,
        effort=packet.effort,
        tutor_mode=packet.tutor_mode,
        manifest_sha256=packet.manifest_sha256,
        prompt_sha256=packet.prompt_sha256,
        input_digest=packet.input_digest,
    )


@dataclass(frozen=True, slots=True)
class OpenCodeEvent:
    type: str
    text: str = field(default="", repr=False)
    done: bool = False


def _extract_text(value: Mapping[str, Any], event_type: str) -> tuple[str, bool]:
    _reject_tool_fields(value)
    if event_type in {"error", "failed", "failure"} or value.get("error") not in (None, False, ""):
        raise AIUnavailable("OpenCode returned an error event")
    done = value.get("done") is True or event_type in {
        "final",
        "complete",
        "completed",
        "response.completed",
        "session.text.ended",
    }
    parts: list[str] = []

    def add(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, list):
            for entry in item:
                if isinstance(entry, Mapping):
                    if entry.get("type") in {"text", "output_text"}:
                        add(entry.get("text"))
                    else:
                        add(entry.get("content"))

    add(value.get("text"))
    add(value.get("content"))
    add(value.get("delta"))
    message = value.get("message")
    if isinstance(message, Mapping):
        add(message.get("content"))
    item = value.get("item")
    if isinstance(item, Mapping):
        add(item.get("text"))
        add(item.get("content"))
    properties = value.get("properties")
    if isinstance(properties, Mapping):
        add(properties.get("text"))
        add(properties.get("content"))
        add(properties.get("delta"))
        part = properties.get("part")
        if isinstance(part, Mapping):
            add(part.get("text"))
            add(part.get("content"))
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            add(choice.get("text"))
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                add(delta.get("content"))
            message = choice.get("message")
            if isinstance(message, Mapping):
                add(message.get("content"))
    return "".join(parts), done


def parse_opencode_event(line: bytes | str) -> OpenCodeEvent:
    if isinstance(line, bytes):
        if len(line) > MAX_EVENT_LINE_BYTES:
            raise AIProtocolError("OpenCode event exceeds the byte limit")
        try:
            text_line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AIProtocolError("OpenCode event is not UTF-8") from exc
    elif isinstance(line, str):
        if len(line.encode("utf-8")) > MAX_EVENT_LINE_BYTES:
            raise AIProtocolError("OpenCode event exceeds the byte limit")
        text_line = line
    else:
        raise AIProtocolError("OpenCode event is invalid")
    try:
        value = json.loads(text_line)
    except (ValueError, RecursionError) as exc:
        raise AIProtocolError("OpenCode event is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise AIProtocolError("OpenCode event must be an object")
    raw_type = value.get("type") or value.get("event") or "metadata"
    if not isinstance(raw_type, str) or len(raw_type) > 128:
        raise AIProtocolError("OpenCode event type is invalid")
    event_type = raw_type.casefold()
    if _forbidden_event_type(event_type):
        raise AIToolEvent("OpenCode returned a forbidden event")
    text, done = _extract_text(value, event_type)
    return OpenCodeEvent(event_type, text, done)


@dataclass(slots=True)
class OpenCodeEventDecoder:
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_events: int = MAX_EVENTS
    _events: int = field(default=0, init=False)
    _text: list[str] = field(default_factory=list, init=False)
    _bytes: int = field(default=0, init=False)
    _done: bool = field(default=False, init=False)

    def feed(self, line: bytes | str) -> OpenCodeEvent:
        if self._events >= self.max_events:
            raise AIProtocolError("OpenCode event count exceeds the limit")
        event = parse_opencode_event(line)
        self._events += 1
        if event.text:
            self._text.append(event.text)
            self._bytes += len(event.text.encode("utf-8"))
            if self._bytes > self.max_output_bytes:
                raise AIProtocolError("OpenCode output exceeds the byte limit")
        self._done = self._done or event.done
        return event

    def finish(self, exit_code: int = 0) -> str:
        if exit_code != 0:
            raise AIUnavailable("OpenCode request failed")
        result = "".join(self._text)
        if not result:
            raise AIProtocolError("OpenCode returned no text")
        return result


def decode_opencode_events(lines: Iterable[bytes | str], *, exit_code: int = 0) -> str:
    decoder = OpenCodeEventDecoder()
    for line in lines:
        decoder.feed(line)
    return decoder.finish(exit_code)


__all__ = [
    "MAX_EVENT_LINE_BYTES",
    "OpenCodeEvent",
    "OpenCodeEventDecoder",
    "OpenCodeRequestSpec",
    "build_opencode_request",
    "decode_opencode_events",
    "parse_opencode_event",
]
