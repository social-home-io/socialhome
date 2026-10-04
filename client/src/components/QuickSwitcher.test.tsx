import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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

describe('QuickSwitcher links under the HA ingress prefix', () => {
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
  })

  it('every result keeps the ingress prefix (/api/hassio_ingress/<token>/)', async () => {
    const { QuickSwitcher } = await import('./QuickSwitcher')
    fireEvent.keyDown(document, { key: 'k', ctrlKey: true })
    const { container } = render(<QuickSwitcher />)
    const hrefs = [...container.querySelectorAll('a')].map(a => a.getAttribute('href'))
    expect(hrefs).toContain('/api/hassio_ingress/tok/calendar?tab=timetable')
    expect(hrefs.length).toBeGreaterThan(5)
    expect(hrefs.filter(h => !h?.startsWith('/api/hassio_ingress/tok/'))).toEqual([])
  })
})
