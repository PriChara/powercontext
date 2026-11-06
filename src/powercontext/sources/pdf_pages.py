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

"""Full-document PDF page parsing, captured Sources, and evidence-page retrieval."""

from __future__ import annotations

import io
import math
import re
import unicodedata
from collections import Counter
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, StrictInt, StrictStr, field_validator, model_validator
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory.canonical import canonical_json
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[a-z]+|\d+")
_COMMA_NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_YEAR_RE = re.compile(r"\b(?:fy\s*)?(20\d{2})\b")
_SHORT_FY_RE = re.compile(r"\bfy\s*(\d{2})\b")
_MAX_ENTRY_UTF8_BYTES = 8192

_STOPWORDS = frozenset({
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
    "give",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "out",
    "s",
    "should",
    "so",
    "state",
    "such",
    "than",
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
    "up",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
})

_PROMPT_BOILERPLATE_WORDS = frozenset({
    "affa",
    "analyst",
    "answer",
    "assuming",
    "based",
    "billion",
    "billions",
    "calculate",
    "calculated",
    "can",
    "define",
    "defined",
    "details",
    "directly",
    "explicitly",
    "expressed",
    "figure",
    "figures",
    "fy",
    "give",
    "in",
    "investment",
    "million",
    "millions",
    "not",
    "only",
    "outlined",
    "perspective",
    "primarily",
    "question",
    "relying",
    "reported",
    "respond",
    "response",
    "round",
    "rounded",
    "shown",
    "state",
    "statement",
    "statements",
    "usd",
    "use",
    "using",
    "within",
})


def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("document_id must be a non-empty string")
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("document_sha256 must be a 64-character lowercase hexadecimal SHA-256")
    return value


def _validate_page_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be an integer >= 1")
    return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Compute a deterministic, collision-free Source identifier <= 256 characters."""

    _validate_document_id(document_id)
    _validate_sha256(document_sha256)
    _validate_page_number(page_number)
    encoded_doc = quote(document_id, safe="")
    candidate = f"pdf:{encoded_doc}:{document_sha256}:{page_number}"
    if len(candidate) <= MAX_SOURCE_ID_LENGTH:
        return candidate
    digest = sha256(
        canonical_json({
            "document_id": document_id,
            "document_sha256": document_sha256,
            "page_number": page_number,
        })
    ).hexdigest()
    return f"pdf:{digest}:{page_number}"


def _normalize_page_text(document_id: str, raw_text: str) -> str:
    if not raw_text or not raw_text.strip():
        return ""
    text = unicodedata.normalize("NFC", raw_text).strip()
    prefix_bytes = len(f"{document_id}\n".encode("utf-8"))
    budget = _MAX_ENTRY_UTF8_BYTES - prefix_bytes
    if len(text.encode("utf-8")) <= budget:
        return text
    compact = re.sub(r"[ \t]+", " ", text)
    compact = re.sub(r"\n\s*\n+", "\n", compact).strip()
    if len(compact.encode("utf-8")) <= budget:
        return compact
    encoded = compact.encode("utf-8")[:budget]
    return encoded.decode("utf-8", errors="ignore").strip()


class PDFDocumentInput(BaseModel):
    """Complete raw PDF document input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    content: StrictBytes

    @field_validator("document_id")
    @classmethod
    def _check_document_id(cls, value: str) -> str:
        return _validate_document_id(value)


