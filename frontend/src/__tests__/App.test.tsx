import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'

const mockDocuments = [
  {
    id: 'doc-1',
    filename: 'policy.pdf',
    file_type: 'pdf',
    status: 'ready',
    created_at: '2024-01-01T00:00:00Z',
    chunk_count: 15,
    error_message: null,
    processed_at: '2024-01-01T00:01:00Z',
  },
  {
    id: 'doc-2',
    filename: 'manual.pdf',
    file_type: 'pdf',
    status: 'processing',
    created_at: '2024-01-02T00:00:00Z',
    chunk_count: null,
    error_message: null,
    processed_at: null,
  },
]

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('renders header', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve([]),
    } as Response)
    render(<App />)
    expect(screen.getByText('Agentic Document Intelligence')).toBeInTheDocument()
  })

  it('loads and displays documents', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(mockDocuments),
    } as Response)
    render(<App />)
    await waitFor(() => {
      expect(screen.getByText('policy.pdf')).toBeInTheDocument()
    })
    expect(screen.getByText('manual.pdf')).toBeInTheDocument()
  })

  it('allows selecting a ready document', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(mockDocuments),
    } as Response)
    const user = userEvent.setup()
    render(<App />)
    await waitFor(() => {
      expect(screen.getByText('policy.pdf')).toBeInTheDocument()
    })
    await user.click(screen.getByText('policy.pdf'))
    const input = screen.getByRole('textbox', { name: /question input/i })
    expect(input).not.toBeDisabled()
  })

  it('shows error banner on fetch failure', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error('Network error'))
    render(<App />)
    await waitFor(() => {
      expect(screen.getByText(/Cannot reach the backend/)).toBeInTheDocument()
    })
  })

  it('shows upload panel', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve([]),
    } as Response)
    render(<App />)
    expect(screen.getByText('Upload PDF')).toBeInTheDocument()
  })

  it('shows chat panel with disabled state', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve([]),
    } as Response)
    render(<App />)
    expect(screen.getByText('Select a document to start chatting.')).toBeInTheDocument()
  })
})
