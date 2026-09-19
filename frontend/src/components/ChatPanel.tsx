import { useState, useRef, useEffect } from 'react'
import type { AskState, ChatMessage } from '../types'
import { askQuestion, ApiError } from '../api'
import { SourcesList } from './SourcesList'

const MAX_QUERY_LENGTH = 8000

const SEMANTIC_NO_ANSWER_STATUSES = new Set([
  'below_similarity_threshold',
  'no_chunks_retrieved',
  'no_chunk_fits_budget',
  'invalid_query',
])

interface ChatPanelProps {
  documentId: string | null
  documentReady: boolean
}

export function ChatPanel({ documentId, documentReady }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [query, setQuery] = useState('')
  const [askState, setAskState] = useState<AskState>('idle')
  const [globalError, setGlobalError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (messagesEndRef.current?.scrollIntoView) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  useEffect(() => {
    setMessages([])
    setQuery('')
    setAskState('idle')
    setGlobalError(null)
  }, [documentId])

  const canAsk = documentReady && documentId && askState !== 'asking' && query.trim().length > 0

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canAsk || !documentId) return

    const trimmed = query.trim()
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: trimmed,
    }
    setMessages((prev) => [...prev, userMsg])
    setQuery('')
    setAskState('asking')
    setGlobalError(null)

    try {
      const res = await askQuestion(documentId, trimmed)
      const isNoAnswer =
        !res.answer ||
        SEMANTIC_NO_ANSWER_STATUSES.has(res.context_status)

      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: isNoAnswer
          ? res.answer ||
            'No relevant information found in this document for your question.'
          : res.answer || 'No answer generated.',
        sources: res.sources,
        contextStatus: res.context_status,
        isSemanticNoAnswer: isNoAnswer,
      }
      setMessages((prev) => [...prev, assistantMsg])
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.code === 'NETWORK_ERROR' || err.status === 0
            ? 'Cannot reach the backend server. Please check your connection.'
            : err.code === 'UPSTREAM_UNAVAILABLE'
              ? 'The AI model is currently unavailable. Please try again later.'
              : err.message
          : 'An unexpected error occurred.'
      setGlobalError(message)
      setAskState('error')
      return
    } finally {
      setAskState('asking')
      setAskState('idle')
    }
  }

  return (
    <div className="chat-panel">
      {!documentReady && (
        <div className="chat-panel__disabled">
          {documentId
            ? 'Document is not ready for questions.'
            : 'Select a document to start chatting.'}
        </div>
      )}

      <div className="chat-panel__messages" data-testid="chat-messages">
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`chat-message chat-message--${msg.role} ${msg.isSemanticNoAnswer ? 'chat-message--no-answer' : ''}`}
          >
            <div className="chat-message__role">
              {msg.role === 'user' ? 'You' : 'Assistant'}
            </div>
            <div className="chat-message__content">{msg.content}</div>
            {msg.role === 'assistant' && msg.sources && (
              <SourcesList sources={msg.sources} />
            )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {globalError && (
        <div className="chat-panel__error" role="alert">
          {globalError}
          <button
            type="button"
            onClick={() => setGlobalError(null)}
            aria-label="Dismiss error"
          >
            x
          </button>
        </div>
      )}

      <form onSubmit={handleSubmit} className="chat-panel__form">
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={
            documentReady
              ? 'Ask a question about this document...'
              : 'Select a ready document first'
          }
          disabled={!documentReady || askState === 'asking'}
          maxLength={MAX_QUERY_LENGTH}
          className="chat-panel__input"
          aria-label="Question input"
        />
        <span className="chat-panel__char-count">
          {query.length}/{MAX_QUERY_LENGTH}
        </span>
        <button
          type="submit"
          disabled={!canAsk}
          className="chat-panel__submit"
        >
          {askState === 'asking' ? 'Thinking...' : 'Ask'}
        </button>
      </form>
    </div>
  )
}
