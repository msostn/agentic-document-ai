# Phase 12: Frontend Integration — Local-First MVP

Status: Implemented

---

## Goal

Turn the existing backend into a complete, usable, demonstrable local-first MVP by adding the minimal React frontend and the small backend hardening required for browser integration.

## Target User Flow

```
Open frontend
 ↓
backend health check
 ↓
upload PDF
 ↓
synchronous ingestion
 ↓
document becomes ready
 ↓
select document
 ↓
ask question
 ↓
bounded Phase 11 agent
 ↓
grounded answer
 ↓
backend-attributed sources/pages
 ↓
display answer + sources
```

## Deployment

Supported Phase 12 deployment is local-first:

```
React/Vite (localhost:5173)
  ↓
FastAPI (localhost:8000)
  ↓
Supabase PostgreSQL
  ↓
Ollama qwen3:4b (localhost:11434)
```

## What Was Implemented

### Backend (minimal hardening)

| Addition | Status | Notes |
|----------|--------|-------|
| CORS | Already existed | `allow_credentials` changed to `False` |
| GET /health | Already existed | No changes |
| GET /health/ready | **New** | Checks DB + Ollama |
| GET /documents | Already existed | No changes |
| POST /documents/{id}/ask | Already existed | No changes |
| ENVIRONMENT config | **New** | Defaults to `development` |
| httpx in requirements.txt | **Fixed** | Was used but unlisted |

### Frontend (complete React SPA)

| File | Purpose |
|------|---------|
| `src/types.ts` | TypeScript types matching backend schemas |
| `src/api.ts` | Centralized API client with error normalization |
| `src/components/UploadPanel.tsx` | PDF upload with validation |
| `src/components/DocumentList.tsx` | Document list with status badges |
| `src/components/ChatPanel.tsx` | Chat interface with question input |
| `src/components/SourcesList.tsx` | Source page/chunk display |
| `src/components/StatusBadge.tsx` | Document status indicator |
| `src/components/ErrorBanner.tsx` | Error display with dismiss |
| `src/App.tsx` | Main app layout and state |
| `src/App.css` | Component styles |
| `src/index.css` | Global styles with CSS custom properties |
| `.env.example` | Frontend environment template |
| `vitest.config.ts` | Test configuration |
| `src/setupTests.ts` | Test setup with jest-dom |
| `src/__tests__/*.test.*` | 39 frontend tests |

### Tests

- Frontend: 39/39 passing (Vitest + React Testing Library)
- Backend: 22 new Phase 12 tests (health/ready, CORS, documents list, ask endpoint, config)
- Build: `npm run build` succeeds with no TypeScript errors

## Known Limitations

- No authentication (document IDs are guessable)
- No PDF persistence (re-upload required on failure)
- Synchronous ingestion (generous 120s timeout)
- Local-first only (Ollama must be on localhost)
- No streaming (full answer after agent loop completes)
- In-memory chat history (lost on refresh)
