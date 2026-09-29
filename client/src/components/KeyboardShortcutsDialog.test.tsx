import { describe, it, expect, afterEach } from 'vitest'
import { render, fireEvent, act, cleanup } from '@testing-library/preact'
import { KeyboardShortcutsDialog } from './KeyboardShortcutsDialog'
import { shortcutsHelpOpen } from '@/lib/shortcuts'

describe('KeyboardShortcutsDialog', () => {
  afterEach(() => {
    cleanup()
    shortcutsHelpOpen.value = false
  })

  it('renders nothing while closed', () => {
    const { container } = render(<KeyboardShortcutsDialog />)
    expect(container.textContent).toBe('')
  })

  it('opens as an accessible modal listing every shortcut', () => {
    shortcutsHelpOpen.value = true
    const { getByRole } = render(<KeyboardShortcutsDialog />)
    const dialog = getByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    const text = dialog.textContent ?? ''
    for (const label of ['Jump to a page', 'Search', 'Write a post or message', 'Show this help', 'Close a dialog']) {
      expect(text).toContain(label)
    }
    expect(dialog.querySelectorAll('kbd').length).toBeGreaterThanOrEqual(6)
  })

  it('Escape closes it', () => {
    shortcutsHelpOpen.value = true
    const { queryByRole } = render(<KeyboardShortcutsDialog />)
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(shortcutsHelpOpen.value).toBe(false)
    expect(queryByRole('dialog')).toBeNull()
  })

  it('the close button closes it', () => {
    shortcutsHelpOpen.value = true
    const { getByLabelText } = render(<KeyboardShortcutsDialog />)
    act(() => { fireEvent.click(getByLabelText('Close dialog')) })
    expect(shortcutsHelpOpen.value).toBe(false)
  })
})
