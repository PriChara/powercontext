"""Whole PDF page evidence survives extraction and real Runtime persistence."""

from __future__ import annotations

import asyncio
import io
from hashlib import sha256
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _text_pdf(page_texts: list[str]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=1200, height=2000)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        stream = DecodedStreamObject()
        lines = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in text.splitlines()]
        operators = " ".join(f"({line}) Tj T*" for line in lines)
        stream.set_data(f"BT /F1 1 Tf 1 1800 Td 2 TL {operators} ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _long_text_pdf() -> bytes:
    return _text_pdf([" ".join(f"revenue{i:04d}" for i in range(2000))])


def test_pdf_extraction_preserves_text_beyond_memory_summary_limit() -> None:
    raw = _long_text_pdf()
    expected = PdfReader(io.BytesIO(raw)).pages[0].extract_text().strip()
    assert len(expected.encode("utf-8")) > 8192
    pages = parse_pdf_document(PDFDocumentInput(document_id="long-report", content=raw))
    assert len(pages) == 1
    assert pages[0].document_sha256 == sha256(raw).hexdigest()
    assert pages[0].text == expected
    assert pages[0].text.endswith("revenue1999")


class _FullPagePipeline:
    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
        )


def test_full_page_can_be_cited_after_runtime_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        raw = _long_text_pdf()
        page = parse_pdf_document(PDFDocumentInput(document_id="long-report", content=raw))[0]
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        source = await registry.resolve(page)
        observation = project_source_for_transport(registry, source)
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="PDF", summary="Full page regression", idempotency_key="pdf-regression")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
        async with open_builtin_runtime(config, candidate_pipeline=_FullPagePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).processed
            assert not (await memory.flush()).processed
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "revenue1999", limit=1)
            assert len(citations) == 1
            record = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
            assert record.entry.text == f"long-report\n{page.text}"
            assert record.entry.sources[0].source_id == observation.name

    asyncio.run(scenario())


def test_financial_concepts_and_company_abbreviation_select_real_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        document_id = "NORTHHARBORCOMPANY_2024_10K"
        report = _text_pdf([
            "PART I\nItem 1. Business\nNorth Harbor Company makes industrial materials.\nItem 1A. Risk Factors",
            "CONSOLIDATED\nBALANCE SHEETS\nProperty plant equipment 1,000 2,000 3,000\n"
            "Current assets 4,000 5,000\nTotal assets 6,000\nTotal liabilities 7,000",
            *[f"Capital intensive business investment activity {i}. " * 30 for i in range(25)],
        ])
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        observations = [
            project_source_for_transport(registry, await registry.resolve(page))
            for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=report))
        ]
        other = parse_pdf_document(
            PDFDocumentInput(
                document_id="EASTFIELDCOMPANY_2024_10K",
                content=_text_pdf([
                    "Consolidated Balance Sheets\nCapital intensive business\n"
                    "Property plant equipment 1,000 2,000 3,000 4,000 5,000 6,000\nTotal assets 7,000"
                ]),
            )
        )[0]
        observations.append(project_source_for_transport(registry, await registry.resolve(other)))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retrieval.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_FullPagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Independent companies", idempotency_key="retrieval")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass
            citations = await search_pdf_pages(memory, "Is NhCo a capital-intensive business?", limit=20)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert len(entries) == 20
            assert all(entry.text.startswith(document_id + "\n") for entry in entries)
            assert any("consolidated balance sheets" in " ".join(entry.text.lower().split()) for entry in entries)

    asyncio.run(scenario())
