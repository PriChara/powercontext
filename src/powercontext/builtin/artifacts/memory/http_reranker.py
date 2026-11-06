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

"""HTTP cross-encoder reranker adapter for Memory search candidates."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


def _validate_non_empty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_and_validate_scores(raw_content: bytes, expected_ranks: set[int]) -> dict[int, float]:
    try:
        payload = json.loads(raw_content)
    except Exception as exc:
        raise ValueError("reranker response body is not valid JSON") from exc

    if not isinstance(payload, dict) or set(payload.keys()) != {"scores"}:
        raise ValueError("reranker response JSON must contain only the 'scores' field")

    scores_list = payload["scores"]
    if not isinstance(scores_list, list):
        raise ValueError("reranker 'scores' field must be a list")

    if len(scores_list) != len(expected_ranks):
        raise ValueError("reranker response score count does not match batch candidate count")

    batch_scores: dict[int, float] = {}
    for item in scores_list:
        if not isinstance(item, dict) or set(item.keys()) != {"rank", "score"}:
            raise ValueError("each score entry must contain only 'rank' and 'score'")
        rank = item["rank"]
        score = item["score"]
        if type(rank) is not int:
            raise ValueError("score entry 'rank' must be an integer")
        if type(score) not in (int, float):
            raise ValueError("score entry 'score' must be a number")
        score_float = float(score)
        if not math.isfinite(score_float):
            raise ValueError("score entry 'score' must be finite")
        if rank not in expected_ranks:
            raise ValueError(f"unexpected or out-of-batch rank {rank}")
        if rank in batch_scores:
            raise ValueError(f"duplicate rank {rank} in reranker response")
        batch_scores[rank] = score_float

    if set(batch_scores.keys()) != expected_ranks:
        raise ValueError("reranker response did not cover all expected batch ranks")

    return batch_scores


class HTTPMemoryReranker:
    """Batch-aware HTTP cross-encoder reranker implementing MemoryReranker."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        self._endpoint = _validate_non_empty_str(endpoint, "endpoint")
        if client is None or not hasattr(client, "post"):
            raise ValueError("client must be an httpx.AsyncClient")
        self._client = client
        self._batch_size = _validate_positive_int(batch_size, "batch_size")
        self.policy_id = _validate_non_empty_str(policy_id, "policy_id")

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score candidates across bounded HTTP batches and return top-limit original ranks."""

        _validate_non_empty_str(query, "query")
        if not isinstance(candidates, (tuple, list)) or len(candidates) < 1:
            raise ValueError("candidates must be non-empty")
        total = len(candidates)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > total:
            raise ValueError("limit must be an integer between 1 and len(candidates)")

        indexed_candidates: list[tuple[int, str]] = []
        for rank, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, MemoryHit) or not isinstance(candidate.text, str) or not candidate.text:
                raise ValueError("each candidate must be a valid MemoryHit with non-empty text")
            indexed_candidates.append((rank, candidate.text))

        global_scores: dict[int, float] = {}
        request_count = 0

        for start in range(0, total, self._batch_size):
            batch = indexed_candidates[start : start + self._batch_size]
            expected_ranks = {rank for rank, _text in batch}
            request_body: dict[str, Any] = {
                "query": query,
                "candidates": [{"rank": rank, "text": text} for rank, text in batch],
            }
            response = await self._client.post(self._endpoint, json=request_body)
            response.raise_for_status()
            request_count += 1
            batch_scores = _parse_and_validate_scores(response.content, expected_ranks)
            global_scores.update(batch_scores)

        ordered_ranks = sorted(
            global_scores.keys(),
            key=lambda r: (-global_scores[r], r),
        )
        selected_ranks = tuple(ordered_ranks[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=request_count),
            discarded_rank_count=0,
            used_fallback=False,
        )


__all__ = [
    "DEFAULT_HTTP_RERANK_POLICY_ID",
    "HTTPMemoryReranker",
]
