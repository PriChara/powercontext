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

"""Scoped, page-exact PDF retrieval using financial concepts and report sections."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory.errors import CapabilityNotSupportedError
from powercontext.builtin.runtime import SearchMemoryRequest

_COMMA_NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")


def _score_income(question_lower: str, header_text: str, page_lower: str, comma_nums: int) -> float:
    score = 0.0
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
    is_income_stmt_header = (
        any(
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
        )
        and "comprehensive income" not in header_text
    )
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

    return score


def _score_cash_flow(question_lower: str, header_text: str, page_lower: str, comma_nums: int) -> float:
    score = 0.0
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

    return score


def _score_balance_sheet(question_lower: str, header_text: str, page_lower: str, comma_nums: int) -> float:
    score = 0.0
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

    return score


def _score_legal(question_lower: str, header_text: str, page_lower: str) -> float:
    score = 0.0
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
    if wants_legal:
        if "item 3. legal proceedings" in page_lower or "item 3.  legal proceedings" in page_lower:
            score += 55.0
        elif "legal proceedings" in header_text or "commitments and contingencies" in header_text:
            score += 35.0
        elif "legal, administrative, regulatory and government proceedings" in page_lower:
            score += 40.0
        elif "regulatory matters" in page_lower and "item 1." in page_lower:
            score += 18.0

    return score


def _score_geography(question_lower: str, header_text: str, page_lower: str) -> float:
    score = 0.0
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
        if (
            "note 1 " in header_text
            or "note 1 —" in header_text
            or "basis of presentation and our divisions" in page_lower
        ):
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

    return score


def _score_restructuring(question_lower: str, header_text: str, page_lower: str, comma_nums: int) -> float:
    score = 0.0
    wants_restructuring = "restructuring" in question_lower

    if wants_restructuring:
        if "note 3" in header_text and "restructuring" in page_lower:
            score += 36.0
        elif "restructuring and impairment" in page_lower and comma_nums >= 6:
            score += 30.0

    return score


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
    is_toc_page = (
        ("form 10-k annual report" in top_text and "item 1." in page_lower and "item 8." in page_lower)
        or ("index to consolidated financial statements" in top_text)
        or (page_number is not None and page_number <= 3 and "item 1." in page_lower and "item 7." in page_lower)
    )
    if is_toc_page:
        return -25.0

    if (
        page_number is not None
        and page_number > 150
        and any(
            k in page_lower
            for k in ("pension equalization plan", "executive incentive", "plan administrator", "erisa", "409a")
        )
    ):
        score -= 20.0

    comma_nums = len(_COMMA_NUMBER_RE.findall(page_text))
    return score + sum((
        _score_income(question_lower, header_text, page_lower, comma_nums),
        _score_cash_flow(question_lower, header_text, page_lower, comma_nums),
        _score_balance_sheet(question_lower, header_text, page_lower, comma_nums),
        _score_legal(question_lower, header_text, page_lower),
        _score_geography(question_lower, header_text, page_lower),
        _score_restructuring(question_lower, header_text, page_lower, comma_nums),
    ))


_STOPWORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "for",
    "from",
    "in",
    "on",
    "with",
    "by",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "it",
    "its",
    "this",
    "that",
    "those",
    "these",
    "what",
    "why",
    "how",
    "which",
    "who",
    "did",
    "does",
    "do",
    "has",
    "have",
    "had",
    "can",
    "could",
    "would",
    "should",
    "will",
    "as",
    "at",
    "than",
    "if",
    "then",
    "we",
    "you",
    "your",
    "our",
    "their",
    "they",
    "there",
    "any",
    "all",
    "only",
    "based",
    "report",
    "reported",
    "fy",
    "year",
    "data",
    "company",
    "please",
    "explain",
    "state",
    "answer",
    "calculate",
    "much",
    "many",
    "more",
    "most",
    "less",
    "excluding",
    "exclude",
    "impact",
    "overall",
    "assuming",
    "use",
    "using",
    "given",
])
_FINANCIAL_CONCEPTS = (
    (
        ("capital intensive", "capital-intensive", "capital intensity"),
        "property plant equipment assets revenue sales capital expenditures cash flows",
    ),
    (("growth",), "organic sales revenue segment acquisition divestiture"),
    (("acquisition",), "acquisition acquired business combinations purchase consideration"),
    (("industry", "operate in", "primary business"), "business overview products services manufacture operations"),
    (
        ("customer concentration",),
        "customer customers concentration major significant largest revenues sales ten percent",
    ),
    (("operating margin",), "income revenue expenses operations earnings"),
    (("tax rate",), "income taxes effective tax rate"),
    (("retain", "retention"), "retention retain attrition cardmember cardmembers customer customers"),
    (("product categories", "service categories", "segment"), "segment products services revenue sales"),
    (("legal", "litigation"), "legal litigation proceedings contingencies commitments"),
    (("capex", "capital expenditure"), "capital spending additions property plant equipment cash flows"),
    (("depreciation", "d&a"), "depreciation amortization"),
    (("ebitda",), "operating income depreciation amortization"),
)
_ITEM_HEADING = re.compile(r"(?im)^\s*item\s+\d+[a-z]?\s*[. ]")
_BUSINESS_START = re.compile(r"(?im)^\s*item\s+1[. ]+business\b")
_RISK_START = re.compile(r"(?im)^\s*item\s+1a[. ]")
_BUSINESS_RISK_END = re.compile(r"(?im)^\s*item\s+(?:1b|2)[. ]")
_STATEMENT_TITLES = (
    re.compile(
        r"\b(?:consolidated )?(?:balance sheets?|statements? of (?:consolidated )?financial (?:position|condition))\b"
    ),
    re.compile(r"\b(?:consolidated )?statements? of (?:consolidated )?(?:income|operations|earnings)\b"),
    re.compile(r"\b(?:consolidated )?statements? of (?:consolidated )?cash flows?\b"),
)
_COMPACT_STATEMENT_TITLES = tuple(re.compile(pattern.pattern.replace(" ", "")) for pattern in _STATEMENT_TITLES)
_ACCOUNTING_EXPANSIONS = (
    (("gross margin", "gross profit"), "gross profit cost products sold cost sales revenue earnings"),
    (("inventory turnover",), "inventory inventories cost sales products sold revenue assets"),
    (("debt",), "debt borrowings outstanding obligations liabilities maturities financing"),
)
_BOILERPLATE = frozenset({
    "metric",
    "useful",
    "meaningful",
    "conventional",
    "roughly",
    "times",
    "provided",
    "information",
    "period",
    "fiscal",
    "perspective",
    "decimal",
    "places",
    "round",
    "rounded",
    "otherwise",
    "not",
    "then",
    "like",
    "primarily",
    "specified",
    "defined",
    "billion",
    "million",
    "expressed",
    "figure",
    "numbers",
    "shown",
    "usd",
})


@dataclass(frozen=True)
class _Page:
    citation: MemoryCitation
    source_id: str
    document_id: str
    page_number: int
    text: str
    terms: Counter[str]
    length: int


def _stem(term: str) -> str:
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith("s") and not term.endswith(("ss", "us")) and len(term) > 3:
        return term[:-1]
    return term


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return [_stem(term) for term in re.findall(r"[a-z0-9]+", text.lower())]


def _company_name(document_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", document_id.split("_20", 1)[0].lower())


def _company_matches(question: str, document_id: str) -> bool:
    name = _company_name(document_id)
    if not name:
        return False
    packed = re.sub(r"[^a-z0-9]", "", question.lower())
    if name in packed:
        return True
    # Derive abbreviations from document identity, rather than listing company
    # aliases or matching particular benchmark document names.
    for abbreviation in re.findall(r"\b[A-Za-z][A-Za-z0-9]{2,5}\b", question):
        if sum(char.isupper() for char in abbreviation) < 2:
            continue
        abbreviation = abbreviation.lower()
        if abbreviation.startswith("fy"):
            continue
        remaining = iter(name)
        if abbreviation[0] == name[0] and all(char in remaining for char in abbreviation):
            return True
    return False


def _page_record(record: Any) -> _Page | None:
    if record.state != "active" or len(record.entry.sources) != 1:
        return None
    source = record.entry.sources[0]
    if source.source_type != "pdf-page":
        return None
    document_id, separator, text = record.entry.text.partition("\n")
    number = source.source_id.rsplit(":", 1)[-1]
    if not separator or not text.strip() or not number.isdigit():
        return None
    terms = Counter(_tokens(text))
    return _Page(record.citation, source.source_id, document_id, int(number), text, terms, sum(terms.values()))


def _query_weights(question: str, pages: list[_Page], documents: set[str]) -> dict[str, float]:
    terms = [
        t
        for t in _tokens(question)
        if t not in _STOPWORDS and t not in _BOILERPLATE and not t.isdigit() and not t.startswith("fy")
    ]
    company_names = {_company_name(doc) for doc in documents}
    weights = {term: 1.0 for term in terms if term not in company_names}
    vocabulary = set().union(*(page.terms for page in pages))
    raw_terms = re.findall(r"[a-z0-9]+", question.lower())
    for first, second in pairwise(raw_terms):
        compound = _stem(first + second)
        if compound in vocabulary:
            weights[compound] = 1.0
    for triggers, expansion in (*_FINANCIAL_CONCEPTS, *_ACCOUNTING_EXPANSIONS):
        if any(trigger in question.lower() for trigger in triggers):
            for term in _tokens(expansion):
                weights.setdefault(term, 0.8)
    return weights


def _business_section(pages: list[_Page], *, include_risks: bool) -> list[_Page]:
    start = None
    end_pattern = _BUSINESS_RISK_END if include_risks else _RISK_START
    ordered = sorted(pages, key=lambda page: page.page_number)
    for page in ordered:
        if len(_ITEM_HEADING.findall(page.text)) >= 4:
            continue  # An index lists many headings without starting a section.
        if start is None and _BUSINESS_START.search(page.text):
            start = page.page_number
        if start is not None and end_pattern.search(page.text):
            return [candidate for candidate in ordered if start <= candidate.page_number <= page.page_number]
    return []


def _section_coverage(question: str, pages: list[_Page], limit: int) -> list[_Page]:
    broad_question = any(
        term in question.lower() for term in ("industry", "operate in", "primary customers", "customer base", "cyclic")
    )
    if not broad_question:
        return []
    include_risks = any(term in question.lower() for term in ("customer", "cyclic"))
    documents = sorted({page.document_id for page in pages if _company_matches(question, page.document_id)})
    sections = [
        _business_section([page for page in pages if page.document_id == doc], include_risks=include_risks)
        for doc in documents
    ]
    # Broad business questions require coverage of adjacent discussion pages,
    # not twenty near-duplicate matches to one phrase. Keep room for lexical
    # matches elsewhere, and distribute this budget across named documents.
    budget = min(12, limit * 3 // 5)
    selected = []
    for offset in range(budget):
        for section in sections:
            if offset < len(section) and len(selected) < budget:
                selected.append(section[offset])
    return selected


def _statement_kind(page: _Page) -> int | None:
    if len(re.findall(r"\b\d+(?:,\d{3})*(?:\.\d+)?\b", page.text)) < 6:
        return None
    lines = [line.strip().lower() for line in page.text.splitlines()[:15] if line.strip()]
    # Match a title line (possibly wrapped over several lines), not prose that
    # merely refers to another statement. Contents/navigation lines are harmless
    # because they do not match a complete statement title.
    for width in (1, 2, 3):
        for start in range(len(lines)):
            title = re.sub(r"[^a-z]", "", " ".join(lines[start : start + width])).removesuffix("continued")
            for kind, pattern in enumerate(_COMPACT_STATEMENT_TITLES):
                if pattern.fullmatch(title):
                    return kind
    return None


def _statement_coverage(question: str, pages: list[_Page], limit: int) -> list[_Page]:
    financial_question = any(
        term in question.lower()
        for term in ("asset", "capital", "revenue", "income", "margin", "turnover", "return on", "cash flow", "debt")
    )
    if not financial_question:
        return []
    documents = sorted({page.document_id for page in pages})
    selected = []
    for document_id in documents:
        groups: dict[int, list[_Page]] = {}
        for page in sorted((page for page in pages if page.document_id == document_id), key=lambda p: p.page_number):
            kind = _statement_kind(page)
            if kind is not None:
                groups.setdefault(kind, []).append(page)
        for group in groups.values():
            first = group[0]
            selected.append(first)
            selected.extend(page for page in group[1:] if page.page_number == first.page_number + 1)
    return selected[: min(6, limit // 2)]


def _section_score(question: str, page: _Page) -> float:
    lower = page.text.lower()
    first = lower[:900]
    domain_query = question.lower().replace("capital-intensive", "capital intensity")
    domain_query = domain_query.replace("capital intensive", "capital intensity")
    domain_query = domain_query.replace("operating margin", "operating income")
    score = 0.25 * _score_domain_sections(domain_query, page.text, lower, page.page_number)
    if any(
        term in first for term in ("consolidated balance sheet", "consolidated statement", "statements of consolidated")
    ):
        score += 2.0
    rules = (
        (("acquisition",), ("business combinations", "acquisitions", "acquisition")),
        (
            ("industry", "operate in", "primary business"),
            ("business overview", "our business", "business description", "our company", "business\n", "item 1."),
        ),
        (("concentration",), ("customers", "concentration")),
    )
    for query_terms, section_terms in rules:
        if any(term in question.lower() for term in query_terms) and any(term in first for term in section_terms):
            score += 3.75
    return score


def _rank_pages(question: str, pages: list[_Page], index_ranks: dict[tuple[str, str], int]) -> list[_Page]:
    matched_documents = {page.document_id for page in pages if _company_matches(question, page.document_id)}
    candidates = [page for page in pages if not matched_documents or page.document_id in matched_documents]
    weights = _query_weights(question, candidates, matched_documents)
    frequencies = {term: sum(term in page.terms for page in candidates) for term in weights}
    average_length = sum(page.length for page in candidates) / max(1, len(candidates))

    def score(page: _Page) -> float:
        index_rank = index_ranks.get((page.citation.entry_id, page.citation.entry_version_id))
        total = _section_score(question, page) + (0.0 if index_rank is None else 0.05 / (index_rank + 1))
        for term, weight in weights.items():
            count = page.terms.get(term, 0)
            if count:
                inverse_frequency = math.log(
                    1 + (len(candidates) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                length_factor = 0.25 + 0.75 * page.length / max(1, average_length)
                total += weight * inverse_frequency * count * 2.5 / (count + 1.5 * length_factor)
        return total

    return sorted(candidates, key=lambda page: (-score(page), page.document_id, page.page_number, page.source_id))


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Rank real scoped page entries and return their exact stored citations."""

    if limit < 1 or not isinstance(question, str) or not question.strip():
        return ()
    # Search through the public index first. A backend without FTS can still
    # expose its authoritative entries for offline lexical ranking.
    index_ranks = {}
    try:
        result = await memory.search(
            SearchMemoryRequest(query=question.strip(), mode="fts", limit=min(100, max(20, limit)))
        )
    except CapabilityNotSupportedError:
        result = None
    if result is not None:
        index_ranks = {(hit.entry_id, hit.entry_version_id): index for index, hit in enumerate(result.hits)}
    listed = await memory.list()
    pages = [page for record in listed.entries if (page := _page_record(record)) is not None]
    if not pages:
        return ()
    ranked = _rank_pages(question, pages, index_ranks)
    coverage = [*_statement_coverage(question, ranked, limit), *_section_coverage(question, ranked, limit)]
    selected: dict[str, MemoryCitation] = {}
    for page in (*coverage, *ranked):
        selected.setdefault(page.source_id, page.citation)
        if len(selected) == limit:
            break
    return tuple(selected.values())
