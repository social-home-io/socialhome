import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const openPersonSpy = vi.fn()
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

const { PersonButton, openKnownPerson, openSpaceMention } = await import('./PersonButton')
const { spaceMembers } = await import('@/store/spaceMembers')
const { householdUsers } = await import('@/store/householdUsers')

describe('PersonButton', () => {
  beforeEach(() => {
    openPersonSpy.mockReset()
    spaceMembers.value = {}
    householdUsers.value = new Map()
  })

  it('is a real button that opens the sheet with the hint', () => {
    const { getByRole } = render(
      <PersonButton userId="u-anna" hint={{ display_name: 'Anna' }}>Anna</PersonButton>,
    )
    const btn = getByRole('button', { name: 'Anna' })
    expect(btn.getAttribute('type')).toBe('button')
    fireEvent.click(btn)
    expect(openPersonSpy).toHaveBeenCalledWith('u-anna', { person: { display_name: 'Anna' } })
  })

  it('builds a thunk hint only on click', () => {
    const thunk = vi.fn(() => ({ display_name: 'Anna' }))
    const { getByRole } = render(<PersonButton userId="u-anna" hint={thunk}>Anna</PersonButton>)
    expect(thunk).not.toHaveBeenCalled()
    fireEvent.click(getByRole('button'))
    expect(thunk).toHaveBeenCalledTimes(1)
    expect(openPersonSpy).toHaveBeenCalledWith('u-anna', { person: { display_name: 'Anna' } })
  })

  it('does not let the tap reach the card underneath', () => {
    const cardClick = vi.fn()
    const { getByRole } = render(
      <div onClick={cardClick}><PersonButton userId="u-a">A</PersonButton></div>,
    )
    fireEvent.click(getByRole('button'))
    expect(cardClick).not.toHaveBeenCalled()
    expect(openPersonSpy).toHaveBeenCalledWith('u-a', {})
  })

  it('decorative: tappable, but no tab stop and hidden from screen readers', () => {
    const { container } = render(
      <PersonButton userId="u-a" decorative label="ignored">face</PersonButton>,
    )
    const btn = container.querySelector('button')!
    expect(btn.getAttribute('tabindex')).toBe('-1')
    expect(btn.getAttribute('aria-hidden')).toBe('true')
    expect(btn.getAttribute('aria-label')).toBeNull()
    fireEvent.click(btn)
    expect(openPersonSpy).toHaveBeenCalledTimes(1)
  })

  it('runs onBeforeOpen first and forwards onAliasChanged', () => {
    const order: string[] = []
    const before = vi.fn(() => { order.push('before') })
    openPersonSpy.mockImplementation(() => { order.push('open') })
    const onAlias = vi.fn()
    const { getByRole } = render(
      <PersonButton userId="u-a" onBeforeOpen={before} onAliasChanged={onAlias}>A</PersonButton>,
    )
    fireEvent.click(getByRole('button'))
    expect(order).toEqual(['before', 'open'])
    expect(openPersonSpy.mock.calls[0][1]).toEqual({ onAliasChanged: onAlias })
  })
})

describe('openKnownPerson / openSpaceMention', () => {
  beforeEach(() => {
    openPersonSpy.mockReset()
    spaceMembers.value = {
      s1: new Map([['ru-chidi', {
        user_id: 'ru-chidi', role: 'member', joined_at: '', space_display_name: null,
        picture_hash: null, picture_url: 'api/p/chidi', display_name: 'Chidi',
        mention: 'Chidi', instance_id: 'p-ok', household_name: 'Okafor family',
      }]]),
    }
  })

  it('openKnownPerson passes the cached hint', () => {
    openKnownPerson('s1', 'ru-chidi')
    expect(openPersonSpy).toHaveBeenCalledWith('ru-chidi', {
      person: expect.objectContaining({ display_name: 'Chidi', picture_url: 'api/p/chidi' }),
    })
  })

  it('a known @token opens that member (case-insensitive)', () => {
    openSpaceMention('s1', 'chidi')
    expect(openPersonSpy).toHaveBeenCalledWith('ru-chidi', expect.anything())
  })

  it('an unknown token, @here or no space does nothing', () => {
    openSpaceMention('s1', 'nobody')
    openSpaceMention('s1', 'here')
    openSpaceMention(null, 'chidi')
    expect(openPersonSpy).not.toHaveBeenCalled()
  })
})
