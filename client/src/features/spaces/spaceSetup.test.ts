/**
 * "What is it for?": presets, the name guess, the features body and the
 * per-user "still asks" flag (server-side preference).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const patch = vi.fn().mockResolvedValue({})
vi.mock('@/api', () => ({ api: { patch: (...a: unknown[]) => patch(...a) } }))

const { currentUser } = await import('@/store/auth')
const {
  PRESET_SECTIONS, clearSetupPending, featuresFor, guessPreset, isSetupPending,
  markSetupPending, sectionsOn,
} = await import('./spaceSetup')

function prefs(p: Record<string, unknown>) {
  currentUser.value = { user_id: 'u1', preferences_json: JSON.stringify(p) } as unknown as typeof currentUser.value
}

beforeEach(() => { patch.mockClear(); prefs({}) })

describe('guessPreset', () => {
  it.each([
    ['Vizeli Family', 'family'],
    ['Familie Muster', 'family'],
    ['Book club', 'club'],
    ['FC Hottingen', null],
    ['Chor Zürich', 'club'],
    ['Nachbarschaft Seefeld', 'neighbours'],
    ['Neighbourhood garden', 'neighbours'],
    ['Untitled', null],
  ])('%s → %s', (name, want) => {
    expect(guessPreset(name)).toBe(want)
  })

  it('falls back to the discovery category', () => {
    expect(guessPreset('X', 'family_parenting')).toBe('family')
    expect(guessPreset('X', 'local')).toBe('neighbours')
    expect(guessPreset('X', 'gaming')).toBe('club')
    expect(guessPreset('X', 'non_profit')).toBe('club')
    expect(guessPreset('X', 'community')).toBe('club')
    expect(guessPreset('X', 'general')).toBeNull()
  })
})

describe('presets and features', () => {
  it('"Just a feed" turns everything off; no preset turns the Map on', () => {
    expect(PRESET_SECTIONS.feed).toEqual([])
    for (const p of Object.values(PRESET_SECTIONS)) expect(p).not.toContain('map')
  })

  it('featuresFor sends every offered key, on or off', () => {
    expect(featuresFor(['chat', 'tasks', 'map'])).toEqual({
      chat: true, calendar: false, todo: true, pages: false, gallery: false,
      stickies: false, bazaar: false, location: true, timetable: false,
    })
  })

  it('sectionsOn reads the features (absent = server class default)', () => {
    expect(sectionsOn({ chat: true, calendar: true, todo: false, pages: false, gallery: false,
      stickies: false, bazaar: false, location: false, timetable: false })).toEqual(['chat', 'calendar'])
    expect(sectionsOn(undefined)).toEqual(['chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar'])
  })
})

describe('the setup flag (users.preferences_json)', () => {
  it('create marks it, a choice clears it — stored on the server', async () => {
    prefs({ space_setup_pending: ['old'] })
    await markSetupPending('s1')
    expect(patch).toHaveBeenLastCalledWith('/api/me', { preferences: { space_setup_pending: ['old', 's1'] } })
    expect(isSetupPending('s1')).toBe(true)
    await clearSetupPending('s1')
    expect(patch).toHaveBeenLastCalledWith('/api/me', { preferences: { space_setup_pending: ['old'] } })
    expect(isSetupPending('s1')).toBe(false)
    await clearSetupPending('old')
    expect(patch).toHaveBeenLastCalledWith('/api/me', { preferences: { space_setup_pending: null } })
  })

  it('keeps at most 20, dropping the oldest', async () => {
    prefs({ space_setup_pending: Array.from({ length: 20 }, (_, i) => `s${i}`) })
    await markSetupPending('new')
    const sent = (patch.mock.calls.at(-1)![1] as { preferences: { space_setup_pending: string[] } })
      .preferences.space_setup_pending
    expect(sent).toHaveLength(20)
    expect(sent[0]).toBe('s1')
    expect(sent.at(-1)).toBe('new')
  })

  it('a malformed preference reads as nothing pending', () => {
    prefs({ space_setup_pending: 'nope' })
    expect(isSetupPending('nope')).toBe(false)
  })
})
