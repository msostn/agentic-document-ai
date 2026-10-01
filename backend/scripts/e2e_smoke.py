"""Release end-to-end probe — black-box HTTP against a running backend.

Phase 17 makes the local/Docker E2E gate reproducible: the same 24 checks run
against a natively started backend and against the Dockerised one.

    cd backend
    python scripts/e2e_smoke.py                          # local, default base URL
    python scripts/e2e_smoke.py --base-url http://localhost:8000

Covers: health, readiness, upload, synchronous ingestion, grounded /ask,
sources, citation validity, out-of-scope rejection, document-scoped search,
cross-document isolation, and the JSON error contract.

The probe creates its own documents and deletes them again, so it is safe to
run repeatedly against a shared development database.

Exit code: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from scripts.test_ingestion import make_pdf  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


PDF_A = make_pdf(
    [
        "PLATINUM HEALTH POLICY\n"
        "Section 1 - Coverage summary. This Platinum Health Policy provides "
        "comprehensive medical coverage for the insured member and eligible "
        "dependents. " * 8,
        "Section 2 - Deductible. The annual deductible for this plan is $500 "
        "per individual and $1000 per family. The deductible must be met "
        "before coinsurance begins. " * 8,
        "Section 3 - Copay. Office visits carry a $30 copay after the "
        "deductible is satisfied. " * 8,
    ]
)

PDF_B = make_pdf(
    [
        "UNIVERSITY STUDENT HANDBOOK\n"
        "Chapter 1 - Library hours. The main library closes at 9 PM on "
        "weekdays and at 5 PM on weekends. " * 8,
        "Chapter 2 - Parking. Parking permits are issued by the transport "
        "office and cost 120 dollars per semester. " * 8,
    ]
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="backend base URL (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    with httpx.Client(timeout=300, base_url=args.base_url) as c:
        # --- health -----------------------------------------------------
        r = c.get("/health")
        check("GET /health", r.status_code == 200 and r.json().get("status") == "ok", str(r.status_code))
        check("health response carries X-Request-ID", "x-request-id" in r.headers, r.headers.get("x-request-id", ""))

        r = c.get("/health/ready")
        ready = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(
            "GET /health/ready returns JSON checks",
            r.status_code in (200, 503) and {"status", "checks"} <= set(ready),
            f"{r.status_code} {ready.get('checks')}",
        )

        created: list[str] = []
        try:
            # --- upload -------------------------------------------------
            r = c.post(
                "/documents/upload",
                files={"file": ("platinum_policy.pdf", io.BytesIO(PDF_A), "application/pdf")},
            )
            check("upload PDF -> 201", r.status_code == 201, str(r.status_code))
            if r.status_code != 201:
                print(r.text[:300])
                return report()
            doc_a = r.json()
            created.append(doc_a["id"])

            check(
                "ingestion completed synchronously (status ready)",
                doc_a.get("status") == "ready",
                f"status={doc_a.get('status')} chunks={doc_a.get('chunk_count')}",
            )
            check(
                "chunk count > 0",
                (doc_a.get("chunk_count") or 0) > 0,
                str(doc_a.get("chunk_count")),
            )

            r = c.get(f"/documents/{doc_a['id']}")
            check(
                "GET /documents/{id} -> 200 with processed_at set",
                r.status_code == 200 and bool(r.json().get("processed_at")),
                str(r.status_code),
            )

            # --- grounded ask --------------------------------------------
            r = c.post(
                f"/documents/{doc_a['id']}/ask",
                json={"query": "What is the annual deductible for this plan?"},
            )
            answer = r.json()
            check("grounded ask -> 200", r.status_code == 200, str(r.status_code))
            check(
                "answer is non-empty and mentions 500",
                bool(answer.get("answer")) and "500" in answer["answer"],
                answer.get("answer", "")[:70],
            )
            sources = answer.get("sources") or []
            check("answer carries sources", len(sources) > 0, f"n={len(sources)}")
            check(
                "answer envelope is bound to the requested document",
                answer.get("document_id") == doc_a["id"],
                str(answer.get("document_id")),
            )
            check(
                "sources carry citation metadata",
                all(s.get("chunk_id") and s.get("page_number") for s in sources),
                "chunk_id/page_number present",
            )
            source_ids = {s.get("chunk_id") for s in sources}
            r = c.post(
                f"/documents/{doc_a['id']}/search",
                json={"query": "deductible", "top_k": 10},
            )
            doc_a_chunk_ids = {x.get("chunk_id") for x in r.json().get("results", [])}
            check(
                "every cited chunk belongs to the requested document",
                source_ids <= doc_a_chunk_ids,
                f"{len(source_ids - doc_a_chunk_ids)} unknown chunks",
            )

            # --- out-of-scope rejection -----------------------------------
            r = c.post(
                f"/documents/{doc_a['id']}/ask",
                json={"query": "What is the capital of France?"},
            )
            reject = r.json()
            text = (reject.get("answer") or "").lower()
            check("out-of-scope ask -> 200", r.status_code == 200, str(r.status_code))
            check(
                "out-of-scope question is rejected, not fabricated",
                reject.get("context_status") in ("no_answer", "empty", "error")
                or "could not find" in text
                or "couldn't find" in text
                or "no relevant" in text,
                f"context_status={reject.get('context_status')}",
            )
            check(
                "rejection does not quote unrelated document content",
                "library closes" not in text,
                text[:70],
            )

            # --- search ---------------------------------------------------
            r = c.post(
                f"/documents/{doc_a['id']}/search",
                json={"query": "deductible", "top_k": 3},
            )
            found = (r.json().get("results") or []) if r.status_code == 200 else []
            check("search -> 200 with results", r.status_code == 200 and bool(found), str(r.status_code))
            check(
                "search results are document-scoped",
                all(x.get("document_id") == doc_a["id"] for x in found),
                str({x.get("document_id") for x in found}),
            )

            # --- document isolation ---------------------------------------
            r = c.post(
                "/documents/upload",
                files={"file": ("handbook.pdf", io.BytesIO(PDF_B), "application/pdf")},
            )
            check("second document upload -> 201", r.status_code == 201, str(r.status_code))
            if r.status_code != 201:
                return report()
            doc_b = r.json()
            created.append(doc_b["id"])

            r = c.post(
                f"/documents/{doc_a['id']}/ask",
                json={"query": "What time does the library close?"},
            )
            cross = (r.json().get("answer") or "").lower()
            check(
                "document isolation: doc B facts never leak into doc A",
                "9 pm" not in cross and "9 p.m" not in cross,
                cross[:70],
            )

            r = c.post(
                f"/documents/{doc_b['id']}/search",
                json={"query": "deductible", "top_k": 5},
            )
            b_search = r.json() if r.status_code == 200 else {}
            leak = b_search.get("results") or []
            check(
                "document isolation: doc B search results are scoped to doc B",
                b_search.get("document_id") == doc_b["id"]
                and all(x.get("document_id") == doc_b["id"] for x in leak),
                str({x.get("document_id") for x in leak}),
            )

            # --- error contract -------------------------------------------
            r = c.get("/documents/not-a-uuid")
            check("malformed UUID -> 422 JSON", r.status_code == 422 and "detail" in r.json(), str(r.status_code))
            r = c.get("/documents/00000000-0000-0000-0000-000000000000")
            check(
                "missing document -> 404 JSON",
                r.status_code == 404 and "detail" in r.json(),
                str(r.status_code),
            )
            check(
                "no stack trace in error body",
                "Traceback" not in r.text and "site-packages" not in r.text,
                "",
            )
        finally:
            for doc_id in created:
                c.delete(f"/documents/{doc_id}")

    return report()


def report() -> int:
    failed = [n for n, ok, _ in results if not ok]
    print("=" * 60)
    print(f"E2E SMOKE: {len(results) - len(failed)}/{len(results)} passed")
    for name in failed:
        print(f"  FAILED: {name}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPError as exc:
        print(f"[e2e] request failed (is the backend running at --base-url?): {exc}")
        raise SystemExit(1)
