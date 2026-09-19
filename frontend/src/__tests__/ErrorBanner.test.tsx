import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorBanner } from '../components/ErrorBanner'

describe('ErrorBanner', () => {
  it('renders error message', () => {
    render(<ErrorBanner message="Something went wrong" />)
    expect(screen.getByText('Something went wrong')).toBeInTheDocument()
  })

  it('renders dismiss button when onDismiss provided', () => {
    const onDismiss = vi.fn()
    render(<ErrorBanner message="Error" onDismiss={onDismiss} />)
    const btn = screen.getByRole('button', { name: /dismiss/i })
    expect(btn).toBeInTheDocument()
  })

  it('does not render dismiss button without onDismiss', () => {
    render(<ErrorBanner message="Error" />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('has alert role', () => {
    render(<ErrorBanner message="Error" />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})
