import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/preact'
import { SpaceListRow } from './SpaceListRow'
import { HA_SPACE_SYMBOL } from './spaceGlyph'

function tile(container: Element): HTMLElement {
  return container.querySelector('.sh-spaces-row__ic') as HTMLElement
}

describe('SpaceListRow — the space symbol', () => {
  afterEach(() => cleanup())

  it("the space's picture wins over its emoji", () => {
    const { container } = render(
      <ul><SpaceListRow to="/spaces/s1" iconUrl="/api/media/ic.webp?sig=x" emoji="📚" name="Book club" /></ul>,
    )
    const ic = tile(container)
    expect(ic.querySelector('img')?.getAttribute('src')).toBe('/api/media/ic.webp?sig=x')
    expect(ic.textContent).not.toContain('📚')
  })

  it('the Home Assistant symbol draws the logo, not a house', () => {
    const { container } = render(
      <ul><SpaceListRow to="/spaces/s1" iconUrl={null} emoji={HA_SPACE_SYMBOL} name="Smart home" /></ul>,
    )
    const ic = tile(container)
    expect(ic.querySelector('svg.sh-ha-logo')).not.toBeNull()
    expect(ic.textContent).toBe('')
  })

  it('an emoji shows as is; no emoji shows the initial', () => {
    const { container: a } = render(<ul><SpaceListRow emoji="📚" name="Book club" /></ul>)
    expect(tile(a).textContent).toBe('📚')
    const { container: b } = render(<ul><SpaceListRow emoji={null} name="garden" /></ul>)
    expect(tile(b).textContent).toBe('G')
  })
})
