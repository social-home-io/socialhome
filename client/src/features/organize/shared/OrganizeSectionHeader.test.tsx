import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { OrganizeSectionHeader } from './OrganizeSectionHeader'

describe('OrganizeSectionHeader', () => {
  it('renders the title as a heading, the counts and the actions', () => {
    const { getByRole, container } = render(
      <OrganizeSectionHeader
        title="Shopping list"
        counts={[{ label: '4 to buy', tone: 'open' }, { label: '3 done', tone: 'done' }]}
      >
        <button type="button">Stores</button>
      </OrganizeSectionHeader>,
    )
    expect(getByRole('heading', { level: 2, name: 'Shopping list' })).toBeTruthy()
    const counts = container.querySelector('.sh-organize-header__counts')!
    expect(counts.textContent).toBe('4 to buy·3 done')
    expect(container.querySelector('.sh-organize-header__count--open')!.textContent).toBe('4 to buy')
    expect(container.querySelector('.sh-organize-header__sep')!.getAttribute('aria-hidden')).toBe('true')
    expect(getByRole('button', { name: 'Stores' })).toBeTruthy()
  })

  it('omits the counts line and actions slot when empty', () => {
    const { container } = render(<OrganizeSectionHeader title="Stickies" />)
    expect(container.querySelector('.sh-organize-header__counts')).toBeNull()
    expect(container.querySelector('.sh-organize-header__actions')).toBeNull()
  })

  it('can hide the title visually while keeping it as the section heading', () => {
    const { getByRole } = render(
      <OrganizeSectionHeader title="Shopping list" hideTitle counts={[{ label: '1 to buy' }]} />,
    )
    expect(getByRole('heading', { level: 2, name: 'Shopping list' }).className).toBe('sr-only')
  })

  it('renders no heading when there is no title', () => {
    const { queryByRole } = render(<OrganizeSectionHeader counts={[{ label: '1 to buy' }]} />)
    expect(queryByRole('heading')).toBeNull()
  })
})
