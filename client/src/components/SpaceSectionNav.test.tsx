/**
 * SpaceSectionNav: section links (not tabs), priority+ fitting, the More
 * disclosure (popover on desktop, bottom sheet on a phone), the two
 * badge kinds and the notification level row.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup, within } from '@testing-library/preact'

const { mockPut, mockGet } = vi.hoisted(() => ({ mockPut: vi.fn(), mockGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: { get: mockGet, put: mockPut, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { SpaceSectionNav } from './SpaceSectionNav'
import type { SpaceSection } from '@/features/spaces/spaceTabs'
import { setLocale } from '@/i18n/i18n'

const ALL: SpaceSection[] = ['feed', 'chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar']

/** jsdom has no layout: give every measured copy a width per label
 *  length and the row a fixed width. */
function layout(rowWidth: number) {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const w = this.textContent ? this.textContent.length * 10 + 20 : 0
    return { width: w, height: 48, top: 0, left: 0, right: w, bottom: 48, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
  })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList?.contains('sh-space-sections__row') ? rowWidth : 0
    },
  })
}

function phone(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on && q.includes('max-width'),
    media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    onchange: null, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

function renderNav(over: Partial<Parameters<typeof SpaceSectionNav>[0]> = {}) {
  const props = {
    spaceId: 's1',
    spaceName: 'Vizeli Family',
    sections: ALL,
    active: 'feed' as SpaceSection,
    chatUnread: 0,
    members: { count: 8, admins: 2 },
    needsYou: null,
    notifLevel: 'all' as const,
    onNotifLevel: vi.fn(),
    settingsHref: '/spaces/s1/settings',
    canEditSections: false,
    ...over,
  }
  return { props, ...render(<SpaceSectionNav {...props} />) }
}

beforeEach(() => {
  ;(window as unknown as Record<string, unknown>).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  phone(false)
  mockPut.mockReset()
  mockGet.mockReset()
  mockGet.mockResolvedValue({
    tasks: { open: 5, mine: 2 }, pages: { count: 3 }, gallery: { albums: 2, items: 5 },
    stickies: { count: 0 }, bazaar: null, calendar: { upcoming: 1, days: 30 },
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
})

const inlineHrefs = (c: Element) =>
  Array.from(c.querySelectorAll('.sh-space-sections__row a')).map((a) => a.getAttribute('href'))

describe('priority+', () => {
  it('a wide row shows every section inline, in the fixed order', () => {
    layout(2000)
    const { container } = renderNav()
    expect(inlineHrefs(container)).toEqual([
      '/spaces/s1', '/spaces/s1/chat', '/spaces/s1/calendar', '/spaces/s1/tasks',
      '/spaces/s1/pages', '/spaces/s1/gallery', '/spaces/s1/stickies', '/spaces/s1/bazaar',
    ])
  })

  it('a phone-width row keeps the first sections and moves the rest into More', () => {
    // Feed 60 · Chat 60 · Calendar 100 · Tasks 70 · More 60 (+2 px gaps).
    layout(360)
    const { container, getByRole } = renderNav()
    expect(inlineHrefs(container)).toEqual(
      ['/spaces/s1', '/spaces/s1/chat', '/spaces/s1/calendar', '/spaces/s1/tasks'])
    fireEvent.click(getByRole('button', { name: /^More/ }))
    const panel = container.querySelector('.sh-space-more__popover') as HTMLElement
    const rows = Array.from(panel.querySelectorAll('a')).map((a) => a.getAttribute('href'))
    // Members first, then the overflow in the same order — nothing reordered.
    expect(rows).toEqual([
      '/spaces/s1/members', '/spaces/s1/pages', '/spaces/s1/gallery',
      '/spaces/s1/stickies', '/spaces/s1/bazaar',
    ])
  })

  it('German labels are longer: fewer fit in the same row', async () => {
    layout(360)
    await setLocale('de')
    try {
      const { container } = renderNav()
      expect(inlineHrefs(container).length).toBeLessThan(4)
      expect(inlineHrefs(container)[0]).toBe('/spaces/s1')
    } finally {
      await setLocale('en')
    }
  })

  it('a section open from More marks the More button and its row', () => {
    layout(360)
    const { getByRole, container } = renderNav({ active: 'gallery' })
    const more = getByRole('button', { name: 'More, showing Gallery' })
    expect(more.classList.contains('sh-space-section--active')).toBe(true)
    fireEvent.click(more)
    const row = within(container.querySelector('.sh-space-more__popover') as HTMLElement)
      .getByRole('link', { name: /^Gallery/ })
    expect(row.getAttribute('aria-current')).toBe('page')
  })
})

describe('links and badges', () => {
  it('the open section is a link with aria-current="page"; no tab roles', () => {
    layout(2000)
    const { getByRole, queryAllByRole } = renderNav({ active: 'calendar' })
    expect(getByRole('navigation', { name: 'Space sections' })).toBeTruthy()
    expect(getByRole('link', { name: 'Calendar' }).getAttribute('aria-current')).toBe('page')
    expect(getByRole('link', { name: 'Feed' }).getAttribute('aria-current')).toBeNull()
    expect(queryAllByRole('tab')).toEqual([])
  })

  it('unread is a quiet count, read as "Chat, 4 unread"', () => {
    layout(2000)
    const { getByRole } = renderNav({ chatUnread: 4 })
    const chat = getByRole('link', { name: 'Chat, 4 unread' })
    expect(chat.querySelector('.sh-space-badge--unread')?.textContent).toBe('4')
    expect(chat.querySelector('.sh-space-badge--needs')).toBeNull()
  })

  it('needs-you is a filled count rolled onto More, read as "More, 3 need you"', () => {
    layout(2000)
    const { getByRole, container } = renderNav({ needsYou: { review: 2, reports: 0, joinRequests: 1, proposals: 0 } })
    const more = getByRole('button', { name: 'More, 3 need you' })
    expect(more.querySelector('.sh-space-badge--needs')?.textContent).toBe('3')
    fireEvent.click(more)
    const mod = within(container.querySelector('.sh-space-more__popover') as HTMLElement)
      .getByRole('link', { name: /^Moderation/ })
    expect(mod.textContent).toContain('2 to review · 1 join request')
    expect(mod.querySelector('.sh-space-badge--needs')?.textContent).toBe('3')
  })

  it('a proposal awaiting my vote counts on More, not on Moderation', () => {
    layout(2000)
    const { getByRole, container } = renderNav({ needsYou: { review: 2, reports: 0, joinRequests: 1, proposals: 1 } })
    const more = getByRole('button', { name: 'More, 4 need you' })
    expect(more.querySelector('.sh-space-badge--needs')?.textContent).toBe('4')
    fireEvent.click(more)
    const mod = within(container.querySelector('.sh-space-more__popover') as HTMLElement)
      .getByRole('link', { name: /^Moderation/ })
    expect(mod.querySelector('.sh-space-badge--needs')?.textContent).toBe('3')
  })

  it('the gear is one link; none without a settings href', () => {
    layout(2000)
    const { getByRole } = renderNav()
    expect(getByRole('link', { name: 'Space settings' }).getAttribute('href')).toBe('/spaces/s1/settings')
    cleanup()
    const r2 = renderNav({ settingsHref: null })
    expect(r2.queryByRole('link', { name: 'Space settings' })).toBeNull()
  })
})

describe('More on a desktop: a non-modal popover', () => {
  it('toggles aria-expanded, points aria-controls at the panel, Esc closes and returns focus', async () => {
    layout(2000)
    const { getByRole, container } = renderNav()
    const more = getByRole('button', { name: /^More/ })
    more.focus()
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    const panel = container.querySelector('.sh-space-more__popover') as HTMLElement
    expect(panel.id).toBe(more.getAttribute('aria-controls'))
    expect(panel.getAttribute('aria-modal')).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(more.getAttribute('aria-expanded')).toBe('false'))
    expect(container.querySelector('.sh-space-more__popover')).toBeNull()
    expect(document.activeElement).toBe(more)
  })

  it('tabbing out of it closes it; tabbing through its rows does not', async () => {
    layout(2000)
    const { getByRole, container } = renderNav()
    fireEvent.click(getByRole('button', { name: /^More/ }))
    const members = within(container.querySelector('.sh-space-more__popover') as HTMLElement)
      .getByRole('link', { name: /^Members/ })
    members.focus()
    expect(container.querySelector('.sh-space-more__popover')).toBeTruthy()
    getByRole('link', { name: 'Space settings' }).focus()
    await waitFor(() => expect(container.querySelector('.sh-space-more__popover')).toBeNull())
  })

  it('a click outside closes it', () => {
    layout(2000)
    const { getByRole, container } = renderNav()
    fireEvent.click(getByRole('button', { name: /^More/ }))
    fireEvent.mouseDown(document.body)
    expect(container.querySelector('.sh-space-more__popover')).toBeNull()
  })

  it('Edit sections only for admins', () => {
    layout(2000)
    const { getByRole, queryByRole } = renderNav({ canEditSections: true })
    fireEvent.click(getByRole('button', { name: /^More/ }))
    // Deep-links to the Sections part of the settings page.
    expect(getByRole('link', { name: 'Edit sections' }).getAttribute('href')).toBe('/spaces/s1/settings/sections')
    cleanup()
    const r2 = renderNav({ canEditSections: false })
    fireEvent.click(r2.getByRole('button', { name: /^More/ }))
    expect(queryByRole('link', { name: 'Edit sections' })).toBeNull()
  })
})

describe('More on a phone: a bottom sheet', () => {
  it('is a modal dialog with a heading and a close button; Esc closes it', async () => {
    layout(360)
    phone(true)
    const { getByRole, queryByRole } = renderNav()
    const more = getByRole('button', { name: /^More/ })
    more.focus()
    fireEvent.click(more)
    const dialog = getByRole('dialog', { name: 'More in Vizeli Family' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(within(dialog).getByRole('heading', { name: 'More in Vizeli Family' })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: 'Close dialog' })).toBeTruthy()
    expect(within(dialog).getByRole('link', { name: /^Members/ })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(queryByRole('dialog')).toBeNull())
    expect(more.getAttribute('aria-expanded')).toBe('false')
    await waitFor(() => expect(document.activeElement).toBe(more))
  })
})

describe('Your notifications', () => {
  it('shows the level, opens a radio choice, saves the pick and goes back', async () => {
    layout(2000)
    mockPut.mockResolvedValue({ level: 'mentions', default_level: 'all' })
    const onNotifLevel = vi.fn()
    const { getByRole, container } = renderNav({ onNotifLevel })
    fireEvent.click(getByRole('button', { name: /^More/ }))
    const row = getByRole('button', { name: /^Your notifications/ })
    expect(row.textContent).toContain('Everything')
    fireEvent.click(row)
    const all = getByRole('radio', { name: /^Everything/ }) as HTMLInputElement
    expect(all.checked).toBe(true)
    await waitFor(() => expect(document.activeElement).toBe(all))
    fireEvent.click(getByRole('radio', { name: /^Replies and mentions/ }))
    await waitFor(() => expect(onNotifLevel).toHaveBeenCalledWith('mentions'))
    expect(mockPut).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs', { level: 'mentions' })
    await waitFor(() => expect(container.querySelector('[data-row="notifications"]')).toBeTruthy())
  })

  it('no row while the level is unknown', () => {
    layout(2000)
    const { getByRole, queryByRole } = renderNav({ notifLevel: null })
    fireEvent.click(getByRole('button', { name: /^More/ }))
    expect(queryByRole('button', { name: /^Your notifications/ })).toBeNull()
  })
})

describe('what the hidden sections hold', () => {
  it('reads /summary when More opens and shows it under each hidden section', async () => {
    layout(360)
    const { getByRole } = renderNav()
    expect(mockGet).not.toHaveBeenCalled()
    fireEvent.click(getByRole('button', { name: /^More/ }))
    expect(mockGet).toHaveBeenCalledWith('/api/spaces/s1/summary')
    await waitFor(() =>
      expect(getByRole('link', { name: /^Pages/ }).textContent).toContain('3 pages'))
    expect(getByRole('link', { name: /^Gallery/ }).textContent).toContain('2 albums · 5 photos')
    expect(getByRole('link', { name: /^Stickies/ }).textContent).toContain('No notes yet')
    // Bazaar is off on the server side of the summary: no line.
    expect(getByRole('link', { name: /^Bazaar/ }).querySelector('.sh-space-more__meta')).toBeNull()
  })

  it('a failed read leaves the rows label-only', async () => {
    layout(360)
    mockGet.mockRejectedValue(new Error('offline'))
    const { getByRole } = renderNav()
    fireEvent.click(getByRole('button', { name: /^More/ }))
    await waitFor(() => expect(mockGet).toHaveBeenCalled())
    expect(getByRole('link', { name: /^Pages/ }).querySelector('.sh-space-more__meta')).toBeNull()
  })
})

describe('Leave space', () => {
  it('a member gets "Leave space" in the footer; it closes More and asks the page', () => {
    layout(2000)
    const onLeave = vi.fn()
    const { getByRole, queryByRole } = renderNav({ leave: 'member', onLeave })
    const more = getByRole('button', { name: /^More/ })
    fireEvent.click(more)
    fireEvent.click(getByRole('button', { name: 'Leave space' }))
    expect(onLeave).toHaveBeenCalled()
    expect(queryByRole('button', { name: 'Leave space' })).toBeNull()
    expect(document.activeElement).toBe(more)
  })

  it('the owner reads why they cannot leave yet; a follower sees neither', () => {
    layout(2000)
    const { getByRole, queryByRole, container, unmount } = renderNav({ leave: 'owner', onLeave: vi.fn() })
    fireEvent.click(getByRole('button', { name: /^More/ }))
    expect(queryByRole('button', { name: 'Leave space' })).toBeNull()
    expect(container.querySelector('.sh-space-more__owner-note')?.textContent).toContain('You own this space')
    // …and where to hand it on: the owner's transfer lives in settings.
    expect(getByRole('link', { name: 'Hand over ownership' }).getAttribute('href'))
      .toBe('/spaces/s1/settings/danger')
    unmount()
    const r = renderNav({ leave: null })
    fireEvent.click(r.getByRole('button', { name: /^More/ }))
    expect(r.queryByRole('button', { name: 'Leave space' })).toBeNull()
    expect(r.container.querySelector('.sh-space-more__owner-note')).toBeNull()
  })
})
