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

"""Row-level CSV Source capture and canonical text-evidence projection."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"


def _validate_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


def _validate_primary_key_name(primary_key: str) -> str:
    if not isinstance(primary_key, str) or not primary_key or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty, non-blank string")
    return primary_key


def _validate_primary_key_value(primary_key_value: str) -> str:
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-blank string")
    return primary_key_value


def _validate_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    if not columns:
        raise ValueError("columns must not be empty")
    seen: set[str] = set()
    for column in columns:
        if not isinstance(column, str) or not column or not column.strip():
            raise ValueError("column names must be non-empty, non-blank strings")
        if column in seen:
            raise ValueError(f"duplicate column name: {column!r}")
        seen.add(column)
    return columns


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build a delimiter-safe, bounded identity for one CSV row."""
    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    encoded_doc = quote(document_id, safe="")
    encoded_key = quote(primary_key_value, safe="")
    source_id = f"csv:{encoded_doc}:{encoded_key}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"csv row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


class CSVDocumentInput(BaseModel):
    """Offline UTF-8 CSV payload with document identity and primary-key column name."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """Canonical row capture input preserving header order and raw string values."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_row_contract(self) -> CSVRowInput:
        _validate_document_id(self.document_id)
        _validate_primary_key_name(self.primary_key)
        _validate_primary_key_value(self.primary_key_value)
        _validate_columns(self.columns)
        if len(self.values) != len(self.columns):
            raise ValueError("row values length must match columns length")
        try:
            pk_index = self.columns.index(self.primary_key)
        except ValueError as error:
            raise ValueError(f"primary_key {self.primary_key!r} not found in columns") from error
        if self.values[pk_index] != self.primary_key_value:
            raise ValueError("primary_key_value must match the value at the primary_key column")
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


class CSVRowSource(Source):
    """Captured row-level Source preserving raw CSV columns and values."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_source_contract(self) -> CSVRowSource:
        expected_id = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != expected_id or self.name != expected_id:
            raise ValueError("CSVRowSource name and source_id must match csv_row_source_id")
        if self.materialization is not SourceMaterialization.CAPTURED:
            raise ValueError("CSVRowSource materialization must be CAPTURED")
        _validate_primary_key_name(self.primary_key)
        _validate_columns(self.columns)
        if len(self.values) != len(self.columns):
            raise ValueError("row values length must match columns length")
        try:
            pk_index = self.columns.index(self.primary_key)
        except ValueError as error:
            raise ValueError(f"primary_key {self.primary_key!r} not found in columns") from error
        if self.values[pk_index] != self.primary_key_value:
            raise ValueError("primary_key_value must match the value at the primary_key column")
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and atomically validate a full UTF-8 CSV document into row inputs."""
    if not isinstance(value, CSVDocumentInput):
        raise ValueError("value must be a CSVDocumentInput")
    _validate_document_id(value.document_id)
    _validate_primary_key_name(value.primary_key)
    if not isinstance(value.content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw_bytes = bytes(value.content)
    if not raw_bytes:
        raise ValueError("CSV content must not be empty")
    try:
        text = raw_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error

    if not text:
        raise ValueError("CSV content must not be empty")
    if "\x00" in text:
        raise ValueError("CSV content must not contain NUL bytes")

    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        records = list(reader)
    except csv.Error as error:
        raise ValueError(f"malformed CSV content: {error}") from error

    if not records:
        raise ValueError("CSV document must contain a header row")

    header = records[0]
    if not header:
        raise ValueError("CSV header row must not be empty")

    columns = _validate_columns(tuple(header))
    try:
        pk_index = columns.index(value.primary_key)
    except ValueError as error:
        raise ValueError(f"primary_key {value.primary_key!r} is not present in CSV header") from error

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError(f"CSV row width {len(record)} does not match header width {len(columns)}")
        pk_value = record[pk_index]
        _validate_primary_key_value(pk_value)
        if pk_value in seen_keys:
            raise ValueError(f"duplicate primary_key_value: {pk_value!r}")
        seen_keys.add(pk_value)
        csv_row_source_id(value.document_id, pk_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=pk_value,
                columns=columns,
                values=tuple(record),
            )
        )

    return tuple(rows)


class _CSVRowSourceAdapter:
    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        if not isinstance(value, CSVRowInput):
            raise ValueError("value must be a CSVRowInput")
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        if not isinstance(source, CSVRowSource):
            raise TypeError("source must be a CSVRowSource")
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row_mapping = dict(zip(source.columns, source.values, strict=True))
        content = json.dumps(row_mapping, ensure_ascii=False, separators=(",", ":"))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=content,
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row_mapping,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    _CSVRowSourceAdapter(),
    version="1",
    projections=(_CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
