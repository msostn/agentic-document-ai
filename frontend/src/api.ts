import type {
  AnswerResponse,
  ApiErrorResponse,
  Document,
  HealthResponse,
  ReadinessResponse,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const UPLOAD_TIMEOUT_MS = Number(import.meta.env.VITE_UPLOAD_TIMEOUT_MS) || 120000

export class ApiError extends Error {
  status: number
  code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function normalizeError(status: number, body: unknown): ApiError {
  const data = body as ApiErrorResponse
  let message = 'An unexpected error occurred'
  let code: string | undefined

  if (typeof data === 'string') {
    message = data
  } else if (data?.detail) {
    if (typeof data.detail === 'string') {
      message = data.detail
    } else if (typeof data.detail === 'object' && data.detail.message) {
      message = data.detail.message
    }
  }

  if (status === 415) code = 'UNSUPPORTED_FILE_TYPE'
  else if (status === 413) code = 'FILE_TOO_LARGE'
  else if (status === 404) code = 'NOT_FOUND'
  else if (status === 409) code = 'CONFLICT'
  else if (status === 422) code = 'VALIDATION_ERROR'
  else if (status === 502) code = 'UPSTREAM_UNAVAILABLE'
  else if (status === 504) code = 'TIMEOUT'

  return new ApiError(message, status, code)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`
  try {
    const res = await fetch(url, init)
    if (!res.ok) {
      let body: unknown
      try {
        body = await res.json()
      } catch {
        body = await res.text()
      }
      throw normalizeError(res.status, body)
    }
    if (res.status === 204) return undefined as T
    return res.json() as Promise<T>
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('Request timed out', 408, 'TIMEOUT')
    }
    throw new ApiError(
      err instanceof Error ? err.message : 'Network error',
      0,
      'NETWORK_ERROR',
    )
  }
}

export function checkHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

export function checkReadiness(): Promise<ReadinessResponse> {
  return request<ReadinessResponse>('/health/ready')
}

export function listDocuments(): Promise<Document[]> {
  return request<Document[]>('/documents')
}

export function getDocument(documentId: string): Promise<Document> {
  return request<Document>(`/documents/${documentId}`)
}

export async function uploadDocument(file: File): Promise<Document> {
  const formData = new FormData()
  formData.append('file', file)

  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS)

  try {
    const res = await request<Document>('/documents/upload', {
      method: 'POST',
      body: formData,
      signal: controller.signal,
    })
    return res
  } finally {
    clearTimeout(timeoutId)
  }
}

export function askQuestion(
  documentId: string,
  query: string,
): Promise<AnswerResponse> {
  return request<AnswerResponse>(`/documents/${documentId}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  })
}
