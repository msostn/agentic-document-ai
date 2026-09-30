"""Unit tests for the Phase 15 retrieval quality change.

These tests are pure unit tests: no live server, no database, no embedding
model. They guard the configuration, the chunk-boundary regression that
caused the Phase 14 case-005 failure, and the expanded golden dataset.

The live embedding/threshold behaviour is covered by
scripts/test_phase15_retrieval_quality.py.

Run with:
    python -m pytest tests/test_phase15_units.py -v
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from app.config import BACKEND_DIR, settings
from app.rag.chunker import chunk_document
from evaluate import load_golden_dataset

sys.path.insert(0, str(Path(BACKEND_DIR) / "scripts"))

from create_phase14_sample_document import SAMPLE_TEXT  # noqa: E402

BACKEND = Path(BACKEND_DIR)
ENV_EXAMPLE = BACKEND / ".env.example"
ENV_LOCAL = BACKEND / ".env"
GOLDEN_SET = Path(settings.EVAL_GOLDEN_SET_DIR) / "sample_insurance_policy.json"

PHASE_14_CHUNK_SIZE = 800
PHASE_14_CHUNK_OVERLAP = 150
PHASE_15_CHUNK_SIZE = 500
PHASE_15_CHUNK_OVERLAP = 100
RAG_MIN_SIMILARITY = 0.30

SECTION_3_SENTENCE = (
    "Section 3. Waiting period. A waiting period of thirty (30) days applies "
    "to all claims arising from pre-existing conditions."
)
SECTION_MARKERS = (
    "Section 1.",
    "Section 2.",
    "Section 3.",
    "Section 4.",
    "Section 5.",
    "Section 6.",
)

REFUSAL_CASE_IDS = {"case-006", "case-007", "case-019", "case-020", "case-021"}


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="module")
def sample_chunks():
    import uuid

    return chunk_document(uuid.UUID(int=0), [(1, SAMPLE_TEXT)])


@pytest.fixture(scope="module")
def golden_cases():
    return load_golden_dataset(GOLDEN_SET)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_chunking_configuration_matches_phase15():
    assert settings.CHUNK_SIZE == PHASE_15_CHUNK_SIZE
    assert settings.CHUNK_OVERLAP == PHASE_15_CHUNK_OVERLAP
    assert settings.MIN_CHUNK_SIZE < settings.CHUNK_SIZE


def test_phase14_chunking_values_were_replaced_not_appended():
    assert settings.CHUNK_SIZE != PHASE_14_CHUNK_SIZE
    assert settings.CHUNK_OVERLAP != PHASE_14_CHUNK_OVERLAP


def test_similarity_threshold_was_not_lowered():
    assert settings.RAG_MIN_SIMILARITY == pytest.approx(RAG_MIN_SIMILARITY)


def test_top_k_configuration_unchanged():
    assert settings.RETRIEVAL_TOP_K_DEFAULT == 5
    assert settings.RETRIEVAL_TOP_K_MAX == 20


def test_env_example_documents_phase15_config():
    values = _parse_env(ENV_EXAMPLE)
    assert values.get("CHUNK_SIZE") == str(PHASE_15_CHUNK_SIZE)
    assert values.get("CHUNK_OVERLAP") == str(PHASE_15_CHUNK_OVERLAP)
    assert values.get("RAG_MIN_SIMILARITY") == "0.30"
    assert values.get("RAG_CONTEXT_MAX_CHARS")


def test_local_env_matches_phase15_chunking():
    if not ENV_LOCAL.exists():  # .env is gitignored and optional
        pytest.skip("backend/.env not present in this checkout")
    values = _parse_env(ENV_LOCAL)
    assert values.get("CHUNK_SIZE") == str(PHASE_15_CHUNK_SIZE)
    assert values.get("CHUNK_OVERLAP") == str(PHASE_15_CHUNK_OVERLAP)
    assert values.get("RAG_MIN_SIMILARITY") == "0.30"


def test_chunker_defaults_read_from_settings():
    params = inspect.signature(chunk_document).parameters
    assert params["chunk_size"].default == settings.CHUNK_SIZE
    assert params["chunk_overlap"].default == settings.CHUNK_OVERLAP
    assert params["min_chunk_size"].default == settings.MIN_CHUNK_SIZE


# ---------------------------------------------------------------------------
# Chunk-boundary regression (the mechanism behind the case-005 failure)
# ---------------------------------------------------------------------------


def test_sample_document_splits_into_more_than_two_chunks(sample_chunks):
    assert len(sample_chunks) >= 3


def test_no_chunk_exceeds_the_configured_budget(sample_chunks):
    assert all(len(c.content) <= settings.CHUNK_SIZE for c in sample_chunks)


def test_case005_answer_sentence_is_not_split_across_chunks(sample_chunks):
    holders = [c for c in sample_chunks if SECTION_3_SENTENCE in c.content]
    assert len(holders) == 1


def test_all_sections_survive_chunking(sample_chunks):
    joined = " ".join(c.content for c in sample_chunks)
    missing = [marker for marker in SECTION_MARKERS if marker not in joined]
    assert not missing, f"sections dropped during chunking: {missing}"


def test_chunking_is_deterministic(sample_chunks):
    import uuid

    again = chunk_document(uuid.UUID(int=0), [(1, SAMPLE_TEXT)])
    assert [c.content for c in again] == [c.content for c in sample_chunks]


# ---------------------------------------------------------------------------
# Golden dataset expansion
# ---------------------------------------------------------------------------


def test_golden_dataset_exists():
    assert GOLDEN_SET.exists()


def test_golden_dataset_is_expanded_beyond_phase14(golden_cases):
    assert len(golden_cases) >= 15


def test_golden_case_ids_are_unique(golden_cases):
    ids = [c.id for c in golden_cases]
    assert len(ids) == len(set(ids))


def test_case005_is_still_scored(golden_cases):
    case = next(c for c in golden_cases if c.id == "case-005")
    assert case.expected_answerable is True


def test_answerable_and_refusal_balances(golden_cases):
    answerable = [c for c in golden_cases if c.expected_answerable]
    refusals = [c for c in golden_cases if not c.expected_answerable]
    assert len(answerable) >= 10
    assert len(refusals) >= 5
    assert all(c.query.strip() for c in answerable)


def test_refusal_cases_cover_the_known_out_of_scope_queries(golden_cases):
    refusal_ids = {c.id for c in golden_cases if not c.expected_answerable}
    assert REFUSAL_CASE_IDS <= refusal_ids


def test_case005_paraphrase_is_present(golden_cases):
    paraphrase = next(c for c in golden_cases if c.id == "case-015")
    assert paraphrase.expected_answerable is True
    assert paraphrase.query != next(
        c for c in golden_cases if c.id == "case-005"
    ).query
