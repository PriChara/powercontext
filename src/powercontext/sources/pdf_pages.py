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
import re
import unicodedata
from hashlib import sha256
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, StrictInt, StrictStr, field_validator, model_validator
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.canonical import canonical_json
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources._pdf_retrieval import search_pdf_pages
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class _InvalidPDF(ValueError):
    def __init__(self, reason: str, document_id: str | None = None) -> None:
        messages = {
            "document-id": "document_id must be a non-empty string",
            "digest": "document_sha256 must be a 64-character lowercase hexadecimal SHA-256",
            "page-number": "page_number must be an integer >= 1",
            "source-identity": "PDFPageSource name and source_id must match pdf_page_source_id",
            "source-materialization": "PDFPageSource materialization must be CAPTURED",
            "header": "input content is empty or not a valid PDF header",
            "truncated": "PDF content is truncated (missing EOF marker)",
            "password": "encrypted PDF requires a non-empty user password",
            "empty": "PDF document contains no pages",
            "parse": f"failed to parse PDF document {document_id!r}",
        }
        super().__init__(messages[reason])


class _InvalidPDFSourceType(TypeError):
    def __init__(self) -> None:
        super().__init__("source must be a PDFPageSource")


def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise _InvalidPDF("document-id")
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise _InvalidPDF("digest")
    return value


def _validate_page_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _InvalidPDF("page-number")
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


def _normalize_page_text(raw_text: str) -> str:
    if not raw_text or not raw_text.strip():
        return ""
    # A page is the evidence boundary. Storage admission must never discard
    # extractable text to make that evidence fit a smaller Memory body.
    return unicodedata.normalize("NFC", raw_text).strip()


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
            raise _InvalidPDF("source-identity")
        if self.materialization is not SourceMaterialization.CAPTURED:
            raise _InvalidPDF("source-materialization")
        return self


def _extract_pages(reader: PdfReader, document_id: str, document_sha256: str) -> tuple[PDFPageInput, ...]:
    if reader.is_encrypted and not reader.decrypt(""):
        raise _InvalidPDF("password")
    if not len(reader.pages):
        raise _InvalidPDF("empty")
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=document_sha256,
            page_number=index + 1,
            text=_normalize_page_text(page.extract_text() or ""),
        )
        for index, page in enumerate(reader.pages)
    )


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse all physical pages of a complete PDF document atomically."""

    if not isinstance(value, PDFDocumentInput):
        value = PDFDocumentInput.model_validate(value)
    raw = value.content
    if not raw or not raw.startswith(b"%PDF-"):
        raise _InvalidPDF("header")
    if b"%%EOF" not in raw:
        raise _InvalidPDF("truncated")

    document_sha256 = sha256(raw).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        return _extract_pages(reader, value.document_id, document_sha256)
    except _InvalidPDF:
        raise
    except Exception as exc:
        raise _InvalidPDF("parse", value.document_id) from exc


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
            raise _InvalidPDFSourceType
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


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
