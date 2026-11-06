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

"""Incremental ContextPack wire format, deterministic chunking, and delta reconstruction."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_JSON_HEADER_MARKER = 'BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{"trust":"untrusted_history","items":['
_TEXT_HEADER_MARKER = "BEGIN_POWERCONTEXT_PREPARED_TEXT_V1\n\n"


class ContextPackError(ValueError):
    """Raised when a ContextPack, ContextPackDelta, or chunk boundary violates the contract."""


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_b64(data: bytes) -> str:
    if type(data) is not bytes:
        raise ContextPackError("chunk item must be bytes")
    return base64.b64encode(data).decode("ascii")


def _decode_b64(encoded: object) -> bytes:
    if type(encoded) is not str:
        raise ContextPackError("wire chunk item must be a Base64 string")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise ContextPackError("invalid Base64 chunk encoding") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ContextPackError("non-canonical Base64 chunk encoding")
    return raw


def _validate_digest_format(digest: object, field_name: str) -> str:
    if type(digest) is not str or not _DIGEST_RE.match(digest):
        raise ContextPackError(f"{field_name} must be a canonical sha256:<hex> digest")
    return digest


def _validate_items_tuple(items: object) -> tuple[bytes, ...]:
    if not isinstance(items, (tuple, list)):
        raise ContextPackError("items must be a sequence of bytes")
    result: list[bytes] = []
    for item in items:
        if type(item) is not bytes:
            raise ContextPackError("each item must be bytes")
        result.append(item)
    return tuple(result)


def _compute_pack_digest_and_bytes(items: tuple[bytes, ...]) -> tuple[str, int, bytes]:
    encoded_items = [_encode_b64(item) for item in items]
    unsigned: dict[str, object] = {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": encoded_items,
    }
    canonical_unsigned = _canonical_json_bytes(unsigned)
    digest = "sha256:" + hashlib.sha256(canonical_unsigned).hexdigest()

    byte_count = 0
    for _ in range(4):
        payload: dict[str, object] = {
            "schema": CONTEXT_PACK_SCHEMA,
            "items": encoded_items,
            "digest": digest,
            "byte_count": byte_count,
        }
        wire = _canonical_json_bytes(payload)
        if len(wire) == byte_count:
            return digest, byte_count, wire
        byte_count = len(wire)

    payload = {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": encoded_items,
        "digest": digest,
        "byte_count": byte_count,
    }
    wire = _canonical_json_bytes(payload)
    return digest, len(wire), wire


def _chunk_segment_utf8(segment: str, chunk_bytes: int) -> list[bytes]:
    if not segment:
        return []
    chunks: list[bytes] = []
    current_chars: list[str] = []
    current_len = 0
    for ch in segment:
        ch_bytes = ch.encode("utf-8")
        ch_len = len(ch_bytes)
        if ch_len > chunk_bytes:
            raise ContextPackError(
                f"UTF-8 character of {ch_len} bytes cannot fit in chunk_bytes={chunk_bytes}"
            )
        if current_len + ch_len > chunk_bytes:
            chunks.append("".join(current_chars).encode("utf-8"))
            current_chars = [ch]
            current_len = ch_len
        else:
            current_chars.append(ch)
            current_len += ch_len
    if current_chars:
        chunks.append("".join(current_chars).encode("utf-8"))
    return chunks


def _split_structural_segments(content: str) -> list[str]:
    if _JSON_HEADER_MARKER in content:
        idx = content.index(_JSON_HEADER_MARKER) + len(_JSON_HEADER_MARKER)
        header = content[:idx]
        rest = content[idx:]
        segments = [header]
        parts = rest.split("},{")
        if len(parts) == 1:
            segments.append(rest)
        else:
            for i, part in enumerate(parts):
                if i == 0:
                    segments.append(part + "}")
                elif i < len(parts) - 1:
                    segments.append(",{" + part + "}")
                else:
                    segments.append(",{" + part)
        return [s for s in segments if s]

    if _TEXT_HEADER_MARKER in content:
        idx = content.index(_TEXT_HEADER_MARKER) + len(_TEXT_HEADER_MARKER)
        header = content[:idx]
        rest = content[idx:]
        segments = [header]
        parts = rest.split("\n\n### ")
        for i, part in enumerate(parts):
            segments.append(part if i == 0 else "\n\n### " + part)
        return [s for s in segments if s]

    return [content]


@dataclass(frozen=True)
class ContextPack:
    """Immutable chunked context payload with canonical digest and wire byte count."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        validated_items = _validate_items_tuple(self.items)
        object.__setattr__(self, "items", validated_items)
        _validate_digest_format(self.digest, "digest")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ContextPackError("byte_count must be a non-negative integer")
        expected_digest, expected_bytes, _ = _compute_pack_digest_and_bytes(validated_items)
        if self.digest != expected_digest:
            raise ContextPackError("ContextPack digest does not match canonical items")
        if self.byte_count != expected_bytes:
            raise ContextPackError("ContextPack byte_count does not match serialized wire length")

    @classmethod
    def build(cls, items: Sequence[bytes]) -> ContextPack:
        validated = _validate_items_tuple(items)
        digest, byte_count, _ = _compute_pack_digest_and_bytes(validated)
        return cls(items=validated, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if content is None or content == "":
            return cls.build(())
        if type(content) is not str:
            raise ContextPackError("content must be a string or None")

        chunks: list[bytes] = []
        for segment in _split_structural_segments(content):
            chunks.extend(_chunk_segment_utf8(segment, chunk_bytes))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        _, _, wire = _compute_pack_digest_and_bytes(self.items)
        return wire

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        if type(raw) is not bytes:
            raise ContextPackError("raw wire data must be bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ContextPackError("invalid UTF-8 or JSON in ContextPack wire bytes") from exc

        if not isinstance(payload, dict) or set(payload.keys()) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("ContextPack JSON must contain exact keys: schema, items, digest, byte_count")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("unsupported ContextPack schema")
        raw_items = payload["items"]
        if not isinstance(raw_items, list):
            raise ContextPackError("ContextPack items must be a list")
        decoded_items = tuple(_decode_b64(item) for item in raw_items)
        digest = _validate_digest_format(payload["digest"], "digest")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("ContextPack byte_count must be a non-negative integer")

        pack = cls(items=decoded_items, digest=digest, byte_count=byte_count)
        if len(raw) != pack.byte_count:
            raise ContextPackError("ContextPack raw byte length does not match byte_count")
        return pack


@dataclass(frozen=True)
class ContextPackDelta:
    """Prefix-reuse delta transforming a base ContextPack into a target ContextPack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest_format(self.base_digest, "base_digest")
        _validate_digest_format(self.target_digest, "target_digest")
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        validated_appended = _validate_items_tuple(self.appended)
        object.__setattr__(self, "appended", validated_appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("base and target must be ContextPack instances")
        prefix_len = 0
        max_prefix = min(len(base.items), len(target.items))
        while prefix_len < max_prefix and base.items[prefix_len] == target.items[prefix_len]:
            prefix_len += 1
        appended = target.items[prefix_len:]
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix_len,
            appended=appended,
        )

    def to_bytes(self) -> bytes:
        payload: dict[str, object] = {
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [_encode_b64(item) for item in self.appended],
        }
        return _canonical_json_bytes(payload)

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        if type(raw) is not bytes:
            raise ContextPackError("raw wire data must be bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ContextPackError("invalid UTF-8 or JSON in ContextPackDelta wire bytes") from exc

        expected_keys = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if not isinstance(payload, dict) or set(payload.keys()) != expected_keys:
            raise ContextPackError("ContextPackDelta JSON must contain exact required keys")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("unsupported ContextPackDelta schema")
        base_digest = _validate_digest_format(payload["base_digest"], "base_digest")
        target_digest = _validate_digest_format(payload["target_digest"], "target_digest")
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        raw_appended = payload["appended"]
        if not isinstance(raw_appended, list):
            raise ContextPackError("ContextPackDelta appended must be a list")
        appended = tuple(_decode_b64(item) for item in raw_appended)
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate and apply a ContextPackDelta to a base ContextPack."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("base must be a ContextPack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta must be a ContextPackDelta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base_digest does not match base ContextPack digest")
    if type(delta.prefix_items) is not int or delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix_items is out of bounds for base ContextPack")

    target_items = base.items[: delta.prefix_items] + delta.appended
    rebuilt = ContextPack.build(target_items)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("reconstructed ContextPack digest does not match delta target_digest")
    return rebuilt


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
