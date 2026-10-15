import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

import { HANDLE_ERROR_ID, HandleEditor, handleBaseline } from './HandleEditor'

// The handle is now one field of Settings › Profile's single form: the
// form owns the value and the save (``ProfileTab.test.tsx`` covers the
// POST /api/me/handle round-trip, the 422 mapping and partial failure).
// These tests pin the field itself.

describe('handleBaseline', () => {
  it('is the public handle when one is set', () => {
    expect(handleBaseline({ handle: 'alice_h', username: 'alice' })).toBe('alice_h')
  })

  it('falls back to the username while the handle is still null (legacy rows)', () => {
    expect(handleBaseline({ handle: null, username: 'alice' })).toBe('alice')
  })

  it('is empty without a user', () => {
    expect(handleBaseline(null)).toBe('')
  })
})

describe('HandleEditor', () => {
  it('renders a labelled handle field with the given value and the hint', () => {
    const { getByLabelText, getByText } = render(
      <HandleEditor value="alice" onInput={() => {}} error={null} />,
    )
    const input = getByLabelText('Public @handle') as HTMLInputElement
    expect(input.value).toBe('alice')
    expect(input.getAttribute('maxLength') ?? input.maxLength.toString()).toBe('32')
    expect(getByText(/this is how others find you/i)).toBeTruthy()
    // The hint describes the field; no error yet.
    expect(input.getAttribute('aria-describedby')).toBe('sh-profile-handle-hint')
    expect(input.getAttribute('aria-invalid')).toBeNull()
  })

  it('shows the "@" as a fixed prefix, not part of the value', () => {
    const { container } = render(
      <HandleEditor value="alice" onInput={() => {}} error={null} />,
    )
    expect(container.querySelector('.sh-profile-handle__at')?.textContent).toBe('@')
  })

  it('reports every keystroke to the form', () => {
    const onInput = vi.fn()
    const { getByLabelText } = render(
      <HandleEditor value="alice" onInput={onInput} error={null} />,
    )
    fireEvent.input(getByLabelText('Public @handle'), { target: { value: 'alice_2' } })
    expect(onInput).toHaveBeenCalledWith('alice_2')
  })

  it('shows a server error in a role=alert tied to the input', () => {
    const { getByLabelText, getByRole } = render(
      <HandleEditor value="taken" onInput={() => {}} error="That handle is taken." />,
    )
    const input = getByLabelText('Public @handle')
    expect(getByRole('alert').textContent).toBe('That handle is taken.')
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect(input.getAttribute('aria-describedby')).toContain(HANDLE_ERROR_ID)
  })

  it('locks while the form saves', () => {
    const { getByLabelText } = render(
      <HandleEditor value="alice" onInput={() => {}} error={null} disabled />,
    )
    expect((getByLabelText('Public @handle') as HTMLInputElement).disabled).toBe(true)
  })
})
