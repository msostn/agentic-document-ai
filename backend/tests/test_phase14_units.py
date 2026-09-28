"""Unit tests for the Phase 14 timing/metrics utilities and eval scoring.

These tests are pure unit tests: no live server, no database, no Ollama.

Run with:
    python -m pytest tests/test_phase14_units.py -v
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from app import timing
from app.config import settings
from evaluate import (
    EvaluationCase,
    EvaluationClient,
    EvaluationReport,
    ScoringResult,
    generate_report,
    keyword_heuristic,
    load_golden_dataset,
    run_evaluation,
)

ALLOWED_LOG_KEYS = {
    "request_id",
    "document_id",
    "stage",
    "duration_ms",
    "outcome",
    "error_category",
}

LOG_RECORD_BASE_KEYS = set(vars(logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None))) | {
    # added by logging.Handler.format() / logging.Formatter
    "message",
    "asctime",
}


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """Snapshot and restore the process-local metrics accumulator."""
    saved = dict(timing._runtimeMetrics)
    timing._runtimeMetrics.update(
        {key: 0 for key in timing._runtimeMetrics if key != "uptime_seconds"}
    )
    yield
    timing._runtimeMetrics.clear()
    timing._runtimeMetrics.update(saved)


@pytest.fixture(autouse=True)
def _restore_flags():
    saved_enabled = timing.ENABLE_METRICS_ENDPOINT
    saved_logging = settings.ENABLE_REQUEST_LOGGING
    yield
    timing.ENABLE_METRICS_ENDPOINT = saved_enabled
    settings.ENABLE_REQUEST_LOGGING = saved_logging


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Request correlation IDs
# ---------------------------------------------------------------------------


class TestRequestIds:
    def test_set_get_clear_roundtrip(self):
        timing.set_request_id("abc-123")
        assert timing.get_request_id() == "abc-123"
        timing.clear_request_id()
        assert timing.get_request_id() != "abc-123"

    def test_ids_are_unique(self):
        ids = {str(uuid.uuid4()) for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Context-building accumulator
# ---------------------------------------------------------------------------


class TestContextAccumulator:
    def test_pop_returns_accumulated_and_resets(self):
        timing.add_context_building_ms(12.5)
        timing.add_context_building_ms(7.5)
        assert timing.pop_context_building_ms() == pytest.approx(20.0)
        assert timing.pop_context_building_ms() == pytest.approx(0.0)

    def test_zero_is_default(self):
        assert timing.pop_context_building_ms() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Metrics accumulator
# ---------------------------------------------------------------------------


class TestMetricsAccumulator:
    def test_zero_state_is_sane(self):
        snapshot = timing.get_metrics_snapshot()
        assert snapshot["total_requests"] == 0
        assert snapshot["total_errors"] == 0
        assert snapshot["average_latency_ms"] == 0.0
        assert snapshot["average_retrieval_ms"] == 0.0
        assert snapshot["uptime_seconds"] >= 0.0

    def test_record_request_increments_counters(self):
        timing.record_request(total_ms=100.0, error=False)
        timing.record_request(total_ms=300.0, error=True)
        snapshot = timing.get_metrics_snapshot()
        assert snapshot["total_requests"] == 2
        assert snapshot["total_errors"] == 1
        assert snapshot["total_request_ms"] == pytest.approx(400.0)
        assert snapshot["average_latency_ms"] == pytest.approx(200.0)

    def test_add_stage_metrics_does_not_count_requests(self):
        timing.record_request(total_ms=50.0)
        timing.add_stage_metrics(
            retrieval_ms=10.0,
            context_building_ms=5.0,
            generation_ms=20.0,
        )
        snapshot = timing.get_metrics_snapshot()
        assert snapshot["total_requests"] == 1
        assert snapshot["total_retrieval_ms"] == pytest.approx(10.0)
        assert snapshot["total_context_building_ms"] == pytest.approx(5.0)
        assert snapshot["total_generation_ms"] == pytest.approx(20.0)
        assert snapshot["average_retrieval_ms"] == pytest.approx(10.0)

    def test_add_stage_metrics_ignores_zero_values(self):
        timing.add_stage_metrics()
        snapshot = timing.get_metrics_snapshot()
        assert snapshot["total_retrieval_ms"] == 0.0
        assert snapshot["total_generation_ms"] == 0.0

    def test_snapshot_exposes_only_aggregate_numbers(self):
        timing.record_request(total_ms=1.0, error=True)
        timing.add_stage_metrics(retrieval_ms=1.0, generation_ms=1.0)
        snapshot = timing.get_metrics_snapshot()
        allowed = {
            "uptime_seconds",
            "total_requests",
            "total_errors",
            "total_retrieval_ms",
            "total_context_building_ms",
            "total_generation_ms",
            "total_request_ms",
            "average_latency_ms",
            "average_retrieval_ms",
            "average_context_building_ms",
            "average_generation_ms",
        }
        assert set(snapshot) == allowed
        assert all(
            isinstance(value, (int, float)) for value in snapshot.values()
        )

    def test_metrics_endpoint_toggle(self):
        timing.set_metrics_enabled(False)
        assert timing.is_metrics_enabled() is False
        timing.set_metrics_enabled(True)
        assert timing.is_metrics_enabled() is True


# ---------------------------------------------------------------------------
# Timing context managers
# ---------------------------------------------------------------------------


class TestTimingContextManagers:
    def test_timed_block_records_duration(self):
        with timing.timed_block("retrieval") as timer:
            sum(range(10_000))
        assert timer.ms >= 0.0

    def test_timed_block_does_not_swallow_exceptions(self):
        with pytest.raises(ValueError):
            with timing.timed_block("retrieval") as timer:
                raise ValueError("boom")
        assert timer.ms >= 0.0

    def test_request_timings_does_not_swallow_exceptions(self):
        with pytest.raises(RuntimeError):
            with timing.request_timings() as timings:
                raise RuntimeError("boom")
        assert timings["total_ms"] >= 0.0


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    def test_log_query_event_only_emits_safe_keys(self):
        logger = logging.getLogger("phase14.test.safe")
        handler = _RecordingHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            timing.log_query_event(
                logger,
                level=logging.INFO,
                request_id="req-1",
                document_id="doc-1",
                stage="retrieval",
                duration_ms=12.0,
                outcome="success",
            )
        finally:
            logger.removeHandler(handler)

        assert len(handler.records) == 1
        record = handler.records[0]
        extras = set(record.__dict__) - LOG_RECORD_BASE_KEYS
        assert extras <= ALLOWED_LOG_KEYS, f"unexpected log fields: {extras}"
        assert record.getMessage() == "query_event"
        assert record.request_id == "req-1"
        assert record.stage == "retrieval"
        assert record.duration_ms == 12.0

    def test_request_logging_flag_silences_info_but_not_errors(self):
        logger = logging.getLogger("phase14.test.gated")
        handler = _RecordingHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        settings.ENABLE_REQUEST_LOGGING = False
        try:
            timing.log_query_event(logger, level=logging.INFO, stage="retrieval")
            assert handler.records == []
            timing.log_query_event(logger, level=logging.ERROR, stage="generation")
            assert len(handler.records) == 1
        finally:
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Eval scoring
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal stand-in for EvaluationClient (no HTTP)."""

    def __init__(self, doc_id: str, chunk_id: str, leak: bool = False):
        self.doc_id = doc_id
        self.chunk_id = chunk_id
        self.leak = leak
        self.response: dict = {}

    def ask(self, document_id: str, query: str) -> dict:
        return self.response

    def search(self, document_id: str, query: str, top_k: int = 20) -> dict:
        chunk_id = "leaked-chunk" if self.leak else self.chunk_id
        return {
            "document_id": document_id,
            "results": [
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "chunk_index": 0,
                    "page_number": 1,
                    "content": "x",
                    "distance": 0.1,
                    "similarity": 0.9,
                }
            ],
        }

    def list_documents(self) -> list:
        return []

    def close(self) -> None:
        return None


