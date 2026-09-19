import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../components/StatusBadge'

describe('StatusBadge', () => {
  it('renders processing status', () => {
    render(<StatusBadge status="processing" />)
    expect(screen.getByText('Processing')).toBeInTheDocument()
  })

  it('renders ready status', () => {
    render(<StatusBadge status="ready" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('renders failed status', () => {
    render(<StatusBadge status="failed" />)
    expect(screen.getByText('Failed')).toBeInTheDocument()
  })

  it('renders empty status', () => {
    render(<StatusBadge status="empty" />)
    expect(screen.getByText('No text found')).toBeInTheDocument()
  })

  it('applies correct CSS class', () => {
    const { container } = render(<StatusBadge status="ready" />)
    expect(container.firstChild).toHaveClass('status-badge', 'status-badge--ready')
  })
})
