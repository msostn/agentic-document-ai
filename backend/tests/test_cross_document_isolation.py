"""Cross-document data-isolation regression test.

This test demonstrates that a question scoped to Document A cannot return
Document B's chunks/sources even where semantic content overlaps.

This is a security-relevant regression test. It verifies the document_id
filtering enforced at the query-construction level (Phase 8 retrieval).

The test uses the real API and does NOT query PostgreSQL directly.
It sends HTTP requests to the /documents, /search and /ask endpoints.

Two synthetic PDFs with overlapping semantic content ("cancellation
policy" appears in both, with different specifics) are uploaded for the
run and deleted afterwards.

Usage:
    python -m pytest tests/test_cross_document_isolation.py -v
"""

from __future__ import annotations

import io
import uuid

import httpx
import pytest

# ---------------------------------------------------------------------------
# Test configuration
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"
TIMEOUT = 120.0

# Overlapping question: both documents talk about a "cancellation policy".
OVERLAPPING_QUESTION = "What is the cancellation policy?"

DOC_A_FILENAME = f"phase14_isolation_doc_a_{uuid.uuid4().hex[:8]}.pdf"
DOC_B_FILENAME = f"phase14_isolation_doc_b_{uuid.uuid4().hex[:8]}.pdf"

# Synthetic, clearly non-sensitive content.
DOC_A_TEXT = (
    "Acme Insurance Policy\n"
    "Section 4. Cancellation policy. The policyholder may cancel this "
    "insurance policy at any time by giving thirty (30) days written notice "
    "to Acme Insurance. A pro-rated refund of premiums is returned for the "
    "unused portion of the policy period.\n"
    "Section 5. Deductible. The annual hospitalization deductible is five "
    "hundred dollars (500 USD) per insured person.\n"
    "Section 6. Indemnification. Acme Insurance indemnifies the "
    "policyholder against covered claims subject to policy limits.\n"
)

