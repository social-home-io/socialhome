import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({
  t: (k: string) => k,
}))

import { EventOverflowMenu } from './EventOverflowMenu'

describe('EventOverflowMenu', () => {
  it('hides the export link until the kebab is clicked', () => {
    const { container } = render(<EventOverflowMenu eventId="ev-1" />)
    expect(container.querySelector('.sh-post-menu')).toBeNull()
  })

  it('reveals the .ics download link when opened', () => {
    const { container, getByRole } = render(<EventOverflowMenu eventId="ev-1" />)
    fireEvent.click(getByRole('button'))
    const link = container.querySelector('a[role=menuitem]') as HTMLAnchorElement
    expect(link).not.toBeNull()
    expect(link.href).toContain('/api/calendars/events/ev-1/export.ics')
    expect(link.hasAttribute('download')).toBe(true)
  })

  it('renders parent-supplied menu items above the export link', () => {
    const { container, getByRole } = render(
      <EventOverflowMenu eventId="ev-1">
        <button role="menuitem" data-testid="extra">Custom</button>
      </EventOverflowMenu>,
    )
    fireEvent.click(getByRole('button', { name: 'event.actions' }))
    const items = container.querySelectorAll('[role=menuitem]')
    // First item is the parent-supplied button, last is the export link.
    expect(items.length).toBe(2)
    expect((items[0] as HTMLElement).getAttribute('data-testid')).toBe('extra')
    expect((items[1] as HTMLAnchorElement).href).toContain('/export.ics')
  })
})
