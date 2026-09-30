import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

describe('QuickSwitcher', () => {
  it('module exports exist', async () => {
    const mod = await import('./QuickSwitcher')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('offers Timetable, pointing at the Calendar page tab', async () => {
    const { QuickSwitcher } = await import('./QuickSwitcher')
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true })
    const { container } = render(<QuickSwitcher />)
    const links = Array.from(container.querySelectorAll('a'))
    const tt = links.find(a => a.textContent?.includes('Timetable'))
    expect(tt?.getAttribute('href')).toBe('/calendar?tab=timetable')
    const labels = links.map(a => a.textContent)
    expect(labels.findIndex(l => l?.includes('Timetable')))
      .toBe(labels.findIndex(l => l?.includes('Calendar')) + 1)
  })
})
