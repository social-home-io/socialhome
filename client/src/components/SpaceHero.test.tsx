import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { SpaceHero, tierLabel } from './SpaceHero'

afterEach(() => { vi.restoreAllMocks() })

const faces = [
  { user_id: 'u1', name: 'Lina', picture_url: '/p/1.webp' },
  { user_id: 'u2', name: 'Noah', picture_url: null },
  { user_id: 'u3', name: 'Ruth', picture_url: '/p/3.webp' },
  { user_id: 'u4', name: 'Tobi', picture_url: '/p/4.webp' },
  { user_id: 'u5', name: 'Sara', picture_url: '/p/5.webp' },
]

describe('SpaceHero', () => {
  it('renders the cover image, the name and the about markdown when set', () => {
    const { container } = render(
      <SpaceHero name="Family Hub" emoji="🏡" coverUrl="/media/cover.webp"
                 about={'# Welcome\n\nOur **family** space.'} />,
    )
    const img = container.querySelector('.sh-space-hero-image') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/media/cover.webp')
    expect(container.querySelector('.sh-space-hero--cover')).not.toBeNull()
    expect(container.querySelector('h2.sh-space-hero-name')?.textContent).toBe('Family Hub')
    expect(container.textContent).toContain('Welcome')
  })

  it('without a cover there is no cover block at all — the identity row starts the card', () => {
    const { container } = render(
      <SpaceHero name="Book club" emoji="📚" coverUrl={null} about={null} />,
    )
    expect(container.querySelector('.sh-space-hero-banner')).toBeNull()
    expect(container.querySelector('.sh-space-hero--cover')).toBeNull()
    expect(container.querySelector('.sh-space-hero-body')?.firstElementChild?.className)
      .toBe('sh-space-hero-identity')
  })

  it('the emoji is the icon; else the name initial', () => {
    const { container, rerender } = render(
      <SpaceHero name="Family Hub" emoji="🏡" coverUrl={null} about={null} />,
    )
    expect(container.querySelector('.sh-space-hero-avatar')?.textContent).toBe('🏡')
    rerender(<SpaceHero name="zeta" emoji={null} coverUrl={null} about={null} />)
    expect(container.querySelector('.sh-space-hero-avatar')?.textContent).toBe('Z')
  })

  it('one line: a facepile of at most four members + the count, linking to Members, then the tier', () => {
    const { container } = render(
      <SpaceHero name="Family" emoji={null} coverUrl={null} about={null} memberCount={8}
                 members={faces} membersHref="/spaces/s1/members" tier="private" />,
    )
    const link = container.querySelector('a.sh-space-hero-members') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/spaces/s1/members')
    expect(link.querySelectorAll('.sh-space-hero-faces > *')).toHaveLength(4)
    // The faces are decoration: the link reads "8 members".
    const pile = link.querySelector('.sh-space-hero-faces') as HTMLElement
    expect(pile.getAttribute('aria-hidden')).toBe('true')
    expect(link.textContent?.replace(pile.textContent ?? '', '')).toBe('8 members')
    expect(container.querySelector('.sh-space-hero-scope')?.textContent).toBe('· Private')
  })

  it('a singular member count and no members link', () => {
    const { container } = render(
      <SpaceHero name="zeta" emoji={null} coverUrl={null} about={null} memberCount={1} />,
    )
    expect(container.querySelector('a.sh-space-hero-members')).toBeNull()
    expect(container.querySelector('.sh-space-hero-members')?.textContent).toBe('1 member')
  })

  it('quick links are chips with a link icon, http(s) only, opening a new tab', () => {
    const { container } = render(
      <SpaceHero name="Family" emoji={null} coverUrl={null} about={null}
                 links={[{ id: 'a', label: 'School portal', href: 'https://school.example/' }]} />,
    )
    const nav = container.querySelector('nav.sh-space-hero-links') as HTMLElement
    expect(nav.getAttribute('aria-label')).toBe('Quick links')
    const a = nav.querySelector('a') as HTMLAnchorElement
    expect(a.textContent).toBe('School portal')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.querySelector('svg')).not.toBeNull()
    expect(a.querySelector('img')).toBeNull()
  })

  it('no links → no links row', () => {
    const { container } = render(<SpaceHero name="F" emoji={null} coverUrl={null} about={null} />)
    expect(container.querySelector('.sh-space-hero-links')).toBeNull()
  })

  it('a short About has no "Read more"', () => {
    const { container } = render(
      <SpaceHero name="F" emoji={null} coverUrl={null} about="Short." />,
    )
    expect(container.querySelector('.sh-space-hero-more')).toBeNull()
  })

  it('a clamped About offers "Read more", which opens the whole text in a dialog', () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(120)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(48)
    const { container, getByText } = render(
      <SpaceHero name="Vizeli Family" emoji={null} coverUrl={null}
                 about={'This is **our family space** — plans, lists and photos.'} />,
    )
    const more = container.querySelector('.sh-space-hero-more') as HTMLButtonElement
    expect(more.getAttribute('aria-haspopup')).toBe('dialog')
    fireEvent.click(getByText('Read more'))
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.textContent).toContain('About Vizeli Family')
    expect(dialog.textContent).toContain('our family space')
  })
})

describe('tierLabel', () => {
  it('names the four tiers and nothing else', () => {
    expect(['private', 'household', 'public', 'global'].map(tierLabel))
      .toEqual(['Private', 'Household', 'Public', 'Global'])
    expect(tierLabel('weird')).toBeNull()
    expect(tierLabel(null)).toBeNull()
  })
})
