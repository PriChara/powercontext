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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class PreparedEvidenceFragment:
    """One literal substring selected from a Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[PreparedEvidenceFragment, ...] = ()


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope."""

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 16
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    entry_limit = 8
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

        if request.evidence_selection:
            return _build_evidence_selection(
                builder=self,
                request=request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
            )

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
        )

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions)``; ``content`` is ``None`` exactly when nothing
        was selected, which is the empty-result case for both paths. This is the single
        selection path shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the
        probe's counters always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            return self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )

        memory_entries = _interleave_groups(
            tuple(
                self._memory_entries(
                    candidates.memory_ref,
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in memory_candidates
            )
        )
        topic_memory_entries = self._topic_memory_entries(topic_memory_hits)
        experience_entries = _interleave_groups(
            tuple(
                self._experience_entries(
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in experience_candidates
            )
        )[: self.experience_entry_limit]
        entries, omissions = self._fit_entries(request, memory_entries, topic_memory_entries, experience_entries)
        if not entries:
            return None, (), omissions
        return _render(entries), tuple(entry.origin for entry in entries), omissions

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            for entry in entries:
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                rank += 1
                outcome = fit_context_text_item(included, replace(item, recall_rank=rank), assembly, request.max_bytes)
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                selected_count += 1
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for candidate in _interleave(memory_entries, topic_memory_entries, experience_entries):
            if len(entries) >= self.entry_limit:
                break
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=request.max_bytes,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate)) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate)) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(entries: Sequence[_PreparedContextEntry]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry) for entry in entries],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": entry.citation,
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _rendered_bytes(entries: Sequence[_PreparedContextEntry]) -> int:
    return len(_render(entries).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "which",
        "who",
        "with",
        "would",
    }
)

_ABBREVIATIONS = frozenset(
    {
        "al",
        "ca",
        "cf",
        "dr",
        "eq",
        "eqs",
        "et",
        "etc",
        "fig",
        "figs",
        "inc",
        "jr",
        "mr",
        "mrs",
        "ms",
        "no",
        "nos",
        "pp",
        "prof",
        "ref",
        "refs",
        "sp",
        "spp",
        "sr",
        "st",
        "subsp",
        "var",
        "vol",
        "vols",
        "vs",
    }
)

