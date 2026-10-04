/**
 * Tests for HouseholdToggles — verifies the module exports and that the
 * Toggles interface includes Presence/Gallery and NOT Highlights/Momentum.
 *
 * The component loads data from ``/api/household/preferences`` and sends
 * updates to the same endpoint via PUT.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({
      feat_feed: true, feat_pages: true, feat_tasks: true,
      feat_stickies: true, feat_calendar: true,
      feat_presence: true, feat_gallery: true, feat_timetable: true,
      allow_text: true, allow_image: true, allow_video: true,
      allow_file: true, allow_poll: true, allow_schedule: true,
      allow_highlight_share: true,
      allow_link_preview: true,
      household_name: 'Test Household',
    }),
    put: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/ws', () => ({
  ws: {
    on: vi.fn().mockReturnValue(() => {}),
  },
}))

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

describe('HouseholdToggles', () => {
  beforeEach(async () => {
    // Reset the module-level signal between tests
    const mod = await import('./HouseholdToggles')
    mod.toggles.value = null
  })

  it('module exports exist (loadToggles, toggles, HouseholdToggles)', async () => {
    const mod = await import('./HouseholdToggles')
    expect(mod.loadToggles).toBeTruthy()
    expect(mod.toggles).toBeTruthy()
    expect(mod.HouseholdToggles).toBeTruthy()
  })

  it('loadToggles fetches from /api/household/preferences', async () => {
    const { api } = await import('@/api')
    const { loadToggles } = await import('./HouseholdToggles')
    await loadToggles()
    expect(api.get).toHaveBeenCalledWith('/api/household/preferences')
  })

  it('loadToggles populates feat_presence and feat_gallery', async () => {
    const { loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    expect(toggles.value?.feat_presence).toBe(true)
    expect(toggles.value?.feat_gallery).toBe(true)
  })

  it('toggles signal does NOT include feat_highlights or feat_momentum after load', async () => {
    const { loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    // The Toggles interface no longer defines these fields; they must
    // not appear in the populated signal.
    expect((toggles.value as unknown as Record<string, unknown>)['feat_highlights']).toBeUndefined()
    expect((toggles.value as unknown as Record<string, unknown>)['feat_momentum']).toBeUndefined()
  })

  it('renders a card per feature + post type (titles + subtitles)', async () => {
    const { HouseholdToggles, loadToggles } = await import('./HouseholdToggles')
    await loadToggles()
    const { getByText, container } = render(<HouseholdToggles />)
    // Feature cards
    expect(getByText('Feed')).toBeTruthy()
    expect(getByText('The shared household feed')).toBeTruthy()
    expect(getByText('Gallery')).toBeTruthy()
    // Post-type cards
    expect(getByText('Text')).toBeTruthy()
    expect(getByText('Allow text posts in the feed')).toBeTruthy()
    expect(getByText('Shared highlight')).toBeTruthy()
    // 8 features + 7 post types + 1 link-preview switch = 16 cards
    const cards = container.querySelectorAll('.sh-radio-card')
    expect(cards).toHaveLength(16)
  })

  it('the admin can switch link previews off', async () => {
    const { api } = await import('@/api')
    const { HouseholdToggles, loadToggles } = await import('./HouseholdToggles')
    await loadToggles()
    const { getByText } = render(<HouseholdToggles />)
    const card = getByText('Link previews').closest('.sh-radio-card')!
    expect(card.classList.contains('sh-radio-card--selected')).toBe(true)
    fireEvent.click(card.querySelector<HTMLInputElement>('input[type="checkbox"]')!)
    expect(api.put).toHaveBeenCalledWith('/api/household/preferences', {
      toggles: { allow_link_preview: false },
    })
  })

  it('renders an enabled feature card as selected', async () => {
    const { HouseholdToggles, loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    toggles.value = { ...toggles.value!, feat_feed: true }
    const { getByText } = render(<HouseholdToggles />)
    const card = getByText('Feed').closest('.sh-radio-card')
    expect(card?.classList.contains('sh-radio-card--selected')).toBe(true)
  })

  it('clicking a feature card checkbox PUTs the inverted toggle', async () => {
    const { api } = await import('@/api')
    const { HouseholdToggles, loadToggles, toggles } = await import('./HouseholdToggles')
    await loadToggles()
    toggles.value = { ...toggles.value!, feat_feed: true }
    const { getByText } = render(<HouseholdToggles />)
    const card = getByText('Feed').closest('.sh-radio-card')!
    const box = card.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    fireEvent.click(box)
    expect(api.put).toHaveBeenCalledWith('/api/household/preferences', {
      toggles: { feat_feed: false },
    })
  })

  it('lists Timetable right after Calendar and toggles feat_timetable', async () => {
    const { api } = await import('@/api')
    const { HouseholdToggles, loadToggles } = await import('./HouseholdToggles')
    await loadToggles()
    const { getByText, container } = render(<HouseholdToggles />)
    const titles = Array.from(container.querySelectorAll('.sh-radio-card__title'))
      .map(el => el.textContent)
    expect(titles.indexOf('Timetable')).toBe(titles.indexOf('Calendar') + 1)
    expect(getByText('Weekly school timetables')).toBeTruthy()
    const card = getByText('Timetable').closest('.sh-radio-card')!
    fireEvent.click(card.querySelector<HTMLInputElement>('input[type="checkbox"]')!)
    expect(api.put).toHaveBeenCalledWith('/api/household/preferences', {
      toggles: { feat_timetable: false },
    })
  })
})
