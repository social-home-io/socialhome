/** @jsxImportSource preact */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, screen, cleanup } from '@testing-library/preact'
import { MessageContextSheet } from './MessageContextSheet'

afterEach(() => cleanup())

describe('MessageContextSheet', () => {
  it('renders quick-react emoji row + the supplied actions', () => {
    render(
      <MessageContextSheet
        onReact={() => {}}
        onPickMore={() => {}}
        actions={[
          { label: 'Reply', glyph: '↩', onClick: () => {} },
          { label: 'Delete', glyph: '🗑', destructive: true, onClick: () => {} },
        ]}
        onClose={() => {}}
      />
    )
    // Quick-react row — at least the 6 default emoji.
    expect(screen.getAllByRole('button', { name: /React with /i }).length).toBeGreaterThanOrEqual(6)
    expect(screen.getByRole('button', { name: /Reply/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Delete/i })).toBeTruthy()
  })

  it('emits onReact + onClose when a quick-react glyph is tapped', () => {
    const onReact = vi.fn()
    const onClose = vi.fn()
    render(
      <MessageContextSheet
        onReact={onReact}
        onPickMore={() => {}}
        actions={[]}
        onClose={onClose}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'React with 👍' }))
    expect(onReact).toHaveBeenCalledWith('👍')
    expect(onClose).toHaveBeenCalled()
  })

  it('emits onPickMore when the "+" tile is tapped', () => {
    const onPickMore = vi.fn()
    render(
      <MessageContextSheet
        onReact={() => {}}
        onPickMore={onPickMore}
        actions={[]}
        onClose={() => {}}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Pick another emoji' }))
    expect(onPickMore).toHaveBeenCalled()
  })

  it('clicking the backdrop closes the sheet', () => {
    const onClose = vi.fn()
    const { container } = render(
      <MessageContextSheet
        onReact={() => {}}
        onPickMore={() => {}}
        actions={[]}
        onClose={onClose}
      />
    )
    const backdrop = container.querySelector('.sh-context-sheet-backdrop')!
    fireEvent.click(backdrop)
    expect(onClose).toHaveBeenCalled()
  })

  it('Escape key closes the sheet', () => {
    const onClose = vi.fn()
    render(
      <MessageContextSheet
        onReact={() => {}}
        onPickMore={() => {}}
        actions={[]}
        onClose={onClose}
      />
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})

describe('MessageContextSheet — read-only thread', () => {
  it('no onReact: no reaction row, the actions stay', () => {
    render(
      <MessageContextSheet
        actions={[{ label: 'Copy text', glyph: '⧉', onClick: () => {} }]}
        onClose={() => {}}
      />
    )
    expect(screen.queryAllByRole('button', { name: /React with /i })).toHaveLength(0)
    expect(screen.queryByRole('group')).toBeNull()
    expect(screen.getByRole('button', { name: /Copy text/i })).toBeTruthy()
  })
})
