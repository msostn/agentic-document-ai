# PHASE 13 — Reproducible Packaging & Deployment Readiness
### (Docker Compose for backend + frontend, Supabase cloud unchanged, Ollama remains a native host dependency)

**Project:** Agentic Document AI — Grounded Document Q&A
**Phase:** 13
**Depends on:** Phases 1–12 (complete, committed, 208/208 + 15/15 backend, 39/39 frontend, all passing)
**Status of this document:** Specification only. No implementation code included. To be handed to a fresh implementation-agent session.

---

## 0. MANDATORY FIRST STEP — READ THIS BEFORE CHANGING ANYTHING

This specification was written from a project summary, **not** from the live repository. The repository is the source of truth. Before writing or modifying a single file, the implementation agent MUST:

1. Read `ARCHITECTURE.md`.
2. Read the current `README.md`.
3. Inspect the actual backend directory tree.
4. Inspect the actual frontend directory tree.
5. Inspect `config.py` and any `.env.example` file(s).
6. Inspect `package.json` and `requirements.txt` (or `pyproject.toml`, if that's actually used instead).
7. Inspect the existing startup/run commands (README instructions, `Makefile`, npm scripts, etc.).
8. Inspect current Docker status — there may already be partial Docker artifacts; do not assume there are none.
9. Inspect existing tests (backend `tests/` and frontend test files) to understand current coverage and naming conventions before adding new tests.
10. Confirm what Phase 12 actually shipped (endpoints, CORS config, health checks, frontend structure) rather than trusting this document's summary of it.

**If the real repository differs from anything assumed below, the real repository wins.** Adapt file paths, base images, dependency names, and existing behavior accordingly, while preserving the intent of each change described here. Do not silently skip a requirement because the assumed file doesn't exist — find the real equivalent or flag the discrepancy in the implementation report.

---

## 1. Phase Objective

Make the project **reproducible and demonstrable by someone other than the original developer**, without pretending it is a publicly hosted cloud SaaS product. Concretely: replace "clone the repo, hope your Python/Node versions match, manually start three processes in three terminals, hope Ollama is running" with a documented, scripted, mostly one-command local deployment — and harden the configuration layer (env vars, CORS, health checks, logging, error handling) so that this deployment behaves correctly and safely regardless of who runs it or on what machine.

## 2. Why This Phase Comes Now

Phases 1–12 built a functioning, well-tested local RAG application. Nothing in the retrieval/agent/RAG pipeline is broken or missing for the stated interview/portfolio goal. The actual gap is **operational**: the project currently only "works" inside the original developer's exact local setup (specific Python/Node versions installed, Ollama already running as a background app, `.env` already configured by hand). That gap is what would stop a reviewer, interviewer, or collaborator from actually running it. Packaging and configuration hardening is therefore the highest-leverage next phase — higher than any new RAG/agent feature — because it converts "a project that works on my machine" into "a project I can hand to someone else."

## 3. Current-State Assumptions

These are assumptions carried over from the project summary and **must be verified against the real repo per Section 0** before implementation:

- Backend is FastAPI + SQLAlchemy + psycopg, configured via Pydantic Settings (`config.py`), talking to a Supabase-hosted PostgreSQL/pgvector instance over a connection string.
- Frontend is React + Vite + TypeScript with plain CSS, currently pointed at a hardcoded or `.env`-based local backend URL.
- Ollama runs natively on the developer's Windows machine (not containerized), serving `qwen3:4b`.
- CORS, `/health`, and `/health/ready` already exist from Phase 12 in some form — their exact current behavior needs to be read from code, not assumed.
- No Docker artifacts currently exist (assumed absent — verify).
- No authentication, no PDF byte storage, no async queue, synchronous ingestion — all still true and **not** being changed in this phase.

## 4. Actual Architectural Goal

Move from:

```
Developer's machine only:
  Terminal 1: npm run dev          (frontend, hardcoded/local .env)
  Terminal 2: uvicorn ...          (backend, manually configured .env)
  Terminal 3: (Ollama already running as a tray app)
```

To:

```
Any machine with Docker + Ollama installed, and a Supabase project:
  1. Install Ollama, run `ollama pull qwen3:4b`               (documented prerequisite, ~5 min)
  2. Copy .env.example → .env, fill in Supabase connection    (documented, ~2 min)
  3. docker compose up --build                                (backend + frontend containers)
  4. Open the frontend URL, upload a doc, ask a question
```

Native (non-Docker) development continues to work exactly as before — Docker is **additive**, not a replacement requirement for day-to-day development.

## 5. Explicit Non-Goals

Phase 13 does **not**:

- Add authentication, user accounts, or document ownership.
- Add persistent chat history.
- Store uploaded PDF bytes.
- Make ingestion asynchronous or add a job queue.
- Add Redis, Celery, or any background worker.
- Containerize Ollama (see Section 14 for the reasoning).
- Deploy the backend to a public cloud host reachable from the open internet.
- Add streaming responses or multi-document reasoning.
- Introduce LangChain/LangGraph, Kubernetes, or microservices.
- Change RAG, chunking, embedding, or agent-loop logic from Phases 5–11.
- Change existing API request/response contracts unless a concrete deployment reason forces it (none currently identified).

## 6. Architecture Changes

**Before (Phase 12):**

```
React (Vite dev server, native)
      ↓
FastAPI (uvicorn, native)
      ↓
Agent → search_document tool → RAG context
      ↓
pgvector (Supabase, cloud)
      ↓
Ollama (native, local, hardcoded URL)
      ↓
answer + backend-owned sources
```

**After (Phase 13):**

```
Browser
   ↓  (calls VITE_API_BASE_URL, e.g. http://localhost:8000)
Frontend container (nginx serving built React/Vite static assets)
                              — OR native `npm run dev`, unchanged —

FastAPI container (uvicorn)  — OR native `uvicorn`, unchanged —
   ↓
Agent → search_document tool → RAG context     [UNCHANGED FROM PHASE 11]
   ↓
pgvector (Supabase, cloud — UNCHANGED)
   ↓
Ollama (native host process, reachable via OLLAMA_BASE_URL —
        host.docker.internal:11434 from inside the backend container,
        or localhost:11434 in native dev mode)
   ↓
answer + backend-owned sources     [UNCHANGED FROM PHASE 10/11]
```

The only structural addition is a configuration/networking layer around the existing pipeline. No node in the RAG/agent graph itself changes.

## 7. Deployment Strategy

**Primary strategy: Dockerized local/reproducible deployment ("bring your own Ollama and Supabase").**

- Backend and frontend run as containers via `docker compose up`.
- Supabase Postgres/pgvector remains external and cloud-hosted (it already is "deployed" — no change needed).
- Ollama remains a native host install (already works; not containerized — see Section 14).
- This is **not** a publicly reachable internet deployment. It is a one-command, cross-machine-reproducible deployment that any reviewer can run on their own laptop.

**Alternatives considered and explicitly rejected for this phase:**

| Option | Verdict | Reason |
|---|---|---|
| D — static frontend hosted on Vercel/Netlify | Rejected for now | Would need a permanently-reachable backend URL, which requires either a public backend host (see E) or the developer's machine to stay online — fragile and not the honest "official" deployment path. Could be a nice-to-have appendix, not core to Phase 13. |
| E — backend on a free/low-cost cloud host | Rejected | Free tiers (Render, Railway, Fly.io free plans) don't have the RAM/CPU to run Ollama, and the backend is useless without reachable Ollama. Running backend in the cloud while Ollama stays on the developer's laptop means the deployed backend would depend on the developer's machine being online and network-reachable — not a real deployment. |
| F — hybrid (remote frontend + local backend + local Ollama + cloud DB) | Rejected | Same fragility as D/E combined: a publicly hosted frontend pointing at a backend that only exists when the developer's laptop is on and port-forwarded is misleading, not "deployed." |
| G — Docker Compose (backend+frontend) + Supabase cloud + native Ollama | **Selected** | Only option that is both honest about the local-inference constraint and actually improves reproducibility for a third party. |
| A — full production/local packaging (Docker for everything, including Ollama) | Partially adopted | Backend + frontend containerized; Ollama deliberately left native (Section 14). |

## 8. Local Development Strategy

Two supported paths, both documented in the README, neither one deprecating the other:

1. **Native dev (unchanged from Phase 12):** `npm run dev` for frontend, `uvicorn` for backend, Ollama already running. This remains the fastest inner loop for active development.
2. **Docker Compose (new in Phase 13):** `docker compose up --build`. Intended for demoing, onboarding a reviewer, or verifying the app works outside the developer's personal environment.

Both paths must read configuration from environment variables (Section 10) rather than hardcoded values, so switching between them requires only changing `.env`, not code.

## 9. Production Configuration Strategy

"Production" here means "not the developer's exact dev shell" — i.e., environment-aware behavior, not a hosted SaaS posture. Concretely:

- An `ENVIRONMENT` setting (`development` | `production`) in `config.py` controls: whether `uvicorn` runs with `--reload` (dev only), how verbose logs are (Section 19), and whether CORS defaults to a permissive localhost list or requires an explicit allowlist (Section 17).
- All previously-hardcoded values that differ between "my machine" and "someone else's machine" (Ollama URL, CORS origins, DB connection string, frontend API base URL) must be environment-variable-driven with sensible local defaults.
- No new "production" infrastructure (no separate prod database, no secrets manager) — that would be overbuilding for this project's actual audience (interviewers/reviewers running it locally).

## 10. Environment Variable Strategy

Consolidate and document every variable in a root-level `.env.example` (and backend/frontend-specific ones if the real repo already separates them — verify per Section 0). Proposed set, to be reconciled with what already exists in `config.py`:

**Backend:**
| Variable | Purpose | Example |
|---|---|---|
| `DATABASE_URL` | Supabase Postgres connection string | `postgresql+psycopg://...` |
| `OLLAMA_BASE_URL` | Where the backend reaches Ollama | `http://localhost:11434` (native) / `http://host.docker.internal:11434` (Docker) |
| `OLLAMA_MODEL` | Model name | `qwen3:4b` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed frontend origins | `http://localhost:5173,http://localhost:3000` |
| `ENVIRONMENT` | `development` or `production` | `development` |
| `MAX_UPLOAD_SIZE_MB` | Existing or new upload guard (verify if already present) | `20` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

**Frontend:**
| Variable | Purpose | Example |
|---|---|---|
| `VITE_API_BASE_URL` | Backend origin the browser calls | `http://localhost:8000` |

Every variable must have a safe local-dev default documented in `.env.example` with a one-line comment. No secret values are ever committed — verify `.env` is in `.gitignore` (should already be true; confirm, don't assume).

## 11. Frontend Changes

| Change | Why | Files likely affected | Must not break |
|---|---|---|---|
| Read backend URL from `VITE_API_BASE_URL` instead of a hardcoded value | Needed so the same build can point at a native or Dockerized backend without code edits | The API client/fetch wrapper (exact file to be located during inspection), `.env.example` | Existing upload/list/ask/status UI flows and error handling from Phase 12 |
| Add production build config for Docker (multi-stage build → static assets served by nginx) | Needed to serve the SPA from a container without running the Vite dev server in production | New `frontend/Dockerfile`, possibly `frontend/nginx.conf` | The existing `npm run dev` native workflow must remain untouched and working |
| No visual/UX changes | Preserve principle of not rewriting working functionality | — | All 39 existing frontend tests |

## 12. Backend Changes

| Change | Why | Files likely affected | Must not break |
|---|---|---|---|
| Externalize `OLLAMA_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `ENVIRONMENT`, `LOG_LEVEL` into `config.py` (Pydantic Settings) | Needed so behavior differs by environment without code changes | `config.py`, `.env.example` | Existing settings already read successfully in Phases 7–12 |
| Enhance `/health/ready` to report per-dependency status (DB reachable, Ollama reachable, embedding model loaded) rather than a single boolean, if it doesn't already | Needed so container orchestration and manual debugging can tell *which* dependency is down | The health/readiness route module | Existing `/health` liveness semantics from Phase 12 |
| Confirm/add request size limits and Ollama call timeouts | Basic robustness for a process that may now be started by someone unfamiliar with the system | Upload route, Ollama client wrapper | Grounded-answer behavior and error mapping from Phase 10 |
| Ensure `uvicorn` startup command differs by `ENVIRONMENT` (`--reload` only in development) | Avoid accidentally running reload/debug mode in a "production-configured" container | `Dockerfile` CMD / entrypoint, or a small `run.py` if one exists | Native dev startup command must remain simple and unchanged |
| Non-root user in the backend Docker image | Basic container security hygiene | `backend/Dockerfile` | Nothing functional — this is container-only |

**No changes to:** chunking, embedding, retrieval, RAG context construction, agent loop, or prompt construction (Phases 5–11 stay untouched).

## 13. Database Changes

None. Supabase Postgres/pgvector connection and schema are unchanged. Row-Level Security remains explicitly out of scope (still no auth boundary to enforce it against). Docker Compose does **not** run a local Postgres — using the same Supabase project for both native and Docker runs avoids dual-database drift and matches "preserve existing architecture."

## 14. Ollama / Model Strategy

**Decision: Ollama remains a native host-installed dependency, not a Docker Compose service, in this phase.**

Reasoning (explicitly required per the design principles):

- It already works. Phases 10 and 11 are built and tested against it. Containerizing it is a change to something that currently isn't broken, for marginal reproducibility gain.
- Containerizing Ollama would still require the same ~2.5–3 GB one-time model download and the same host RAM/CPU — it doesn't remove the heaviest prerequisite, it just relocates where the download happens.
- It avoids new failure modes this phase would otherwise have to test and document: container volume persistence for model weights, first-run auto-pull entrypoints, and Docker-vs-native GPU/CPU passthrough quirks on Windows.
- It keeps the Docker surface area small and easy to reason about in an interview ("I containerized the two components that vary across machines; I left the model runtime as a documented native prerequisite because that's genuinely simpler and doesn't reduce reproducibility").

The backend reaches Ollama via `OLLAMA_BASE_URL`, defaulting to `http://localhost:11434` for native dev and documented as `http://host.docker.internal:11434` for the Docker Compose path (Docker Desktop on Windows/Mac supports this out of the box; Linux hosts need the compose file's backend service to declare `extra_hosts: ["host.docker.internal:host-gateway"]`, which will be included).

**This is revisitable in a later phase** if there's a concrete reason (e.g., wanting a single `docker compose up` with zero native prerequisites) — noted in Section 28.

**Hosted/cloud LLM: remains explicitly out of scope.** Not introduced "because it would make deployment easier" — that would violate the project's zero-cost/local-first identity and the explicit instruction not to add a paid LLM to ease deployment.

## 15. Docker Strategy

**Docker is justified now**, specifically for the backend and frontend, because:

- It removes "which Python/Node version do you have" as a source of failure for anyone else running the project.
- It replaces "start these in three terminals in the right order" with one command.
- It's a legitimate, common, easily-explained interview talking point when scoped this narrowly (two simple containers, no orchestration platform).

**Docker is kept simple**: no Kubernetes, no service mesh, no multi-stage orchestration beyond what's needed to build the frontend's static assets. Two Dockerfiles and one `docker-compose.yml` at the repo root.

Illustrative shape (to be adapted to the real dependency files found during inspection):

```yaml
# docker-compose.yml (illustrative — verify real ports/paths against repo)
services:
  backend:
    build: ./backend
    env_file: .env
    ports:
      - "8000:8000"
    extra_hosts:
      - "host.docker.internal:host-gateway"   # needed on Linux only; harmless elsewhere

  frontend:
    build:
      context: ./frontend
      args:
        VITE_API_BASE_URL: ${VITE_API_BASE_URL:-http://localhost:8000}
    ports:
      - "5173:80"
```

```dockerfile
# backend/Dockerfile (illustrative — verify real requirements file name/path)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m appuser && chown -R appuser /app
USER appuser
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```dockerfile
# frontend/Dockerfile (illustrative — verify real build output dir, e.g. dist/)
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
```

The implementation agent must replace file names, entry-point module names (`main:app`), and build output directories with what actually exists in the repo.

## 16. Security Considerations

- No authentication is added — this remains a single-user/demo-oriented tool. This limitation must be stated plainly in the README: **this deployment is not safe to expose on the open internet** (no document ownership boundary, no rate limiting, no auth).
- CORS moves from any hardcoded/wildcard origin to an explicit allowlist read from `CORS_ALLOWED_ORIGINS`.
- Backend Docker image runs as a non-root user.
- `.env` stays out of version control (verify `.gitignore`, don't assume).
- No secrets are baked into Docker images; all secrets come from `.env` at runtime via `env_file`.
- Upload size limits and Ollama call timeouts are confirmed/enforced so a misbehaving upload or a hung model call can't hang the process indefinitely.

## 17. CORS Strategy

Replace whatever Phase 12 currently hardcodes with an explicit, environment-driven allowlist:

- `CORS_ALLOWED_ORIGINS` is a comma-separated list, parsed into `allow_origins` for FastAPI's `CORSMiddleware`.
- Local defaults cover the Vite dev server port and the Docker frontend port.
- In `development`, if the variable is unset, fall back to a permissive localhost-only default (never a wildcard `*`, since credentials/cookies are not in play but explicit is still safer and clearer for interview explanation).
- In `production` mode, an unset `CORS_ALLOWED_ORIGINS` should fail loudly at startup rather than silently defaulting, so a misconfigured deployment is caught immediately rather than producing confusing browser errors.

## 18. Health/Readiness Strategy

- `/health` stays a pure liveness check (process is up) — unchanged in spirit from Phase 12.
- `/health/ready` is enhanced (or added, if not already this granular) to check and report, per dependency:
  - Database reachable (`SELECT 1`)
  - Ollama reachable (lightweight call, e.g. list models or a version endpoint)
  - Embedding model loaded in memory
- Response shape: overall `status: "ready" | "not_ready"` plus a `checks` object with per-dependency booleans/messages, so a reviewer (or the implementation agent during testing) can immediately tell *what* isn't working rather than getting an opaque failure.
- HTTP status: `200` when fully ready, `503` when any check fails — matching standard readiness-probe conventions, useful even without an orchestrator, purely for manual/scripted verification.

## 19. Logging/Error Handling Strategy

- Introduce `LOG_LEVEL` (default `INFO`) and route through Python's standard `logging`, verifying whatever logging already exists from earlier phases isn't duplicated or fought with.
- Log key lifecycle events already present from Phase 7 (ingestion start/end/failure) and Phase 10/11 (Ollama call start/failure, agent iteration bounds hit) — verify these exist; add only if genuinely missing, don't restructure working logging.
- Error handling contracts from Phase 10 (Ollama errors → controlled HTTP responses) are preserved exactly; Phase 13 only adds configuration around *how much* gets logged, not new error paths.
- No chain-of-thought or raw model reasoning is ever logged — preserve this Phase 11 guarantee explicitly when touching any logging code near the agent loop.

## 20. File-Level Implementation Plan

*(Paths are illustrative pending Section 0 inspection; the implementation agent must confirm/adjust against the real tree.)*

**New files:**
- `backend/Dockerfile`
- `frontend/Dockerfile`
- `frontend/nginx.conf` (if nginx is used for serving)
- `docker-compose.yml` (repo root)
- `.dockerignore` (backend and frontend)
- `DEPLOYMENT.md` (new, root-level — step-by-step reproducible setup, prerequisites, hardware expectations, troubleshooting)
- `.env.example` (root-level, or updated if one already exists — must be reconciled, not duplicated)

**Modified files (expected, pending inspection):**
- `config.py` — add `OLLAMA_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `ENVIRONMENT`, `LOG_LEVEL` settings
- CORS setup module/route — read from settings instead of hardcoded values
- Health/readiness route — per-dependency reporting
- Frontend API client module — read `VITE_API_BASE_URL`
- `README.md` — add "Docker deployment" section pointing to `DEPLOYMENT.md`, without removing existing native-run instructions
- `ARCHITECTURE.md` — update the architecture diagram to reflect Section 6 above and add the honest limitations from Section 27

**Not touched:** chunking, embedding, retrieval, RAG context builder, agent orchestration, prompt templates, existing document/ask route logic (Phases 5–11 files).

## 21. Exact API Changes

- No changes to existing request/response contracts for `/documents`, `/documents/{id}/search`, or `/documents/{id}/ask`.
- `/health/ready` response body gains a `checks` object (additive, non-breaking) if not already structured this way — existing consumers checking only top-level status continue to work.

## 22. Testing Strategy

- All existing tests must still pass, unmodified in behavior: Phases 5–11 backend (208), Phase 12 backend (15), frontend (39) — **exact counts to be re-verified by the implementation agent and reported, not assumed**.
- New backend tests to add:
  - Config loading: `CORS_ALLOWED_ORIGINS` parses correctly from a comma-separated string; sensible default in development; startup failure in production when unset.
  - `/health/ready`: returns per-dependency detail; returns `503` when a dependency check is mocked as failing; returns `200` when all pass.
- New frontend tests to add (if the existing test setup supports it):
  - API client correctly uses `VITE_API_BASE_URL` when set, and a sensible default when not.
- No changes to existing test files for Phases 5–11 functionality.

## 23. Manual E2E Verification

Both paths must be manually verified end-to-end:

**Native path (regression check — must still work exactly as before):**
1. Start backend natively, frontend natively, confirm Ollama running.
2. Upload a document → ready → ask a grounded question → get answer + sources → ask an unrelated question → get correct rejection.

**Docker path (new):**
1. `docker compose up --build`.
2. Confirm `/health` and `/health/ready` both report healthy once Ollama (native, already running) and Supabase are reachable.
3. Open the frontend container's URL in a browser.
4. Upload a document, confirm it reaches `ready`.
5. Ask a grounded question, confirm answer + sources render.
6. Ask an unrelated question, confirm correct rejection.
7. `docker compose down`, confirm clean shutdown.

## 24. Deployment Verification

Per the project's explicit testing requirement, Docker must be **actually built and run**, not just present as files:

- [ ] `docker compose build` completes with no errors.
- [ ] `docker compose up` boots both containers successfully.
- [ ] `/health/ready` reports fully healthy against real Supabase + real native Ollama.
- [ ] Full upload → ingest → retrieve → agent → answer → sources flow succeeds through the containerized frontend hitting the containerized backend.
- [ ] All pre-existing automated tests still pass (exact counts reported).
- [ ] New Phase 13 tests pass (exact counts reported).
- [ ] Frontend production build (`npm run build`, and the Docker image build) both succeed with zero TypeScript errors.
- [ ] The documented `DEPLOYMENT.md` procedure is followed literally, start to finish, as if by someone unfamiliar with the repo, and it works.

## 25. Rollback / Failure Considerations

- Docker artifacts are additive and isolated (new files + config/env plumbing only) — reverting Phase 13 is a matter of removing the new Docker files and the small config additions; no Phase 5–11 logic is touched, so rollback carries no risk to the RAG/agent pipeline.
- If the Docker path fails for a given reviewer's machine (e.g., Docker not installed, `host.docker.internal` unsupported), the native path remains fully documented and functional as a fallback — this must be explicitly stated in `DEPLOYMENT.md`, not left implicit.
- If `/health/ready` reports a specific dependency down, the response body should be specific enough that the failure is self-diagnosing without reading logs.

## 26. Acceptance Criteria

Phase 13 is complete only when **all** of the following are true:

1. `docker compose up --build` successfully starts backend and frontend containers.
2. The full document flow (upload → ready → grounded ask → answer + sources → correct rejection of unrelated question) works through the Dockerized stack against real Supabase and real native Ollama.
3. The native (non-Docker) workflow still works, unmodified in behavior.
4. All previously-passing tests still pass, with exact counts reported.
5. New Phase 13 tests (config + health/readiness) pass, with exact counts reported.
6. `CORS_ALLOWED_ORIGINS`, `OLLAMA_BASE_URL`, `ENVIRONMENT`, and `VITE_API_BASE_URL` are all environment-driven, documented in `.env.example`, with no hardcoded equivalents left in source.
7. `README.md` and `ARCHITECTURE.md` accurately describe both run modes and the current, honest limitations.
8. `DEPLOYMENT.md` exists and its procedure has been literally followed and verified to work.
9. No chunking/embedding/retrieval/RAG/agent logic from Phases 5–11 was modified.
10. Nothing is committed automatically — an implementation report is produced for review first.

## 27. Known Limitations (Updated)

Carried forward, still true after Phase 13:

- No authentication, no user accounts, no document ownership boundary.
- No persistent chat history.
- Uploaded PDF bytes are not stored.
- Ingestion remains synchronous.
- No streaming responses, no multi-document reasoning.
- No background job system.

New/clarified in Phase 13:

- This deployment is **not** intended for public internet exposure — it has no multi-tenant isolation and no rate limiting. It is a reproducible local/demo deployment, not a hosted product.
- Ollama remains a native host prerequisite; `docker compose up` alone does not provide a fully self-contained zero-prerequisite deployment.
- Inference speed is entirely bound by the host machine's CPU — no GPU acceleration is assumed or required, and answer latency will vary significantly by hardware.
- First-time setup requires a one-time ~2.5–3 GB model download via `ollama pull qwen3:4b`, independent of Docker.
- The frontend's backend URL is set at Docker build time (via `VITE_API_BASE_URL` build arg); changing it after the image is built requires a rebuild, not just an env var change at runtime. This is an accepted simplicity trade-off for this phase.

## 28. Explicit Boundary for Phase 14

Not in scope now, potentially valuable later, **only if a concrete need emerges**:

- Authentication and per-user document ownership (would also finally justify enabling Postgres RLS as an actual auth boundary).
- Persistent chat history.
- Async ingestion for large documents (would justify a queue only if synchronous ingestion becomes a demonstrated real problem).
- Streaming answer responses.
- Multi-document reasoning.
- Revisiting whether Ollama should be containerized (if zero-native-prerequisite deployment becomes a real goal).
- An optional, clearly-opt-in hosted-LLM fallback path, kept separate from the zero-cost local default — not a replacement for it.
- Public-facing hardening (rate limiting, stricter request validation) — only relevant if public exposure is ever actually intended.

None of the above should be started as part of Phase 13.
