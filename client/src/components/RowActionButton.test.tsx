import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { RowActionButton } from './RowActionButton'

describe('RowActionButton', () => {
  it('exports the host class that reveals it', async () => {
    const mod = await import('./RowActionButton')
    expect(mod.ROW_REVEAL_HOST).toBe('sh-row-reveal-host')
  })

  it('renders a labelled icon button on the shared icon-button base', () => {
    const onClick = vi.fn()
    const { getByRole } = render(
      <RowActionButton label="Delete Milk" icon="✕" onClick={onClick} />,
    )
    const btn = getByRole('button', { name: 'Delete Milk' })
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.getAttribute('title')).toBe('Delete Milk')
    expect(btn.className).toContain('sh-icon-btn')
    expect(btn.className).toContain('sh-row-action')
    expect(btn.textContent).toBe('✕')
    fireEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('hides the glyph from assistive tech', () => {
    const { container } = render(
      <RowActionButton label="Delete" icon="✕" onClick={() => {}} />,
    )
    expect(container.querySelector('[aria-hidden="true"]')?.textContent).toBe('✕')
  })

  it('carries the danger and reveal modifiers when asked', () => {
    const { getByRole } = render(
      <RowActionButton label="Delete" icon="✕" danger reveal class="x" onClick={() => {}} />,
    )
    const cls = getByRole('button').className
    expect(cls).toContain('sh-row-action--danger')
    expect(cls).toContain('sh-row-action--reveal')
    expect(cls).toContain('x')
  })
})
