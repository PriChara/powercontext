# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pi v3 session branch event capture, text-evidence projection, and incremental connector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import rfc8785
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.errors import SourceConflictError
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"


def _require_non_blank_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty, non-blank string")
    return value


def _pi_event_source_id(session_id: str, event_id: str) -> str:
    _require_non_blank_str(session_id, "session_id")
    _require_non_blank_str(event_id, "event_id")
    encoded = f"pi-event:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(encoded) <= MAX_SOURCE_ID_LENGTH:
        return encoded
    digest = hashlib.sha256(rfc8785.dumps([session_id, event_id])).hexdigest()
    return f"pi-event:sha256:{digest}"


def _record_fingerprint(record: dict[str, JsonValue]) -> str:
    return hashlib.sha256(rfc8785.dumps(record)).hexdigest()


class PiSessionInput(BaseModel):
    """Raw Pi v3 session JSONL payload and optional target leaf node ID."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event along a selected Pi session branch."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_event_input(self) -> PiEventInput:
        _require_non_blank_str(self.session_id, "session_id")
        _require_non_blank_str(self.event_id, "event_id")
        if self.parent_id is not None:
            _require_non_blank_str(self.parent_id, "parent_id")
        _require_non_blank_str(self.event_type, "event_type")
        _require_non_blank_str(self.role, "role")
        _require_non_blank_str(self.content, "content")
        if not isinstance(self.record, dict):
            raise ValueError("record must be a JSON object")
        return self


class PiEventSource(Source):
    """Captured Source representing one immutable Pi session event."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_event_source(self) -> PiEventSource:
        expected_name = _pi_event_source_id(self.session_id, self.event_id)
        if self.name != expected_name:
            raise ValueError("PiEventSource name must match canonical session/event identity")
        if self.materialization is not SourceMaterialization.CAPTURED:
            raise ValueError("PiEventSource materialization must be CAPTURED")
        _require_non_blank_str(self.event_type, "event_type")
        _require_non_blank_str(self.role, "role")
        _require_non_blank_str(self.content, "content")
        return self


def _extract_content_blocks(raw_content: object, *, allow_tool_calls: bool) -> str:
    if isinstance(raw_content, str):
        return raw_content
    if not isinstance(raw_content, list):
        raise ValueError("content must be a string or list of content blocks")
    parts: list[str] = []
    for block in raw_content:
        if not isinstance(block, dict):
            raise ValueError("content block must be an object")
        block_type = block.get("type")
        if not isinstance(block_type, str) or not block_type:
            raise ValueError("content block must declare a string type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("text content block must contain a string text field")
            if text:
                parts.append(text)
        elif block_type in {"toolCall", "tool_use"} and allow_tool_calls:
            name = _require_non_blank_str(block.get("name"), "toolCall.name")
            call_id = _require_non_blank_str(block.get("id"), "toolCall.id")
            arguments = block.get("arguments", block.get("input", {}))
            compact_args = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            parts.append(f"[tool_call {name} {call_id}] {compact_args}")
        elif block_type in {"thinking", "redacted_thinking", "image"}:
            continue
        else:
            raise ValueError(f"unsupported content block type: {block_type!r}")
    return "\n".join(parts)


def _format_visible_event(
    session_id: str,
    record: dict[str, JsonValue],
) -> PiEventInput | None:
    event_id = _require_non_blank_str(record.get("id"), "record.id")
    parent_raw = record.get("parentId")
    parent_id: str | None
    if parent_raw is None:
        parent_id = None
    else:
        parent_id = _require_non_blank_str(parent_raw, "record.parentId")
    event_type = _require_non_blank_str(record.get("type"), "record.type")

    role: str
    body: str

    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("message record must contain a message object")
        role = _require_non_blank_str(message.get("role"), "message.role")
        if role == "bashExecution":
            command = message.get("command")
            output = message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("bashExecution message requires string command and output")
            if not command.strip() and not output.strip():
                return None
            body = f"$ {command}\n{output}" if output else f"$ {command}"
        elif role in {"toolResult", "tool_result"}:
            role = "toolResult"
            tool_name_raw = message.get("toolName")
            tool_name = tool_name_raw if isinstance(tool_name_raw, str) and tool_name_raw.strip() else "unknown"
            tool_call_id = _require_non_blank_str(
                message.get("toolCallId", message.get("tool_use_id")),
                "toolResult.toolCallId",
            )
            text = _extract_content_blocks(message.get("content", ""), allow_tool_calls=False)
            if not text or not text.strip():
                return None
            body = f"[tool_result {tool_name} {tool_call_id}] {text}"
        else:
            body = _extract_content_blocks(message.get("content", ""), allow_tool_calls=True)
            if not body or not body.strip():
                return None
    elif event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            raise ValueError(f"{event_type} record must contain a string summary")
        if not summary.strip():
            return None
        role = event_type
        body = summary
    elif event_type == "custom_message":
        raw_content = record.get("content")
        if raw_content is None and isinstance(record.get("message"), dict):
            raw_content = record["message"].get("content")
        if raw_content is None:
            raise ValueError("custom_message record must contain content")
        body = _extract_content_blocks(raw_content, allow_tool_calls=False)
        if not body or not body.strip():
            return None
        role = "custom"
    else:
        return None

    content = f"{role}: {body}"
    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=event_type,
        role=role,
        content=content,
        record=record,
    )


