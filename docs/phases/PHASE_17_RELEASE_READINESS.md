# Phase 17 — Release Candidate, CI/CD & Final Production Readiness

## 1. Objective

Make the Phase 16 result reproducible, verifiable and reviewable **without
changing the product**: no application module, no endpoint, no retrieval
behaviour, no RAG/agent parameter was touched. Everything in this document is
infrastructure, documentation and evidence.

Spec: `docs/phases/PHASE_17_SPEC.md`. Architecture record: `ARCHITECTURE.md §17`.

## 2. Starting baseline (re-verified before any change)

| Gate | Result at `e19a3e1` |
|------|---------------------|
| `python -m pytest` (backend) | 76 passed, 0 failed, 0 collection errors |
| `python scripts/run_all_regressions.py` | 11 suites / 267 checks, 0 failed, exit 0 |
| `npm run lint` | 0 errors, 2 warnings, exit 0 |
| `npm test` | 43 passed (8 files) |
| `npm run build` | pass |
| `python evaluate.py` | 21/21, retrieval 100%, citation 100%, rejection 100% |
| Local E2E probe | 24/24 |
| Perf reference (Phase 16) | ≈1924 ms latency, ≈789 ms generation, ≈11 ms retrieval |

## 3. Dependency reproducibility (Workstreams A/B)

**Problem.** `backend/requirements.txt` was unpinned and the repository had two
live interpreters with *different* versions of the same libraries (system
Python 3.13.7 vs `backend/.venv`), so a fresh install could silently change the
numerical stack that produces the embeddings.

**Decision — pin every direct runtime dependency to the versions the running
server actually uses, and align every environment on those pins.**

```
torch==2.14.0                fastapi==0.141.1           uvicorn[standard]==0.53.0
pydantic-settings==2.15.0    sqlalchemy==2.0.54          psycopg[binary]==3.3.5
pgvector==0.5.0              pymupdf==1.28.2             python-multipart==0.0.32
sentence-transformers==6.0.1 numpy==2.5.3                httpx==0.28.1
```

* Verified by diffing each pin against `pip list` in `backend/.venv`: **12/12
  match** (`torch==2.14.0` is satisfied by the installed `2.14.0+cpu` wheel —
  PEP 440 `==` ignores the local segment).
* `backend/requirements-dev.txt` pins `pytest==9.1.1` and stays out of the
  image.
* **Python 3.12+ (verified on 3.13).** `python:3.11-slim` was not an option:
  `numpy==2.5.3` and `scipy>=1.18` require ≥3.12. Dockerfile and CI both use
  3.13, so image ≡ CI ≡ host.
* **Node 22+** — the frontend toolchain requires `^20.19 || ^22.12 || >=24`;
  README's "Node.js 18+" was wrong and is corrected.
* Fresh-venv proof: `%TEMP%\opencode\ci_venv` installed `requirements.txt` +
  `requirements-dev.txt` from scratch and ran the CI steps.

## 4. CI (Workstream C)

`.github/workflows/ci.yml` (new): deterministic gates only.

* **backend** (Python 3.13, pip cache): CPU-torch install → pinned requirements
  → `python -m compileall -q app scripts` → import validation →
  `pytest -m "not integration" --strict-markers` → `scripts/test_chunker.py` →
  `scripts/test_phase13_deployment.py`. Job env: placeholder `DATABASE_URL`
  (engine builds at import; nothing connects), `OLLAMA_BASE_URL` to a closed
  port.
* **frontend** (Node 22): `npm ci` → `npm run lint` → `npm test` → `npm run build`.
* No `continue-on-error`, no `|| true`, no retries. Live-service gates
  (regression drivers, E2E, evaluation) are **not** faked in CI — a GitHub
  runner has no Supabase PostgreSQL and no Ollama.
* Validated by reproducing every step locally in a fresh venv with CI's exact
  environment: compileall 0, imports ok, **72 passed / 4 deselected** (no
  `.env`, placeholder URL), chunker **17/17**, phase13 **19/19**.

## 5. Release verification command (Workstreams D/E)

`scripts/verify_release.py` (new, repository root) — twelve stages, one child
process each, stop at first failure, per-stage summary, non-zero exit:

