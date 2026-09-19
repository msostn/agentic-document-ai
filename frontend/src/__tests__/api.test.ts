import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const mockFetch = vi.fn()

describe('api.ts', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('DOMException', class extends Error {
      name = 'AbortError'
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  describe('ApiError', () => {
    it('stores status and code', async () => {
      const { ApiError } = await import('../api')
      const err = new ApiError('test', 404, 'NOT_FOUND')
      expect(err.message).toBe('test')
      expect(err.status).toBe(404)
      expect(err.code).toBe('NOT_FOUND')
      expect(err.name).toBe('ApiError')
    })
  })

  describe('listDocuments', () => {
    it('returns document list on success', async () => {
      const docs = [{ id: '1', filename: 'test.pdf', status: 'ready' }]
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(docs),
      })
      const { listDocuments } = await import('../api')
      const result = await listDocuments()
      expect(result).toEqual(docs)
      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:8000/documents',
        undefined,
      )
    })

    it('throws ApiError on non-ok response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ detail: 'Server error' }),
      })
      const { listDocuments, ApiError } = await import('../api')
      await expect(listDocuments()).rejects.toThrow(ApiError)
    })
  })

  describe('uploadDocument', () => {
    it('sends FormData with POST method', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ id: '1', status: 'processing' }),
      })
      const { uploadDocument } = await import('../api')
      const file = new File(['content'], 'test.pdf', { type: 'application/pdf' })
      await uploadDocument(file)

      const [, init] = mockFetch.mock.calls[0]
      expect(init.method).toBe('POST')
      expect(init.body).toBeInstanceOf(FormData)
    })
  })

  describe('askQuestion', () => {
    it('sends JSON body with query', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ answer: 'test', sources: [] }),
      })
      const { askQuestion } = await import('../api')
      await askQuestion('doc-1', 'What is this?')

      const [, init] = mockFetch.mock.calls[0]
      expect(init.method).toBe('POST')
      expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
      expect(JSON.parse(init.body)).toEqual({ query: 'What is this?' })
    })
  })

  describe('checkHealth', () => {
    it('returns health status', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ status: 'ok' }),
      })
      const { checkHealth } = await import('../api')
      const result = await checkHealth()
      expect(result).toEqual({ status: 'ok' })
    })
  })

  describe('network errors', () => {
    it('normalizes fetch rejection to ApiError', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Failed to fetch'))
      const { listDocuments, ApiError } = await import('../api')
      await expect(listDocuments()).rejects.toThrow(ApiError)
    })
  })
})
