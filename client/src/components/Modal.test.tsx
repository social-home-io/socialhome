import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { Modal } from './Modal'

describe('Modal', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <Modal open={false} title="T" onClose={() => {}}>Content</Modal>
    )
    expect(container.textContent).toBe('')
  })

  it('shows title and children when open', () => {
    const { getByText } = render(
      <Modal open={true} title="My Modal" onClose={() => {}}>
        <p>Hello modal</p>
      </Modal>
    )
    expect(getByText('My Modal')).toBeTruthy()
    expect(getByText('Hello modal')).toBeTruthy()
  })

  it('calls onClose when X clicked', () => {
    const fn = vi.fn()
    const { container } = render(
      <Modal open={true} title="T" onClose={fn}>C</Modal>
    )
    const closeBtn = container.querySelector('.sh-modal-close')
    if (closeBtn) fireEvent.click(closeBtn)
    expect(fn).toHaveBeenCalled()
  })

  // Regression: SpaceCreateDialog passed an inline arrow onClose, so
  // every parent re-render (one per keystroke in the controlled
  // ``name`` input) handed Modal a fresh function reference. With
  // ``onClose`` in the focus-trap effect's dep list, the effect re-ran
  // and re-focused the first focusable element (the close ×) on every
  // keystroke, kicking the user out of the input. The fix stashes
  // onClose in a ref and depends only on ``open``.
  it('does not steal focus from inputs when the parent re-renders', async () => {
    function Host() {
      const [n, setN] = useState('')
      return (
        <Modal open={true} title="T" onClose={() => {}}>
          <input
            data-testid="name"
            value={n}
            onInput={(e) => setN((e.target as HTMLInputElement).value)}
          />
        </Modal>
      )
    }
    const { getByTestId } = render(<Host />)
    const input = getByTestId('name') as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)
    fireEvent.input(input, { target: { value: 'a' } })
    fireEvent.input(input, { target: { value: 'ab' } })
    fireEvent.input(input, { target: { value: 'abc' } })
    // After three keystrokes the focused element must still be the
    // input, not the close × button.
    expect(document.activeElement).toBe(input)
  })
})
