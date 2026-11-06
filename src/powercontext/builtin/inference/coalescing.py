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

"""Concurrent request coalescing and bounded LRU caching for embedding models."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


class _CoalescedBatch:
    """One pending or in-flight provider embedding batch."""

    __slots__ = ("error", "event", "task", "texts", "usage", "vectors")

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.event = asyncio.Event()
        self.vectors: dict[str, EmbeddingVector] = {}
        self.usage = InferenceUsage(requests=0)
        self.error: BaseException | None = None
        self.task: asyncio.Task[None] | None = None


class CoalescingEmbeddingModel:
    """Coalesce concurrent embedding requests and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer.")
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number.")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer.")

        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _CoalescedBatch] = {}
        self._open_batch: _CoalescedBatch | None = None

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        """Embed ``texts`` while deduplicating concurrent requests and reusing cached vectors."""

        items = tuple(texts)
        if not items:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique_texts = list(dict.fromkeys(items))
        resolved: dict[str, EmbeddingVector] = {}
        waited_batches: list[_CoalescedBatch] = []
        seen_batches: set[_CoalescedBatch] = set()

        for text in unique_texts:
            if self._cache_size > 0 and text in self._cache:
                self._cache.move_to_end(text)
                resolved[text] = self._cache[text]
                continue

            batch = self._inflight.get(text)
            if batch is None:
                batch = self._enqueue_text(text)
            if batch not in seen_batches:
                seen_batches.add(batch)
                waited_batches.append(batch)

        if waited_batches:
            await asyncio.gather(*(batch.event.wait() for batch in waited_batches))
            for batch in waited_batches:
                if batch.error is not None:
                    raise batch.error
                for text, vector in batch.vectors.items():
                    resolved[text] = vector

        ordered_vectors = tuple(resolved[text] for text in items)
        usage = self._combine_usage(waited_batches)
        return EmbeddingResult(vectors=ordered_vectors, usage=usage)

    def _enqueue_text(self, text: str) -> _CoalescedBatch:
        batch = self._open_batch
        if batch is None or len(batch.texts) >= self._max_batch_size:
            batch = _CoalescedBatch()
            self._open_batch = batch
            batch.task = asyncio.create_task(self._run_batch(batch))

        batch.texts.append(text)
        self._inflight[text] = batch
        if len(batch.texts) >= self._max_batch_size and self._open_batch is batch:
            self._open_batch = None
        return batch

    async def _run_batch(self, batch: _CoalescedBatch) -> None:
        try:
            if self._coalesce_seconds > 0.0:
                await asyncio.sleep(self._coalesce_seconds)
            else:
                await asyncio.sleep(0)

            if self._open_batch is batch:
                self._open_batch = None

            request_texts = tuple(batch.texts)
            result = await self._model.embed(request_texts)
            if len(result.vectors) != len(request_texts):
                raise ValueError(
                    f"Embedding vector count mismatch: expected {len(request_texts)}, got {len(result.vectors)}."
                )

            vectors = {
                text: tuple(float(component) for component in vector)
                for text, vector in zip(request_texts, result.vectors, strict=True)
            }
            batch.vectors = vectors
            batch.usage = result.usage

            if self._cache_size > 0:
                for text, vector in vectors.items():
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
        except BaseException as exc:
            batch.error = exc
        finally:
            if self._open_batch is batch:
                self._open_batch = None
            for text in batch.texts:
                if self._inflight.get(text) is batch:
                    self._inflight.pop(text, None)
            batch.event.set()

    @staticmethod
    def _combine_usage(batches: Sequence[_CoalescedBatch]) -> InferenceUsage:
        if not batches:
            return InferenceUsage(requests=0)
        if len(batches) == 1:
            single = batches[0].usage
            return InferenceUsage(
                requests=single.requests,
                input_tokens=single.input_tokens,
                output_tokens=single.output_tokens,
            )

        requests = sum(batch.usage.requests for batch in batches)
        input_values = [batch.usage.input_tokens for batch in batches if batch.usage.input_tokens is not None]
        output_values = [batch.usage.output_tokens for batch in batches if batch.usage.output_tokens is not None]
        return InferenceUsage(
            requests=requests,
            input_tokens=sum(input_values) if input_values else None,
            output_tokens=sum(output_values) if output_values else None,
        )


__all__ = ["CoalescingEmbeddingModel"]