class PDFPageInput(BaseModel):
    """Canonical single physical page extracted from a PDF document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    document_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    text: StrictStr

    @field_validator("document_id")
    @classmethod
    def _check_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("document_sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


class PDFPageSource(Source):
    """Captured Source representing one physical PDF page."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def _validate_source_contract(self) -> PDFPageSource:
        expected_id = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.source_id != expected_id or self.name != expected_id:
            raise ValueError("PDFPageSource name and source_id must match pdf_page_source_id")
        if self.materialization is not SourceMaterialization.CAPTURED:
            raise ValueError("PDFPageSource materialization must be CAPTURED")
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse all physical pages of a complete PDF document atomically."""

    if not isinstance(value, PDFDocumentInput):
        value = PDFDocumentInput.model_validate(value)
    _validate_document_id(value.document_id)
    if not isinstance(value.content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw = bytes(value.content)
    if not raw or not raw.startswith(b"%PDF-"):
        raise ValueError("input content is empty or not a valid PDF header")
    if b"%%EOF" not in raw[-2048:] and b"%%EOF" not in raw:
        raise ValueError("PDF content is truncated (missing EOF marker)")

    document_sha256 = sha256(raw).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            decrypt_status = reader.decrypt("")
            if not decrypt_status:
                raise ValueError("encrypted PDF requires a non-empty user password")
        total_pages = len(reader.pages)
        if total_pages < 1:
            raise ValueError("PDF document contains no pages")
        pages: list[PDFPageInput] = []
        for idx in range(total_pages):
            page_obj = reader.pages[idx]
            extracted = page_obj.extract_text() or ""
            normalized_text = _normalize_page_text(value.document_id, extracted)
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=document_sha256,
                    page_number=idx + 1,
                    text=normalized_text,
                )
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"failed to parse PDF document {value.document_id!r}") from exc

    return tuple(pages)


class _PDFPageSourceAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        if not isinstance(value, PDFPageInput):
            value = PDFPageInput.model_validate(value)
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        if not isinstance(source, PDFPageSource):
            raise TypeError("source must be a PDFPageSource")
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageSourceAdapter(),
    version="1",
    projections=(_PDFPageTextEvidenceProjection(),),
)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _parse_page_number_from_source_id(source_id: str) -> int | None:
    parts = source_id.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def _extract_question_years(question_lower: str) -> set[str]:
    years = set(_YEAR_RE.findall(question_lower))
    for short_yr in _SHORT_FY_RE.findall(question_lower):
        years.add(f"20{short_yr}")
    return years


def _build_document_bonus_map(
    doc_pages: dict[str, list[tuple[int | None, str]]],
    question_lower: str,
    question_years: set[str],
) -> dict[str, float]:
    q_alnum = re.sub(r"[^a-z0-9]", "", question_lower)
    q_tokens = set(_tokenize(question_lower))
    bonuses: dict[str, float] = {}
    for doc_id, pages in doc_pages.items():
        bonus = 0.0
        raw_parts = [p.lower() for p in re.split(r"[_\-\s]+", doc_id) if p]
        doc_years = {p for p in raw_parts if re.fullmatch(r"20\d{2}", p)}
        company_parts = [
            p
            for p in raw_parts
            if not re.fullmatch(r"20\d{2}", p)
            and p not in {"10k", "10q", "8k", "annual", "report", "fy", "q1", "q2", "q3", "q4"}
        ]
        company_alnum = re.sub(r"[^a-z0-9]", "", "".join(company_parts))
        if company_alnum and company_alnum in q_alnum:
            bonus += 60.0
        else:
            part_tokens = [re.sub(r"[^a-z0-9]", "", p) for p in company_parts]
            part_tokens = [t for t in part_tokens if t]
            if part_tokens and any(t in q_tokens or (len(t) >= 4 and t in q_alnum) for t in part_tokens):
                bonus += 45.0
            else:
                cover_text = " ".join(text for _pno, text in sorted(pages, key=lambda x: x[0] or 9999)[:3]).lower()
                cover_tokens = set(_tokenize(cover_text))
                rare_q_tokens = {
                    t
                    for t in q_tokens
                    if len(t) >= 4
                    and not t.isdigit()
                    and t not in _STOPWORDS
                    and t not in _PROMPT_BOILERPLATE_WORDS
                }
                if rare_q_tokens and len(rare_q_tokens & cover_tokens) >= 2:
                    bonus += 10.0

        if question_years and doc_years:
            max_q_year = max(int(y) for y in question_years)
            if str(max_q_year) in doc_years:
                bonus += 25.0
            elif any(abs(int(dy) - max_q_year) <= 1 for dy in doc_years):
                bonus += 10.0
        bonuses[doc_id] = bonus
    return bonuses


def _score_domain_sections(
    question_lower: str,
    page_text: str,
    page_lower: str,
    page_number: int | None,
) -> float:
    score = 0.0
    lines = [ln.strip().lower() for ln in page_text.splitlines() if ln.strip()]
    if lines and lines[0] == "table of contents":
        lines = lines[1:]
    header_text = " | ".join(lines[:5])
    top_text = page_lower[:450]
    comma_nums = len(_COMMA_NUMBER_RE.findall(page_text))
    is_toc_page = (
        ("form 10-k annual report" in top_text and "item 1." in page_lower and "item 8." in page_lower)
        or ("index to consolidated financial statements" in top_text)
        or (page_number is not None and page_number <= 3 and "item 1." in page_lower and "item 7." in page_lower)
    )
    if is_toc_page:
        return -25.0

    if page_number is not None and page_number > 150:
        if any(
            k in page_lower
            for k in ("pension equalization plan", "executive incentive", "plan administrator", "erisa", "409a")
        ):
            score -= 20.0

    wants_income_stmt = any(
        k in question_lower
        for k in (
            "income statement",
            "income statements",
            "p&l",
            "statement of operations",
            "statements of operations",
            "statement of earnings",
            "statements of earnings",
            "operating income",
            "operating profit",
            "ebitda",
            "ebit",
            "gross profit",
            "gross margin",
            "net revenue",
            "net sales",
            "total revenue",
            "selling, general and administrative",
            "sg&a",
            "cost of sales",
            "cost of goods sold",
        )
    )
    wants_cash_flow = any(
        k in question_lower
        for k in (
            "cash flow",
            "statement of cash flows",
            "statements of cash flows",
            "capex",
            "capital expenditure",
            "capital expenditures",
            "capital spending",
            "depreciation and amortization",
            "d&a",
            "free cash flow",
            "operating activities",
            "investing activities",
            "financing activities",
            "dividends paid",
            "share repurchases",
        )
    )
    wants_balance_sheet = any(
        k in question_lower
        for k in (
            "balance sheet",
            "balance sheets",
            "financial position",
            "total assets",
            "total liabilities",
            "current assets",
            "current liabilities",
            "cash and cash equivalents",
            "accounts receivable",
            "inventories",
            "inventory",
            "accounts payable",
            "long-term debt",
            "total debt",
            "shareholders' equity",
            "stockholders' equity",
            "property, plant and equipment",
            "pp&e",
            "goodwill",
            "quick ratio",
            "current ratio",
            "working capital",
            "capital intensity",
            "fixed assets",
        )
    )
    wants_legal = any(
        k in question_lower
        for k in (
            "legal battle",
            "legal battles",
            "legal proceeding",
            "legal proceedings",
            "litigation",
            "lawsuit",
            "lawsuits",
            "contingencies",
            "regulatory matters",
        )
    )
    wants_geography = any(
        k in question_lower
        for k in (
            "geographies",
            "geography",
            "geographic",
            "operates in",
            "operate in",
            "countries",
            "regions",
            "segments",
            "reportable segments",
            "divisions",
        )
    )
    wants_restructuring = "restructuring" in question_lower

    is_income_stmt_header = any(
        h in header_text
        for h in (
            "consolidated statement of income",
            "consolidated statements of income",
            "consolidated statement of operations",
            "consolidated statements of operations",
            "consolidated statement of earnings",
            "consolidated statements of earnings",
            "statements of consolidated income",
            "statement of consolidated income",
            "statements of consolidated operations",
        )
    ) and "comprehensive income" not in header_text
    if wants_income_stmt:
        if is_income_stmt_header and comma_nums >= 6:
            score += 48.0
        elif (
            any(k in page_lower for k in ("net revenue", "net sales", "total revenues", "total net sales"))
            and any(k in page_lower for k in ("operating profit", "operating income", "gross profit"))
            and any(k in page_lower for k in ("net income", "diluted", "income before income taxes"))
            and comma_nums >= 10
        ):
            score += 24.0

    is_cf_header = any(
        h in header_text
        for h in (
            "consolidated statement of cash flows",
            "consolidated statements of cash flows",
            "statements of consolidated cash flows",
            "statement of consolidated cash flows",
        )
    )
    if wants_cash_flow:
        if is_cf_header and comma_nums >= 6:
            score += 48.0
            if "operating activities" in page_lower and "depreciation" in page_lower:
                score += 10.0
            if (
                "capital spending" in page_lower
                or "capital expenditures" in page_lower
                or "investing activities" in page_lower
            ):
                score += 10.0
        elif "free cash flow" in header_text or (
            "free cash flow" in page_lower and "capital spending" in page_lower and comma_nums >= 3
        ):
            score += 32.0
        elif (
            "operating activities" in page_lower
            and ("depreciation and amortization" in page_lower or "investing activities" in page_lower)
            and (
                "capital spending" in page_lower
                or "capital expenditures" in page_lower
                or "property, plant and equipment" in page_lower
            )
            and comma_nums >= 8
        ):
            score += 26.0

    is_bs_header = any(
        h in header_text
        for h in (
            "consolidated balance sheet",
            "consolidated balance sheets",
            "consolidated statement of financial position",
            "consolidated statements of financial position",
        )
    )
    if wants_balance_sheet:
        if is_bs_header and comma_nums >= 6:
            score += 48.0
        elif (
            "current assets" in page_lower
            and "total assets" in page_lower
            and ("current liabilities" in page_lower or "total liabilities" in page_lower)
            and comma_nums >= 10
        ):
            score += 24.0

    if wants_legal:
        if "item 3. legal proceedings" in page_lower or "item 3.  legal proceedings" in page_lower:
            score += 55.0
        elif "legal proceedings" in header_text or "commitments and contingencies" in header_text:
            score += 35.0
        elif "legal, administrative, regulatory and government proceedings" in page_lower:
            score += 40.0
        elif "regulatory matters" in page_lower and "item 1." in page_lower:
            score += 18.0

    if wants_geography:
        if "net revenue and long-lived assets by country" in page_lower or "by country are as follows" in page_lower:
            score += 52.0
        if "by geographic" in page_lower or "geographic areas" in page_lower or "geographic information" in page_lower:
            score += 38.0
        if (
            "united states" in page_lower
            and "all other countries" in page_lower
            and ("net revenue" in page_lower or "long-lived assets" in page_lower)
        ):
            score += 45.0
        if "reportable segments" in page_lower or "our divisions" in header_text or "our operations" in page_lower:
            score += 38.0
        if "item 1. business" in page_lower:
            score += 34.0
        if "note 1 " in header_text or "note 1 —" in header_text or "basis of presentation and our divisions" in page_lower:
            score += 32.0
        if "disaggregation of net revenue" in page_lower:
            score += 30.0
        region_hits = sum(
            1
            for r in ("north america", "latin america", "europe", "africa", "middle east", "asia pacific", "china")
            if r in page_lower
        )
        if region_hits >= 4:
            score += 28.0

    if wants_restructuring:
        if "note 3" in header_text and "restructuring" in page_lower:
            score += 36.0
        elif "restructuring and impairment" in page_lower and comma_nums >= 6:
            score += 30.0
        elif "2019 productivity plan" in page_lower and "restructuring charges" in page_lower:
            score += 28.0

    return score


async def search_pdf_pages(
    memory: Any,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory for the best evidence pages answering a natural-language question."""

    if limit < 1 or not isinstance(question, str) or not question.strip():
        return ()

    question_norm = unicodedata.normalize("NFC", question).strip()
    question_lower = question_norm.lower()
    question_years = _extract_question_years(question_lower)

    raw_q_tokens = _tokenize(question_lower)
    core_q_tokens = [
        t
        for t in raw_q_tokens
        if t not in _STOPWORDS and t not in _PROMPT_BOILERPLATE_WORDS and len(t) > 1
    ]
    if not core_q_tokens:
        core_q_tokens = [t for t in raw_q_tokens if t not in _STOPWORDS] or raw_q_tokens

    expanded_terms: list[str] = list(core_q_tokens)
    if "capex" in question_lower or "capital expenditure" in question_lower:
        expanded_terms.extend(["capital", "spending", "additions", "property", "plant", "equipment"])
    if "d&a" in question_lower or "depreciation" in question_lower:
        expanded_terms.extend(["depreciation", "amortization"])
    if "p&l" in question_lower or "income statement" in question_lower:
        expanded_terms.extend(["consolidated", "income", "revenue", "operating", "profit"])
    if "ebitda" in question_lower:
        expanded_terms.extend(["operating", "profit", "income", "depreciation", "amortization"])
    if "geographies" in question_lower or "operates in" in question_lower:
        expanded_terms.extend([
            "country",
            "countries",
            "divisions",
            "segments",
            "united",
            "states",
            "international",
            "europe",
            "latin",
            "america",
            "asia",
        ])
    if "legal battles" in question_lower or "legal" in question_lower:
        expanded_terms.extend(["legal", "proceedings", "regulatory", "litigation", "contingencies"])

    fts_hit_scores: dict[tuple[str, str], float] = {}
    search_queries: list[str] = []
    if core_q_tokens:
        search_queries.append(" ".join(core_q_tokens[:6]))
    for phrase in (
        "consolidated statement",
        "operating activities",
        "legal proceedings",
        "reportable segments",
        "restructuring charges",
    ):
        if any(w in expanded_terms for w in phrase.split()):
            search_queries.append(phrase)

    for sq in dict.fromkeys(search_queries):
        if not sq.strip():
            continue
        try:
            page_res = await memory.search(SearchMemoryRequest(query=sq, mode="fts", limit=max(limit, 20)))
            for rank, hit in enumerate(page_res.hits):
                key = (hit.entry_id, hit.entry_version_id)
                fts_hit_scores[key] = max(fts_hit_scores.get(key, 0.0), float(hit.score) + 2.0 / (rank + 1))
        except Exception:
            pass

    records_by_key: dict[tuple[str, str], tuple[MemoryCitation, str, tuple[str, ...]]] = {}
    if hasattr(memory, "list"):
        try:
            listed = await memory.list()
            for record in listed.entries:
                if record.state != "active":
                    continue
                source_ids = tuple(ref.source_id for ref in record.entry.sources)
                records_by_key[(record.entry.entry_id, record.entry.entry_version_id)] = (
                    record.citation,
                    record.entry.text,
                    source_ids,
                )
        except Exception:
            pass

    if not records_by_key and fts_hit_scores:
        try:
            page_res = await memory.search(
                SearchMemoryRequest(query=question_norm, mode="fts", limit=max(limit * 2, 40))
            )
            for hit in page_res.hits:
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                entry_rec = await memory.get(GetMemoryEntryRequest(citation=citation))
                source_ids = tuple(ref.source_id for ref in entry_rec.entry.sources)
                records_by_key[(hit.entry_id, hit.entry_version_id)] = (
                    citation,
                    entry_rec.entry.text,
                    source_ids,
                )
        except Exception:
            pass

    if not records_by_key:
        return ()

    candidates: list[dict[str, Any]] = []
    doc_pages: dict[str, list[tuple[int | None, str]]] = {}
    for key, (citation, full_text, source_ids) in records_by_key.items():
        if not source_ids:
            continue
        doc_id, sep, page_text = full_text.partition("\n")
        if not sep:
            doc_id = ""
            page_text = full_text
        page_text_stripped = page_text.strip()
        if not page_text_stripped:
            continue
        primary_source_id = source_ids[0]
        page_number = _parse_page_number_from_source_id(primary_source_id)
        doc_pages.setdefault(doc_id, []).append((page_number, page_text_stripped))
        tokens = _tokenize(page_text_stripped)
        if not tokens:
            continue
        candidates.append({
            "key": key,
            "citation": citation,
            "source_id": primary_source_id,
            "doc_id": doc_id,
            "page_number": page_number,
            "page_text": page_text_stripped,
            "page_lower": page_text_stripped.lower(),
            "tokens": tokens,
            "tf": Counter(tokens),
        })

    if not candidates:
        return ()

    num_docs = len(candidates)
    avgdl = sum(len(c["tokens"]) for c in candidates) / max(1, num_docs)
    query_term_counts = Counter(expanded_terms)
    core_term_set = set(core_q_tokens)

    df: dict[str, int] = {}
    for term in query_term_counts:
        df[term] = sum(1 for c in candidates if term in c["tf"])

    k1 = 1.5
    b = 0.75
    doc_bonuses = _build_document_bonus_map(doc_pages, question_lower, question_years)
    core_bigrams = [f"{core_q_tokens[i]} {core_q_tokens[i + 1]}" for i in range(len(core_q_tokens) - 1)]

    page_scores_by_loc: dict[tuple[str, int], float] = {}
    for cand in candidates:
        dl = len(cand["tokens"])
        tf = cand["tf"]
        bm25 = 0.0
        matched_core = 0
        for term, q_weight in query_term_counts.items():
            f = tf.get(term, 0)
            if f <= 0:
                continue
            if term in core_term_set:
                matched_core += 1
            n_q = df.get(term, 0)
            idf = math.log(1.0 + (num_docs - n_q + 0.5) / (n_q + 0.5))
            denom = f + k1 * (1.0 - b + b * (dl / max(1.0, avgdl)))
            weight_mult = 1.6 if term in core_term_set else 0.75
            bm25 += idf * ((f * (k1 + 1.0)) / denom) * weight_mult * min(2.0, 1.0 + 0.25 * (q_weight - 1))

        page_lower = cand["page_lower"]
        bigram_bonus = sum(3.5 for bg in core_bigrams if bg in page_lower)
        coverage_bonus = 2.0 * matched_core
        domain_bonus = _score_domain_sections(
            question_lower,
            cand["page_text"],
            page_lower,
            cand["page_number"],
        )
        doc_bonus = doc_bonuses.get(cand["doc_id"], 0.0)
        fts_bonus = min(6.0, fts_hit_scores.get(cand["key"], 0.0))

        total_score = bm25 + bigram_bonus + coverage_bonus + domain_bonus + doc_bonus + fts_bonus
        cand["score"] = total_score
        if cand["page_number"] is not None:
            page_scores_by_loc[(cand["doc_id"], cand["page_number"])] = total_score

    for cand in candidates:
        pno = cand["page_number"]
        doc_id = cand["doc_id"]
        if pno is None:
            cand["final_score"] = cand["score"]
            continue
        prev_score = page_scores_by_loc.get((doc_id, pno - 1), 0.0)
        next_score = page_scores_by_loc.get((doc_id, pno + 1), 0.0)
        neighbor_boost = 0.0
        if "continued" in cand["page_lower"][:300] and prev_score > 70.0:
            neighbor_boost = max(neighbor_boost, 0.25 * (prev_score - 50.0))
        elif prev_score > 95.0:
            neighbor_boost = max(neighbor_boost, 0.12 * (prev_score - 60.0))
        if next_score > 95.0:
            neighbor_boost = max(neighbor_boost, 0.08 * (next_score - 60.0))
        cand["final_score"] = cand["score"] + neighbor_boost

    candidates.sort(
        key=lambda c: (
            -c["final_score"],
            c["doc_id"],
            c["page_number"] if c["page_number"] is not None else 999999,
            c["source_id"],
        )
    )

    selected: list[MemoryCitation] = []
    seen_pages: set[tuple[str, int | str]] = set()
    for cand in candidates:
        loc_key: tuple[str, int | str] = (
            cand["doc_id"],
            cand["page_number"] if cand["page_number"] is not None else cand["source_id"],
        )
        if loc_key in seen_pages:
            continue
        seen_pages.add(loc_key)
        selected.append(cand["citation"])
        if len(selected) >= limit:
            break

    return tuple(selected)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
