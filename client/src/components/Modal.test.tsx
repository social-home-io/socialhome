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
  it('restores focus to the trigger element when closed', async () => {
    function Host({ open }: { open: boolean }) {
      return (
        <>
          <button data-testid="trigger">open</button>
          <Modal open={open} title="T" onClose={() => {}}>
            <button data-testid="inside">inside</button>
          </Modal>
        </>
      )
    }
    const { getByTestId, rerender } = render(<Host open={false} />)
    const trigger = getByTestId('trigger') as HTMLButtonElement
    trigger.focus()
    expect(document.activeElement).toBe(trigger)
    rerender(<Host open={true} />)
    // Modal moves focus to its first focusable child.
    expect(document.activeElement).not.toBe(trigger)
    rerender(<Host open={false} />)
    // On close, focus returns to the element that had it before open.
    expect(document.activeElement).toBe(trigger)
  })

  it('renders the bottom-sheet drag-grabber affordance when open', () => {
    const { container } = render(
      <Modal open={true} title="T" onClose={() => {}}>C</Modal>,
    )
    // The grabber is purely decorative on desktop (hidden via CSS) but
    // present in the DOM so mobile users see it without a JS check.
    expect(container.querySelector('.sh-modal-grabber')).not.toBeNull()
  })

  it('keeps focus on the dialog itself on touch viewports', () => {
    // Pretend we're on a coarse-pointer device (phone / tablet).
    // The Modal then lands focus on the dialog (tabindex=-1) rather
    // than the first input — keeps the soft keyboard from popping
    // open and covering half the bottom-sheet.
    const realMatchMedia = window.matchMedia
    window.matchMedia = ((query: string) => ({
      matches: query.includes('coarse'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
    try {
      const { container } = render(
        <Modal open={true} title="T" onClose={() => {}}>
          <input data-testid="first-input" />
        </Modal>,
      )
      const dialog = container.querySelector('[role=dialog]')
      expect(document.activeElement).toBe(dialog)
    } finally {
      window.matchMedia = realMatchMedia
    }
  })

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

  it('Escape closes only the topmost dialog of a stack', () => {
    // A confirm prompt opens over the dialog that asked for it. One
    // Escape used to close both, so cancelling a revoke also threw the
    // user out of the invite tray they were working in.
    const outer = vi.fn()
    const inner = vi.fn()
    const { rerender } = render(
      <>
        <Modal open={true} title="Outer" onClose={outer}>outer body</Modal>
        <Modal open={true} title="Inner" onClose={inner}>inner body</Modal>
      </>
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()

    // With the inner one gone the outer takes Escape again.
    rerender(
      <>
        <Modal open={true} title="Outer" onClose={outer}>outer body</Modal>
        <Modal open={false} title="Inner" onClose={inner}>inner body</Modal>
      </>
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(outer).toHaveBeenCalledTimes(1)
  })
})