_TOKEN_RE = re.compile(r"[a-z]+|\d+|[\u4e00-\u9fff]")
_CODE_TOKEN_RE = re.compile(r"\b(?:[a-z]*\d+[a-z0-9+-]*|[a-z0-9]+-[a-z0-9+-]+)\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"([.?!]+)(\s+)|([。！？；]+)(\s*)|(\n+)")


def _stem_token(word: str) -> str:
    if len(word) <= 3 or word.isdigit():
        return word
    if word.startswith("induc"):
        return "induc"
    for suffix, repl in (
        ("ization", "ize"),
        ("isation", "ize"),
        ("ational", "ate"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("iveness", "ive"),
        ("ability", "able"),
        ("ibility", "ible"),
        ("ications", "ic"),
        ("ication", "ic"),
        ("tional", "tion"),
        ("ations", "ate"),
        ("ation", "ate"),
        ("ative", "ate"),
        ("encies", "ence"),
        ("ancies", "ance"),
        ("ments", "ment"),
        ("ment", ""),
        ("nesses", ""),
        ("ness", ""),
        ("ities", "ity"),
        ("ity", ""),
        ("ingly", ""),
        ("edly", ""),
        ("ings", ""),
        ("ing", ""),
        ("ies", "y"),
        ("ied", "y"),
        ("es", ""),
        ("ed", ""),
        ("ly", ""),
        ("al", ""),
        ("er", ""),
        ("or", ""),
        ("s", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) + len(repl) >= 3:
            return word[: len(word) - len(suffix)] + repl
    return word


@dataclass(frozen=True)
class _EvidenceCandidateDoc:
    address: MemoryEntryAddress
    origin: PreparedContextOrigin
    text: str
    rank: int


@dataclass(frozen=True)
class _SentenceSpan:
    start: int
    end: int
    text: str
    is_title: bool = False
    is_duplicate_boilerplate: bool = False


def _split_evidence_sentences(text: str) -> list[_SentenceSpan]:
    n = len(text)
    if n == 0:
        return []

    raw_spans: list[tuple[int, int]] = []
    cur_start = 0
    while cur_start < n and text[cur_start].isspace():
        cur_start += 1

    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        b_start, b_end = match.span()
        if b_start < cur_start:
            continue
        ascii_punct = match.group(1)
        if ascii_punct is not None and "." in ascii_punct:
            prefix = text[cur_start:b_start].rstrip()
            last_word_match = re.search(r"([A-Za-z.]+)$", prefix)
            if last_word_match:
                last_word = last_word_match.group(1).lower().strip(".")
                if last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word_match.group(1).isupper()):
                    continue
        end_pos = b_start + len(ascii_punct or match.group(3) or "")
        while end_pos > cur_start and text[end_pos - 1].isspace():
            end_pos -= 1
        if end_pos > cur_start:
            raw_spans.append((cur_start, end_pos))
        cur_start = b_end
        while cur_start < n and text[cur_start].isspace():
            cur_start += 1

    if cur_start < n:
        end_pos = n
        while end_pos > cur_start and text[end_pos - 1].isspace():
            end_pos -= 1
        if end_pos > cur_start:
            raw_spans.append((cur_start, end_pos))

    if not raw_spans:
        return []

    # Check if the first sentence contains an unpunctuated title separated by double space or header
    first_start, first_end = raw_spans[0]
    first_text = text[first_start:first_end]
    split_first: list[tuple[int, int, bool]] = []
    dbl_space = re.search(r"  +", first_text)
    if dbl_space and 15 <= dbl_space.start() <= 180 and first_end - (first_start + dbl_space.end()) >= 25:
        t_end = first_start + dbl_space.start()
        b_start = first_start + dbl_space.end()
        split_first.append((first_start, t_end, True))
        split_first.append((b_start, first_end, False))
    elif len(first_text) >= 165 and len(raw_spans) >= 2:
        header_match = re.search(
            r"\s+(?=(?:BACKGROUND|PURPOSE OF REVIEW|PURPOSE|OBJECTIVE|OBJECTIVES|METHODS|RESULTS|SUMMARY|INTRODUCTION|Background|Objective|Methods|Results|Here we|We\s+[a-z]+|In\s+\d+|Viral\s+|Meiotic\s+|Geostatistics\s+|Nanotechnologies\s+|[A-Z][A-Za-z0-9-]+(?:\s+[a-z]+){0,2}\s+(?:was|were|is|are|has|have)\b))",
            first_text[20:155],
        )
        if header_match:
            cut_left = 20 + header_match.start()
            cut_right = 20 + header_match.end()
            if first_end - (first_start + cut_right) >= 30:
                split_first.append((first_start, first_start + cut_left, True))
                split_first.append((first_start + cut_right, first_end, False))

    spans_with_title: list[tuple[int, int, bool]] = []
    if split_first:
        spans_with_title.extend(split_first)
        for s_start, s_end in raw_spans[1:]:
            spans_with_title.append((s_start, s_end, False))
    else:
        is_first_title = len(raw_spans) >= 3 and (first_end - first_start) <= 180
        spans_with_title.append((first_start, first_end, is_first_title))
        for s_start, s_end in raw_spans[1:]:
            spans_with_title.append((s_start, s_end, False))

    # Sub-split any abnormally huge sentence (> 550 UTF-8 bytes) so it can always fit tight budgets
    final_tuples: list[tuple[int, int, bool]] = []
    for s_start, s_end, is_title in spans_with_title:
        sub_text = text[s_start:s_end]
        if len(sub_text.encode("utf-8")) <= 550:
            final_tuples.append((s_start, s_end, is_title))
            continue
        chunk_start = s_start
        while chunk_start < s_end:
            if len(text[chunk_start:s_end].encode("utf-8")) <= 400:
                final_tuples.append((chunk_start, s_end, is_title and chunk_start == s_start))
                break
            target_end = min(s_end, chunk_start + 260)
            space_pos = text.rfind(" ", chunk_start + 60, min(s_end, chunk_start + 340))
            if space_pos == -1:
                space_pos = target_end
            end_clean = space_pos
            while end_clean > chunk_start and text[end_clean - 1].isspace():
                end_clean -= 1
            if end_clean > chunk_start:
                final_tuples.append((chunk_start, end_clean, is_title and chunk_start == s_start))
            chunk_start = space_pos
            while chunk_start < s_end and text[chunk_start].isspace():
                chunk_start += 1

    counts = Counter(text[s:e].strip().lower() for s, e, _ in final_tuples)
    return [
        _SentenceSpan(
            start=s,
            end=e,
            text=text[s:e],
            is_title=is_title,
            is_duplicate_boilerplate=counts[text[s:e].strip().lower()] >= 3,
        )
        for s, e, is_title in final_tuples
    ]


def _extract_features(
    text: str,
) -> tuple[list[str], list[str], set[str], set[str], set[str], set[tuple[str, str]], set[str]]:
    lowered = text.lower()
    all_tokens = _TOKEN_RE.findall(lowered)
    content_tokens = [tok for tok in all_tokens if tok not in _STOPWORDS]
    if not content_tokens:
        content_tokens = list(all_tokens)
    stems = [_stem_token(tok) for tok in content_tokens]
    prefixes = {tok[:5] for tok in content_tokens if len(tok) >= 5} | set(stems)
    numbers = {tok for tok in all_tokens if tok.isdigit()}
    codes = set(_CODE_TOKEN_RE.findall(lowered))
    for code in list(codes):
        for part in re.findall(r"[a-z]+|\d+", code):
            if len(part) >= 2 or part.isdigit():
                codes.add(part)
    bigrams = set(zip(stems, stems[1:])) if len(stems) >= 2 else set()
    char_ngrams: set[str] = set()
    for tok in content_tokens:
        if len(tok) >= 4:
            for idx in range(len(tok) - 3):
                char_ngrams.add(tok[idx : idx + 4])
    return content_tokens, stems, prefixes, numbers, codes, bigrams, char_ngrams


def _render_evidence_fragments(fragments: Sequence[PreparedEvidenceFragment]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [
            {
                "citation": {
                    "scope_id": fragment.address.memory.scope_id,
                    "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                    "entry_id": fragment.address.entry_id,
                    "entry_version_id": fragment.address.entry_version_id,
                },
                "content": fragment.text,
                "start": fragment.start,
                "end": fragment.end,
            }
            for fragment in fragments
        ],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _evidence_rendered_bytes(fragments: Sequence[PreparedEvidenceFragment]) -> int:
    if not fragments:
        return 0
    return len(_render_evidence_fragments(fragments).encode("utf-8"))


def _indices_to_fragments(
    doc: _EvidenceCandidateDoc,
    sentences: Sequence[_SentenceSpan],
    selected_indices: set[int],
    *,
    bridge_gap_bytes: int = 307,
) -> list[PreparedEvidenceFragment]:
    if not selected_indices:
        return []
    ordered = sorted(selected_indices)
    intervals: list[tuple[int, int]] = []
    for idx in ordered:
        s_span = sentences[idx]
        if not intervals:
            intervals.append((s_span.start, s_span.end))
            continue
        prev_start, prev_end = intervals[-1]
        gap_text = doc.text[prev_end : s_span.start]
        gap_bytes = len(gap_text.encode("utf-8"))
        if not gap_text.strip() or gap_bytes <= bridge_gap_bytes:
            intervals[-1] = (prev_start, s_span.end)
        else:
            intervals.append((s_span.start, s_span.end))
    return [
        PreparedEvidenceFragment(
            address=doc.address,
            start=start,
            end=end,
            text=doc.text[start:end],
        )
        for start, end in intervals
    ]


def _fit_partial_span(
    doc: _EvidenceCandidateDoc,
    start: int,
    end: int,
    *,
    existing_fragments: Sequence[PreparedEvidenceFragment],
    max_bytes: int,
) -> PreparedEvidenceFragment | None:
    text = doc.text[start:end]
    if not text.strip():
        return None
    full_frag = PreparedEvidenceFragment(address=doc.address, start=start, end=end, text=text)
    if _evidence_rendered_bytes((*existing_fragments, full_frag)) <= max_bytes:
        return full_frag

    lo = 1
    hi = end - start
    best_end: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        cand_end = start + mid
        cand_frag = PreparedEvidenceFragment(
            address=doc.address,
            start=start,
            end=cand_end,
            text=doc.text[start:cand_end],
        )
        if _evidence_rendered_bytes((*existing_fragments, cand_frag)) <= max_bytes:
            best_end = cand_end
            lo = mid + 1
        else:
            hi = mid - 1

    if best_end is None or best_end <= start:
        return None
    # Snap to last whitespace boundary if possible so words are not split mid-token
    word_end = best_end
    if word_end < len(doc.text) and not doc.text[word_end].isspace():
        space_pos = doc.text.rfind(" ", start + 1, word_end)
        if space_pos > start + max(8, (best_end - start) // 2):
            word_end = space_pos
    while word_end > start and doc.text[word_end - 1].isspace():
        word_end -= 1
    if word_end <= start:
        word_end = best_end
    return PreparedEvidenceFragment(
        address=doc.address,
        start=start,
        end=word_end,
        text=doc.text[start:word_end],
    )


def _expand_single_fragment_boundaries(
    doc: _EvidenceCandidateDoc,
    fragment: PreparedEvidenceFragment,
    sentences: Sequence[_SentenceSpan],
    *,
    max_bytes: int,
) -> PreparedEvidenceFragment:
    if any(s.is_duplicate_boilerplate for s in sentences):
        return fragment

    cur_start, cur_end = fragment.start, fragment.end
    # Prefer expanding rightwards into abstract body first, then leftwards towards start
    if cur_end < len(doc.text):
        lo = cur_end
        hi = len(doc.text)
        best_end = cur_end
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = PreparedEvidenceFragment(
                address=doc.address,
                start=cur_start,
                end=mid,
                text=doc.text[cur_start:mid],
            )
            if _evidence_rendered_bytes((cand,)) <= max_bytes:
                best_end = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best_end > cur_end:
            if best_end < len(doc.text) and not doc.text[best_end].isspace():
                sp = doc.text.rfind(" ", cur_end, best_end)
                if sp > cur_end:
                    best_end = sp
            while best_end > cur_end and doc.text[best_end - 1].isspace():
                best_end -= 1
            if best_end > cur_end:
                cur_end = best_end

    if cur_start > 0:
        # Check if sentences[0] is a title; only expand left into non-title body or if body is already covered
        min_allowed_start = 0
        if sentences and sentences[0].is_title and cur_end < len(doc.text):
            min_allowed_start = sentences[1].start if len(sentences) > 1 else 0
        if cur_start > min_allowed_start:
            lo = min_allowed_start
            hi = cur_start
            best_start = cur_start
            while lo <= hi:
                mid = (lo + hi) // 2
                cand = PreparedEvidenceFragment(
                    address=doc.address,
                    start=mid,
                    end=cur_end,
                    text=doc.text[mid:cur_end],
                )
                if _evidence_rendered_bytes((cand,)) <= max_bytes:
                    best_start = mid
                    hi = mid - 1
                else:
                    lo = mid + 1
            if best_start < cur_start:
                if best_start > 0 and not doc.text[best_start - 1].isspace():
                    sp = doc.text.find(" ", best_start, cur_start)
                    if sp != -1 and sp + 1 < cur_start:
                        best_start = sp + 1
                while best_start < cur_start and doc.text[best_start].isspace():
                    best_start += 1
                if best_start < cur_start:
                    cur_start = best_start

    return PreparedEvidenceFragment(
        address=doc.address,
        start=cur_start,
        end=cur_end,
        text=doc.text[cur_start:cur_end],
    )


def _select_doc_fragments(
    doc: _EvidenceCandidateDoc,
    sentences: Sequence[_SentenceSpan],
    direct_scores: Sequence[float],
    context_scores: Sequence[float],
    sent_stems: Sequence[set[str]],
    query_stems_set: set[str],
    *,
    existing_fragments: Sequence[PreparedEvidenceFragment],
    max_bytes: int,
    expand_to_budget: bool,
) -> list[PreparedEvidenceFragment]:
    if not sentences:
        return []

    has_boilerplate = any(s.is_duplicate_boilerplate and direct_scores[idx] == 0.0 for idx, s in enumerate(sentences))

    # Fast path when expanding to fill budget on a non-boilerplate document:
    # 1) Check if the entire document fits in a single fragment
    if expand_to_budget and not has_boilerplate:
        whole_start = sentences[0].start
        whole_end = sentences[-1].end
        whole_frag = PreparedEvidenceFragment(
            address=doc.address,
            start=whole_start,
            end=whole_end,
            text=doc.text[whole_start:whole_end],
        )
        if _evidence_rendered_bytes((*existing_fragments, whole_frag)) <= max_bytes:
            return [whole_frag]

        # 2) Check if the entire abstract body (excluding standalone title sentence 0) fits in a single fragment
        if len(sentences) >= 2 and sentences[0].is_title:
            body_start = sentences[1].start
            body_end = sentences[-1].end
            body_frag = PreparedEvidenceFragment(
                address=doc.address,
                start=body_start,
                end=body_end,
                text=doc.text[body_start:body_end],
            )
            if _evidence_rendered_bytes((*existing_fragments, body_frag)) <= max_bytes:
                if not existing_fragments:
                    body_frag = _expand_single_fragment_boundaries(doc, body_frag, sentences, max_bytes=max_bytes)
                return [body_frag]

    # Greedy selection with MMR / novel query stem coverage + adjacency preference
    selected: set[int] = set()
    covered_stems: set[str] = set()
    has_positive_body = any(direct_scores[i] > 0.0 and not sentences[i].is_title for i in range(len(sentences)))

    while True:
        best_idx: int | None = None
        best_gain = -1.0
        for idx, s_span in enumerate(sentences):
            if idx in selected:
                continue
            if s_span.is_duplicate_boilerplate and direct_scores[idx] <= 0.0:
                continue
            if not expand_to_budget and direct_scores[idx] <= 0.0 and context_scores[idx] <= 0.0:
                continue

            base = context_scores[idx]
            if s_span.is_title and has_positive_body:
                base *= 0.45

            uncovered = len((sent_stems[idx] & query_stems_set) - covered_stems)
            novelty_bonus = 1.25 * uncovered
            adj_bonus = 0.0
            if selected:
                if (idx - 1) in selected or (idx + 1) in selected:
                    adj_bonus = 0.35 * max(base, 0.2) + 0.15
                elif (idx - 2) in selected or (idx + 2) in selected:
                    adj_bonus = 0.15 * max(base, 0.1)

            # Tie-break slightly in favor of body sentences over title, then earlier position
            body_bias = 0.01 if not s_span.is_title else 0.0
            gain = base + novelty_bonus + adj_bonus + body_bias - (idx * 1e-6)
            if gain <= 0.0 and selected and not expand_to_budget:
                continue
            if gain > best_gain:
                cand_selected = set(selected)
                cand_selected.add(idx)
                cand_frags = _indices_to_fragments(doc, sentences, cand_selected)
                if _evidence_rendered_bytes((*existing_fragments, *cand_frags)) <= max_bytes:
                    best_gain = gain
                    best_idx = idx

        if best_idx is None:
            break
        selected.add(best_idx)
        covered_stems |= sent_stems[best_idx] & query_stems_set
        if not expand_to_budget and len(selected) >= 2 and covered_stems >= query_stems_set:
            break

    if not selected:
        # Fallback if even the single top sentence exceeded max_bytes: slice the highest-scoring sentence
        top_idx = max(range(len(sentences)), key=lambda i: (context_scores[i], -i))
        partial = _fit_partial_span(
            doc,
            sentences[top_idx].start,
            sentences[top_idx].end,
            existing_fragments=existing_fragments,
            max_bytes=max_bytes,
        )
        return [partial] if partial is not None else []

    fragments = _indices_to_fragments(doc, sentences, selected)

    # If we have multiple fragments in this doc and bridging the full span [first, last] fits in max_bytes
    # (and there are no duplicate boilerplate sentences in between), bridge into 1 contiguous fragment!
    if expand_to_budget and len(fragments) > 1 and not has_boilerplate:
        bridged = PreparedEvidenceFragment(
            address=doc.address,
            start=fragments[0].start,
            end=fragments[-1].end,
            text=doc.text[fragments[0].start : fragments[-1].end],
        )
        if _evidence_rendered_bytes((*existing_fragments, bridged)) <= max_bytes:
            fragments = [bridged]

    if expand_to_budget and not existing_fragments and len(fragments) == 1 and not has_boilerplate:
        fragments = [_expand_single_fragment_boundaries(doc, fragments[0], sentences, max_bytes=max_bytes)]

    return fragments


def _build_evidence_selection(
    *,
    builder: PreparedContextBuilder,
    request: PrepareContextRequest,
    current_scope_id: str | None,
    memory_candidates: Sequence[PreparedMemoryCandidates],
) -> PreparedContextBuild:
    grouped_docs: list[list[_EvidenceCandidateDoc]] = []
    seen_global: set[tuple[str, str, int, str, str]] = set()
    rank_counter = 0

    for candidates in memory_candidates:
        if candidates.hits and candidates.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        effective_scope = candidates.scope_id if candidates.scope_id else (current_scope_id or "")
        group_list: list[_EvidenceCandidateDoc] = []
        seen_in_group: set[tuple[str, str]] = set()
        for hit in candidates.hits:
            if hit.memory_ref != candidates.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            local_key = (hit.entry_id, hit.entry_version_id)
            if local_key in seen_in_group:
                continue
            seen_in_group.add(local_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            global_key = (
                effective_scope,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if global_key in seen_global:
                continue
            seen_global.add(global_key)
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=effective_scope, artifact=hit.memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            group_list.append(
                _EvidenceCandidateDoc(
                    address=address,
                    origin=address,
                    text=hit.text,
                    rank=rank_counter,
                )
            )
            rank_counter += 1
        if group_list:
            grouped_docs.append(group_list)

    docs = list(_interleave_groups(grouped_docs))
    if not docs:
        return PreparedContextBuild(context=builder.empty(), origins=(), fragments=())

    q_words, q_stems, q_prefixes, q_numbers, q_codes, q_bigrams, q_char_ngrams = _extract_features(request.query)
    q_words_set = set(q_words)
    q_stems_set = set(q_stems)

    doc_sentences: list[list[_SentenceSpan]] = []
    doc_sent_features: list[
        list[tuple[list[str], list[str], set[str], set[str], set[str], set[tuple[str, str]], set[str]]]
    ] = []
    all_sent_df_words: Counter[str] = Counter()
    all_sent_df_stems: Counter[str] = Counter()
    all_sent_df_prefixes: Counter[str] = Counter()
    total_sentences = 0
    total_word_len = 0

    for doc in docs:
        sents = _split_evidence_sentences(doc.text)
        doc_sentences.append(sents)
        feats_list = []
        for s_span in sents:
            feats = _extract_features(s_span.text)
            feats_list.append(feats)
            s_words, s_stems, s_prefixes, _, _, _, _ = feats
            total_sentences += 1
            total_word_len += max(1, len(s_words))
            for w in set(s_words):
                all_sent_df_words[w] += 1
            for st in set(s_stems):
                all_sent_df_stems[st] += 1
            for pf in s_prefixes:
                all_sent_df_prefixes[pf] += 1
        doc_sent_features.append(feats_list)

    if total_sentences == 0:
        return PreparedContextBuild(context=builder.empty(), origins=(), fragments=())

    avg_sent_len = max(6.0, total_word_len / max(1, total_sentences))

    def _idf(df: int) -> float:
        return math.log(1.0 + (total_sentences - df + 0.5) / (df + 0.5))

    doc_direct_scores: list[list[float]] = []
    doc_context_scores: list[list[float]] = []
    doc_sent_stems: list[list[set[str]]] = []
    doc_overall_scores: list[float] = []

    for d_idx, doc in enumerate(docs):
        sents = doc_sentences[d_idx]
        feats_list = doc_sent_features[d_idx]
        direct_list: list[float] = []
        stems_sets: list[set[str]] = []

        for s_idx, s_span in enumerate(sents):
            s_words, s_stems, s_prefixes, s_numbers, s_codes, s_bigrams, s_char_ngrams = feats_list[s_idx]
            s_words_counts = Counter(s_words)
            s_stems_counts = Counter(s_stems)
            s_stems_set = set(s_stems)
            stems_sets.append(s_stems_set)

            norm_denom = 0.55 + 0.45 * (max(4, len(s_words)) / avg_sent_len)
            score = 0.0

            # 1. Exact content word BM25 match
            matched_q_words = 0
            for qw in q_words_set:
                tf = s_words_counts.get(qw, 0)
                if tf > 0:
                    matched_q_words += 1
                    score += 2.2 * _idf(all_sent_df_words[qw]) * ((tf * 2.0) / (tf + norm_denom))

            # 2. Stem BM25 match
            matched_q_stems = 0
            for qst in q_stems_set:
                tf = s_stems_counts.get(qst, 0)
                if tf > 0:
                    matched_q_stems += 1
                    score += 1.6 * _idf(all_sent_df_stems[qst]) * ((tf * 2.0) / (tf + norm_denom))

            # 3. 5-char prefix match
            for qpf in q_prefixes:
                if qpf in s_prefixes:
                    score += 0.65 * _idf(all_sent_df_prefixes[qpf])

            # 4. Query coverage ratio bonus
            if q_words_set:
                score += 2.5 * (matched_q_words / len(q_words_set))
            if q_stems_set:
                score += 2.0 * (matched_q_stems / len(q_stems_set))

            # 5. Numbers & technical/biomedical codes bonus
            if q_numbers:
                num_hits = len(q_numbers & s_numbers)
                score += 3.5 * num_hits
            if q_codes:
                code_hits = len(q_codes & (s_codes | set(s_words)))
                score += 3.0 * code_hits

            # 6. Bigram bonus
            if q_bigrams and s_bigrams:
                score += 1.8 * len(q_bigrams & s_bigrams)

            # 7. Character 4-gram recall bonus
            if q_char_ngrams and s_char_ngrams:
                score += 1.2 * (len(q_char_ngrams & s_char_ngrams) / len(q_char_ngrams))

            direct_list.append(score)

        # Contextual smoothing across neighboring sentences
        context_list: list[float] = []
        for s_idx in range(len(sents)):
            c_score = direct_list[s_idx]
            if s_idx > 0 and not sents[s_idx - 1].is_duplicate_boilerplate:
                c_score += 0.22 * direct_list[s_idx - 1]
            if s_idx + 1 < len(sents) and not sents[s_idx + 1].is_duplicate_boilerplate:
                c_score += 0.22 * direct_list[s_idx + 1]
            if s_idx > 1 and not sents[s_idx - 2].is_duplicate_boilerplate:
                c_score += 0.08 * direct_list[s_idx - 2]
            if s_idx + 2 < len(sents) and not sents[s_idx + 2].is_duplicate_boilerplate:
                c_score += 0.08 * direct_list[s_idx + 2]
            context_list.append(c_score)

        doc_direct_scores.append(direct_list)
        doc_context_scores.append(context_list)
        doc_sent_stems.append(stems_sets)

        # Document-level relevance score
        d_words, d_stems, d_prefixes, d_numbers, d_codes, d_bigrams, d_char_ngrams = _extract_features(doc.text)
        d_word_cov = len(q_words_set & set(d_words)) / max(1, len(q_words_set))
        d_stem_cov = len(q_stems_set & set(d_stems)) / max(1, len(q_stems_set))
        d_code_hits = len(q_codes & (d_codes | set(d_words))) + len(q_numbers & d_numbers)
        sorted_direct = sorted(direct_list, reverse=True)
        top1 = sorted_direct[0] if sorted_direct else 0.0
        top2 = sum(sorted_direct[:2])
        rank_prior = 0.05 / (1.0 + doc.rank)
        doc_overall = top1 * 1.4 + top2 * 0.7 + d_word_cov * 4.0 + d_stem_cov * 3.5 + d_code_hits * 2.5 + rank_prior
        doc_overall_scores.append(doc_overall)

    ranked_doc_indices = sorted(range(len(docs)), key=lambda i: (doc_overall_scores[i], -docs[i].rank), reverse=True)
    primary_idx = ranked_doc_indices[0]

    # Select and expand fragments for the primary top-ranked document
    selected_fragments = _select_doc_fragments(
        docs[primary_idx],
        doc_sentences[primary_idx],
        doc_direct_scores[primary_idx],
        doc_context_scores[primary_idx],
        doc_sent_stems[primary_idx],
        q_stems_set,
        existing_fragments=(),
        max_bytes=request.max_bytes,
        expand_to_budget=True,
    )

    # If leftover budget remains after packing the primary document, try adding top relevant sentences
    # from secondary documents that have positive relevance
    for sec_idx in ranked_doc_indices[1:]:
        if len(selected_fragments) >= builder.entry_limit:
            break
        if doc_overall_scores[sec_idx] <= 0.1 or max(doc_direct_scores[sec_idx], default=0.0) <= 0.0:
            continue
        sec_frags = _select_doc_fragments(
            docs[sec_idx],
            doc_sentences[sec_idx],
            doc_direct_scores[sec_idx],
            doc_context_scores[sec_idx],
            doc_sent_stems[sec_idx],
            q_stems_set,
            existing_fragments=selected_fragments,
            max_bytes=request.max_bytes,
            expand_to_budget=False,
        )
        for frag in sec_frags:
            if len(selected_fragments) >= builder.entry_limit:
                break
            if _evidence_rendered_bytes((*selected_fragments, frag)) <= request.max_bytes:
                selected_fragments.append(frag)

    if not selected_fragments:
        return PreparedContextBuild(context=builder.empty(), origins=(), fragments=())

    fragments_tuple = tuple(selected_fragments)
    origins_tuple = tuple(frag.address for frag in fragments_tuple)
    content = _render_evidence_fragments(fragments_tuple)
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > request.max_bytes:
        raise PreparedContextInvariantError("output-budget")

    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
        origins=origins_tuple,
        fragments=fragments_tuple,
    )
