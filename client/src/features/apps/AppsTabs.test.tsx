import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { AppsTabs } from './AppsTabs'
import type { AppsTab } from './appsTab'

function Host({ start = 'yours' as AppsTab, spy = vi.fn() }) {
  const [tab, setTab] = useState<AppsTab>(start)
  return <AppsTabs value={tab} onChange={(t) => { spy(t); setTab(t) }} />
}

describe('AppsTabs', () => {
  it('is a tab strip: Your apps and Store, the selected one in the Tab order', () => {
    const { getByRole, getAllByRole } = render(<Host />)
    expect(getByRole('tablist').getAttribute('aria-label')).toBe('Apps')
    const tabs = getAllByRole('tab')
    expect(tabs.map(t => t.textContent)).toEqual(['Your apps', 'Store'])
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
    expect(tabs[0].tabIndex).toBe(0)
    expect(tabs[1].tabIndex).toBe(-1)
    expect(tabs[0].getAttribute('aria-controls')).toBe('sh-apps-panel-yours')
    expect(tabs[0].id).toBe('sh-apps-tab-yours')
  })

  it('a click switches', () => {
    const spy = vi.fn()
    const { getByRole } = render(<Host spy={spy} />)
    fireEvent.click(getByRole('tab', { name: 'Store' }))
    expect(spy).toHaveBeenCalledWith('store')
    expect(getByRole('tab', { name: 'Store' }).getAttribute('aria-selected')).toBe('true')
  })

  it('arrow keys, Home and End move focus and switch', () => {
    const spy = vi.fn()
    const { getByRole } = render(<Host spy={spy} />)
    const yours = getByRole('tab', { name: 'Your apps' })
    yours.focus()
    fireEvent.keyDown(yours, { key: 'ArrowRight' })
    expect(spy).toHaveBeenLastCalledWith('store')
    expect(document.activeElement).toBe(getByRole('tab', { name: 'Store' }))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' })
    expect(spy).toHaveBeenLastCalledWith('yours')
    fireEvent.keyDown(document.activeElement!, { key: 'End' })
    expect(spy).toHaveBeenLastCalledWith('store')
    fireEvent.keyDown(document.activeElement!, { key: 'Home' })
    expect(spy).toHaveBeenLastCalledWith('yours')
    expect(document.activeElement).toBe(yours)
  })

  it('mirrors the arrows in a right-to-left language', () => {
    const spy = vi.fn()
    const { getByRole } = render(<div dir="rtl"><Host spy={spy} /></div>)
    fireEvent.keyDown(getByRole('tab', { name: 'Your apps' }), { key: 'ArrowLeft' })
    expect(spy).toHaveBeenLastCalledWith('store')
  })
})