def _parse_pi_archive(
    value: PiSessionInput,
) -> tuple[str, dict[str, dict[str, JsonValue]], tuple[PiEventInput, ...]]:
    if not isinstance(value, PiSessionInput):
        raise ValueError("value must be a PiSessionInput")
    if not isinstance(value.content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw_bytes = bytes(value.content)
    if not raw_bytes:
        raise ValueError("Pi session content must not be empty")
    try:
        text = raw_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Pi session content must not be empty")

    records: list[dict[str, JsonValue]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSON line in Pi session: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("each Pi session line must be a JSON object")
        records.append(parsed)

    header = records[0]
    if header.get("type") != "session" or type(header.get("version")) is not int or header.get("version") != 3:
        raise ValueError("first record must be a Pi v3 session header")
    session_id = _require_non_blank_str(header.get("id"), "session.id")

    nodes_by_id: dict[str, dict[str, JsonValue]] = {}
    formatted_by_id: dict[str, PiEventInput | None] = {}
    last_node_id: str | None = None

    for record in records[1:]:
        node_id = _require_non_blank_str(record.get("id"), "record.id")
        if node_id in nodes_by_id:
            raise ValueError(f"duplicate Pi session node id: {node_id!r}")
        if "parentId" not in record:
            raise ValueError(f"record {node_id!r} is missing parentId")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id.strip():
                raise ValueError(f"record {node_id!r} has invalid parentId")
            if parent_id not in nodes_by_id:
                raise ValueError(f"record {node_id!r} references missing or later parentId {parent_id!r}")
        _require_non_blank_str(record.get("type"), "record.type")
        formatted_by_id[node_id] = _format_visible_event(session_id, record)
        nodes_by_id[node_id] = record
        last_node_id = node_id

    if value.leaf_id is not None:
        leaf_id = _require_non_blank_str(value.leaf_id, "leaf_id")
        if leaf_id not in nodes_by_id:
            raise ValueError(f"leaf_id {leaf_id!r} not found in Pi session")
        target_leaf: str | None = leaf_id
    else:
        target_leaf = last_node_id

    if target_leaf is None:
        return session_id, nodes_by_id, ()

    chain_ids: list[str] = []
    visited: set[str] = set()
    cursor: str | None = target_leaf
    while cursor is not None:
        if cursor in visited:
            raise ValueError("cycle detected in Pi session graph")
        visited.add(cursor)
        chain_ids.append(cursor)
        parent = nodes_by_id[cursor].get("parentId")
        cursor = parent if isinstance(parent, str) else None

    chain_ids.reverse()
    events = tuple(
        formatted_by_id[node_id]
        for node_id in chain_ids
        if formatted_by_id[node_id] is not None
    )
    return session_id, nodes_by_id, events  # type: ignore[return-value]


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse a Pi v3 session archive and return visible events from root to selected leaf."""
    _, _, events = _parse_pi_archive(value)
    return events


class _PiEventSourceAdapter:
    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not isinstance(value, PiEventInput):
            raise ValueError("value must be a PiEventInput")
        source_name = _pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_name,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        if not isinstance(source, PiEventSource):
            raise TypeError("source must be a PiEventSource")
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class _PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventSourceAdapter(),
    version="1",
    projections=(_PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Incrementally capture visible Pi session branch events with mutation detection."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        try:
            raw_bytes = self._path.read_bytes()
        except OSError as error:
            raise ValueError(f"unable to read Pi session file: {error}") from error

        session_id, nodes_by_id, branch_events = _parse_pi_archive(
            PiSessionInput(content=raw_bytes, leaf_id=self._leaf_id)
        )

        previous_checkpoint = session.checkpoint
        imported_hashes: dict[str, str] = {}
        if previous_checkpoint is not None:
            if not isinstance(previous_checkpoint, dict):
                raise ValueError("corrupted Pi session checkpoint")
            prev_session_id = previous_checkpoint.get("session_id")
            if prev_session_id is not None and prev_session_id != session_id:
                raise ValueError("Pi session_id changed across incremental runs")
            raw_events = previous_checkpoint.get("events", {})
            if not isinstance(raw_events, dict):
                raise ValueError("corrupted Pi session checkpoint events")
            for event_id, expected_hash in raw_events.items():
                if not isinstance(event_id, str) or not isinstance(expected_hash, str):
                    raise ValueError("corrupted Pi session checkpoint entry")
                if event_id not in nodes_by_id:
                    raise ValueError(f"previously imported Pi event {event_id!r} is missing from session file")
                actual_hash = _record_fingerprint(nodes_by_id[event_id])
                if actual_hash != expected_hash:
                    raise ValueError(f"previously imported Pi event {event_id!r} was mutated")
                imported_hashes[event_id] = expected_hash

        next_hashes = dict(imported_hashes)
        for event in branch_events:
            event_hash = _record_fingerprint(event.record)
            if event.event_id in imported_hashes:
                if imported_hashes[event.event_id] != event_hash:
                    raise ValueError(f"previously imported Pi event {event.event_id!r} was mutated")
                continue
            try:
                await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            except SourceConflictError as error:
                raise ValueError(f"conflicting Pi event {event.event_id!r}") from error
            next_hashes[event.event_id] = event_hash

        if previous_checkpoint is not None and next_hashes == imported_hashes:
            next_checkpoint: JsonValue = previous_checkpoint
        else:
            next_checkpoint = {
                "session_id": session_id,
                "events": next_hashes,
            }

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=next_checkpoint,
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
