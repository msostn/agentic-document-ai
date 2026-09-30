import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ChatPanel } from '../components/ChatPanel'

describe('ChatPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('shows disabled message when no document selected', () => {
    render(<ChatPanel documentId={null} documentReady={false} />)
    expect(screen.getByText('Select a document to start chatting.')).toBeInTheDocument()
  })

  it('shows disabled message when document not ready', () => {
    render(<ChatPanel documentId="doc-1" documentReady={false} />)
    expect(screen.getByText('Document is not ready for questions.')).toBeInTheDocument()
  })

  it('enables input when document is ready', () => {
    render(<ChatPanel documentId="doc-1" documentReady={true} />)
    const input = screen.getByRole('textbox', { name: /question input/i })
    expect(input).not.toBeDisabled()
  })

  it('disables submit when input is empty', () => {
    render(<ChatPanel documentId="doc-1" documentReady={true} />)
    const btn = screen.getByRole('button', { name: /ask/i })
    expect(btn).toBeDisabled()
  })

  it('enables submit when input has text', async () => {
    const user = userEvent.setup()
    render(<ChatPanel documentId="doc-1" documentReady={true} />)
    const input = screen.getByRole('textbox', { name: /question input/i })
    await user.type(input, 'What is this?')
    const btn = screen.getByRole('button', { name: /ask/i })
    expect(btn).not.toBeDisabled()
  })

  it('shows character counter', () => {
    render(<ChatPanel documentId="doc-1" documentReady={true} />)
    expect(screen.getByText('0/8000')).toBeInTheDocument()
  })

  it('clears messages when documentId changes', () => {
    const { rerender } = render(<ChatPanel documentId="doc-1" documentReady={true} />)
    rerender(<ChatPanel documentId="doc-2" documentReady={true} />)
    expect(screen.queryByText('You')).not.toBeInTheDocument()
  })

  it('does not deliver an in-flight answer to another document', async () => {
    let resolveAsk: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      resolveAsk = resolve
    })
    vi.mocked(fetch).mockReturnValueOnce(pending as Promise<Response>)

    const user = userEvent.setup()
    const { rerender } = render(<ChatPanel documentId="doc-1" documentReady={true} />)

    const input = screen.getByRole('textbox', { name: /question input/i })
    await user.type(input, 'What is the deductible?')
    await user.click(screen.getByRole('button', { name: /ask/i }))

    // The user switches documents while the request is still in flight.
    rerender(<ChatPanel documentId="doc-2" documentReady={true} />)

    await act(async () => {
      resolveAsk({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            document_id: 'doc-1',
            query: 'What is the deductible?',
            answer: 'STALE_ANSWER_BELONGING_TO_DOC_1',
            sources: [],
            context_status: 'ok',
            model: 'qwen3:4b',
          }),
      })
      await pending
    })

    expect(
      screen.queryByText('STALE_ANSWER_BELONGING_TO_DOC_1'),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('You')).not.toBeInTheDocument()
  })
})
