export interface Document {
  id: string
  filename: string
  file_type: string
  status: 'processing' | 'ready' | 'failed' | 'empty'
  created_at: string
  chunk_count: number | null
  error_message: string | null
  processed_at: string | null
}

export interface AnswerSource {
  chunk_id: string
  chunk_index: number
  page_number: number
}

export interface AnswerResponse {
  document_id: string
  query: string
  answer: string | null
  sources: AnswerSource[]
  context_status: string
  model: string
}

export interface HealthResponse {
  status: string
}

export interface ReadinessResponse {
  status: string
  checks: Record<string, string>
}

export interface ApiErrorResponse {
  detail?: string | { message?: string; document_id?: string }
}

export type UploadState = 'idle' | 'uploading' | 'success' | 'error'

export type AskState = 'idle' | 'asking' | 'error'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources?: AnswerSource[]
  contextStatus?: string
  isSemanticNoAnswer?: boolean
}
