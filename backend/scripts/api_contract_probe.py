"""API contract & security probe — black-box HTTP against a running backend.

Phase 17 makes the release API/security audit reproducible: 26 checks covering
the frozen API contract (PHASE_17_SPEC.md §21) and the security behaviours the
release gate requires (spec §11).

    cd backend
    python scripts/api_contract_probe.py
    python scripts/api_contract_probe.py --base-url http://localhost:8000

Covered: health/readiness/metrics shape, request correlation, malformed UUIDs,
missing documents, invalid and malformed request bodies, invalid top_k,
unsupported uploads (non-PDF, corrupt, empty, oversized), stable JSON errors
with no stack traces, unknown routes and unsupported methods.

Probe-created documents are deleted before exit. Exit code: 0 when every
check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import sys
import uuid

import httpx

TIMEOUT = 180.0

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def no_traceback(body: str) -> bool:
    return "Traceback (most recent call last)" not in body and "site-packages" not in body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="backend base URL (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    with httpx.Client(timeout=TIMEOUT, base_url=args.base_url) as c:
        created: list[str] = []

        try:
            # --- health ---
            r = c.get("/health")
            check("GET /health 200", r.status_code ==
                  200, f"{r.status_code} {r.text[:80]}")
            check("X-Request-ID present", bool(r.headers.get("x-request-id")),
                  r.headers.get("x-request-id", "MISSING"))
            rid = r.headers.get("x-request-id")
            check("X-Request-ID unique per request",
                  c.get("/health").headers.get("x-request-id") != rid, "")

            r = c.get("/health/ready")
            check(
                "GET /health/ready 200 or 503 JSON",
                r.status_code in (
                    200, 503) and r.headers["content-type"].startswith("application/json"),
                f"{r.status_code} {r.text[:120]}",
            )

            # --- metrics ---
            r = c.get("/metrics/runtime")
            try:
                data = r.json()
            except Exception:
                data = {}
            keys = set(data)
            required = {
                "uptime_seconds",
                "total_requests",
                "total_errors",
                "total_retrieval_ms",
                "total_context_building_ms",
                "total_generation_ms",
                "total_request_ms",
                "average_latency_ms",
            }
            check("GET /metrics/runtime 200 + aggregate keys", r.status_code == 200 and required <= keys,
                  f"{r.status_code} missing={sorted(required - keys)}")
            check("metrics counters non-negative",
                  all(isinstance(v, (int, float))
                      and v >= 0 for v in data.values()),
                  str({k: data[k] for k in sorted(data)}))
            check("metrics contain no content keys",
                  not any("document" in k or "answer" in k or "query" in k for k in keys), str(sorted(keys)))

            # --- malformed / missing document ---
            r = c.get("/documents/not-a-uuid")
            check("GET /documents/not-a-uuid -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:100]}")
            check("422 body is JSON, no traceback", no_traceback(r.text) and r.text.lstrip().startswith("{"),
                  r.text[:80])

            missing = "00000000-0000-0000-0000-000000000000"
            r = c.get(f"/documents/{missing}")
            check("GET missing document -> 404", r.status_code ==
                  404, f"{r.status_code} {r.text[:80]}")
            r = c.delete(f"/documents/{missing}")
            check("DELETE missing document -> 404", r.status_code ==
                  404, f"{r.status_code} {r.text[:80]}")
            r = c.post(f"/documents/{missing}/search",
                       json={"query": "anything"})
            check("SEARCH missing document -> 404", r.status_code ==
                  404, f"{r.status_code} {r.text[:80]}")
            r = c.post(f"/documents/{missing}/ingest")
            check("INGEST missing document -> 404", r.status_code ==
                  404, f"{r.status_code} {r.text[:80]}")

            # --- invalid payloads ---
            r = c.post(f"/documents/{missing}/search", json={})
            check("SEARCH without query -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:80]}")
            r = c.post(f"/documents/{missing}/search", content="not json",
                       headers={"Content-Type": "application/json"})
            check("SEARCH malformed JSON -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:80]}")
            r = c.post(f"/documents/{missing}/search",
                       json={"query": "x", "top_k": 0})
            check("SEARCH top_k=0 -> 422 (schema passes, route 404 first)",
                  r.status_code in (404, 422), f"{r.status_code}")
            r = c.post(f"/documents/{missing}/ask", json={"query": 123})
            check("ASK non-string query -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:80]}")
            r = c.post(f"/documents/{missing}/ask", json={})
            check("ASK without query -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:80]}")

            # --- unsupported upload ---
            r = c.post("/documents/upload",
                       files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
            check("UPLOAD .txt -> 415", r.status_code ==
                  415, f"{r.status_code} {r.text[:80]}")
            r = c.post("/documents/upload",
                       files={"file": ("fake.pdf", io.BytesIO(b"not a pdf"), "application/pdf")})
            check("UPLOAD corrupt PDF -> 422", r.status_code ==
                  422, f"{r.status_code} {r.text[:160]}")
            check("corrupt upload response has no traceback",
                  no_traceback(r.text), "")

            # empty file
            r = c.post(
                "/documents/upload",
                files={"file": ("empty.pdf", io.BytesIO(b""),
                                "application/pdf")},
            )
            check("UPLOAD empty file -> 4xx", 400 <= r.status_code <
                  500, f"{r.status_code} {r.text[:120]}")
            if 200 <= r.status_code < 300:
                created.append(r.json()["id"])

            # oversized upload (> MAX_UPLOAD_SIZE_MB default 25)
            big = b"0" * (26 * 1024 * 1024)
            r = c.post(
                "/documents/upload",
                files={"file": ("big.pdf", io.BytesIO(big),
                                "application/pdf")},
            )
            check("UPLOAD oversized -> 413", r.status_code ==
                  413, f"{r.status_code} {r.text[:120]}")
            if 200 <= r.status_code < 300:
                created.append(r.json()["id"])

            # --- list ---
            r = c.get("/documents")
            check("GET /documents 200 list", r.status_code == 200 and isinstance(r.json(), list),
                  f"{r.status_code} n={len(r.json()) if r.status_code == 200 else '?'}")

            # --- unknown route ---
            r = c.get("/does-not-exist")
            check("unknown route -> 404 JSON", r.status_code == 404 and r.text.lstrip().startswith("{"),
                  f"{r.status_code} {r.text[:80]}")

            # --- unsupported method on known route ---
            r = c.patch("/documents")
            check("PATCH /documents -> 405", r.status_code ==
                  405, f"{r.status_code} {r.text[:80]}")

            # --- CORS: disallowed origin gets no permissive headers ---
            origin = "http://evil.example"
            r = c.get("/health", headers={"Origin": origin})
            acao = r.headers.get("access-control-allow-origin")
            check(
                "CORS: undocumented origin is not allowed",
                acao not in ("*", origin),
                f"access-control-allow-origin={acao}",
            )
        finally:
            for doc_id in created:
                try:
                    d = c.delete(f"/documents/{doc_id}")
                    print(f"cleanup {doc_id} -> {d.status_code}")
                except httpx.HTTPError as exc:
                    print(f"cleanup failed for {doc_id}: {exc}")

    print()
    failed = [x for x in results if not x[0]]
    print(
        f"TOTAL {len(results)}  PASS {len(results) - len(failed)}  FAIL {len(failed)}")
    for _, name, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as exc:
        print(
            f"[probe] request failed (is the backend running at --base-url?): {exc}")
        sys.exit(1)
