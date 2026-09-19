import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SourcesList } from '../components/SourcesList'

describe('SourcesList', () => {
  it('renders nothing when sources are empty', () => {
    const { container } = render(<SourcesList sources={[]} />)
    expect(container.innerHTML).toBe('')
  })

  it('renders source items', () => {
    const sources = [
      { chunk_id: 'c1', chunk_index: 0, page_number: 1 },
      { chunk_id: 'c2', chunk_index: 3, page_number: 5 },
    ]
    render(<SourcesList sources={sources} />)
    expect(screen.getByText('Page 1')).toBeInTheDocument()
    expect(screen.getByText('Page 5')).toBeInTheDocument()
    expect(screen.getByText('Chunk 0')).toBeInTheDocument()
    expect(screen.getByText('Chunk 3')).toBeInTheDocument()
  })

  it('renders Sources heading', () => {
    const sources = [{ chunk_id: 'c1', chunk_index: 0, page_number: 1 }]
    render(<SourcesList sources={sources} />)
    expect(screen.getByText('Sources')).toBeInTheDocument()
  })
})
