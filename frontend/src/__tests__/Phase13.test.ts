import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const mockFetch = vi.fn()

describe('Phase 13 - VITE_API_BASE_URL configuration', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('api.ts uses VITE_API_BASE_URL from env or defaults to localhost:8000', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ status: 'ok' }),
    })
    const { checkHealth } = await import('../api')
    await checkHealth()
    const calledUrl = mockFetch.mock.calls[0]?.[0] as string
    expect(calledUrl).toMatch(/^https?:\/\/.+\/health$/)
  })

  it('api.ts constructs URL from API_BASE + path', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve([]),
    })
    const { listDocuments } = await import('../api')
    await listDocuments()
    const calledUrl = mockFetch.mock.calls[0]?.[0] as string
    expect(calledUrl).toMatch(/\/documents$/)
  })
})