| # | Stage | Needs |
|---|-------|-------|
| 1 | backend syntax check (`compileall app scripts`) | — |
| 2 | backend import validation | — |
| 3 | pytest (full; `-m "not integration" --strict-markers` with `--offline`) | — |
| 4 | `scripts/test_chunker.py` (17 checks) | — |
| 5 | `scripts/test_phase13_deployment.py` (19 checks) | — |
| 6 | `scripts/run_all_regressions.py` (11 suites / 267 checks) | database + Ollama |
| 7 | frontend `npm run lint` | Node 22+ |
| 8 | frontend `npm test` | |
| 9 | frontend `npm run build` | |
| 10 | `backend/scripts/api_contract_probe.py` (27 checks) | running backend |
| 11 | `backend/scripts/e2e_smoke.py` (24 checks) | running backend |
| 12 | `backend/evaluate.py` **+ report gate** (21/21, failed=0, 100/100/100) | running backend |

`--offline` runs stages 1–5 and 7–9 and prints every live stage as `NOT RUN`
(never as passed); it is the local equivalent of the CI workflow.

Design notes:

* Stage 12 re-reads `eval/reports/*.json` after the harness exits, so a harness
  that prints `21/21` while writing a report with a failed case still fails.
* `npm` is resolved with `shutil.which` — `subprocess.run(["npm", ...])`
  raises `WinError 2` on Windows because npm is `npm.CMD`.
