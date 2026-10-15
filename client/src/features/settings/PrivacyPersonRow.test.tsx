import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { PrivacyPersonRow, privacyPersonName } from './PrivacyPersonRow'
import type { PersonSummary } from '@/types'

function renderRow(person: PersonSummary, since = '2026-10-15 09:00:00') {
  return render(
    <ul>
      <PrivacyPersonRow person={person} since={since}>
        <button type="button">Unblock</button>
      </PrivacyPersonRow>
    </ul>,
  )
}

function meta(container: Element): string {
  return container.querySelector('.sh-person-row__meta')?.textContent ?? ''
}

describe('privacyPersonName', () => {
  it('prefers the nickname, then the real name, then "Unknown person"', () => {
    expect(privacyPersonName({ user_id: 'a', display_name: 'Ruth', personal_alias: 'Oma' })).toBe('Oma')
    expect(privacyPersonName({ user_id: 'a', display_name: 'Ruth' })).toBe('Ruth')
    expect(privacyPersonName({ user_id: 'ru-1', display_name: null })).toBe('Unknown person')
  })
})

describe('PrivacyPersonRow', () => {
  it('a remote person shows their name and household, not the raw id', () => {
    const { container, getByText } = renderRow({
      user_id: 'ru-7ae1d43cad72',
      display_name: 'Annegret Müller-Lüdenscheidt',
      household: 'Familie Müller-Lüdenscheidt',
    })
    expect(getByText('Annegret Müller-Lüdenscheidt')).toBeTruthy()
    expect(meta(container)).toMatch(/^Familie Müller-Lüdenscheidt · since /)
    expect(container.textContent).not.toContain('ru-7ae1d43cad72')
  })

  it('a nickname leads; the real name opens the meta line', () => {
    const { container } = renderRow({
      user_id: 'ru-ruth',
      display_name: 'Grandma Ruth',
      personal_alias: 'Oma',
      household: 'Vizeli',
    })
    expect(container.querySelector('.sh-person-row__name')?.textContent).toBe('Oma')
    expect(meta(container)).toMatch(/^Grandma Ruth · Vizeli · since /)
  })

  it('an unknown id reads "Unknown person" from a household no longer connected', () => {
    const { container } = renderRow({ user_id: 'ru-gone', display_name: null, household: null })
    expect(container.querySelector('.sh-person-row__name')?.textContent).toBe('Unknown person')
    expect(meta(container)).toMatch(/^Household no longer connected · since /)
    expect(container.textContent).not.toContain('ru-gone')
  })

  it('a known person whose household is unpaired just shows since', () => {
    const { container } = renderRow({ user_id: 'ru-obi', display_name: 'Obi', household: null })
    expect(meta(container)).toMatch(/^since /)
  })

  it('renders the signed picture when there is one', () => {
    const { container } = renderRow({
      user_id: 'ru-ruth',
      display_name: 'Grandma Ruth',
      picture_url: 'api/users/ru-ruth/picture?v=1&exp=2&sig=s',
    })
    expect(container.querySelector('img')?.getAttribute('src'))
      .toBe('api/users/ru-ruth/picture?v=1&exp=2&sig=s')
  })

  it('reads a naive SQLite timestamp as UTC', () => {
    const { container } = renderRow({ user_id: 'u', display_name: 'Bob' }, '2026-10-15 23:30:00')
    expect(container.querySelector('time')?.getAttribute('datetime'))
      .toBe('2026-10-15T23:30:00.000Z')
  })

  it('omits the date for an unparseable timestamp without a dangling separator', () => {
    const { container } = renderRow(
      { user_id: 'u', display_name: 'Bob', household: 'Vizeli' }, 'not-a-date',
    )
    expect(meta(container)).toBe('Vizeli')
    expect(container.querySelector('time')).toBeNull()
  })
})
