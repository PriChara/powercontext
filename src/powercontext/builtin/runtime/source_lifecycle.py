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

"""Scoped Source-lifecycle dependency propagation for Memory and Topic Memory."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryEntryVersion
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Raised when a Source lifecycle registration, event, or resolution violates contracts."""


class SourceLifecycleEvent(BaseModel):
    """Validated Source availability transition event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=1)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return value


@dataclass(slots=True)
class _StoredNode:
    node_id: str
    node_kind: Literal["source", "memory", "topic"]
    target_id: str
    reference_json: str
    parents: tuple[str, ...]
    policy: str
    revision: int | None
    last_sequence: int
    last_event_json: str | None
    available: bool
    registration_order: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not node_id or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty string.")
    return node_id


def _validate_policy(policy: object) -> Literal["all", "any"]:
    if policy not in ("all", "any"):
        raise SourceLifecycleError("policy must be 'all' or 'any'.")
    return policy


def _validate_parents(
    node_id: str,
    parents: Sequence[str],
    existing_by_id: dict[str, _StoredNode],
) -> tuple[str, ...]:
    if isinstance(parents, (str, bytes)) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents must be a sequence of node_ids.")
    parent_tuple = tuple(parents)
    if not parent_tuple:
        raise SourceLifecycleError("Derived nodes require at least one parent.")
    seen: set[str] = set()
    for parent in parent_tuple:
        _validate_node_id(parent)
        if parent == node_id:
            raise SourceLifecycleError(f"Self-dependency is not allowed: {node_id}.")
        if parent in seen:
            raise SourceLifecycleError(f"Duplicate parent node_id: {parent}.")
        if parent not in existing_by_id:
            raise SourceLifecycleError(f"Parent node is not registered: {parent}.")
        seen.add(parent)
    return parent_tuple


def _has_cycle(
    node_id: str,
    parents: tuple[str, ...],
    existing_by_id: dict[str, _StoredNode],
) -> bool:
    stack = list(parents)
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        if current == node_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        node = existing_by_id.get(current)
        if node is not None:
            stack.extend(node.parents)
    return False


def _evaluate_policy(
    policy: str,
    parents: tuple[str, ...],
    available_by_id: dict[str, bool],
) -> bool:
    support = [available_by_id[parent] for parent in parents]
    return all(support) if policy == "all" else any(support)


class ScopedSourceLifecycleService:
    """Manage Source dependency DAG registration, propagation, and resolution in one scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    def _database(self) -> AsyncDatabase:
        database = getattr(self._runtime._provider, "database", None)
        if database is None:
            raise SourceLifecycleError("Lifecycle persistence requires a relational database provider.")
        return database

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        *,
        revision: int,
    ) -> None:
        """Register one captured Source root node in the scope's dependency graph."""

        validated_id = _validate_node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef.")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer.")

        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except Exception as error:
            raise SourceLifecycleError(f"Source reference is not readable: {source_ref}") from error

        reference_json = _canonical_json(source_ref.model_dump(mode="json"))
        target_id = f"{source_ref.source_type}:{source_ref.source_id}"
        parents_json = _canonical_json([])

        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._load_nodes(connection)
                existing_by_id = {node.node_id: node for node in nodes}
                existing = existing_by_id.get(validated_id)
                if existing is not None:
                    if (
                        existing.node_kind == "source"
                        and existing.reference_json == reference_json
                        and existing.parents == ()
                        and existing.policy == "source"
                        and existing.revision == revision
                    ):
                        return
                    raise SourceLifecycleError(f"Conflicting registration for node: {validated_id}")
                if any(node.node_kind == "source" and node.target_id == target_id for node in nodes):
                    raise SourceLifecycleError(f"Source reference is already bound in scope: {target_id}")

                next_order = 1 if not nodes else max(node.registration_order for node in nodes) + 1
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                        scope_id=self.scope_id,
                        node_id=validated_id,
                        node_kind="source",
                        target_id=target_id,
                        reference_json=reference_json,
                        parents_json=parents_json,
                        policy="source",
                        revision=revision,
                        last_sequence=0,
                        last_event_json=None,
                        available=True,
                        registration_order=next_order,
                    )
                )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"],
    ) -> None:
        """Register one Memory entry node and immediately align its state with its parents."""

        validated_id = _validate_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation.")
        validated_policy = _validate_policy(policy)

        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:
            raise SourceLifecycleError(f"Memory citation is not readable: {citation}") from error

        reference_json = _canonical_json(citation.model_dump(mode="json"))
        target_id = citation.entry_id

        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._load_nodes(connection)
                existing_by_id = {node.node_id: node for node in nodes}
                existing = existing_by_id.get(validated_id)
                if existing is not None:
                    if (
                        existing.node_kind == "memory"
                        and existing.reference_json == reference_json
                        and existing.parents == tuple(parents)
                        and existing.policy == validated_policy
                    ):
                        await self._recompute_and_persist(connection, nodes)
                    else:
                        raise SourceLifecycleError(f"Conflicting registration for node: {validated_id}")
                else:
                    validated_parents = _validate_parents(validated_id, parents, existing_by_id)
                    if _has_cycle(validated_id, validated_parents, existing_by_id):
                        raise SourceLifecycleError(f"Cycle detected when registering node: {validated_id}")
                    if any(node.node_kind == "memory" and node.target_id == target_id for node in nodes):
                        raise SourceLifecycleError(f"Memory entry is already bound in scope: {target_id}")

                    available_by_id = {node.node_id: node.available for node in nodes}
                    available = _evaluate_policy(validated_policy, validated_parents, available_by_id)
                    next_order = 1 if not nodes else max(node.registration_order for node in nodes) + 1
                    await connection.execute(
                        insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                            scope_id=self.scope_id,
                            node_id=validated_id,
                            node_kind="memory",
                            target_id=target_id,
                            reference_json=reference_json,
                            parents_json=_canonical_json(list(validated_parents)),
                            policy=validated_policy,
                            revision=None,
                            last_sequence=0,
                            last_event_json=None,
                            available=available,
                            registration_order=next_order,
                        )
                    )
            await self._sync_memory_states(reason="lifecycle registration")

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"],
    ) -> None:
        """Register one Topic Memory node and immediately align its state with its parents."""

        validated_id = _validate_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("artifact_ref must be a topic-memory ArtifactRef.")
        validated_policy = _validate_policy(policy)

        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                "topic-memory",
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except Exception as error:
            raise SourceLifecycleError(f"Topic Memory reference is not readable: {artifact_ref}") from error

        reference_json = _canonical_json(artifact_ref.model_dump(mode="json"))
        target_id = artifact_ref.artifact_id

        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._load_nodes(connection)
                existing_by_id = {node.node_id: node for node in nodes}
                existing = existing_by_id.get(validated_id)
                if existing is not None:
                    if (
                        existing.node_kind == "topic"
                        and existing.reference_json == reference_json
                        and existing.parents == tuple(parents)
                        and existing.policy == validated_policy
                    ):
                        await self._recompute_and_persist(connection, nodes)
                        return
                    raise SourceLifecycleError(f"Conflicting registration for node: {validated_id}")

                validated_parents = _validate_parents(validated_id, parents, existing_by_id)
                if _has_cycle(validated_id, validated_parents, existing_by_id):
                    raise SourceLifecycleError(f"Cycle detected when registering node: {validated_id}")
                if any(node.node_kind == "topic" and node.target_id == target_id for node in nodes):
                    raise SourceLifecycleError(f"Topic Memory is already bound in scope: {target_id}")

                available_by_id = {node.node_id: node.available for node in nodes}
                available = _evaluate_policy(validated_policy, validated_parents, available_by_id)
                next_order = 1 if not nodes else max(node.registration_order for node in nodes) + 1
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                        scope_id=self.scope_id,
                        node_id=validated_id,
                        node_kind="topic",
                        target_id=target_id,
                        reference_json=reference_json,
                        parents_json=_canonical_json(list(validated_parents)),
                        policy=validated_policy,
                        revision=None,
                        last_sequence=0,
                        last_event_json=None,
                        available=available,
                        registration_order=next_order,
                    )
                )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source lifecycle event, propagate availability, and return sorted active node IDs."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent.")

        event_json = _canonical_json(event.model_dump(mode="json"))

        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._load_nodes(connection)
                existing_by_id = {node.node_id: node for node in nodes}
                target = existing_by_id.get(event.node_id)
                if target is None or target.node_kind != "source":
                    raise SourceLifecycleError(f"Unknown or non-source lifecycle node: {event.node_id}")
                if target.revision != event.revision:
                    raise SourceLifecycleError(
                        f"Source revision mismatch for {event.node_id}: expected {target.revision}, got {event.revision}"
                    )
                if event.sequence < target.last_sequence:
                    raise SourceLifecycleError(
                        f"Outdated event sequence for {event.node_id}: {event.sequence} < {target.last_sequence}"
                    )
                if event.sequence == target.last_sequence:
                    if target.last_event_json != event_json:
                        raise SourceLifecycleError(
                            f"Conflicting event for sequence {event.sequence} on {event.node_id}"
                        )
                else:
                    target.available = event.available
                    target.last_sequence = event.sequence
                    target.last_event_json = event_json
                    await connection.execute(
                        update(SOURCE_LIFECYCLE_NODES_TABLE)
                        .where(
                            SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                            SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == target.node_id,
                        )
                        .values(
                            available=target.available,
                            last_sequence=target.last_sequence,
                            last_event_json=target.last_event_json,
                        )
                    )

                active_ids = await self._recompute_and_persist(connection, nodes)

            await self._sync_memory_states(reason=event.reason)
            return active_ids

    async def resolve(
        self,
        node_id: str,
        *,
        purpose: Literal["generation", "audit"],
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        """Resolve one registered node's original reference for generation or audit."""

        validated_id = _validate_node_id(node_id)
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError(f"Unsupported resolve purpose: {purpose}")

        async with self._runtime._scoped_operation(self.scope_id):
            async with self._database().transaction() as connection:
                row = (
                    await connection.execute(
                        select(
                            SOURCE_LIFECYCLE_NODES_TABLE.c.node_kind,
                            SOURCE_LIFECYCLE_NODES_TABLE.c.reference_json,
                            SOURCE_LIFECYCLE_NODES_TABLE.c.available,
                        ).where(
                            SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                            SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == validated_id,
                        )
                    )
                ).one_or_none()

        if row is None:
            raise SourceLifecycleError(f"Unknown lifecycle node: {validated_id}")

        node_kind = str(row.node_kind)
        available = bool(row.available)
        if purpose == "generation" and not available:
            raise SourceLifecycleError(f"Lifecycle node is unavailable for generation: {validated_id}")

        payload = json.loads(str(row.reference_json))
        if node_kind == "source":
            return SourceRef.model_validate(payload)
        if node_kind == "memory":
            return MemoryCitation.model_validate(payload)
        if node_kind == "topic":
            return ArtifactRef.model_validate(payload)
        raise SourceLifecycleError(f"Corrupted lifecycle node kind: {node_kind}")

    async def _load_nodes(self, connection: AsyncConnection) -> list[_StoredNode]:
        rows = (
            await connection.execute(
                select(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_kind,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.target_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.reference_json,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.parents_json,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.policy,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.revision,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.last_sequence,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.last_event_json,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.registration_order,
                )
                .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id)
                .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.registration_order.asc())
            )
        ).all()
        return [
            _StoredNode(
                node_id=str(row.node_id),
                node_kind=str(row.node_kind),  # type: ignore[arg-type]
                target_id=str(row.target_id),
                reference_json=str(row.reference_json),
                parents=tuple(str(item) for item in json.loads(str(row.parents_json))),
                policy=str(row.policy),
                revision=None if row.revision is None else int(row.revision),
                last_sequence=int(row.last_sequence),
                last_event_json=None if row.last_event_json is None else str(row.last_event_json),
                available=bool(row.available),
                registration_order=int(row.registration_order),
            )
            for row in rows
        ]

    async def _recompute_and_persist(
        self,
        connection: AsyncConnection,
        nodes: Sequence[_StoredNode],
    ) -> list[str]:
        available_by_id: dict[str, bool] = {}
        for node in nodes:
            if node.node_kind == "source":
                available_by_id[node.node_id] = node.available
                continue
            computed = _evaluate_policy(node.policy, node.parents, available_by_id)
            available_by_id[node.node_id] = computed
            if computed != node.available:
                node.available = computed
                await connection.execute(
                    update(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node.node_id,
                    )
                    .values(available=computed)
                )
        return sorted(node_id for node_id, is_available in available_by_id.items() if is_available)

    async def _sync_memory_states(self, *, reason: str) -> None:
        async with self._database().transaction() as connection:
            nodes = await self._load_nodes(connection)
        memory_nodes = [node for node in nodes if node.node_kind == "memory"]
        if not memory_nodes:
            return

        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            try:
                current = await service.head(context.artifacts.memory_artifact_id)
            except ArtifactNotFoundError:
                return

            manifest_by_entry = {item.entry_id: item for item in current.content.manifest.entries}
            entries_by_id = {entry.entry_id: entry for entry in await service.entries(current)}

            to_deactivate: list[MemoryEntryVersion] = []
            to_reactivate: list[MemoryEntryVersion] = []
            for node in memory_nodes:
                item = manifest_by_entry.get(node.target_id)
                entry_version = entries_by_id.get(node.target_id)
                if item is None or entry_version is None:
                    continue
                if not node.available and item.state == "active":
                    to_deactivate.append(entry_version)
                elif node.available and item.state == "inactive":
                    to_reactivate.append(entry_version)

            if to_deactivate:
                current = await service.forget(current, entries=tuple(to_deactivate), reason=reason)
            if to_reactivate:
                await service.reactivate(current, entries=tuple(to_reactivate), reason=reason)


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        return ScopedSourceLifecycleService(self._runtime, scope_id)


__all__ = [
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]
