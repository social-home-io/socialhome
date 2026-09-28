import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }))
vi.mock('./Toast', () => ({ showToast: toastMock }))

import { SecretReveal } from './SecretReveal'

beforeEach(() => {
  toastMock.mockReset()
})

describe('SecretReveal', () => {
  it('shows the secret, the one-time warning, and copies on click', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { getByLabelText, getByRole, getByText } = render(
      <SecretReveal title="New token" secret="s3cret" secretLabel="API token" />,
    )
    expect(getByLabelText('API token').textContent).toBe('s3cret')
    expect(getByText(/you won't see it again/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('s3cret'))
    await waitFor(() => expect(getByRole('button', { name: 'Copied ✓' })).toBeTruthy())
  })

  it('falls back to a manual-copy hint when the clipboard is blocked', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    const { getByRole } = render(
      <SecretReveal title="t" secret="x" secretLabel="Secret" />,
    )
    fireEvent.click(getByRole('button', { name: 'Copy' }))
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(expect.stringContaining('manually'), 'error'),
    )
  })

  it('renders a dismiss button only when asked to', () => {
    const onDismiss = vi.fn()
    const { getByRole, rerender, queryByRole } = render(
      <SecretReveal title="t" secret="x" secretLabel="Secret" />,
    )
    expect(queryByRole('button', { name: "I've saved it" })).toBeNull()
    rerender(
      <SecretReveal title="t" secret="x" secretLabel="Secret"
                    dismissLabel="I've saved it" onDismiss={onDismiss} />,
    )
    fireEvent.click(getByRole('button', { name: "I've saved it" }))
    expect(onDismiss).toHaveBeenCalled()
  })
})