* Two probes are now checked in and parameterised with `--base-url`, so the
  *identical* run works against a native server and a container:
  `backend/scripts/api_contract_probe.py` (Phase 16's 26 checks + "undocumented
  CORS origin is refused" → **27**) and `backend/scripts/e2e_smoke.py`
  (**24** checks).

## 6. Security review (Workstreams F/G)

* Repository-wide scan of tracked files: **no secrets, no credentials, no
  `.pem`/keys**; `git check-ignore` confirms `.env`, `frontend/dist`,
  `frontend/node_modules`, `backend/.venv` are ignored.
* `backend/.dockerignore` now also excludes `scripts/`, `requirements-dev.txt`,
  `evaluate.py` (on top of `tests/`, `pytest.ini`, `.env`, venvs, caches).
  **Verified against the built image**: `/app` contains exactly `Dockerfile`,
  `app`, `requirements.txt`; absent: `.env`, `tests`, `scripts`, `pytest.ini`,
  `requirements-dev.txt`, `evaluate.py`, `__pycache__`, `.venv`.
* Image runs as **`appuser` (uid/gid 1000)**, verified with `docker run --entrypoint id`.
* **Auth decision (spec §): documented, not implemented.** Local/demo
  deployment; adding auth would change the API contract and the frontend.
  `README.md` and `DEPLOYMENT.md` state the consequence and the "do not expose
  publicly" rule.
* **API contract frozen:** no endpoint, method, status code or payload added,
  renamed or removed. The Phase 16 `POST /documents` (spec) vs
  `POST /documents/upload` (implementation) discrepancy is left as documented —
  renaming a live route during an RC phase breaks clients.

## 7. Scope decisions — what was deliberately *not* built

| Requested to be preserved | Decision |
|---------------------------|----------|
| Chat persistence | not added (in-memory history remains a documented limitation) |
| Multi-document QA | not added |
| Async ingestion / background workers | not added (ingestion stays synchronous) |
| Observability | preserved as-is: `X-Request-ID`, `query_event` stage logs, `/metrics/runtime` |
| Auth | documented local/demo status only |

## 8. Eval artifact policy (Workstream K)

**Frozen history + ignored future artifacts.**

* `.gitignore` gains `eval/reports/*`: the four Phase 16 reports already
  tracked stay tracked as evidence (tracked files ignore `.gitignore`), future
  runs stop growing the diff.
* Golden sets stay version-controlled — they *are* the gate.
* Nothing was deleted.

## 9. Docker verification (Workstream E)

`backend/Dockerfile`: `python:3.13-slim`, CPU torch wheel first, fully pinned
`requirements.txt`, non-root, no baked model cache (Phase 13/16 behaviour kept).

Executed end to end:

```
docker compose build        → backend + frontend images built, exit 0
docker compose up -d        → backend /health ok in 2 s; /health/ready
                              {"database":"ok","ollama":"ok"}; frontend 200 (nginx SPA)
api_contract_probe.py       → 27/27 against the container
e2e_smoke.py                → 24/24 against the container
evaluate.py                 → 21/21, 100/100/100 against the container
image content / identity    → non-root; app + requirements.txt only; no .env/tests/scripts
docker compose down         → containers and network removed
```

**Cold-start note (documented in `DEPLOYMENT.md`):** the image bakes no model
cache by design, so the *first* ingestion in a fresh container downloads the
embedding model (~80 MB) into the container layer. On this connection that took
≈5 minutes and expired the E2E probe's 300 s client timeout on the first
attempt; the download continues server-side, so warming the container once
(upload → `status: ready`) and then running the probes is the documented
procedure. Subsequent runs are unaffected.

## 10. Tests and gates (Workstream N)

Full gate run with the pinned interpreter (`backend/.venv`) against a freshly
restarted backend:

| Gate | Result |
|------|--------|
| syntax / imports | pass |
| `pytest` | 76 passed (offline/CI subset: 72 passed, 4 deselected, 1 skipped with no `.env`) |
| chunker driver | 17/17 |
| deployment contract driver | 19/19 |
| `run_all_regressions.py` | 11 suites / 267 checks, 0 failed |
| frontend lint / test / build | 0 errors · 43 passed · pass |
| `api_contract_probe.py` | 27/27 (native **and** container) |
| `e2e_smoke.py` | 24/24 (native **and** container) |
| `evaluate.py` + report gate | 21/21, retrieval 100%, citation 100%, rejection 100% (native **and** container) |

`scripts/verify_release.py --offline` → deterministic stages PASS, live stages
`NOT RUN`, exit 0. Full run → all stages PASS, exit 0.

## 11. Performance (spec §27)

| Stage | Phase 16 reference | Phase 17 measurement |
|-------|--------------------|----------------------|
| ingestion (1500-word PDF, synchronous) | not recorded | ≈0.88 s upload → `ready` (4 chunks) |
| retrieval + query embedding | ≈11 ms | ≈86–103 ms per `/ask` |
| generation | ≈789 ms | ≈4.6–6.3 s per answerable `/ask` |
| total `/ask` latency | ≈1924 ms | ≈5.9–6.8 s answerable; ≈0.21 s rejected |

No application code changed, and per spec §27 **nothing was optimised**. Two
measured causes, not assumptions:

1. **Retrieval is CPU-bound** (in-process `sentence-transformers` on `cpu`);
   the host was at ~63 % CPU with ~2 GB free during measurement (qbittorrent,
   Chrome, OpenCode, Docker), so the delta tracks load. Absolute value stays
   under 0.1 s.
2. **Generation throughput is unchanged** — a direct `/api/chat` call measured
   **≈53 tok/s** on the same `qwen3:4b` (RTX 3050, model fully resident in
   VRAM, no competing GPU process). The delta is *token count*: current runs
   emit ≈250–330 tokens per answer (reasoning chain + answer) where the
   reference implies ≈40. Ollama has been running 0.34.4 since 28 Sep and the
   model file is 12 days old — neither changed during this phase.

Classification: environment/measurement variance, recorded for the next
performance investigation, left alone.

## 12. Documentation changes

* **README** — prerequisites corrected (Python 3.12+/3.13, Node 22+); new
  "Release verification (Phase 17)" + "Continuous integration" sections; env
  table: `EVAL_*` defaults corrected to the absolute paths `Settings` computes
  (and `.env.example` no longer implies `../eval/...` cwd-relative defaults).
* **DEPLOYMENT** — backend image documented as `python:3.13-slim` (was
  `python:3.11-slim`); backend env table completed (`EMBEDDING_DEVICE`,
  `MIN_CHUNK_SIZE`, `MAX_QUERY_LENGTH`, `RAG_CONTEXT_MAX_CHARS`,
  `ENABLE_*`, `EVAL_*`); container contents and cold-start model download
  documented; new "Release verification (Phase 17)" section with the Docker
  procedure (build → up → health → probes → image-content checks → down).
* **ARCHITECTURE** — new `## 17. Phase 17 …` recording the pinning, CI,
  release-verification, security/scope and performance decisions.
* **This document** — `docs/phases/PHASE_17_RELEASE_READINESS.md`.

## 13. Repository hygiene / review notes

* Application code: **untouched** (`backend/app/**` has no diff).
* Files intentionally edited: `backend/requirements.txt`,
  `backend/requirements-dev.txt`, `backend/Dockerfile`,
  `backend/.dockerignore`, `backend/.env.example`, `.gitignore`,
  `.github/workflows/ci.yml` (new), `scripts/verify_release.py` (new),
  `backend/scripts/api_contract_probe.py` (new),
  `backend/scripts/e2e_smoke.py` (new), `pytest.ini` (new, repo root),
  `README.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md`,
  `docs/phases/PHASE_17_RELEASE_READINESS.md` (new).
* **Two pre-existing modified files were also edited by Phase 17 — both must
  be called out in the diff review:**
  * `.gitignore` — was already modified before Phase 17 (line-ending-only: the
    content diff against `e19a3e1` is *only* Phase 17's `eval/reports/*` rule,
    and `git diff` shows LF→CRLF warnings). Nothing of the earlier change was
    overwritten.
  * `backend/requirements.txt` — was already modified before Phase 17 and was
    never staged, so the pre-existing worktree content is **not recoverable
    from git**; Phase 17 replaced it with the pinned list (§3). If that earlier
    modification carried content beyond line endings, it must be recovered from
    the reviewer's own records — flagging it here rather than hiding it.
* Left untouched as pre-existing: `.cursor/rules/project.mdc`,
  `backend/app/database.py`, `backend/app/models/*`,
  `backend/app/routes/__init__.py`, `backend/app/schemas/__init__.py`,
  `backend/scripts/init_db.py`, untracked `docs/phases/PHASE_15_SPEC.md`,
  `docs/phases/PHASE_16_SPEC.md`, `opencode.json`.
* Root `pytest.ini` was added because a bare `pytest` from the repository root
  found no configuration and therefore also collected `backend/scripts/test_*.py`
  (driver scripts whose `record()` helper cannot fail — the exact problem
  `backend/pytest.ini` solves one level down). Both files declare the same
  `testpaths` target and the same markers; `pythonpath = backend` makes
  `import app…` resolve from the root. `cd backend && python -m pytest` (the
  documented command) is unchanged and still uses `backend/pytest.ini`.
* No commit, no tag, no push was made.

## 14. Environment observations (not defects, worth knowing)

1. **Vitest worker spawn failed once** under memory pressure (`Failed to start
   threads worker` for all 8 files) during a full verification run; the host
   had ~2.1 GB free. The same stage passed on the next run and in every
   subsequent run (43/43). Cause: machine memory/CPU pressure, not the suite —
   recorded rather than hidden behind a retry loop.
2. **Database accumulation:** the development database holds 437 documents,
   all created by the project's own verification runs (regression drivers leave
   their fixtures; ~125 appeared during this phase's runs). Not cleaned up
   automatically — deleting rows from a shared dev database is a human
   decision. If a tidy-up is wanted: delete documents by filename
   (`phase7_test.pdf`, `ask_*.pdf`, E2E fixtures) through
   `DELETE /documents/{id}`.
3. **12 documents are stuck in `processing`** and 21 are `failed`/`empty`
   (created by interrupted runs, including this one's 422-upload probes). They
   are visible in the UI but harmless; same cleanup caveat as above.
4. A **pending Ollama update** sits in `%LOCALAPPDATA%\Ollama\updates_v2`
   (downloaded 30 Sep, not applied — the server has been running 0.34.4 since
   28 Sep). Applying it will restart Ollama and is a human decision.

## 15. Final status

Every release gate in the spec passed on the final code state:

```
pytest                          76 passed
run_all_regressions.py          11 suites / 267 checks / 0 failed
frontend lint / test / build    0 errors · 43 passed · pass
api_contract_probe.py           27 / 27   (native + container)
e2e_smoke.py                    24 / 24   (native + container)
evaluate.py + report gate       21 / 21, 100% / 100% / 100%   (native + container)
docker compose                  build → up → health → probes → image checks → down
verify_release.py               all stages PASS (offline subset PASS)
security                        no secrets tracked; image has no .env/tests/scripts; non-root
```

Phase 17 does **not** tag a release: `v1.0.0`/`v0.9.0` is a human decision
made after reviewing this diff (spec §26 — "Do not tag before final
verification").
