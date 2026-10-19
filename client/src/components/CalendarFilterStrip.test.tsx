import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// Hoisted so the ``vi.mock`` factories below can close over the same
// signals the test cases mutate. Without ``vi.hoisted`` the factories
// run before the module-level ``const`` is initialised. ``await import``
// is the lint-friendly way to load ``@preact/signals`` inside the
// hoisted block — a bare ``require`` trips ``no-require-imports``.
const { mockCurrentUser, mockHouseholdUsers } = await vi.hoisted(async () => {
  const { signal } = await import('@preact/signals')
  return {
    mockCurrentUser: signal({
      user_id: 'u-me',
      username: 'me',
      display_name: 'Me',
      is_admin: false,
      picture_url: null,
      bio: null,
      is_new_member: false,
    }),
    mockHouseholdUsers: signal(new Map([
      ['u-me', {
        user_id: 'u-me', username: 'me', display_name: 'Me',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
      ['u-pa', {
        user_id: 'u-pa', username: 'pa', display_name: 'Pascal',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
      ['u-ma', {
        user_id: 'u-ma', username: 'ma', display_name: 'Maria',
        is_admin: false, picture_url: null, bio: null, is_new_member: false,
      }],
    ])),
  }
})

vi.mock('@/store/auth', () => ({ currentUser: mockCurrentUser }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: mockHouseholdUsers,
}))

import { CalendarFilterStrip } from './CalendarFilterStrip'

const mkCal = (id: string, owner: string, name = 'Calendar') => ({
  id, owner_username: owner, name,
})

describe('CalendarFilterStrip', () => {
  beforeEach(() => {
    // Reset to default state in case a previous test mutated.
    mockCurrentUser.value = {
      ...mockCurrentUser.value,
      username: 'me',
    }
  })

  const strip = (visible: string[], onChange = vi.fn(), extra = {}) => render(
    <CalendarFilterStrip
      calendars={[mkCal('c1', 'me'), mkCal('c2', 'pa'), mkCal('c3', 'ma')]}
      visibleCalendarIds={new Set(visible)}
      onChange={onChange}
      onShowAll={() => {}}
      onShowOnlyMine={() => {}}
      {...extra}
    />,
  )
  const pressed = (el: HTMLElement) => el.closest('button')!.getAttribute('aria-pressed')

  it('renders nothing when there is a single owner', () => {
    const { container } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me')]}
        visibleCalendarIds={new Set(['c1'])}
        onChange={() => {}}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    expect(container.querySelector('.sh-cal-people')).toBeNull()
  })

  it('renders the presets, then one flat chip per owner, you first', () => {
    const { container, getByRole } = strip(['c1'])
    expect(getByRole('group', { name: 'Household calendars' })).toBeTruthy()
    const names = Array.from(container.querySelectorAll('button')).map(b => (b.querySelector('.sh-cal-person__name') ?? b).textContent)
    expect(names).toEqual(['Everyone', 'Just me', 'You', 'Maria', 'Pascal'])
    // Flat chips: no taped polaroid, no per-pin tilt.
    expect(container.querySelector('.sh-cal-strip-tape')).toBeNull()
    expect(container.querySelectorAll('.sh-cal-person[style*="--sh-pin-rot"]').length).toBe(0)
  })

  it('every chip is a toggle with one selected style', () => {
    const { getByText } = strip(['c1', 'c2'])
    expect(pressed(getByText('You'))).toBe('true')
    expect(pressed(getByText('Pascal'))).toBe('true')
    expect(pressed(getByText('Maria'))).toBe('false')
    for (const name of ['You', 'Pascal']) {
      expect(getByText(name).closest('button')!.className).toBe('sh-cal-person is-on')
    }
    expect(getByText('Maria').closest('button')!.className).toBe('sh-cal-person')
  })

  it('a preset reads as pressed when it describes what shows', () => {
    let r = strip(['c1'])
    expect(pressed(r.getByText('Just me'))).toBe('true')
    expect(pressed(r.getByText('Everyone'))).toBe('false')
    r.unmount()
    r = strip(['c1', 'c2', 'c3'])
    expect(pressed(r.getByText('Everyone'))).toBe('true')
    expect(pressed(r.getByText('Just me'))).toBe('false')
    r.unmount()
    r = strip(['c2'])
    expect(pressed(r.getByText('Everyone'))).toBe('false')
    expect(pressed(r.getByText('Just me'))).toBe('false')
  })

  it('the presets call their handlers', () => {
    const onShowAll = vi.fn()
    const onShowOnlyMine = vi.fn()
    const { getByText } = strip(['c2'], vi.fn(), { onShowAll, onShowOnlyMine })
    fireEvent.click(getByText('Everyone'))
    fireEvent.click(getByText('Just me'))
    expect(onShowAll).toHaveBeenCalledTimes(1)
    expect(onShowOnlyMine).toHaveBeenCalledTimes(1)
  })

  it('hides "Just me" when you have no calendar of your own', () => {
    const { queryByText } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c2', 'pa'), mkCal('c3', 'ma')]}
        visibleCalendarIds={new Set(['c2'])}
        onChange={() => {}}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    expect(queryByText('Just me')).toBeNull()
  })

  it('toggling an off chip adds its owner calendars to the visible set', () => {
    const onChange = vi.fn()
    const { getByText } = strip(['c1'], onChange)
    fireEvent.click(getByText('Pascal'))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect([...(onChange.mock.calls[0][0] as Set<string>)].sort()).toEqual(['c1', 'c2'])
  })

  it('refuses to hide the last visible owner', () => {
    const onChange = vi.fn()
    const { getByText } = strip(['c1'], onChange)
    fireEvent.click(getByText('You'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a member with several calendars is one chip, mixed while some show', () => {
    const onChange = vi.fn()
    const { getAllByText, getByText } = render(
      <CalendarFilterStrip
        calendars={[mkCal('c1', 'me'), mkCal('c2', 'pa'), mkCal('c4', 'pa', 'Work')]}
        visibleCalendarIds={new Set(['c1', 'c2'])}
        onChange={onChange}
        onShowAll={() => {}}
        onShowOnlyMine={() => {}}
      />,
    )
    expect(getAllByText('Pascal')).toHaveLength(1)
    expect(pressed(getByText('Pascal'))).toBe('mixed')
    fireEvent.click(getByText('Pascal'))
    expect([...(onChange.mock.calls[0][0] as Set<string>)].sort()).toEqual(['c1', 'c2', 'c4'])
  })
})
