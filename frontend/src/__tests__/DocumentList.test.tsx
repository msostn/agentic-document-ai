import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DocumentList } from '../components/DocumentList'
import type { Document } from '../types'

const makeDoc = (overrides: Partial<Document> = {}): Document => ({
  id: 'doc-1',
  filename: 'test.pdf',
  file_type: 'pdf',
  status: 'ready',
  created_at: '2024-01-01T00:00:00Z',
  chunk_count: 10,
  error_message: null,
  processed_at: '2024-01-01T00:01:00Z',
  ...overrides,
})

describe('DocumentList', () => {
  it('renders empty state', () => {
    render(<DocumentList documents={[]} activeDocumentId={null} onSelect={vi.fn()} />)
    expect(screen.getByText('No documents uploaded yet.')).toBeInTheDocument()
  })

  it('renders document list', () => {
    const docs = [makeDoc({ id: '1', filename: 'a.pdf' }), makeDoc({ id: '2', filename: 'b.pdf', status: 'processing' })]
    render(<DocumentList documents={docs} activeDocumentId={null} onSelect={vi.fn()} />)
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
    expect(screen.getByText('b.pdf')).toBeInTheDocument()
  })

  it('shows chunk count', () => {
    const docs = [makeDoc({ chunk_count: 42 })]
    render(<DocumentList documents={docs} activeDocumentId={null} onSelect={vi.fn()} />)
    expect(screen.getByText('42 chunks')).toBeInTheDocument()
  })

  it('disables non-ready documents', () => {
    const docs = [makeDoc({ id: '1', status: 'processing' })]
    render(<DocumentList documents={docs} activeDocumentId={null} onSelect={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /test\.pdf/i })
    expect(btn).toBeDisabled()
  })

  it('enables ready documents', () => {
    const docs = [makeDoc({ id: '1', status: 'ready' })]
    render(<DocumentList documents={docs} activeDocumentId={null} onSelect={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /test\.pdf/i })
    expect(btn).not.toBeDisabled()
  })

  it('highlights active document', () => {
    const docs = [makeDoc({ id: '1' })]
    render(<DocumentList documents={docs} activeDocumentId="1" onSelect={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /test\.pdf/i })
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('shows error message', () => {
    const docs = [makeDoc({ id: '1', error_message: 'Parse failed' })]
    render(<DocumentList documents={docs} activeDocumentId={null} onSelect={vi.fn()} />)
    expect(screen.getByText('Parse failed')).toBeInTheDocument()
  })
})