DOC_B_TEXT = (
    "Riverside Office Lease Agreement\n"
    "Clause 7. Cancellation policy. The tenant may cancel this lease "
    "agreement only by giving sixty (60) days written notice to the "
    "landlord, together with a termination fee equal to one month of rent.\n"
    "Clause 8. Notice period. All notices under this lease must be served "
    "by registered post to the registered office of the landlord.\n"
    "Clause 9. Renewal terms. This lease renews automatically for further "
    "twelve (12) month terms unless either party serves notice under "
    "Clause 7.\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pdf(text: str) -> bytes:
    """Render *text* as a simple single-page PDF."""
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page()
    rect = page.rect + (50, 50, -50, -50)
    page.insert_textbox(rect, text, fontsize=11, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def _upload(client: httpx.Client, filename: str, text: str) -> str:
    """Upload a synthetic PDF and return the document id."""
    pdf_bytes = _make_pdf(text)
    resp = client.post(
        f"{BASE_URL}/documents/upload",
        files={"file": (filename, io.BytesIO(pdf_bytes), "application/pdf")},
    )
    resp.raise_for_status()
    document_id = resp.json()["id"]
    status = resp.json().get("status")
    assert status in ("ready", "processing", "pending", "failed"), (
        f"Unexpected document status after upload: {status}"
    )
    return document_id


def _ask(client: httpx.Client, document_id: str, query: str) -> dict:
    """Send POST /documents/{id}/ask and return parsed JSON response."""
    resp = client.post(
        f"{BASE_URL}/documents/{document_id}/ask", json={"query": query}
    )
    resp.raise_for_status()
    return resp.json()


def _search(
    client: httpx.Client, document_id: str, query: str, top_k: int = 20
) -> dict:
    """Send POST /documents/{id}/search and return parsed JSON response."""
    resp = client.post(
        f"{BASE_URL}/documents/{document_id}/search",
        json={"query": query, "top_k": top_k},
    )
    resp.raise_for_status()
    return resp.json()


def _delete(client: httpx.Client, document_id: str) -> None:
    try:
        client.delete(f"{BASE_URL}/documents/{document_id}")
    except httpx.HTTPError:
        pass


def _chunk_ids(search_response: dict) -> set[str]:
    return {r["chunk_id"] for r in search_response.get("results", [])}


# ---------------------------------------------------------------------------
# Test: document isolation - Doc A questions should not return Doc B sources
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.regression
class TestCrossDocumentIsolation:
    """Questions scoped to Document A must never return Document B's chunks."""

    @pytest.fixture(autouse=True)
    def setup_docs(self, backend_base_url: str):
        """Create two overlapping documents and clean them up afterwards."""
        with httpx.Client(timeout=TIMEOUT) as client:
            doc_a = _upload(client, DOC_A_FILENAME, DOC_A_TEXT)
            doc_b = _upload(client, DOC_B_FILENAME, DOC_B_TEXT)
            try:
                self.client = client
                self.doc_a = doc_a
                self.doc_b = doc_b
                yield
            finally:
                _delete(client, doc_a)
                _delete(client, doc_b)

    def test_search_is_scoped_to_requested_document(self) -> None:
        """Every chunk returned by /search belongs to the requested document."""
        for doc_id in (self.doc_a, self.doc_b):
            result = _search(self.client, doc_id, OVERLAPPING_QUESTION, top_k=20)
            assert result["document_id"] == doc_id
            assert len(result["results"]) > 0, (
                f"Expected chunks for document {doc_id}"
            )
            for chunk in result["results"]:
                assert chunk["document_id"] == doc_id, (
                    f"Search for {doc_id} returned chunk "
                    f"{chunk['chunk_id']} from document {chunk['document_id']}"
                )

    def test_doc_a_sources_belong_only_to_doc_a(self) -> None:
        """Doc A's /ask sources must be Doc A chunks and never Doc B chunks."""
        ids_a = _chunk_ids(
            _search(self.client, self.doc_a, OVERLAPPING_QUESTION, top_k=20)
        )
        ids_b = _chunk_ids(
            _search(self.client, self.doc_b, OVERLAPPING_QUESTION, top_k=20)
        )
        assert ids_a, "Expected Doc A to have retrievable chunks"
        assert ids_b, "Expected Doc B to have retrievable chunks"
        assert not (ids_a & ids_b), "Chunk ids must be unique per document"

        response = _ask(self.client, self.doc_a, OVERLAPPING_QUESTION)
        assert response["document_id"] == self.doc_a

        source_ids = {s["chunk_id"] for s in response.get("sources", [])}
        leaked = source_ids & ids_b
        assert not leaked, (
            f"Doc A answer cited Doc B chunks: {leaked}"
        )
        foreign = source_ids - ids_a
        assert not foreign, (
            f"Doc A answer cited chunks outside Doc A: {foreign}"
        )

    def test_doc_b_sources_belong_only_to_doc_b(self) -> None:
        """Symmetric check: Doc B's /ask sources must never be Doc A chunks."""
        ids_a = _chunk_ids(
            _search(self.client, self.doc_a, OVERLAPPING_QUESTION, top_k=20)
        )
        ids_b = _chunk_ids(
            _search(self.client, self.doc_b, OVERLAPPING_QUESTION, top_k=20)
        )

        response = _ask(self.client, self.doc_b, OVERLAPPING_QUESTION)
        assert response["document_id"] == self.doc_b

        source_ids = {s["chunk_id"] for s in response.get("sources", [])}
        leaked = source_ids & ids_a
        assert not leaked, (
            f"Doc B answer cited Doc A chunks: {leaked}"
        )
        foreign = source_ids - ids_b
        assert not foreign, (
            f"Doc B answer cited chunks outside Doc B: {foreign}"
        )

    def test_overlapping_content_isolation(self) -> None:
        """The critical case: identical question, two overlapping documents.

        Both documents contain a "cancellation policy" clause with
        different specifics (30 days vs 60 days). Each answer must be
        grounded in its own document's chunks only.
        """
        doc_a_response = _ask(self.client, self.doc_a, OVERLAPPING_QUESTION)
        doc_b_response = _ask(self.client, self.doc_b, OVERLAPPING_QUESTION)

        assert doc_a_response["document_id"] == self.doc_a
        assert doc_b_response["document_id"] == self.doc_b

        ids_a = _chunk_ids(
            _search(self.client, self.doc_a, OVERLAPPING_QUESTION, top_k=20)
        )
        ids_b = _chunk_ids(
            _search(self.client, self.doc_b, OVERLAPPING_QUESTION, top_k=20)
        )

        a_sources = {s["chunk_id"] for s in doc_a_response.get("sources", [])}
        b_sources = {s["chunk_id"] for s in doc_b_response.get("sources", [])}

        assert not (a_sources & ids_b), "Doc A leaked Doc B chunks"
        assert not (b_sources & ids_a), "Doc B leaked Doc A chunks"

        # The responses must cite disjoint chunk sets (different documents).
        assert not (a_sources & b_sources), (
            "Both documents cited the same chunk id"
        )


# ---------------------------------------------------------------------------
# Fixture (defined at module level so pytest can discover it)
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_base_url() -> str:
    """Provide the running backend base URL, skipping tests if unreachable."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{BASE_URL}/health")
            resp.raise_for_status()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"backend not reachable at {BASE_URL}: {exc}")
    return BASE_URL
