"""Deterministic offline RAG evaluation harness.

Black-box HTTP client: sends requests to the real API and scores responses.
Does NOT bypass the API, query PostgreSQL directly, or import internal
retrieval/database functions.

Features:
- Golden dataset loading and validation
- Retrieval hit-rate scoring
- Citation validity scoring (source.chunk_id -> belongs to queried document_id)
- Correct rejection scoring (unanswerable questions)
- Keyword heuristic (explicitly labeled as weak/heuristic only)
- Report generation
- Clear failure handling with non-zero exit codes
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"
API_BASE = f"{BASE_URL}/documents"

TIMEOUT_SECONDS = 120

# top_k used when enumerating a document's chunk inventory. Matches the
# configured RETRIEVAL_TOP_K_MAX so no chunk of a small evaluation document
# is missed.
SEARCH_TOP_K = 20

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EvaluationCase:
    """A single evaluation case from the golden dataset."""

    id: str
    document_id: str
    query: str
    expected_answerable: bool  # True if question should have an answer in the document
    expected_has_answer: bool  # True if the question has a verifiable answer
    description: str = ""


@dataclass
class ScoringResult:
    """Scoring results for a single evaluation case."""

    case_id: str
    question: str
    answer: str | None
    success: bool  # Whether the API call succeeded (not a network failure)
    retrieval_hit: bool  # Whether at least one retrieved chunk belongs to the correct document
    citation_valid: bool  # Whether all returned sources belong to the queried document
    correct_rejection: bool  # Whether unanswerable question correctly returned no answer
    keyword_heuristic: bool | None  # Heuristic keyword match (weak metric, labeled as such)
    error: str | None  # Error message if the API call failed
    status_code: int | None = None
    expected_answerable: bool = False


@dataclass
class EvaluationReport:
    """Summary report for an evaluation run."""

    total_cases: int
    passed_cases: int
    failed_cases: int
    retrieval_hit_rate: float  # percentage of answerable cases with a retrieval hit
    citation_validity_rate: float  # percentage of source-returning cases with valid citations
    correct_rejection_rate: float  # percentage of unanswerable questions correctly rejected
    keyword_heuristic_rate: float | None  # weak keyword signal, non-authoritative
    answerable_cases: int = 0
    unanswerable_cases: int = 0
    cases_with_sources: int = 0
    cases: List[ScoringResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Golden dataset
# ---------------------------------------------------------------------------

# A small synthetic/public-safe evaluation dataset.
# Each case has a document_id, query, and expected behavior.
# The document_ids referenced here must exist in the running backend
# for the evaluation to work (the API will return 404 for missing documents).

GOLDEN_DATASET: List[EvaluationCase] = [
    EvaluationCase(
        id="case-001",
        document_id="doc-1",  # must exist in the running backend
        query="What is the cancellation policy?",
        expected_answerable=True,
        expected_has_answer=True,
        description="Question about cancellation policy in a document that contains it",
    ),
    EvaluationCase(
        id="case-002",
        document_id="doc-1",
        query="What is the capital of France?",
        expected_answerable=False,
        expected_has_answer=False,
        description="General knowledge question not in the document (should be refused)",
    ),
    EvaluationCase(
        id="case-003",
        document_id="doc-1",
        query="Tell me about the indemnification clause",
        expected_answerable=True,
        expected_has_answer=True,
        description="Question about indemnification clause in the document",
    ),
    EvaluationCase(
        id="case-004",
        document_id="doc-2",  # different document - tests data isolation
        query="What is the cancellation policy?",
        expected_answerable=True,
        expected_has_answer=True,
        description="Question about cancellation policy in doc-2",
    ),
    EvaluationCase(
        id="case-005",
        document_id="doc-2",
        query="What is the indemnification clause?",
        expected_answerable=True,
        expected_has_answer=True,
        description="Question about indemnification clause in doc-2",
    ),
    EvaluationCase(
        id="case-006",
        document_id="doc-1",
        query="",
        expected_answerable=False,
        expected_has_answer=False,
        description="Empty query (should be rejected)",
    ),
]


def load_golden_dataset(path: Optional[Path] = None) -> List[EvaluationCase]:
    """Load the golden dataset from a JSON file or fall back to the built-in set.

    The JSON file may be either a bare array of case objects or an object
    with a ``cases`` array (the Phase 14 golden-dataset shape). Each case
    must carry: id, document_id, query, expected_answerable. Malformed
    entries raise ``ValueError`` with a message naming the offending file
    and entry.

    If *path* is None the built-in dataset is returned.
    """
    if path is None:
        return GOLDEN_DATASET

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to load golden dataset from {path}: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list):
        raise ValueError(
            f"Golden dataset {path} must be a JSON array of cases or an "
            f"object with a 'cases' array."
        )

    cases: List[EvaluationCase] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(
                f"Golden dataset {path}: entry {index} is not a JSON object."
            )
        missing = [
            key
            for key in ("id", "document_id", "query", "expected_answerable")
            if key not in item
        ]
        if missing:
            raise ValueError(
                f"Golden dataset {path}: entry {index} is missing "
                f"required field(s): {', '.join(missing)}."
            )
        cases.append(
            EvaluationCase(
                id=item["id"],
                document_id=item["document_id"],
                query=item["query"],
                expected_answerable=bool(item["expected_answerable"]),
                expected_has_answer=bool(item.get("expected_has_answer", True)),
                description=item.get("description", ""),
            )
        )
    if not cases:
        raise ValueError(f"Golden dataset {path} contains no cases.")
    return cases


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def score_retrieval_hit(result: ScoringResult) -> bool:
    """Score whether the retrieval hit the correct document.

    A retrieval hit means at least one retrieved chunk belongs to the
    document being queried. This is the basic RAG effectiveness metric.
    """
    return result.retrieval_hit


def score_citation_validity(result: ScoringResult) -> bool:
    """Score whether all cited sources belong to the queried document.

    Citation validity means: every source.chunk_id returned in the answer
    belongs to the queried document_id. This is a security/regression check.
    """
    return result.citation_valid


def score_correct_rejection(result: ScoringResult) -> bool:
    """Score whether an unanswerable question was correctly rejected.

    A correct rejection means: for a question expected to have no answer
    (expected_answerable=False), the API returned answer=null or empty
    sources with context_status indicating no evidence.
    """
    return result.correct_rejection


def keyword_heuristic(result: ScoringResult) -> bool | None:
    """Explicitly weak keyword-based heuristic.

    This is NOT a strong quality metric. It simply checks if any keyword
    from the query appears in the answer text. This is included for
    completeness but should be labeled as a heuristic, not a real metric.

    Returns None if the result has no answer (correct rejection or error).
    """
    if result.answer is None:
        return None

    answer_lower = result.answer.lower()
    query_words = [w.lower() for w in result.question.split() if len(w) > 3]

    if not query_words:
        return None

    # Check if any query word appears in the answer
    matches = any(word in answer_lower for word in query_words)
    return matches


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class EvaluationClient:
    """Black-box HTTP client for the document API."""

    def __init__(self, base_url: str = API_BASE, timeout: float = TIMEOUT_SECONDS):
        api_base = base_url.rstrip("/")
        if not api_base.endswith("/documents"):
            api_base = f"{api_base}/documents"
        self.api_base = api_base
        self.root_url = api_base[: -len("/documents")]
        self.timeout = timeout
        self.client = httpx.Client(timeout=self.timeout)

    def ask(self, document_id: str, query: str) -> dict[str, Any]:
        """Send a POST /ask request and return the JSON response.

        This is a pure HTTP client - it does NOT query PostgreSQL directly,
        does NOT import internal retrieval functions, and does NOT bypass
        the actual API.
        """
        try:
            resp = self.client.post(
                f"{self.api_base}/{document_id}/ask",
                json={"query": query},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # Return error information as a dict so the caller can handle it
            return {
                "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                "detail": exc.response.text[:200] if exc.response else "unknown",
            }
        except httpx.ConnectError as exc:
            return {"error": f"Connection error: {exc}"}
        except httpx.TimeoutException as exc:
            return {"error": f"Timeout: {exc}"}
        except Exception as exc:
            return {"error": f"Unexpected error: {type(exc).__name__}: {exc}"}

    def search(
        self, document_id: str, query: str, top_k: int = SEARCH_TOP_K
    ) -> dict[str, Any]:
        """Send a POST /search request (used for citation verification)."""
        try:
            resp = self.client.post(
                f"{self.api_base}/{document_id}/search",
                json={"query": query, "top_k": top_k},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def list_documents(self) -> list[dict[str, Any]] | dict[str, Any]:
        """GET /documents — used to resolve aliases and build inventories."""
        try:
            resp = self.client.get(f"{self.root_url}/documents")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def close(self) -> None:
        self.client.close()


# Queries used to enumerate a document's chunk inventory for citation
# verification. Retrieval is document-scoped and returns up to top_k chunks
# with no similarity cut-off, so for small evaluation documents this returns
# every chunk the document owns.
INVENTORY_QUERIES = [
    "cancellation policy terms conditions",
    "section clause paragraph details",
    "What is the policy about?",
]


def _chunk_inventory(
    client: EvaluationClient, document_id: str, extra_queries: list[str]
) -> set[str]:
    """Return the set of chunk ids known to belong to *document_id*."""
    ids: set[str] = set()
    for query in list(dict.fromkeys([*INVENTORY_QUERIES, *extra_queries])):
        response = client.search(document_id, query, top_k=SEARCH_TOP_K)
        for chunk in response.get("results", []) or []:
            if chunk.get("document_id") == document_id:
                ids.add(str(chunk["chunk_id"]))
    return ids


def _resolve_document_id(client: EvaluationClient, reference: str) -> str:
    """Resolve a golden-dataset document reference to a real document id.

    Accepts either a UUID or a document filename/alias. Raises ValueError
    with a clear message when the reference cannot be resolved.
    """
    try:
        uuid.UUID(reference)
        return reference
    except (ValueError, AttributeError, TypeError):
        pass

    documents = client.list_documents()
    if isinstance(documents, dict) and "error" in documents:
        raise ValueError(
            f"Cannot resolve document reference {reference!r}: {documents['error']}"
        )
    for doc in documents:
        if doc.get("filename") == reference:
            return str(doc["id"])
    known = ", ".join(sorted(str(d.get("filename")) for d in documents)) or "<none>"
    raise ValueError(
        f"Document reference {reference!r} is neither a UUID nor a known "
        f"document filename (known: {known})."
    )


# ---------------------------------------------------------------------------
# Evaluation orchestration
# ---------------------------------------------------------------------------


def run_evaluation(
    cases: Optional[List[EvaluationCase]] = None,
    dataset_path: Optional[Path] = None,
    client: Optional[EvaluationClient] = None,
) -> EvaluationReport:
    """Run the deterministic evaluation harness.

    Args:
        cases: Optional list of evaluation cases. If None, loads from golden dataset.
        dataset_path: Optional path to a JSON file overriding the golden dataset.
        client: Optional EvaluationClient instance. If None, a new one is created.

    Returns:
        EvaluationReport with scoring results for all cases.
    """
    if client is None:
        client = EvaluationClient()

    if cases is None:
        cases = load_golden_dataset(dataset_path)

    report = EvaluationReport(
        total_cases=len(cases),
        passed_cases=0,
        failed_cases=0,
        retrieval_hit_rate=0.0,
        citation_validity_rate=0.0,
        correct_rejection_rate=0.0,
        keyword_heuristic_rate=None,
    )

    results: List[ScoringResult] = []

    # Resolve every document reference up front so a missing/unknown document
    # produces one clear error instead of one HTTP 404 per case.
    resolved_ids: dict[str, str] = {}
    for case in cases:
        if case.document_id in resolved_ids:
            continue
        try:
            resolved_ids[case.document_id] = _resolve_document_id(
                client, case.document_id
            )
        except ValueError as exc:
            resolved_ids[case.document_id] = ""
            report.errors.append(str(exc))

    all_document_ids = [
        doc_id for doc_id in resolved_ids.values() if doc_id
    ]

    answerable_cases = 0
    retrieval_hits = 0
    unanswerable_cases = 0
    correct_rejections = 0
    cases_with_sources = 0
    citation_valid_cases = 0

    for case in cases:
        document_id = resolved_ids.get(case.document_id, "")

        # Send the request
        if not document_id:
            result = ScoringResult(
                case_id=case.id,
                question=case.query,
                answer=None,
                success=False,
                retrieval_hit=False,
                citation_valid=False,
                correct_rejection=False,
                keyword_heuristic=None,
                error=f"Unresolvable document reference: {case.document_id!r}",
                status_code=None,
                expected_answerable=case.expected_answerable,
            )
            results.append(result)
            report.errors.append(
                f"Case {case.id} ({case.document_id}): unresolvable document "
                f"reference"
            )
            continue

        api_response = client.ask(document_id, case.query)

        # Determine if the call succeeded
        if "error" in api_response:
            result = ScoringResult(
                case_id=case.id,
                question=case.query,
                answer=None,
                success=False,
                retrieval_hit=False,
                citation_valid=False,
                correct_rejection=False,
                keyword_heuristic=None,
                error=api_response["error"],
                status_code=None,
                expected_answerable=case.expected_answerable,
            )
            results.append(result)
            report.errors.append(
                f"Case {case.id} ({case.document_id}): {api_response['error']}"
            )
            continue

        # Extract relevant fields from the API response
        answer = api_response.get("answer")
        sources = api_response.get("sources") or []
        context_status = api_response.get("context_status", "")
        source_ids = {str(s.get("chunk_id")) for s in sources if s.get("chunk_id")}

        retrieval_hit = False
        citation_valid = False
        correct_rejection = False

        if case.expected_answerable:
            answerable_cases += 1
            answered = bool(answer)

            if sources:
                cases_with_sources += 1

                # Deterministic, black-box citation verification: enumerate the
                # chunks each document exposes through /search (retrieval is
                # document-scoped, so this proves ownership without touching
                # the database), then check every cited chunk against it.
                inventory = _chunk_inventory(client, document_id, [case.query])
                native = source_ids & inventory
                unmatched = source_ids - inventory
                foreign: set[str] = set()

                if unmatched:
                    for other_id in all_document_ids:
                        if other_id == document_id:
                            continue
                        other_inventory = _chunk_inventory(
                            client, other_id, [case.query]
                        )
                        foreign |= unmatched & other_inventory
                        unmatched -= other_inventory
                        if not unmatched:
                            break

                retrieval_hit = answered and bool(native) and not foreign
                citation_valid = not unmatched and not foreign
                if citation_valid:
                    citation_valid_cases += 1
            else:
                retrieval_hit = False

            # N/A for answerable cases
            correct_rejection = True
        else:
            unanswerable_cases += 1
            # A "confident" answer is a non-empty answer backed by an ok
            # context status AND sources. Anything else counts as a rejection
            # of the out-of-scope question.
            has_grounded_answer = bool(answer) and context_status == "ok" and bool(sources)
            correct_rejection = not has_grounded_answer
            if correct_rejection:
                correct_rejections += 1

        # Keyword heuristic (explicitly weak, non-authoritative signal)
        kw = keyword_heuristic(
            ScoringResult(
                case_id=case.id,
                question=case.query,
                answer=answer,
                success=True,
                retrieval_hit=retrieval_hit,
                citation_valid=citation_valid,
                correct_rejection=correct_rejection,
                keyword_heuristic=None,
                error=None,
                status_code=api_response.get("status_code"),
            )
        )

        result = ScoringResult(
            case_id=case.id,
            question=case.query,
            answer=answer,
            success=True,
            retrieval_hit=retrieval_hit,
            citation_valid=citation_valid,
            correct_rejection=correct_rejection,
            keyword_heuristic=kw,
            error=None,
            status_code=api_response.get("status_code"),
            expected_answerable=case.expected_answerable,
        )
        results.append(result)

        if retrieval_hit:
            retrieval_hits += 1

    # Calculate rates. Denominators follow the Phase 14 scoring rules:
    #   retrieval hit-rate  -> hits / answerable cases
    #   citation validity   -> valid / cases that returned sources
    #   correct rejection   -> rejections / unanswerable cases
    report.total_cases = len(results)
    report.answerable_cases = answerable_cases
    report.unanswerable_cases = unanswerable_cases
    report.cases_with_sources = cases_with_sources
    report.retrieval_hit_rate = (
        round(retrieval_hits / answerable_cases * 100, 2)
        if answerable_cases
        else 0.0
    )
    report.citation_validity_rate = (
        round(citation_valid_cases / cases_with_sources * 100, 2)
        if cases_with_sources
        else 0.0
    )
    report.correct_rejection_rate = (
        round(correct_rejections / unanswerable_cases * 100, 2)
        if unanswerable_cases
        else 0.0
    )

    # Keyword heuristic rate
    kw_scores = [r.keyword_heuristic for r in results if r.keyword_heuristic is not None]
    if kw_scores:
        report.keyword_heuristic_rate = round(sum(kw_scores) / len(kw_scores) * 100, 2)
    else:
        report.keyword_heuristic_rate = None

    report.cases = results
    report.passed_cases = sum(
        1
        for case_result, case in zip(results, cases)
        if case_result.success
        and (
            (case_result.retrieval_hit and case_result.citation_valid)
            if case.expected_answerable
            else case_result.correct_rejection
        )
    )
    report.failed_cases = report.total_cases - report.passed_cases
    return report


def generate_report(report: EvaluationReport) -> str:
    """Generate a human-readable text report from the evaluation results."""
    lines: List[str] = []

    lines.append("=" * 60)
    lines.append("RAG EVALUATION REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Total cases: {report.total_cases}")
    lines.append(
        f"  (answerable={report.answerable_cases}, "
        f"unanswerable={report.unanswerable_cases}, "
        f"returned sources={report.cases_with_sources})"
    )
    lines.append(f"Passed: {report.passed_cases}   Failed: {report.failed_cases}")
    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Retrieval hit-rate: {report.retrieval_hit_rate}% (of answerable cases)")
    lines.append(f"  - Citation validity:  {report.citation_validity_rate}% (of cases returning sources)")
    lines.append(f"  - Correct rejection:  {report.correct_rejection_rate}% (of unanswerable cases)")
    if report.keyword_heuristic_rate is not None:
        lines.append(f"  - Keyword heuristic:  {report.keyword_heuristic_rate}% (WEAK HEURISTIC - for reference only)")
    lines.append("")

    failures = [
        case
        for case in report.cases
        if case.error
        or (
            (case.retrieval_hit and case.citation_valid)
            if case.expected_answerable
            else case.correct_rejection
        )
        is False
    ]
    if failures:
        lines.append(f"Failures ({len(failures)}):")
        lines.append("-" * 60)
        for case in failures:
            reason = case.error or (
                "retrieval/citation failure"
                if case.expected_answerable
                else "not rejected"
            )
            lines.append(f"  [{case.case_id}] {case.question} -> {reason}")
        lines.append("")
    elif report.errors:
        lines.append(f"Errors ({len(report.errors)}):")
        for error in report.errors:
            lines.append(f"  - {error}")
        lines.append("")

    lines.append("Per-case results:")
    lines.append("-" * 60)
    for case in report.cases:
        lines.append(f"  [{case.case_id}] {case.question}")
        if case.error:
            lines.append(f"    ERROR: {case.error}")
            lines.append(f"    success: NO  (network/backend failure)")
        else:
            lines.append(f"    answer: {case.answer[:80] if case.answer else 'None'}...")
            lines.append(f"    retrieval_hit: {'YES' if case.retrieval_hit else 'NO'}")
            lines.append(f"    citation_valid: {'YES' if case.citation_valid else 'NO'}")
            lines.append(f"    correct_rejection: {'YES' if case.correct_rejection else 'NO'}")
            if case.keyword_heuristic is not None:
                kw_label = "YES (heuristic)" if case.keyword_heuristic else "NO (heuristic)"
                lines.append(f"    keyword_heuristic: {kw_label}  *(weak heuristic, not a strong metric)*")
        lines.append("")

    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    """CLI entry point for the evaluation harness.

    Usage:
        python evaluate.py [--dataset PATH] [--base-url URL]
                           [--report-dir DIR]

    Returns an exit code:
    - 0: evaluation completed successfully
    - 1: hard failure (backend/Ollama unreachable, document missing, ...)
    - 2: usage error / malformed golden dataset
    """
    import time
    from dataclasses import asdict

    try:
        from app.config import settings as app_settings
    except Exception:  # pragma: no cover - allows standalone use
        app_settings = None

    args = argv if argv is not None else sys.argv[1:]

    dataset_path: Optional[Path] = None
    base_url: str = API_BASE
    report_dir: Optional[Path] = None

    i = 0
    while i < len(args):
        if args[i] == "--dataset" and i + 1 < len(args):
            dataset_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--base-url" and i + 1 < len(args):
            base_url = args[i + 1]
            i += 2
        elif args[i] == "--report-dir" and i + 1 < len(args):
            report_dir = Path(args[i + 1])
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__ or "python evaluate.py [--dataset PATH] [--base-url URL] [--report-dir DIR]")
            return 0
        else:
            print(f"[evaluate] Unknown argument: {args[i]}", file=sys.stderr)
            print(
                "Usage: python evaluate.py [--dataset PATH] [--base-url URL] "
                "[--report-dir DIR]",
                file=sys.stderr,
            )
            return 2

    # Default golden dataset: the Phase 14 sample set when it exists.
    if dataset_path is None and app_settings is not None:
        preferred = Path(app_settings.EVAL_GOLDEN_SET_DIR) / "sample_insurance_policy.json"
        if preferred.exists():
            dataset_path = preferred

    # Load golden dataset (clear error + exit 2 when malformed)
    try:
        cases = load_golden_dataset(dataset_path)
    except ValueError as exc:
        print(f"[evaluate] {exc}", file=sys.stderr)
        return 2

    # Run evaluation
    client = EvaluationClient(base_url=base_url)
    try:
        report = run_evaluation(cases=cases, dataset_path=dataset_path, client=client)
    except httpx.ConnectError as exc:
        print(f"[evaluate] Backend unreachable at {base_url}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    # Generate and print report
    report_text = generate_report(report)
    print(report_text)

    # Persist the report next to the golden sets (Markdown + JSON)
    if report_dir is None and app_settings is not None:
        report_dir = Path(app_settings.EVAL_REPORT_DIR)
    if report_dir is not None:
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            alias = dataset_path.stem if dataset_path else "builtin"
            md_path = report_dir / f"{stamp}_{alias}.md"
            json_path = report_dir / f"{stamp}_{alias}.json"
            md_path.write_text(report_text, encoding="utf-8")
            json_path.write_text(
                json.dumps(asdict(report), indent=2, default=str),
                encoding="utf-8",
            )
            print(f"\n[evaluate] Report written to {md_path}")
            print(f"[evaluate] Report written to {json_path}")
        except OSError as exc:
            print(f"[evaluate] Could not write report: {exc}", file=sys.stderr)

    # Determine exit code
    if report.errors:
        # Hard failures: backend/Ollama unreachable or document missing
        print(
            f"\n[evaluate] {len(report.errors)} case(s) failed due to "
            "backend/document issues.",
            file=sys.stderr,
        )
        for error in report.errors:
            print(f"[evaluate]   - {error}", file=sys.stderr)
        return 1

    if report.total_cases > 0 and report.retrieval_hit_rate == 0.0:
        # All retrievals failed - this is a warning, not a hard failure
        print(
            f"\n[evaluate] Warning: 0% retrieval hit-rate. The backend may not have "
            "the expected documents loaded.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())