/**
 * SpaceTimetableTab — a space's shared timetables through the household
 * timetable components: admins get the full editor on the space
 * endpoints, members a view-only timetable (every editing affordance
 * gone, viewing / Picture / List / week navigation / print kept), plus
 * the per-timetable "Show on my Home" pin.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: (...a: unknown[]) => apiPut(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
const wsHandlers: Record<string, (e: { type: string; data: Record<string, unknown> }) => void> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: Record<string, unknown> }) => void) => {
      wsHandlers[type] = h
      return () => {}
    },
  },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn(),
  householdDisplayName: (id: string) => id,
  householdPictureUrl: () => null,
}))
vi.mock('@/components/HouseholdToggles', async () => {
  const { signal } = await vi.importActual<typeof import('@preact/signals')>('@preact/signals')
  return { toggles: signal<{ feat_timetable: boolean } | null>(null) }
})
vi.mock('@/store/auth', async () => {
  const { signal } = await vi.importActual<typeof import('@preact/signals')>('@preact/signals')
  return { currentUser: signal({ user_id: 'u1', username: 'p', display_name: 'P', preferences_json: '{}' }) }
})

import { currentUser } from '@/store/auth'
import { toggles } from '@/components/HouseholdToggles'
import { spaceTimetableStore, timetables as householdTimetables } from '@/store/timetables'
import { entryDialog } from '@/features/timetable/EntryDialog'
import { closeCreateDialog } from '@/features/timetable/TimetableCreateDialog'
import { lessonInfo } from '@/features/timetable/LessonInfoDialog'
import { entry, timetable } from '@/features/timetable/testUtils'
import type { EffectiveLesson, ResolvedWeek, Timetable, TimetableEntry } from '@/types'
import { SpaceTimetableTab } from './SpaceTimetableTab'

const NOW = new Date(2026, 9, 7, 10, 0) // Wed 7 Oct 2026
const store = spaceTimetableStore('s1')

let tt: Timetable
let mathe: TimetableEntry
let slot: TimetableEntry

function setPrefs(json: Record<string, unknown>) {
  currentUser.value = { ...currentUser.value!, preferences_json: JSON.stringify(json) } as typeof currentUser.value
}

beforeEach(() => {
  mathe = entry(0, '08:00', '08:45', { title: 'Mathe', room: '204', note: 'Bring the compass' })
  slot = entry(1, '08:00', '08:45') // an untitled lesson
  tt = timetable({ id: 'stt1', name: 'Class 4b', assignees: [], days: [0, 1], entries: [mathe, slot] })
  store.timetables.value = []
  store.loaded.value = false
  store.selectedId.value = null
  householdTimetables.value = []
  entryDialog.value = null
  lessonInfo.value = null
  closeCreateDialog()
  setPrefs({})
  ;(toggles as unknown as { value: { feat_timetable: boolean } | null }).value = { feat_timetable: true }
  localStorage.clear()
  for (const f of [apiGet, apiPost, apiPatch, apiPut, apiDelete, showToast]) f.mockReset()
})
afterEach(() => cleanup())

function serve(list: Timetable[], weekBody?: ResolvedWeek) {
  apiGet.mockImplementation((p: string) => Promise.resolve(
    p === '/api/spaces/s1/timetables' ? { timetables: list } : { week: weekBody }))
}

function renderTab(canEdit: boolean) {
  return render(<SpaceTimetableTab spaceId="s1" canEdit={canEdit} now={NOW} />)
}

describe('SpaceTimetableTab — empty states', () => {
  it('an admin is offered to create one (school / empty), on the space endpoint, without assignees', async () => {
    serve([])
    const { findByText, getByRole } = renderTab(true)
    expect(await findByText('Create a timetable for this space')).toBeTruthy()
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/timetables')
    fireEvent.click(getByRole('button', { name: 'Create school timetable' }))
    await waitFor(() => expect(getByRole('dialog')).toBeTruthy())
    // Space timetables belong to the space, not to household members.
    expect(document.querySelector('.sh-timetable-assignees')).toBeNull()
    apiPost.mockResolvedValue({ timetable: timetable({ id: 'n1', name: 'Timetable', assignees: [] }) })
    fireEvent.submit(getByRole('dialog').querySelector('form')!)
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    const [url, body] = apiPost.mock.calls[0]
    expect(url).toBe('/api/spaces/s1/timetables')
    expect(body.assignees).toBeUndefined()
    await waitFor(() => expect(store.timetables.value.map(x => x.id)).toEqual(['n1']))
    expect(householdTimetables.value).toEqual([])
  })

  it('a member sees "No timetable yet" and nothing to create', async () => {
    serve([])
    const { findByText, queryByRole } = renderTab(false)
    expect(await findByText('No timetable yet')).toBeTruthy()
    expect(queryByRole('button', { name: 'Create school timetable' })).toBeNull()
    expect(queryByRole('button', { name: 'Start empty' })).toBeNull()
  })
})

describe('SpaceTimetableTab — admin editor', () => {
  it('has every editing affordance', async () => {
    serve([tt])
    const { findByRole, getByRole, getAllByRole } = renderTab(true)
    expect(await findByRole('button', { name: /Class 4b, Rename timetable/ })).toBeTruthy()
    expect(getByRole('button', { name: 'Fill' })).toBeTruthy()
    expect(getByRole('button', { name: 'New timetable' })).toBeTruthy()
    expect(getAllByRole('button', { name: /^Add a lesson on/ }).length).toBeGreaterThan(0)
    expect(getByRole('button', { name: 'Monday: day tools' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Timetable actions' }))
    const menu = getByRole('menu')
    for (const label of ['Settings', 'School weeks & holidays', 'Print', 'Duplicate', 'Delete']) {
      expect(within(menu).getByRole('menuitem', { name: label })).toBeTruthy()
    }
  })

  it('edits through the space endpoints', async () => {
    serve([tt])
    const { findByRole, getByRole } = renderTab(true)
    fireEvent.click(await findByRole('button', { name: /Class 4b, Rename timetable/ }))
    const input = getByRole('textbox', { name: 'Timetable name' }) as HTMLInputElement
    apiPatch.mockResolvedValue({ timetable: { ...tt, version: 2, name: 'Class 4c' } })
    fireEvent.input(input, { target: { value: 'Class 4c' } })
    fireEvent.submit(input.form!)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/spaces/s1/timetables/stt1', { version: 1, name: 'Class 4c' }))
  })

  it('a lesson opens the editor', async () => {
    serve([tt])
    const { findByRole } = renderTab(true)
    fireEvent.click(await findByRole('button', { name: /Mathe/ }))
    expect(entryDialog.value?.timetableId).toBe('stt1')
    expect(lessonInfo.value).toBeNull()
  })
})

describe('SpaceTimetableTab — compact member layout', () => {
  it('one timetable, read-only: no card strip; the "View only" caption sits under the title', async () => {
    serve([tt])
    const { findByRole, queryByRole, container } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    expect(queryByRole('group', { name: 'Timetables' })).toBeNull()
    const caption = container.querySelector('.sh-timetable-head__title .sh-timetable-head__caption')!
    expect(caption.textContent).toBe('View only — the space admins edit this timetable')
  })

  it('several timetables keep the card strip for members; admins always have it', async () => {
    serve([tt, { ...tt, id: 'stt2', name: 'Class 4c' }])
    const member = renderTab(false)
    expect(await member.findByRole('group', { name: 'Timetables' })).toBeTruthy()
    cleanup()
    store.timetables.value = []
    store.loaded.value = false
    serve([tt])
    const admin = renderTab(true)
    expect(await admin.findByRole('group', { name: 'Timetables' })).toBeTruthy()
    expect(document.querySelector('.sh-timetable-head__caption')).toBeNull()
  })

  it('the Home pin lives in the header row beside Pictures / List', async () => {
    serve([tt])
    const { findByRole } = renderTab(false)
    const pin = await findByRole('button', { name: 'Show on my Home' })
    const actions = pin.closest('.sh-timetable-head__actions')!
    expect(actions).not.toBeNull()
    expect(within(actions as HTMLElement).getByRole('button', { name: 'List view' })).toBeTruthy()
    expect(pin.querySelector('.sh-space-timetable__pinicon')!.textContent).toBe('🏠')
    expect(pin.querySelector('.sh-timetable-toggle__short')!.textContent).toBe('Home')
    expect(pin.querySelector('.sh-timetable-toggle__text')!.textContent).toBe('Show on my Home')
    expect(document.querySelector('.sh-space-timetable__bar')).toBeNull()
  })
})

describe('SpaceTimetableTab — member (read-only)', () => {
  it('hides every editing affordance but keeps the views and print', async () => {
    serve([tt])
    const { findByRole, getByRole, queryByRole, queryAllByRole, container } = renderTab(false)
    // The name is plain text, not a rename button.
    expect(await findByRole('heading', { name: 'Class 4b' })).toBeTruthy()
    expect(queryByRole('button', { name: /Rename timetable/ })).toBeNull()
    // No brush, no "+ New" card, no per-day "+" or day tools, no empty-cell adds.
    expect(queryByRole('button', { name: 'Fill' })).toBeNull()
    expect(queryByRole('button', { name: 'New timetable' })).toBeNull()
    expect(queryAllByRole('button', { name: /^Add a lesson/ })).toHaveLength(0)
    expect(queryByRole('button', { name: /day tools$/ })).toBeNull()
    expect(container.querySelector('.sh-timetable-cell-add')).toBeNull()
    expect(container.querySelector('.sh-timetable-setup')).toBeNull()
    // The school-weeks chip is a label, not a way into the editor.
    expect(queryByRole('button', { name: /School weeks/ })).toBeNull()
    expect(container.querySelector('.sh-timetable-head__weeks')).not.toBeNull()
    // No "mark the holidays" nudge.
    expect(queryByRole('note')).toBeNull()
    // Viewing stays: Picture, List, Regular | This week, Print.
    expect(getByRole('button', { name: 'Picture view' })).toBeTruthy()
    expect(getByRole('button', { name: 'List view' })).toBeTruthy()
    expect(getByRole('button', { name: 'This week' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Timetable actions' }))
    const items = within(getByRole('menu')).getAllByRole('menuitem').map(i => i.textContent)
    expect(items).toEqual(['Print'])
  })

  it('untitled slots are hidden entirely; a lesson opens its details (no editor)', async () => {
    serve([tt])
    const { findByRole, getByRole, queryByRole, container } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    expect(container.querySelector(`[data-entry-id="${slot.id}"]`)).toBeNull()
    expect(container.querySelector('.sh-timetable-block--empty')).toBeNull()
    fireEvent.click(getByRole('button', { name: /Mathe/ }))
    expect(entryDialog.value).toBeNull()
    const dialog = getByRole('dialog')
    expect(within(dialog).getByText('Bring the compass')).toBeTruthy()
    expect(within(dialog).getByText('204')).toBeTruthy()
    expect(within(dialog).queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'Delete' })).toBeNull()
    expect(queryByRole('textbox')).toBeNull()
  })

  it('a period row with only untitled slots is dropped (Periods); admins keep it', async () => {
    const deutsch = entry(1, '08:00', '08:45', { title: 'Deutsch' })
    const free = [entry(0, '08:50', '09:35'), entry(1, '08:50', '09:35')]
    const two = { ...tt, entries: [mathe, deutsch, ...free] }
    serve([two])
    const member = renderTab(false)
    await member.findByRole('heading', { name: 'Class 4b' })
    const rows = () => Array.from(document.querySelectorAll('.sh-timetable-periods tbody th'))
      .map(th => th.textContent ?? '')
    expect(document.querySelector('.sh-timetable-periods')).not.toBeNull()
    expect(rows().some(r => r.includes('08:50'))).toBe(false)
    expect(rows().some(r => r.includes('08:00'))).toBe(true)
    cleanup()
    store.timetables.value = []
    store.loaded.value = false
    const admin = renderTab(true)
    await admin.findByRole('button', { name: /Class 4b, Rename timetable/ })
    expect(rows().some(r => r.includes('08:50'))).toBe(true)
    expect(document.querySelector(`[data-entry-id="${free[0].id}"]`)).not.toBeNull()
  })

  it('the phone day view skips untitled slots for members', async () => {
    const original = window.matchMedia
    window.matchMedia = ((q: string) => ({
      matches: true, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia
    try {
      const tue = { ...tt, entries: [mathe, slot, entry(1, '09:00', '09:45', { title: 'Kunst' })] }
      serve([tue])
      const { findByRole, getByRole, container } = renderTab(false)
      await findByRole('heading', { name: 'Class 4b' })
      fireEvent.click(getByRole('tab', { name: /Tuesday/ }))
      expect(container.querySelector('.sh-timetable-day__list')!.textContent).toContain('Kunst')
      expect(container.querySelector(`[data-entry-id="${slot.id}"]`)).toBeNull()
      expect(container.querySelectorAll('.sh-timetable-day__row')).toHaveLength(1)
    } finally {
      window.matchMedia = original
    }
  })

  it('the list view has no edit links', async () => {
    serve([tt])
    const { findByRole, getByRole, container } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    fireEvent.click(getByRole('button', { name: 'List view' }))
    await waitFor(() => expect(container.querySelector('.sh-timetable-list')).not.toBeNull())
    expect(container.querySelector('.sh-timetable-list .sh-link')).toBeNull()
    expect(container.querySelector('.sh-timetable-list')!.textContent).toContain('Mathe')
    // No "Empty slot" rows for members.
    expect(container.querySelector('.sh-timetable-list')!.textContent).not.toContain('Empty slot')
  })

  it('week mode: navigable, no Clear all / Activate / week changes', async () => {
    const lesson = (e: TimetableEntry, date: string, extra: Partial<EffectiveLesson> = {}): EffectiveLesson => {
      const { id, weekday: _wd, ...rest } = e
      void _wd
      return { source_id: id, date, status: 'normal', override_id: null, original: null, ...rest, ...extra }
    }
    const w: ResolvedWeek = {
      anchor: '2026-10-05', valid: true,
      days: [
        { date: '2026-10-05', valid: true, lessons: [lesson(mathe, '2026-10-05', { status: 'cancelled', override_id: 'o1', original: mathe })] },
        { date: '2026-10-06', valid: true, lessons: [] },
      ],
    }
    serve([tt], w)
    const { findByRole, getByRole, queryByRole, findByText } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    fireEvent.click(getByRole('button', { name: 'This week' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/timetables/stt1/weeks/2026-10-05'))
    expect(await findByText('1 change this week')).toBeTruthy()
    expect(queryByRole('button', { name: 'Clear all' })).toBeNull()
    expect(queryByRole('button', { name: /^Add a lesson/ })).toBeNull()
    expect(getByRole('button', { name: 'Next week' })).toBeTruthy()
    // A cancelled lesson opens its details, not the week editor.
    fireEvent.click(getByRole('button', { name: /Mathe/ }))
    expect(entryDialog.value).toBeNull()
    expect(within(getByRole('dialog')).getByText('Cancelled')).toBeTruthy()
    expect(queryByRole('button', { name: 'Cancel this lesson' })).toBeNull()
  })

  it('a holiday week has no Activate this week', async () => {
    const holiday = { ...tt, validity: { valid_from: null, valid_until: null, excluded_weeks: ['2026-10-05'] } }
    serve([holiday], { anchor: '2026-10-05', valid: false, days: [
      { date: '2026-10-05', valid: false, lessons: [] }, { date: '2026-10-06', valid: false, lessons: [] }] })
    const { findByRole, getByRole, queryByRole, findByText } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    fireEvent.click(getByRole('button', { name: 'This week' }))
    expect(await findByText('Not active this week (holiday)')).toBeTruthy()
    expect(queryByRole('button', { name: 'Activate this week' })).toBeNull()
  })
})

describe('SpaceTimetableTab — Show on my Home', () => {
  it('pins and unpins the timetable via the timetable_home_pins preference', async () => {
    serve([tt])
    apiPatch.mockResolvedValue({})
    const { findByRole } = renderTab(false)
    const pin = await findByRole('button', { name: 'Show on my Home' })
    expect(pin.getAttribute('aria-pressed')).toBe('false')
    expect(pin.getAttribute('title')).toBe("Adds today's lessons to your Today card")
    fireEvent.click(pin)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/me',
      { preferences: { timetable_home_pins: ['stt1'] } }))
    await waitFor(() => expect(pin.getAttribute('aria-pressed')).toBe('true'))
    expect(showToast).toHaveBeenLastCalledWith('Added to your Today card on days with lessons', 'success')
    fireEvent.click(pin)
    await waitFor(() => expect(apiPatch).toHaveBeenLastCalledWith('/api/me',
      { preferences: { timetable_home_pins: [] } }))
    await waitFor(() => expect(pin.getAttribute('aria-pressed')).toBe('false'))
  })

  it('household timetables off: says pins won\'t show on Home, and keeps the pin', async () => {
    ;(toggles as unknown as { value: { feat_timetable: boolean } | null }).value = { feat_timetable: false }
    serve([tt])
    apiPatch.mockResolvedValue({})
    const { findByRole } = renderTab(false)
    const pin = await findByRole('button', { name: 'Show on my Home' })
    fireEvent.click(pin)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/me',
      { preferences: { timetable_home_pins: ['stt1'] } }))
    await waitFor(() => expect(pin.getAttribute('aria-pressed')).toBe('true'))
    expect(showToast).toHaveBeenLastCalledWith(
      "Timetables are turned off for your household, so pinned timetables won't show on Home", 'info')
  })

  it('keeps the other pins and is offered to admins too', async () => {
    setPrefs({ timetable_home_pins: ['other'] })
    serve([tt])
    apiPatch.mockResolvedValue({})
    const { findByRole } = renderTab(true)
    fireEvent.click(await findByRole('button', { name: 'Show on my Home' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/me',
      { preferences: { timetable_home_pins: ['other', 'stt1'] } }))
  })

  it('a failed save says so and leaves the pin as it was', async () => {
    serve([tt])
    apiPatch.mockRejectedValue(new Error('offline'))
    const { findByRole } = renderTab(false)
    const pin = await findByRole('button', { name: 'Show on my Home' })
    fireEvent.click(pin)
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('offline', 'error'))
    expect(pin.getAttribute('aria-pressed')).toBe('false')
  })
})

describe('SpaceTimetableTab — live updates', () => {
  it('a timetable.changed frame for this space updates the tab', async () => {
    serve([tt])
    const { findByRole } = renderTab(false)
    await findByRole('heading', { name: 'Class 4b' })
    wsHandlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: 's1', timetable: { ...tt, version: 2, name: 'Class 4c' } } })
    expect(await findByRole('heading', { name: 'Class 4c' })).toBeTruthy()
    expect(householdTimetables.value).toEqual([])
  })
})
