import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/preact'

import { setLocale } from '@/i18n/i18n'
import { catalogFreshness, _resetAppsForTest } from '@/store/apps'
import {
  CatalogFreshnessNote,
  catalogFreshnessText,
  formatCheckedAt,
} from './CatalogFreshnessNote'

afterEach(async () => {
  _resetAppsForTest()
  await setLocale('en')
})

const AT = '2026-10-28T20:15:00+00:00'

describe('catalogFreshnessText', () => {
  it('says nothing while the app list answers', () => {
    expect(catalogFreshnessText({ checkedAt: AT, stale: false, failed: false })).toBeNull()
    expect(catalogFreshnessText({ checkedAt: null, stale: false, failed: false })).toBeNull()
  })

  it('a stale check says when the app list last answered', () => {
    const text = catalogFreshnessText({ checkedAt: AT, stale: true, failed: false })
    expect(text).toBe(`Couldn't reach the app list — showing what we knew at ${formatCheckedAt(AT)}.`)
    expect(text).not.toMatch(/up to date/)
  })

  it('a check with nothing held says it could not reach the app list', () => {
    expect(catalogFreshnessText({ checkedAt: null, stale: false, failed: true }))
      .toBe("Couldn't reach the app list. Check the connection and try again.")
    expect(catalogFreshnessText({ checkedAt: 'not a date', stale: true, failed: false }))
      .toBe("Couldn't reach the app list. Check the connection and try again.")
  })

  it('reads in German', async () => {
    await setLocale('de')
    expect(catalogFreshnessText({ checkedAt: AT, stale: true, failed: false }))
      .toMatch(/^Die App-Liste war nicht erreichbar – angezeigt wird der Stand von .+\.$/)
  })
})

describe('CatalogFreshnessNote', () => {
  it('renders nothing while the app list answers', () => {
    catalogFreshness.value = { checkedAt: AT, stale: false, failed: false }
    const { container } = render(<CatalogFreshnessNote />)
    expect(container.querySelector('.sh-apps-stale')).toBeNull()
  })

  it('shows the stale line as a status, with Retry for admins', () => {
    catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    const onRetry = vi.fn()
    const { getByRole, getByText } = render(<CatalogFreshnessNote onRetry={onRetry} />)
    expect(getByRole('status').textContent).toContain("Couldn't reach the app list")
    fireEvent.click(getByText('Retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('members get the line without a Retry', () => {
    catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    const { queryByText, getByRole } = render(<CatalogFreshnessNote />)
    expect(getByRole('status')).toBeTruthy()
    expect(queryByText('Retry')).toBeNull()
  })
})