class TestEvalScoring:
    def test_keyword_heuristic_none_without_answer(self):
        result = ScoringResult(
            case_id="c1",
            question="What is the deductible?",
            answer=None,
            success=True,
            retrieval_hit=False,
            citation_valid=False,
            correct_rejection=True,
            keyword_heuristic=None,
            error=None,
        )
        assert keyword_heuristic(result) is None

    def test_keyword_heuristic_detects_keyword(self):
        result = ScoringResult(
            case_id="c1",
            question="What is the deductible amount?",
            answer="The deductible is 500 dollars.",
            success=True,
            retrieval_hit=True,
            citation_valid=True,
            correct_rejection=True,
            keyword_heuristic=None,
            error=None,
        )
        assert keyword_heuristic(result) is True

    def test_run_evaluation_returns_a_report(self):
        doc_id = str(uuid.uuid4())
        client = _FakeClient(doc_id, str(uuid.uuid4()))
        client.response = {
            "answer": "The deductible is 500 dollars.",
            "sources": [
                {"chunk_id": client.chunk_id, "chunk_index": 0, "page_number": 1}
            ],
            "context_status": "ok",
        }
        cases = [
            EvaluationCase(
                id="c1",
                document_id=doc_id,
                query="What is the deductible?",
                expected_answerable=True,
                expected_has_answer=True,
            )
        ]
        report = run_evaluation(cases=cases, client=client)  # type: ignore[arg-type]
        assert isinstance(report, EvaluationReport)
        assert report.total_cases == 1
        assert report.retrieval_hit_rate == 100.0
        assert report.citation_validity_rate == 100.0

    def test_citation_leak_is_detected(self):
        doc_id = str(uuid.uuid4())
        client = _FakeClient(doc_id, str(uuid.uuid4()), leak=True)
        client.response = {
            "answer": "The deductible is 500 dollars.",
            "sources": [
                {"chunk_id": "cited-chunk", "chunk_index": 0, "page_number": 1}
            ],
            "context_status": "ok",
        }
        cases = [
            EvaluationCase(
                id="c1",
                document_id=doc_id,
                query="What is the deductible?",
                expected_answerable=True,
                expected_has_answer=True,
            )
        ]
        report = run_evaluation(cases=cases, client=client)  # type: ignore[arg-type]
        assert report.cases[0].citation_valid is False
        assert report.cases[0].retrieval_hit is False

    def test_correct_rejection_scoring(self):
        doc_id = str(uuid.uuid4())
        client = _FakeClient(doc_id, str(uuid.uuid4()))
        client.response = {
            "answer": "I couldn't find enough information in the document to answer that.",
            "sources": [],
            "context_status": "below_similarity_threshold",
        }
        cases = [
            EvaluationCase(
                id="c1",
                document_id=doc_id,
                query="What is the weather tomorrow?",
                expected_answerable=False,
                expected_has_answer=False,
            )
        ]
        report = run_evaluation(cases=cases, client=client)  # type: ignore[arg-type]
        assert report.cases[0].correct_rejection is True
        assert report.correct_rejection_rate == 100.0
        assert report.unanswerable_cases == 1

    def test_malformed_golden_dataset_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text('[{"id": "x"}]', encoding="utf-8")
        with pytest.raises(ValueError, match="missing required field"):
            load_golden_dataset(bad)

    def test_generate_report_includes_denominators(self):
        report = EvaluationReport(
            total_cases=3,
            passed_cases=2,
            failed_cases=1,
            retrieval_hit_rate=100.0,
            citation_validity_rate=100.0,
            correct_rejection_rate=100.0,
            keyword_heuristic_rate=50.0,
            answerable_cases=2,
            unanswerable_cases=1,
            cases_with_sources=2,
        )
        text = generate_report(report)
        assert "answerable=2" in text
        assert "unanswerable=1" in text
        assert "Passed: 2" in text


def test_evaluation_client_defaults_to_documents_api():
    client = EvaluationClient(base_url="http://example.test:8000")
    assert client.api_base == "http://example.test:8000/documents"
    assert client.root_url == "http://example.test:8000"
    client.close()
